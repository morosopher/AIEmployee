"""真实 session lease、unknown fence、ACK-loss 联合核对与 current readiness 的集成回归。"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
from ai_employee.application.ports.oauth_refresh import OAuthRefreshError
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import TransientProviderError
from ai_employee.infrastructure.db.models.sources import (
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
    SqlAlchemyOAuthRefreshCoordinator,
)
from tests.integration.m2.test_credential_rotation_repository import (
    CONNECTION_ID,
    NOW,
    OTHER_USER_ID,
    USER_ID,
    FakeRefreshProvider,
    refresh_request,
    token_response,
)
from tests.integration.m2.test_credential_rotation_repository import (
    oauth_state as oauth_state,  # noqa: PLC0414 - 显式 re-export 让 pytest 在本模块发现 fixture。
)


@pytest.mark.asyncio
async def test_two_workers_share_one_session_lease_and_only_one_refresh_grant(oauth_state) -> None:
    """并发 mail/calendar Worker 不能各自使用同一旧 refresh token 请求供应商。"""
    _, _, coordinator = oauth_state
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_at_provider() -> None:
        """在无业务事务的网络阶段固定交错。"""
        entered.set()
        await release.wait()

    provider = FakeRefreshProvider(token_response(), wait_at_provider)
    task = asyncio.create_task(coordinator.refresh(refresh_request(), provider))
    try:
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
        except TimeoutError:
            if task.done():
                await task
            raise
        with pytest.raises(OAuthRefreshError):
            await coordinator.refresh(
                replace(refresh_request(), capability=ConnectionCapability.CALENDAR_READ),
                provider,
            )
        assert provider.calls == 1
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_lease_acquire_commit_ack_loss_releases_session_lock_before_pool_return(
    oauth_state,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取得锁的 commit ACK 丢失仍须解锁；provider/start 均未发生时允许干净的新 claim。"""
    sessions, _, coordinator = oauth_state
    original_commit = AsyncConnection.commit
    acquiring_pid: int | None = None

    async def lose_first_ack(connection: AsyncConnection) -> None:
        """真实提交首次 acquire，再仅丢掉客户端 ACK，不关闭或替换该数据库会话。"""
        nonlocal acquiring_pid
        should_fail = acquiring_pid is None
        if should_fail:
            acquiring_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
        await original_commit(connection)
        if should_fail:
            raise OSError("synthetic lease acquire commit acknowledgement lost")

    monkeypatch.setattr(AsyncConnection, "commit", lose_first_ack)
    provider = FakeRefreshProvider(token_response())
    with pytest.raises(OSError, match="synthetic lease acquire commit acknowledgement lost"):
        await coordinator.refresh(refresh_request(), provider)
    assert provider.calls == 0
    async with sessions() as session:
        lingering = await session.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND locktype='advisory')"),
            {"pid": acquiring_pid},
        )
        assert lingering is False, "OAuth session lease survived pool return after acquire ACK loss"
        assert (await session.scalars(select(AuditEventModel))).all() == []
    await coordinator.refresh(refresh_request(), provider)
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_network", "after_response", "before_commit"])
@pytest.mark.parametrize("loss", ["unlock", "disconnect"])
async def test_explicit_session_lock_loss_is_fail_closed_and_never_reacquired(
    oauth_state,
    monkeypatch,
    stage: str,
    loss: str,
) -> None:
    """同一个 session 必须在每个 provider/CAS 边界仍持锁，不能重入 acquire 掩盖丢锁。"""
    sessions, _, coordinator = oauth_state
    from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
        PostgreSQLOAuthRefreshLease,
    )

    original = PostgreSQLOAuthRefreshLease.assert_owned
    checks = 0
    target_check = {"before_network": 2, "after_response": 3, "before_commit": 5}[stage]

    async def lose_lock(self):
        """真实释放 session advisory lock；随后仍调用真实 ownership 检查。"""
        nonlocal checks
        checks += 1
        if checks == target_check:
            if loss == "disconnect":
                await self.connection.invalidate()
            else:
                await self.connection.execute(text("SELECT pg_advisory_unlock_all()"))
                await self.connection.commit()
        await original(self)

    monkeypatch.setattr(PostgreSQLOAuthRefreshLease, "assert_owned", lose_lock)
    provider = FakeRefreshProvider(token_response())
    with pytest.raises(OAuthRefreshError):
        await coordinator.refresh(refresh_request(), provider)
    expected_calls = 0 if stage == "before_network" else 1
    assert provider.calls == expected_calls
    with pytest.raises(OAuthRefreshError):
        await coordinator.refresh(refresh_request(), provider)
    assert provider.calls == expected_calls
    async with sessions() as session:
        assert (await session.scalars(select(AuditEventModel.event_type))).all() == [
            "oauth.refresh_started"
        ]


@pytest.mark.asyncio
async def test_network_unknown_and_transient_provider_error_reentry_are_zero_call(
    oauth_state,
) -> None:
    """临时供应商错误也不能重放 refresh grant；进程重建后仍从 PostgreSQL 识别 fence。"""
    sessions, cipher, coordinator = oauth_state

    async def unknown() -> None:
        """模拟 provider 已收到请求而客户端失去结果。"""
        raise TransientProviderError(
            error_code="google_request_failed", message="synthetic unknown"
        )

    provider = FakeRefreshProvider(token_response(), unknown)
    with pytest.raises(OAuthRefreshError) as first:
        await coordinator.refresh(refresh_request(), provider)
    assert first.value.error_code == "oauth_refresh_result_unknown"
    restarted = SqlAlchemyOAuthRefreshCoordinator(
        session_factory=sessions,
        cipher=cipher,
        identity=OAuthRefreshIdentity(bytes(range(32)), key_version=7),
        clock=lambda: NOW,
    )
    for _ in range(2):
        with pytest.raises(OAuthRefreshError):
            await restarted.refresh(refresh_request(), provider)
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_explicit_recovery_lease_shares_automatic_mutex_without_starting_grant(
    oauth_state,
) -> None:
    """预留恢复通道持同一连接 lease；本原语既不消费 state，也不新增 automatic fence。"""
    sessions, _, coordinator = oauth_state
    provider = FakeRefreshProvider(token_response())
    async with coordinator.explicit_recovery_lease(user_id=USER_ID, connection_id=CONNECTION_ID):
        with pytest.raises(OAuthRefreshError) as error:
            await coordinator.refresh(refresh_request(), provider)
        assert error.value.error_code == "oauth_refresh_locked"
        assert provider.calls == 0
        async with sessions() as session:
            assert (await session.scalars(select(AuditEventModel))).all() == []
    await coordinator.refresh(refresh_request(), provider)
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_field", ["access", "expiry", "refresh", "scope_shrink", "unknown_scope"]
)
async def test_invalid_provider_refresh_response_leaves_a_durable_zero_replay_fence(
    oauth_state,
    invalid_field: str,
) -> None:
    """即使 provider 声称返回规范类型，边界重检仍拒绝畸形或缩水事实，后续不重放。"""
    _, _, coordinator = oauth_state
    response = token_response()
    # 故意破坏冻结响应，模拟不遵守端口验证的供应商实现；expected 不调用生产 validator。
    if invalid_field == "access":
        object.__setattr__(response, "access_token", "")
    elif invalid_field == "expiry":
        object.__setattr__(response, "expires_in", 0)
    elif invalid_field == "refresh":
        object.__setattr__(response, "refresh_token", " ")
    elif invalid_field == "scope_shrink":
        object.__setattr__(
            response,
            "granted_scopes",
            frozenset({"https://www.googleapis.com/auth/gmail.readonly"}),
        )
    else:
        object.__setattr__(
            response, "granted_scopes", response.granted_scopes | {"synthetic-unsupported-scope"}
        )
    provider = FakeRefreshProvider(response)
    for _ in range(2):
        with pytest.raises(OAuthRefreshError):
            await coordinator.refresh(refresh_request(), provider)
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["missing_access", "missing_refresh", "bad_aad", "key_version", "other_user"]
)
async def test_local_credential_validation_precedes_started_and_provider(
    oauth_state, corruption: str
) -> None:
    """缺少完整双行、错误 AEAD、root 版本或用户归属时，不产生 started 或网络调用。"""
    sessions, _, coordinator = oauth_state
    request = refresh_request()
    async with sessions.begin() as session:
        rows = {
            row.credential_kind: row
            for row in (await session.scalars(select(EncryptedCredentialModel))).all()
        }
        if corruption.startswith("missing"):
            await session.delete(rows[corruption.removeprefix("missing_") + "_token"])
        elif corruption == "bad_aad":
            rows["refresh_token"].ciphertext = rows["access_token"].ciphertext
            rows["refresh_token"].nonce = rows["access_token"].nonce
        elif corruption == "key_version":
            rows["refresh_token"].key_version = 8
        else:
            request = replace(request, user_id=OTHER_USER_ID)
    provider = FakeRefreshProvider(token_response())
    with pytest.raises(OAuthRefreshError):
        await coordinator.refresh(request, provider)
    assert provider.calls == 0
    async with sessions() as session:
        assert (await session.scalars(select(AuditEventModel))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_commit", [True, False])
async def test_confirmed_commit_ack_loss_uses_fresh_session_without_provider_replay(
    oauth_state, actual_commit: bool
) -> None:
    """真实 commit 后 ACK 异常与 commit 前 rollback 必须由新 session 的结果读取区分。"""
    sessions, _, coordinator = oauth_state
    attached: list = []

    # 通过 writer 返回后安装一次真实 SQLAlchemy commit hook，保持网络与存储真实。
    from ai_employee.infrastructure.db.repositories.credential_rotation import (
        SqlAlchemyCredentialRotationRepository,
    )

    original = SqlAlchemyCredentialRotationRepository.confirm

    async def with_ack_loss(self, *args, **kwargs):
        """执行真实 writer 后让底层 commit 边界产生受控异常。"""
        value = await original(self, *args, **kwargs)
        name = "after_commit" if actual_commit else "before_commit"

        def lost_ack(session):
            del session
            raise RuntimeError("synthetic commit acknowledgement lost")

        event.listen(self.session.sync_session, name, lost_ack, once=True)
        attached.append(self.session)
        return value

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SqlAlchemyCredentialRotationRepository, "confirm", with_ack_loss)
        provider = FakeRefreshProvider(token_response())
        if actual_commit:
            result = await coordinator.refresh(refresh_request(), provider)
            assert result.refreshed
        else:
            with pytest.raises(OAuthRefreshError):
                await coordinator.refresh(refresh_request(), provider)
    assert provider.calls == 1
    async with sessions() as session:
        events = (await session.scalars(select(AuditEventModel).order_by(AuditEventModel.id))).all()
    attempt_id = UUID(events[0].event_metadata["refresh_attempt_id"])
    result = await coordinator.read_result(
        user_id=USER_ID, connection_id=CONNECTION_ID, attempt_id=attempt_id
    )
    assert (result is not None) is actual_commit
    assert len(events) == (2 if actual_commit else 1)
    assert all(item is not None for item in attached)
    # ACK 丢失不能留下借出的业务连接；新 session 核对完成后原 session 也必须关闭。
    assert sessions.engine.pool.checkedout() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed,mutation",
    [
        (False, "access"),
        (False, "same_refresh"),
        (True, "newer"),
        (True, "rollback"),
        (False, "missing"),
        (False, "invalid_aead"),
    ],
)
async def test_historical_closure_and_current_readiness_are_independent(
    oauth_state, changed: bool, mutation: str
) -> None:
    """后续合法事实不重开旧 attempt；只把 changed=true 的 A→B→A 判为 identity rollback。"""
    sessions, cipher, coordinator = oauth_state
    provider = FakeRefreshProvider(token_response("synthetic-new-refresh" if changed else None))
    ready = await coordinator.refresh(refresh_request(), provider)
    async with sessions.begin() as session:
        access = await session.scalar(
            select(EncryptedCredentialModel).where(
                EncryptedCredentialModel.credential_kind == "access_token"
            )
        )
        refresh = await session.scalar(
            select(EncryptedCredentialModel).where(
                EncryptedCredentialModel.credential_kind == "refresh_token"
            )
        )
        if mutation == "missing":
            await session.delete(refresh)
        else:
            row = access if mutation == "access" else refresh
            plaintext = (
                b"synthetic-access-later"
                if mutation == "access"
                else (
                    b"synthetic-third-refresh" if mutation == "newer" else b"synthetic-old-refresh"
                )
            )
            encrypted = cipher.encrypt(
                plaintext, f"{USER_ID}:{CONNECTION_ID}:{row.credential_kind}".encode("ascii")
            )
            row.ciphertext = encrypted.ciphertext
            row.nonce = encrypted.nonce
            row.updated_at += timedelta(seconds=10)
            if mutation == "invalid_aead":
                row.nonce = bytes(12)
        connection = await session.get(OAuthConnectionModel, CONNECTION_ID)
        connection.authorization_generation += 1
    assert (
        await coordinator.read_result(
            user_id=USER_ID, connection_id=CONNECTION_ID, attempt_id=ready.attempt_id
        )
        is not None
    )
    if mutation in {"rollback", "missing", "invalid_aead"}:
        with pytest.raises(OAuthRefreshError) as error:
            await coordinator.read_current(refresh_request())
        assert error.value.error_code == "oauth_credential_state_conflict"
        with pytest.raises(OAuthRefreshError):
            await coordinator.refresh(refresh_request(), provider)
    else:
        assert (
            await coordinator.read_current(refresh_request())
        ).snapshot.authorization_generation == 3
    assert provider.calls == 1
