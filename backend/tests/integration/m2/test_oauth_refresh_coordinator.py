"""真实 session lease、unknown fence、ACK-loss 联合核对与 current readiness 的集成回归。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import object_session

from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
from ai_employee.application.ports.credential_rotation import (
    CredentialReplacedV1,
    RecoveryUnsatisfiedV1,
    parse_recovery_started,
)
from ai_employee.application.ports.oauth import OAuthProvider, OAuthTokenSet
from ai_employee.application.ports.oauth_refresh import (
    OAuthRefreshError,
    OAuthRefreshRequest,
    OAuthRefreshSnapshot,
)
from ai_employee.application.use_cases.connections import (
    ConnectionsUseCase,
    OAuthStateRejectedError,
)
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import DomainError, PermanentProviderError, TransientProviderError
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories import oauth_refresh_coordinator
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStoreFactory
from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
    SqlAlchemyOAuthRefreshCoordinator,
)
from ai_employee.integrations.google.oauth import GoogleOAuthAdapter
from ai_employee.integrations.microsoft.oauth import MICROSOFT_TOKEN_URL, MicrosoftOAuthAdapter
from tests.integration.m2.test_credential_rotation_repository import (
    CONNECTION_ID,
    NOW,
    OTHER_USER_ID,
    SCOPES,
    USER_ID,
    FakeRefreshProvider,
    RecoveryClock,
    RecoveryOAuthAdapter,
    begin_recovery,
    make_unknown_fence,
    persisted_credentials,
    recovery_use_case,
    refresh_request,
    token_response,
)
from tests.integration.m2.test_credential_rotation_repository import (
    oauth_state as oauth_state,  # noqa: PLC0414 - 显式 re-export 让 pytest 在本模块发现 fixture。
)


async def read_across_committed_refresh[T](
    *, read: Callable[[], Awaitable[T]], rotate: Callable[[], Awaitable[object]]
) -> T:
    """固定真实读取凭据→另一会话提交刷新→读取历史的交错，不替换查询或返回事实。

    只暂停指定 reader 的无锁快照查询；writer 的 lease、CAS 和结果读取保持原样。
    超时或断言失败时释放屏障并回收 reader，避免测试留下事务或后台任务。
    """
    entered, release = asyncio.Event(), asyncio.Event()
    original_load = oauth_refresh_coordinator.load_refresh_snapshot

    async def after_credentials(
        session: AsyncSession, request: OAuthRefreshRequest, *, lock: bool
    ) -> tuple[OAuthRefreshSnapshot, EncryptedCredentialModel, EncryptedCredentialModel]:
        """真实读取完成后暂停目标任务，确保后续历史查询尚未开始。"""
        snapshot = await original_load(session, request, lock=lock)
        if not lock and asyncio.current_task() is reader_task:
            entered.set()
            await release.wait()
        return snapshot

    async def invoke_read() -> T:
        """让普通 Awaitable 回调在可识别的独立 asyncio Task 中运行。"""
        return await read()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(oauth_refresh_coordinator, "load_refresh_snapshot", after_credentials)
        reader_task = asyncio.create_task(invoke_read())
        try:
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
            except TimeoutError:
                if reader_task.done():
                    await reader_task
                raise
            await rotate()
            release.set()
            return await asyncio.wait_for(reader_task, timeout=5)
        finally:
            release.set()
            if not reader_task.done():
                reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_read_current_uses_one_snapshot_across_a_concurrent_committed_refresh(
    oauth_state,
) -> None:
    """合法 A→B 提交不能把旧凭据与新历史混读为回滚；旧 readiness 仍受当前 admission 约束。"""
    sessions, cipher, coordinator = oauth_state
    request = refresh_request()
    before = await coordinator.read_current(request)
    concurrent = SqlAlchemyOAuthRefreshCoordinator(
        session_factory=sessions,
        cipher=cipher,
        identity=OAuthRefreshIdentity(bytes(range(32)), key_version=7),
        clock=lambda: NOW,
    )
    provider = FakeRefreshProvider(token_response("synthetic-rotated-refresh"))
    during = await read_across_committed_refresh(
        read=lambda: coordinator.read_current(request),
        rotate=lambda: concurrent.refresh(request, provider),
    )
    assert during == before
    after = await coordinator.read_current(request)
    assert after.snapshot.access != before.snapshot.access
    assert after.snapshot.refresh != before.snapshot.refresh
    reused = await coordinator.refresh(
        replace(request, expected_access=during.snapshot.access), provider
    )
    assert reused == after
    assert provider.calls == 1


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
async def test_lease_acquire_unknown_select_result_discards_the_locked_session(
    oauth_state,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务器已取锁但客户端未获得 SELECT 结果时，必须丢弃会话，不能带锁归池。"""
    sessions, cipher, coordinator = oauth_state
    original_execute = AsyncConnection.execute
    acquiring_pid: int | None = None

    async def lose_first_result(connection: AsyncConnection, statement, *args, **kwargs):
        """真实执行取锁 SQL 后抛非 disconnect 异常，保留活连接来暴露 session 锁泄漏。"""
        nonlocal acquiring_pid
        result = await original_execute(connection, statement, *args, **kwargs)
        if acquiring_pid is None and str(statement) == (
            "SELECT pg_backend_pid(), pg_try_advisory_lock(:key)"
        ):
            pid, acquired = result.one()
            assert acquired is True
            acquiring_pid = int(pid)
            assert not connection.closed and not connection.invalidated
            raise OperationalError(
                "synthetic lease acquire result lost",
                {},
                OSError("synthetic recoverable transport failure"),
                connection_invalidated=False,
            )
        return result

    monkeypatch.setattr(AsyncConnection, "execute", lose_first_result)
    provider = FakeRefreshProvider(token_response())
    # 提前占用另一个物理会话作为观察者，残锁查询不能复用失败后归池的连接。
    async with sessions() as observer:
        observer_pid = await observer.scalar(text("SELECT pg_backend_pid()"))
        with pytest.raises(OperationalError) as failure:
            await coordinator.refresh(refresh_request(), provider)
        assert failure.value.connection_invalidated is False
        assert acquiring_pid is not None and acquiring_pid != observer_pid
        assert provider.calls == 0
        lingering = await observer.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND locktype='advisory')"),
            {"pid": acquiring_pid},
        )
        assert lingering is False, "OAuth session lease survived unknown acquire SELECT result"
        assert (await observer.scalars(select(AuditEventModel))).all() == []
    assert sessions.engine.pool.checkedout() == 0
    restarted = SqlAlchemyOAuthRefreshCoordinator(
        session_factory=sessions,
        cipher=cipher,
        identity=OAuthRefreshIdentity(bytes(range(32)), key_version=7),
        clock=lambda: NOW,
    )
    await restarted.refresh(refresh_request(), provider)
    assert provider.calls == 1
    assert sessions.engine.pool.checkedout() == 0


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


def recovery_tokens(refresh: str | None) -> OAuthTokenSet:
    """满足旧读取能力与新 mail.send 的完整合成响应，不复用生产 scope 计算作断言。"""
    return OAuthTokenSet(
        "synthetic-recovery-access",
        refresh,
        3600,
        SCOPES | {"https://www.googleapis.com/auth/gmail.send"},
    )


async def recovery_facts(sessions):
    """只返回测试所需的 ORM 快照；审计内容只在进程内断言，不写日志。"""
    from ai_employee.infrastructure.db.repositories.credential_rotation import audit_record

    async with sessions() as session:
        connection = await session.get(OAuthConnectionModel, CONNECTION_ID)
        attempts = tuple(
            (
                await session.scalars(
                    select(OAuthAttemptModel).order_by(
                        OAuthAttemptModel.target_authorization_generation
                    )
                )
            ).all()
        )
        events = tuple(
            audit_record(row)
            for row in (
                await session.scalars(select(AuditEventModel).order_by(AuditEventModel.id))
            ).all()
        )
        capabilities = tuple(
            (
                await session.scalars(
                    select(ConnectionCapabilityModel)
                    .where(ConnectionCapabilityModel.connection_id == CONNECTION_ID)
                    .order_by(ConnectionCapabilityModel.capability)
                )
            ).all()
        )
    assert connection is not None
    return connection, attempts, events, capabilities


@pytest.mark.asyncio
async def test_recovery_start_binds_f_s_t_and_repeated_attempts_increment_once(oauth_state) -> None:
    """F 可以早于当前 S；每次显式 start 只产生一次 S→T 和 matching recovery started。"""
    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    async with sessions.begin() as session:
        connection = await session.get(OAuthConnectionModel, CONNECTION_ID)
        connection.authorization_generation = 5
    adapter = RecoveryOAuthAdapter(recovery_tokens(None))
    use_case = recovery_use_case(oauth_state, adapter)
    first_state = await begin_recovery(use_case, adapter)
    second_state = await begin_recovery(use_case, adapter)

    connection, attempts, records, _ = await recovery_facts(sessions)
    assert connection.authorization_generation == 7
    assert [item.target_authorization_generation for item in attempts] == [6, 7]
    assert [item.event_type for item in records] == [
        "oauth.refresh_started",
        "oauth.refresh_recovery_authorization_started",
        "oauth.refresh_recovery_authorization_started",
    ]
    for index, source_generation in enumerate((5, 6), start=1):
        metadata = parse_recovery_started(
            records[index],
            automatic=records[0],
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            key_version=7,
        )
        assert metadata is not None
        assert (
            metadata.fence_generation,
            metadata.source_generation,
            metadata.target_generation,
        ) == (2, source_generation, source_generation + 1)
        assert metadata.oauth_attempt_id == str(attempts[index - 1].id)
        assert records[index].created_at > records[0].created_at
    assert first_state != second_state
    assert adapter.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    (
        "missing",
        "same",
        "empty",
        "invalid_expiry",
        "scope_shrink",
        "denial",
        "known_failure",
        "wrong_identity",
    ),
)
async def test_recovery_unsatisfied_preserves_facts_and_only_closes_recovery(
    oauth_state, outcome
) -> None:
    """未取得有效不同 refresh 时不保存任何 token/scope，并原子收敛 requested 能力。"""
    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    before = await persisted_credentials(sessions)
    adapter = RecoveryOAuthAdapter(
        recovery_tokens(
            "synthetic-old-refresh"
            if outcome == "same"
            else "synthetic-different-refresh"
            if outcome == "wrong_identity"
            else None
        )
    )
    if outcome == "known_failure":
        # 只有已确认供应商拒绝属于 known failure；HTTP timeout/request-failed 单独
        # 使用真实 adapter + HTTP mock 验证 unknown，不能由这个 fake 偷换成已知失败。
        adapter.failure = PermanentProviderError(
            error_code="synthetic_oauth_rejected", message="Safe rejection"
        )
    if outcome == "wrong_identity":
        adapter.account_id = "synthetic-wrong-account"
    if outcome == "scope_shrink":
        adapter.response = OAuthTokenSet(
            "synthetic-recovery-access",
            "synthetic-different-refresh",
            3600,
            frozenset({"https://www.googleapis.com/auth/gmail.send"}),
        )
    if outcome in {"empty", "invalid_expiry"}:
        # 模拟 adapter 边界失守后的规范对象；用例必须再次验证，不能让坏响应更新旧事实。
        adapter.response = recovery_tokens("synthetic-different-refresh")
        object.__setattr__(
            adapter.response,
            "refresh_token" if outcome == "empty" else "expires_in",
            "" if outcome == "empty" else 0,
        )
    use_case = recovery_use_case(oauth_state, adapter)
    state = await begin_recovery(use_case, adapter)
    _, _, _, original_capabilities = await recovery_facts(sessions)
    expected_verified = {
        item.capability: (tuple(item.actual_scopes), item.last_verified_at)
        for item in original_capabilities
    }

    if outcome == "denial":
        await use_case.callback_error(provider="google", state=state)
    else:
        with pytest.raises(DomainError):
            await use_case.callback(code="synthetic-code", state=state)

    connection, attempts, records, capabilities = await recovery_facts(sessions)
    assert connection.status == "connected" and connection.authorization_generation == 3
    assert frozenset(connection.scopes) == SCOPES
    assert await persisted_credentials(sessions) == before
    assert [item.event_type for item in records].count("oauth.refresh_started") == 1
    result = await coordinator.read_result(
        user_id=USER_ID, connection_id=CONNECTION_ID, attempt_id=attempts[0].id, recovery=True
    )
    assert result is not None and isinstance(result.metadata, RecoveryUnsatisfiedV1)
    assert result.metadata.capability_transition == "action_required"
    assert result.metadata.requested_capabilities == ["calendar.read", "mail.read", "mail.send"]
    assert not any(
        "post_" in key or "expires" in key or "deadline" in key or "new_" in key
        for key in result.result.metadata
    )
    assert all(item.status == "action_required" for item in capabilities)
    assert {
        item.capability: (tuple(item.actual_scopes), item.last_verified_at) for item in capabilities
    } == expected_verified
    assert all(item.last_error_code == result.metadata.error_code for item in capabilities)
    assert (
        await coordinator.read_result(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            attempt_id=UUID(records[0].metadata["refresh_attempt_id"]),
        )
        is None
    )
    with pytest.raises(OAuthStateRejectedError):
        await use_case.callback_error(provider="google", state=state)
    assert adapter.calls == (0 if outcome == "denial" else 1)


@pytest.mark.asyncio
async def test_recovery_different_token_consumes_original_after_an_unsatisfied_attempt(
    oauth_state,
) -> None:
    """用户可以创建后续 attempt；只有不同 plaintext 的 replacement 消费原 fence。"""
    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    before = await persisted_credentials(sessions)
    adapter = RecoveryOAuthAdapter(recovery_tokens(None))
    use_case = recovery_use_case(oauth_state, adapter)
    first_state = await begin_recovery(use_case, adapter)
    await use_case.callback_error(provider="google", state=first_state)
    adapter.response = recovery_tokens("synthetic-different-refresh")
    state = await begin_recovery(use_case, adapter)

    assert await use_case.callback(code="synthetic-code", state=state) == CONNECTION_ID

    connection, attempts, records, capabilities = await recovery_facts(sessions)
    assert connection.authorization_generation == 4
    result = await coordinator.read_result(
        user_id=USER_ID, connection_id=CONNECTION_ID, attempt_id=attempts[-1].id, recovery=True
    )
    assert result is not None and isinstance(result.metadata, CredentialReplacedV1)
    assert result.metadata.refresh_identity_changed is True
    assert (
        result.metadata.old_refresh_token_identity_v1
        != result.metadata.new_refresh_token_identity_v1
    )
    assert (
        result.metadata.fence_generation,
        result.metadata.source_generation,
        result.metadata.target_generation,
    ) == (2, 3, 4)
    assert result.metadata.pre_generation == result.metadata.post_generation == 4
    assert result.result.created_at > result.automatic.created_at
    assert result.recovery is not None and result.result.created_at > result.recovery.created_at
    assert {item.capability: item.status for item in capabilities} == {
        "calendar.read": "action_required",
        "mail.read": "enabled",
        "mail.send": "enabled",
    }
    after = await persisted_credentials(sessions)
    assert after["access_token"] != before["access_token"]
    assert after["refresh_token"] != before["refresh_token"]
    assert (
        await coordinator.read_result(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            attempt_id=UUID(records[0].metadata["refresh_attempt_id"]),
        )
        is not None
    )
    assert (
        await coordinator.read_current(refresh_request())
    ).snapshot.authorization_generation == 4
    with pytest.raises(OAuthStateRejectedError):
        await use_case.callback(code="synthetic-code", state=state)
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_recovery_network_holds_shared_lease_but_no_business_row_locks(oauth_state) -> None:
    """真实 code 网络阶段释放业务行锁，但共享 lease 阻断 automatic 且重复 state 零调用。"""
    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_at_exchange() -> None:
        """只在 HTTP 边界停住 callback，所有数据库事务与 lease 均保持生产实现。"""
        entered.set()
        await release.wait()

    adapter = RecoveryOAuthAdapter(
        recovery_tokens("synthetic-different-refresh"), before_response=wait_at_exchange
    )
    use_case = recovery_use_case(oauth_state, adapter)
    state = await begin_recovery(use_case, adapter)
    callback = asyncio.create_task(use_case.callback(code="synthetic-code", state=state))
    automatic = FakeRefreshProvider(token_response())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        # NOWAIT 直接证明 callback 网络等待期间未保留连接、双凭据或 state 的业务行锁。
        async with sessions.begin() as session:
            assert (
                await session.scalar(
                    select(OAuthConnectionModel.id)
                    .where(
                        OAuthConnectionModel.id == CONNECTION_ID,
                        OAuthConnectionModel.user_id == USER_ID,
                    )
                    .with_for_update(nowait=True)
                )
                == CONNECTION_ID
            )
            credentials = (
                await session.scalars(
                    select(EncryptedCredentialModel.id)
                    .where(
                        EncryptedCredentialModel.connection_id == CONNECTION_ID,
                        EncryptedCredentialModel.user_id == USER_ID,
                    )
                    .with_for_update(nowait=True)
                )
            ).all()
            assert len(credentials) == 2
            consumed = await session.scalar(
                select(OAuthAttemptModel.consumed_at)
                .where(
                    OAuthAttemptModel.user_id == USER_ID,
                    OAuthAttemptModel.target_connection_id == CONNECTION_ID,
                )
                .with_for_update(nowait=True)
            )
            assert consumed is not None
        with pytest.raises(OAuthRefreshError) as locked:
            await coordinator.refresh(refresh_request(), automatic)
        assert locked.value.error_code == "oauth_refresh_locked"
        with pytest.raises(OAuthStateRejectedError):
            await use_case.callback(code="synthetic-code", state=state)
        with pytest.raises(OAuthStateRejectedError):
            await use_case.callback_error(provider="google", state=state)
        assert adapter.calls == 1 and automatic.calls == 0
    finally:
        release.set()
        if not callback.done():
            await asyncio.wait_for(callback, timeout=5)
    assert await callback == CONNECTION_ID
    _, _, records, _ = await recovery_facts(sessions)
    assert [item.event_type for item in records] == [
        "oauth.refresh_started",
        "oauth.refresh_recovery_authorization_started",
        "oauth.refresh_credential_replaced",
    ]
    assert sessions.engine.pool.checkedout() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ("unlock", "disconnect"))
async def test_recovery_exchange_after_real_lease_loss_leaves_no_guessed_result(
    oauth_state,
    monkeypatch: pytest.MonkeyPatch,
    loss: str,
) -> None:
    """已交换 code 后真实丢失 session 锁不能补写 unsatisfied/replacement 或重放 state。"""
    from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
        PostgreSQLOAuthRefreshLease,
    )

    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    before = await persisted_credentials(sessions)
    leases = []
    original_assert = PostgreSQLOAuthRefreshLease.assert_owned

    async def capture_owned_lease(lease):
        """保留真实 ownership 检查，仅让 HTTP 故障点拿到实际物理连接。"""
        await original_assert(lease)
        leases.append(lease)

    async def lose_lease_during_exchange() -> None:
        """在响应返回前真实解锁或丢弃连接，不伪造 assert_owned 的返回。"""
        assert leases
        connection = leases[-1].connection
        if loss == "disconnect":
            await connection.invalidate()
        else:
            await connection.execute(text("SELECT pg_advisory_unlock_all()"))
            await connection.commit()

    monkeypatch.setattr(PostgreSQLOAuthRefreshLease, "assert_owned", capture_owned_lease)
    adapter = RecoveryOAuthAdapter(
        recovery_tokens("synthetic-different-refresh"), before_response=lose_lease_during_exchange
    )
    use_case = recovery_use_case(oauth_state, adapter)
    state = await begin_recovery(use_case, adapter)
    with pytest.raises(OAuthRefreshError) as unknown:
        await use_case.callback(code="synthetic-code", state=state)
    assert unknown.value.error_code == "oauth_refresh_result_unknown"
    _, attempts, records, capabilities = await recovery_facts(sessions)
    assert attempts[0].consumed_at is not None
    assert [item.event_type for item in records] == [
        "oauth.refresh_started",
        "oauth.refresh_recovery_authorization_started",
    ]
    assert all(item.status == "authorizing" for item in capabilities)
    assert await persisted_credentials(sessions) == before
    assert (
        await coordinator.read_result(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            attempt_id=attempts[0].id,
            recovery=True,
        )
        is None
    )
    with pytest.raises(OAuthStateRejectedError):
        await use_case.callback(code="synthetic-code", state=state)
    assert adapter.calls == 1 and sessions.engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_recovery_start_audit_failure_rolls_back_generation_attempt_and_capabilities(
    oauth_state,
) -> None:
    """started INSERT 故障必须与唯一 S→T、OAuthAttempt 和 authorizing 全部一起回滚。"""
    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    before = await persisted_credentials(sessions)
    adapter = RecoveryOAuthAdapter(recovery_tokens("synthetic-different-refresh"))
    use_case = recovery_use_case(oauth_state, adapter)
    inserted = 0

    def fail_after_started_insert(mapper, connection, target):
        """数据库真实执行 recovery started INSERT 后抛错，不替换 store 或事务。"""
        nonlocal inserted
        del mapper, connection
        if target.event_type == "oauth.refresh_recovery_authorization_started":
            inserted += 1
            raise RuntimeError("synthetic recovery started insert failure")

    event.listen(AuditEventModel, "after_insert", fail_after_started_insert)
    try:
        with pytest.raises(RuntimeError, match="synthetic recovery started insert failure"):
            await begin_recovery(use_case, adapter)
    finally:
        event.remove(AuditEventModel, "after_insert", fail_after_started_insert)
    connection, attempts, records, capabilities = await recovery_facts(sessions)
    assert inserted == 1 and connection.authorization_generation == 2
    assert attempts == () and len(records) == 1
    assert {item.capability: item.status for item in capabilities} == {
        "mail.read": "enabled",
        "calendar.read": "enabled",
    }
    assert await persisted_credentials(sessions) == before
    assert adapter.calls == 0 and sessions.engine.pool.checkedout() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ("access_token", "refresh_token", "scope", "capability", "result")
)
async def test_recovery_replacement_mutation_failure_rolls_back_all_callback_facts(
    oauth_state,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """每个真实写入阶段失败都回滚凭据、scope、能力与 replacement，先前 state 保持已消费。"""
    from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStore
    from ai_employee.infrastructure.db.repositories.credential_rotation import (
        SqlAlchemyCredentialRotationRepository,
    )

    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    before = await persisted_credentials(sessions)
    adapter = RecoveryOAuthAdapter(recovery_tokens("synthetic-different-refresh"))
    use_case = recovery_use_case(oauth_state, adapter)
    state = await begin_recovery(use_case, adapter)
    _, _, _, original_capabilities = await recovery_facts(sessions)
    expected = {
        item.capability: (item.status, tuple(item.actual_scopes), item.last_verified_at)
        for item in original_capabilities
    }
    injected = 0

    def fail_after_credential_update(mapper, connection, target):
        """只在指定凭据的真实 UPDATE 完成后中断，覆盖双行写入内部的部分失败。"""
        nonlocal injected
        del mapper, connection
        if target.credential_kind == stage:
            injected += 1
            raise RuntimeError("synthetic recovery mutation failure")

    if stage in {"access_token", "refresh_token"}:
        event.listen(EncryptedCredentialModel, "after_update", fail_after_credential_update)
    else:
        owner, name = {
            "scope": (SqlAlchemyConnectionStore, "update_connection_scopes"),
            "capability": (SqlAlchemyConnectionStore, "save_capability_state"),
            "result": (SqlAlchemyCredentialRotationRepository, "_append_recovery_result"),
        }[stage]
        original_mutation = getattr(owner, name)

        async def fail_after_mutation(self, *args, **kwargs):
            """保留真实 mutation/flush，再注入异常以验证外层原子事务。"""
            nonlocal injected
            await original_mutation(self, *args, **kwargs)
            injected += 1
            raise RuntimeError("synthetic recovery mutation failure")

        monkeypatch.setattr(owner, name, fail_after_mutation)
    try:
        with pytest.raises(OAuthRefreshError) as unknown:
            await use_case.callback(code="synthetic-code", state=state)
        assert unknown.value.error_code == "oauth_refresh_result_unknown"
    finally:
        if stage in {"access_token", "refresh_token"}:
            event.remove(EncryptedCredentialModel, "after_update", fail_after_credential_update)
    connection, attempts, records, capabilities = await recovery_facts(sessions)
    assert injected == 1 and connection.authorization_generation == 3
    assert frozenset(connection.scopes) == SCOPES
    assert await persisted_credentials(sessions) == before
    assert {
        item.capability: (item.status, tuple(item.actual_scopes), item.last_verified_at)
        for item in capabilities
    } == expected
    assert [item.event_type for item in records] == [
        "oauth.refresh_started",
        "oauth.refresh_recovery_authorization_started",
    ]
    assert attempts[0].consumed_at is not None
    with pytest.raises(OAuthStateRejectedError):
        await use_case.callback(code="synthetic-code", state=state)
    assert adapter.calls == 1 and sessions.engine.pool.checkedout() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("google", "microsoft"))
@pytest.mark.parametrize("network_failure", ("timeout", "request_failed"))
async def test_recovery_http_exchange_unknown_never_guesses_unsatisfied_or_replays(
    oauth_state,
    provider: str,
    network_failure: str,
) -> None:
    """真实 adapter 的 HTTP 异常仍是未知 exchange，不能因已变成 DomainError 就猜写 result。"""
    sessions, cipher, coordinator = oauth_state
    if provider == "microsoft":
        scopes = frozenset(
            {
                "openid",
                "profile",
                "email",
                "offline_access",
                "User.Read",
                "Mail.Read",
                "Calendars.Read",
            }
        )
        async with sessions.begin() as session:
            connection = await session.get(OAuthConnectionModel, CONNECTION_ID)
            connection.provider, connection.provider_tenant_id = "microsoft", "synthetic-tenant"
            connection.provider_account_id = "synthetic-tenant:synthetic-account"
            connection.account_type, connection.scopes = "work_school", sorted(scopes)
            for capability in (await session.scalars(select(ConnectionCapabilityModel))).all():
                capability.actual_scopes = sorted(scopes)

    async def failed_automatic_response() -> None:
        """只为建立真实 fence 注入一次未知自动结果；恢复本身使用真实 HTTP adapter。"""
        raise TransientProviderError(error_code="synthetic_refresh_unknown", message="Safe failure")

    refresh = FakeRefreshProvider(token_response(), failed_automatic_response)
    refresh.provider = OAuthProvider(provider)
    with pytest.raises(OAuthRefreshError):
        await coordinator.refresh(refresh_request(), refresh)
    before = await persisted_credentials(sessions)
    adapter = (
        GoogleOAuthAdapter(
            "synthetic-client", "synthetic-secret", "https://app.example.test/callback"
        )
        if provider == "google"
        else MicrosoftOAuthAdapter(
            "synthetic-client", "synthetic-secret", "https://app.example.test/callback"
        )
    )
    use_case = ConnectionsUseCase(
        SqlAlchemyConnectionStoreFactory(sessions),
        cipher,
        {provider: adapter},
        RecoveryClock(),
        identity=OAuthRefreshIdentity(bytes(range(32)), key_version=cipher.key_version),
        coordinator=coordinator,
    )
    started = await use_case.start_capability_enable(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        capability=ConnectionCapability.MAIL_SEND,
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    url = "https://oauth2.googleapis.com/token" if provider == "google" else MICROSOFT_TOKEN_URL
    with respx.mock(assert_all_called=True) as mocked:
        endpoint = mocked.post(url).mock(
            side_effect=(
                httpx.ReadTimeout("synthetic response acknowledgement lost")
                if network_failure == "timeout"
                else httpx.ConnectError("synthetic request outcome unavailable")
            )
        )
        with pytest.raises(DomainError) as failure:
            await use_case.callback(code="synthetic-code", state=state)
        _, _, failed_records, _ = await recovery_facts(sessions)
        assert [item.event_type for item in failed_records] == [
            "oauth.refresh_started",
            "oauth.refresh_recovery_authorization_started",
        ]
        assert failure.value.error_code == "oauth_refresh_result_unknown"
        for is_error in (False, True):
            with pytest.raises(OAuthStateRejectedError):
                if is_error:
                    await use_case.callback_error(provider=provider, state=state)
                else:
                    await use_case.callback(code="synthetic-code", state=state)
        assert endpoint.call_count == 1
    _, attempts, records, capabilities = await recovery_facts(sessions)
    assert attempts[0].consumed_at is not None
    assert [item.event_type for item in records] == [
        "oauth.refresh_started",
        "oauth.refresh_recovery_authorization_started",
    ]
    assert all(item.status == "authorizing" for item in capabilities)
    assert await persisted_credentials(sessions) == before
    assert (
        await coordinator.read_result(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            attempt_id=attempts[0].id,
            recovery=True,
        )
        is None
    )


@pytest.mark.asyncio
async def test_recovery_targetless_identity_is_blocked_before_any_save(
    oauth_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无 target 的 OAuth 不得通过身份合并覆写 fenced connection 或创建第二连接。"""
    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    before = await persisted_credentials(sessions)
    adapter = RecoveryOAuthAdapter(recovery_tokens("synthetic-different-refresh"))
    use_case = recovery_use_case(oauth_state, adapter)
    from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStore

    ensure_calls = 0
    original_ensure = SqlAlchemyConnectionStore.ensure_connection

    async def observe_identity_upsert(self, **kwargs):
        """保留真实 ensure 逻辑，仅证明 fenced identity 不会进入已禁止的 upsert 路径。"""
        nonlocal ensure_calls
        ensure_calls += 1
        return await original_ensure(self, **kwargs)

    monkeypatch.setattr(SqlAlchemyConnectionStore, "ensure_connection", observe_identity_upsert)
    await use_case.start(
        user_id=USER_ID, provider="google", capabilities=frozenset({ConnectionCapability.MAIL_READ})
    )
    assert adapter.request is not None

    with pytest.raises(DomainError) as blocked:
        await use_case.callback(code="synthetic-code", state=adapter.request.state)

    assert blocked.value.error_code == "oauth_refresh_recovery_requires_connection_start"
    assert ensure_calls == 0
    connection, _, records, capabilities = await recovery_facts(sessions)
    assert frozenset(connection.scopes) == SCOPES and connection.authorization_generation == 2
    assert await persisted_credentials(sessions) == before
    assert len(records) == 1
    assert all(item.status == "enabled" and item.last_verified_at == NOW for item in capabilities)
    async with sessions() as session:
        assert len((await session.scalars(select(OAuthConnectionModel))).all()) == 1
    assert adapter.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_commit", (False, True))
@pytest.mark.parametrize("outcome", ("denial", "missing", "replacement"))
async def test_recovery_result_commit_ack_loss_uses_fresh_union_and_keeps_state_consumed(
    oauth_state,
    actual_commit: bool,
    outcome: str,
) -> None:
    """真实 commit/rollback 必须分开核对；即使 error callback 结果回滚，state 也不能复用。"""
    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    before = await persisted_credentials(sessions)
    adapter = RecoveryOAuthAdapter(
        recovery_tokens("synthetic-different-refresh" if outcome == "replacement" else None)
    )
    use_case = recovery_use_case(oauth_state, adapter)
    state = await begin_recovery(use_case, adapter)
    injected = []

    def attach_ack_loss(mapper, connection, target):
        """只在真实 result INSERT 后注入底层 commit 边界，不替换业务 writer 或查询。"""
        del mapper, connection
        if target.event_type not in {
            "oauth.refresh_recovery_unsatisfied",
            "oauth.refresh_credential_replaced",
        }:
            return
        sync_session = object_session(target)
        assert sync_session is not None

        def lose_ack(session):
            """after_commit 已持久化，before_commit 则由真实事务上下文回滚。"""
            del session
            raise RuntimeError("synthetic recovery commit acknowledgement lost")

        event.listen(
            sync_session, "after_commit" if actual_commit else "before_commit", lose_ack, once=True
        )
        injected.append(sync_session)

    event.listen(AuditEventModel, "after_insert", attach_ack_loss)
    try:
        if outcome == "denial":
            if actual_commit:
                await use_case.callback_error(provider="google", state=state)
            else:
                with pytest.raises(OAuthRefreshError) as unknown:
                    await use_case.callback_error(provider="google", state=state)
                assert unknown.value.error_code == "oauth_refresh_result_unknown"
        elif actual_commit and outcome == "replacement":
            assert await use_case.callback(code="synthetic-code", state=state) == CONNECTION_ID
        else:
            with pytest.raises(OAuthRefreshError) as error:
                await use_case.callback(code="synthetic-code", state=state)
            assert error.value.error_code == (
                "oauth_refresh_recovery_unsatisfied"
                if actual_commit
                else "oauth_refresh_result_unknown"
            )
    finally:
        event.remove(AuditEventModel, "after_insert", attach_ack_loss)

    _, attempts, records, capabilities = await recovery_facts(sessions)
    assert len(injected) == 1
    assert attempts[0].consumed_at is not None
    result = await coordinator.read_result(
        user_id=USER_ID, connection_id=CONNECTION_ID, attempt_id=attempts[0].id, recovery=True
    )
    assert (result is not None) is actual_commit
    assert (await persisted_credentials(sessions) != before) is (
        actual_commit and outcome == "replacement"
    )
    assert all(
        item.status
        == (
            "enabled"
            if actual_commit and outcome == "replacement"
            else "action_required"
            if actual_commit
            else "authorizing"
        )
        for item in capabilities
    )
    original_closed = await coordinator.read_result(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        attempt_id=UUID(records[0].metadata["refresh_attempt_id"]),
    )
    assert (original_closed is not None) is (actual_commit and outcome == "replacement")
    with pytest.raises(OAuthStateRejectedError):
        await use_case.callback_error(provider="google", state=state)
    with pytest.raises(OAuthStateRejectedError):
        await use_case.callback(code="synthetic-code", state=state)
    assert adapter.calls == (0 if outcome == "denial" else 1)
    assert sessions.engine.pool.checkedout() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error_callback", (False, True))
async def test_recovery_stale_target_is_noop_before_any_code_exchange(
    oauth_state, error_callback: bool
) -> None:
    """旧 T callback 消费自己的 state 和审计，却不能交换 code 或覆盖新 authorizing。"""
    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    adapter = RecoveryOAuthAdapter(recovery_tokens("synthetic-different-refresh"))
    use_case = recovery_use_case(oauth_state, adapter)
    stale_state = await begin_recovery(use_case, adapter)
    await begin_recovery(use_case, adapter)

    if error_callback:
        await use_case.callback_error(provider="google", state=stale_state)
    else:
        with pytest.raises(DomainError):
            await use_case.callback(code="synthetic-code", state=stale_state)

    connection, attempts, _, capabilities = await recovery_facts(sessions)
    assert connection.authorization_generation == 4
    assert all(
        item.status == "authorizing" and item.last_error_code is None for item in capabilities
    )
    result = await coordinator.read_result(
        user_id=USER_ID, connection_id=CONNECTION_ID, attempt_id=attempts[0].id, recovery=True
    )
    assert result is not None and isinstance(result.metadata, RecoveryUnsatisfiedV1)
    assert result.metadata.capability_transition == "stale_target_noop"
    assert adapter.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("during_exchange", (False, True))
async def test_recovery_physical_reencryption_preserves_fence_and_uses_pre_exchange_cas(
    oauth_state,
    during_exchange: bool,
) -> None:
    """历史同明文重加密可显式恢复；网络期间重加密必须使冻结 snapshot CAS 失败。"""
    sessions, cipher, coordinator = oauth_state
    await make_unknown_fence(coordinator)

    async def reencrypt_refresh() -> None:
        """真实事务只重加密同一 plaintext，不改变 identity 或伪造 result。"""
        encrypted = cipher.encrypt(
            b"synthetic-old-refresh", f"{USER_ID}:{CONNECTION_ID}:refresh_token".encode("ascii")
        )
        async with sessions.begin() as session:
            row = await session.scalar(
                select(EncryptedCredentialModel).where(
                    EncryptedCredentialModel.connection_id == CONNECTION_ID,
                    EncryptedCredentialModel.credential_kind == "refresh_token",
                )
            )
            row.ciphertext, row.nonce = encrypted.ciphertext, encrypted.nonce
            row.updated_at = NOW + timedelta(seconds=1)

    if not during_exchange:
        await reencrypt_refresh()
    automatic = FakeRefreshProvider(token_response())
    with pytest.raises(OAuthRefreshError):
        await coordinator.refresh(refresh_request(), automatic)
    assert automatic.calls == 0
    adapter = RecoveryOAuthAdapter(recovery_tokens("synthetic-different-refresh"))
    if during_exchange:
        adapter.before_response = reencrypt_refresh
    use_case = recovery_use_case(oauth_state, adapter)
    state = await begin_recovery(use_case, adapter)
    if during_exchange:
        with pytest.raises(OAuthRefreshError):
            await use_case.callback(code="synthetic-code", state=state)
    else:
        assert await use_case.callback(code="synthetic-code", state=state) == CONNECTION_ID
    _, attempts, records, _ = await recovery_facts(sessions)
    result = await coordinator.read_result(
        user_id=USER_ID, connection_id=CONNECTION_ID, attempt_id=attempts[0].id, recovery=True
    )
    assert (result is not None) is (not during_exchange)
    if result is not None:
        assert isinstance(result.metadata, CredentialReplacedV1)
        assert (
            result.metadata.pre_credential_snapshot_digest_v1
            == records[0].metadata["pre_credential_snapshot_digest_v1"]
        )
        assert (
            result.metadata.pre_refresh_credential_snapshot_digest_v1
            == records[0].metadata["pre_refresh_credential_snapshot_digest_v1"]
        )
    with pytest.raises(OAuthStateRejectedError):
        await use_case.callback(code="synthetic-code", state=state)
    assert adapter.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    (
        "other_user",
        "inactive_user",
        "different_identity",
        "key_version",
        "invalid_aead",
        "missing_refresh",
        "generation_rollback",
        "multiple_fences",
    ),
)
async def test_recovery_start_rejects_invalid_current_proof_without_new_attempt(
    oauth_state,
    mutation: str,
) -> None:
    """恢复 start 必须先验证当前归属/AEAD/固定身份/F≤S/唯一 fence，失败不递增或发出 state。"""
    from ai_employee.application.use_cases.connections import ConnectionNotFoundError
    from ai_employee.infrastructure.db.models.identity import UserModel

    sessions, cipher, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    async with sessions.begin() as session:
        connection = await session.get(OAuthConnectionModel, CONNECTION_ID)
        refresh = await session.scalar(
            select(EncryptedCredentialModel).where(
                EncryptedCredentialModel.connection_id == CONNECTION_ID,
                EncryptedCredentialModel.credential_kind == "refresh_token",
            )
        )
        assert connection is not None and refresh is not None
        if mutation == "inactive_user":
            user = await session.get(UserModel, USER_ID)
            assert user is not None
            user.is_active = False
        elif mutation == "different_identity":
            encrypted = cipher.encrypt(
                b"synthetic-unproven-refresh",
                f"{USER_ID}:{CONNECTION_ID}:refresh_token".encode("ascii"),
            )
            refresh.ciphertext, refresh.nonce = encrypted.ciphertext, encrypted.nonce
        elif mutation == "key_version":
            refresh.key_version = 8
        elif mutation == "invalid_aead":
            refresh.nonce = bytes(12)
        elif mutation == "missing_refresh":
            await session.delete(refresh)
        elif mutation == "generation_rollback":
            connection.authorization_generation = 1
        elif mutation == "multiple_fences":
            original = await session.scalar(select(AuditEventModel))
            assert original is not None
            metadata = dict(original.event_metadata)
            metadata["refresh_attempt_id"] = str(UUID(int=802))
            session.add(
                AuditEventModel(
                    user_id=USER_ID,
                    task_id=None,
                    event_type=original.event_type,
                    actor_type="system",
                    actor_id=None,
                    event_metadata=metadata,
                    created_at=NOW + timedelta(microseconds=1),
                )
            )
    before = await persisted_credentials(sessions)
    adapter = RecoveryOAuthAdapter(recovery_tokens("synthetic-different-refresh"))
    use_case = recovery_use_case(oauth_state, adapter)
    with pytest.raises((OAuthRefreshError, ConnectionNotFoundError)):
        await use_case.start_capability_enable(
            user_id=OTHER_USER_ID if mutation == "other_user" else USER_ID,
            connection_id=CONNECTION_ID,
            capability=ConnectionCapability.MAIL_SEND,
        )
    connection, attempts, records, capabilities = await recovery_facts(sessions)
    assert connection.authorization_generation == (1 if mutation == "generation_rollback" else 2)
    assert attempts == ()
    assert len(records) == (2 if mutation == "multiple_fences" else 1)
    assert all(item.status == "enabled" for item in capabilities)
    assert await persisted_credentials(sessions) == before
    assert adapter.request is None and adapter.calls == 0


@pytest.mark.asyncio
async def test_recovery_disconnect_cannot_reconstruct_missing_old_credential_proof(
    oauth_state,
) -> None:
    """断开真实删除旧凭据后，旧恢复 code 与新 connection-bound start 均不能制造 replacement。"""
    from ai_employee.application.use_cases.connections import ConnectionNotFoundError

    sessions, _, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    adapter = RecoveryOAuthAdapter(recovery_tokens("synthetic-different-refresh"))
    use_case = recovery_use_case(oauth_state, adapter)
    state = await begin_recovery(use_case, adapter)
    async with SqlAlchemyConnectionStoreFactory(sessions)() as store:
        disconnected, _ = await store.disconnect(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            invalidated_at=NOW,
        )
        assert disconnected
    with pytest.raises((DomainError, OAuthStateRejectedError)):
        await use_case.callback(code="synthetic-code", state=state)
    with pytest.raises(ConnectionNotFoundError):
        await begin_recovery(use_case, adapter)
    connection, _, records, _ = await recovery_facts(sessions)
    assert connection.status == "disconnected"
    assert await persisted_credentials(sessions) == {}
    assert not any(item.event_type == "oauth.refresh_credential_replaced" for item in records)
    assert (
        await coordinator.read_result(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            attempt_id=UUID(records[0].metadata["refresh_attempt_id"]),
        )
        is None
    )
    assert adapter.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("access", "same_refresh", "newer", "rollback"))
async def test_recovery_historical_replacement_closure_survives_later_current_changes(
    oauth_state,
    mutation: str,
) -> None:
    """replacement 永久关闭两条 started；后续合法变化仍就绪，A→B→A 只能拒绝当前凭据。"""
    sessions, cipher, coordinator = oauth_state
    await make_unknown_fence(coordinator)
    adapter = RecoveryOAuthAdapter(recovery_tokens("synthetic-different-refresh"))
    use_case = recovery_use_case(oauth_state, adapter)
    state = await begin_recovery(use_case, adapter)
    assert await use_case.callback(code="synthetic-code", state=state) == CONNECTION_ID
    _, attempts, records, _ = await recovery_facts(sessions)
    recovery_result = await coordinator.read_result(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        attempt_id=attempts[0].id,
        recovery=True,
    )
    async with sessions.begin() as session:
        kind = "access_token" if mutation == "access" else "refresh_token"
        row = await session.scalar(
            select(EncryptedCredentialModel).where(
                EncryptedCredentialModel.connection_id == CONNECTION_ID,
                EncryptedCredentialModel.user_id == USER_ID,
                EncryptedCredentialModel.credential_kind == kind,
            )
        )
        assert row is not None
        plaintext = {
            "access": b"synthetic-later-access",
            "same_refresh": b"synthetic-different-refresh",
            "newer": b"synthetic-later-refresh",
            "rollback": b"synthetic-old-refresh",
        }[mutation]
        encrypted = cipher.encrypt(plaintext, f"{USER_ID}:{CONNECTION_ID}:{kind}".encode("ascii"))
        row.ciphertext, row.nonce = encrypted.ciphertext, encrypted.nonce
        row.updated_at = NOW + timedelta(seconds=10)
        connection = await session.get(OAuthConnectionModel, CONNECTION_ID)
        assert connection is not None
        connection.authorization_generation += 1
    assert (
        await coordinator.read_result(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            attempt_id=attempts[0].id,
            recovery=True,
        )
        == recovery_result
    )
    assert (
        await coordinator.read_result(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            attempt_id=UUID(records[0].metadata["refresh_attempt_id"]),
        )
        is not None
    )
    if mutation == "rollback":
        with pytest.raises(OAuthRefreshError) as conflict:
            await coordinator.read_current(refresh_request())
        assert conflict.value.error_code == "oauth_credential_state_conflict"
    else:
        assert (
            await coordinator.read_current(refresh_request())
        ).snapshot.authorization_generation == 4
    with pytest.raises(OAuthStateRejectedError):
        await use_case.callback(code="synthetic-code", state=state)
    assert adapter.calls == 1
