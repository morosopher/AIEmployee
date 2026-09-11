"""真实 app/retention 角色的 OAuth 生命周期场景，网络边界只使用合成 Fake。

测试只复用官方 lifecycle 数据库，不创建角色、修改 ACL 或手工消费 refresh fence。
真实 writer 的锁、完整 CAS、result append 和 commit 都保留，事件只暂停既有事务边界。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
from ai_employee.application.ports.oauth import OAuthTokenSet
from ai_employee.application.ports.oauth_refresh import OAuthRefreshError, OAuthRefreshRequest
from ai_employee.application.use_cases.connections import ConnectionsUseCase
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import TransientProviderError
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories import oauth_lifecycle
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStoreFactory
from ai_employee.infrastructure.db.repositories.credential_rotation import (
    SqlAlchemyCredentialRotationRepository,
)
from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
    SqlAlchemyOAuthRefreshCoordinator,
    read_refresh_events,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from tests.integration.m2.test_credential_rotation_repository import (
    NOW,
    SCOPES,
    FakeRefreshProvider,
    RecoveryClock,
    RecoveryOAuthAdapter,
    token_response,
)
from tests.integration.retention.checkpoint_cases import _wait_for_blocker


@dataclass(frozen=True)
class _OAuthState:
    """只在测试内聚合显式归属和生产用例，不把密文/明文写入断言输出。"""

    user_id: UUID
    connection_id: UUID
    coordinator: SqlAlchemyOAuthRefreshCoordinator
    use_case: ConnectionsUseCase
    provider: FakeRefreshProvider
    recovery: RecoveryOAuthAdapter

    def request(self, *, source: str = "provider_refresh") -> OAuthRefreshRequest:
        """每次请求固定到实际连接；preflight 使用协议要求的合成 rollout digest。"""
        return OAuthRefreshRequest(
            user_id=self.user_id,
            connection_id=self.connection_id,
            capability=(
                ConnectionCapability.CALENDAR_READ
                if source == "calendar_aad_preflight"
                else ConnectionCapability.MAIL_READ
            ),
            source=source,
            rollout_digest_v1="f" * 64 if source == "calendar_aad_preflight" else None,
        )


async def _seed_state(app: ManagedAsyncSessionMaker) -> _OAuthState:
    """以 app 创建唯一用户/连接、完整 AEAD 双行和能力，避免 module fixture 主键交叠。"""
    user_id, connection_id = uuid4(), uuid4()
    cipher = AeadCipher(bytes(range(32)), key_version=1)
    identity = OAuthRefreshIdentity(bytes(range(32)), key_version=1)
    async with app.begin() as session:
        session.add(
            UserModel(
                id=user_id,
                email=f"{user_id}@example.test",
                display_name="Synthetic",
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            OAuthConnectionModel(
                id=connection_id,
                user_id=user_id,
                provider="google",
                provider_account_id=str(connection_id),
                account_email="synthetic@example.test",
                scopes=sorted(SCOPES),
                status="connected",
                authorization_generation=2,
            )
        )
        await session.flush()
        for capability in ("mail.read", "calendar.read"):
            session.add(
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=capability,
                    status="enabled",
                    actual_scopes=sorted(SCOPES),
                    last_verified_at=NOW,
                )
            )
        for kind, plaintext in (
            ("access_token", b"synthetic-old-access"),
            ("refresh_token", b"synthetic-old-refresh"),
        ):
            encrypted = cipher.encrypt(
                plaintext, f"{user_id}:{connection_id}:{kind}".encode("ascii")
            )
            session.add(
                EncryptedCredentialModel(
                    id=uuid4(),
                    user_id=user_id,
                    connection_id=connection_id,
                    credential_kind=kind,
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=NOW + timedelta(minutes=5) if kind == "access_token" else None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    coordinator = SqlAlchemyOAuthRefreshCoordinator(
        session_factory=app,
        cipher=cipher,
        identity=identity,
        clock=lambda: NOW,
    )
    recovery = RecoveryOAuthAdapter(
        OAuthTokenSet(
            "synthetic-recovery-access",
            "synthetic-replacement-refresh",
            3600,
            SCOPES | {"https://www.googleapis.com/auth/gmail.send"},
        ),
        account_id=str(connection_id),
    )
    return _OAuthState(
        user_id,
        connection_id,
        coordinator,
        ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(app),
            cipher,
            {"google": recovery},
            RecoveryClock(),
            identity=identity,
            coordinator=coordinator,
        ),
        FakeRefreshProvider(token_response()),
        recovery,
    )


async def _events(app: ManagedAsyncSessionMaker, state: _OAuthState):
    """仅读取共享内容无关事件投影，所有读取显式注入 user/connection。"""
    async with app() as session:
        return await read_refresh_events(
            session, user_id=state.user_id, connection_id=state.connection_id
        )


async def _prepare_writer(state: _OAuthState, kind: str) -> Callable[[], Awaitable[None]]:
    """真实未知自动 grant 建立 fence，真实 progressive attempt 冻结恢复绑定。"""
    if kind != "confirmed":

        async def unknown() -> None:
            """只有真实网络边界失败；started 的提交由 coordinator 完成。"""
            raise TransientProviderError(
                error_code="synthetic_oauth_timeout", message="Safe failure"
            )

        provider = FakeRefreshProvider(token_response(), before_response=unknown)
        with pytest.raises(OAuthRefreshError):
            await state.coordinator.refresh(state.request(), provider)
        assert provider.calls == 1
        await state.use_case.start_capability_enable(
            user_id=state.user_id,
            connection_id=state.connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )
        assert state.recovery.request is not None
        callback_state = state.recovery.request.state

    async def write() -> None:
        """调用实际应用入口；unsatisfied 专门覆盖没有 session lease 的拒绝回调。"""
        if kind == "confirmed":
            await state.coordinator.refresh(state.request(), state.provider)
        elif kind == "unsatisfied":
            await state.use_case.callback_error(provider="google", state=callback_state)
        else:
            assert (
                await state.use_case.callback(code="synthetic-code", state=callback_state)
                == state.connection_id
            )

    return write


async def _assert_privileges(
    app: ManagedAsyncSessionMaker, retention: ManagedAsyncSessionMaker
) -> None:
    """真实登录逐项复核必要的禁止权限，锁测试不能依赖扩大 audit/credential 授权。"""
    for sessions, role, table, privileges in (
        (app, "ai_employee_app", "audit_events", ("UPDATE", "DELETE")),
        (retention, "ai_employee_retention", "encrypted_credentials", ("UPDATE", "INSERT")),
    ):
        async with sessions() as session:
            assert await session.scalar(text("SELECT current_user")) == role
            for privilege in privileges:
                assert (
                    await session.scalar(
                        text("SELECT has_table_privilege(current_user,:table,:privilege)"),
                        {"table": table, "privilege": privilege},
                    )
                    is False
                )


async def assert_oauth_writer_cleanup_race(
    *,
    app_url: str,
    retention_url: str,
    kind: str,
    order: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 confirmed/unsatisfied/replacement 与保留双向排斥，失败锁不产生部分删除。"""
    app, retention = build_session_factory(app_url), build_session_factory(retention_url)
    release, locked = asyncio.Event(), asyncio.Event()
    jobs: list[asyncio.Task] = []
    queries: list[str] = []

    def observe(connection, cursor, statement, parameters, context, executemany) -> None:
        """只记录 SQL 列名，证明普通 retention 不加载任何 token/expiry/lineage。"""
        del connection, cursor, parameters, context, executemany
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(retention.engine.sync_engine, "before_cursor_execute", observe)
    try:
        await _assert_privileges(app, retention)
        state = await _seed_state(app)
        other = await _seed_state(app)
        # 先通过真实 writer 生成一个合法已闭合候选。新 writer 未提交时，清理仍会实际取表锁。
        await state.coordinator.refresh(state.request(), FakeRefreshProvider(token_response()))
        await other.coordinator.refresh(other.request(), FakeRefreshProvider(token_response()))
        other_before = await _events(app, other)
        write = await _prepare_writer(state, kind)
        before = await _events(app, state)
        cleaner = oauth_lifecycle.OAuthLifecycleCleanup(retention)

        async def clean() -> int:
            return await cleaner.clean_user(
                user_id=state.user_id, cutoff=NOW + timedelta(days=1), batch_size=10
            )

        if order == "writer_first":
            method = {
                "confirmed": "confirm",
                "unsatisfied": "unsatisfied",
                "replacement": "replace",
            }[kind]
            original = getattr(SqlAlchemyCredentialRotationRepository, method)

            async def pause_writer(self, *args, **kwargs):
                """真正 CAS/result flush 后暂停；业务事务仍未提交，物理行锁仍由 app 持有。"""
                result = await original(self, *args, **kwargs)
                locked.set()
                await release.wait()
                return result

            monkeypatch.setattr(SqlAlchemyCredentialRotationRepository, method, pause_writer)
            jobs.append(asyncio.create_task(write()))
            async with asyncio.timeout(10):
                await locked.wait()
                assert await clean() == 0
                still_committed = await _events(app, state)
                assert tuple(still_committed[: len(before)]) == before
                assert len(still_committed) == len(before) + (1 if kind == "confirmed" else 0)
            release.set()
            await jobs[-1]
        else:
            original_locks = oauth_lifecycle.lock_cleanup_refresh_events

            async def pause_cleanup(session, *, user_id, connection_id, refresh_attempt_id=None):
                """真实表锁/共同 audit mutex 后暂停，未释放时 app 的真实行锁必须等待。"""
                records = await original_locks(
                    session,
                    user_id=user_id,
                    connection_id=connection_id,
                    refresh_attempt_id=refresh_attempt_id,
                )
                if user_id == state.user_id and not locked.is_set():
                    locked.set()
                    await release.wait()
                return records

            monkeypatch.setattr(oauth_lifecycle, "lock_cleanup_refresh_events", pause_cleanup)
            jobs.append(asyncio.create_task(clean()))
            async with asyncio.timeout(10):
                await locked.wait()
                jobs.append(asyncio.create_task(write()))
                await _wait_for_blocker(app)
                assert not jobs[-1].done()
                assert state.provider.calls == state.recovery.calls == 0
                release.set()
                assert await jobs[0] == 1
                await jobs[-1]

        assert await clean() == (2 if order == "writer_first" else 1)
        remaining = await _events(app, state)
        assert [row.event_type for row in remaining] == (
            ["oauth.refresh_started"] if kind == "unsatisfied" else []
        )
        assert await _events(app, other) == other_before
        if kind == "unsatisfied":
            # 删除未满足恢复 pair 不能关闭原 fence；普通刷新与迁移 preflight 都保持零网络。
            for source in ("provider_refresh", "calendar_aad_preflight"):
                provider = FakeRefreshProvider(token_response())
                with pytest.raises(OAuthRefreshError):
                    await state.coordinator.refresh(state.request(source=source), provider)
                assert provider.calls == 0
        forbidden = (
            "encrypted_credentials.ciphertext",
            "encrypted_credentials.nonce",
            "encrypted_credentials.key_version",
            "encrypted_credentials.token_expires_at",
            "oauth_connections.authorization_generation",
            "oauth_connections.scopes",
        )
        assert not any(column in query for query in queries for column in forbidden)
        await _assert_privileges(app, retention)
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        event.remove(retention.engine.sync_engine, "before_cursor_execute", observe)
        await retention.dispose()
        await app.dispose()


async def assert_oauth_cutoff_and_rollback(
    *,
    app_url: str,
    retention_url: str,
    kind: str,
) -> None:
    """实际 result 最大时刻决定整组截止；DELETE 后异常必须回滚全部成员且下次可清理。"""
    app, retention = build_session_factory(app_url), build_session_factory(retention_url)
    failure_installed = False

    def fail_after_delete(connection, cursor, statement, parameters, context, executemany) -> None:
        """在实际 DELETE 已发送后抛错，验证事务回滚而非仅模拟零行输入。"""
        del connection, cursor, parameters, context, executemany
        if statement.startswith("DELETE FROM audit_events"):
            raise RuntimeError("synthetic group deletion rollback")

    try:
        state = await _seed_state(app)
        write = await _prepare_writer(state, kind)
        await write()
        before = await _events(app, state)
        cutoff: datetime = max(row.created_at for row in before)
        cleaner = oauth_lifecycle.OAuthLifecycleCleanup(retention)
        assert await cleaner.clean_user(user_id=state.user_id, cutoff=cutoff, batch_size=1) == 0
        assert await _events(app, state) == before
        event.listen(retention.engine.sync_engine, "after_cursor_execute", fail_after_delete)
        failure_installed = True
        with pytest.raises(RuntimeError, match="synthetic group deletion rollback"):
            await cleaner.clean_user(
                user_id=state.user_id, cutoff=cutoff + timedelta(microseconds=1), batch_size=1
            )
        assert await _events(app, state) == before
        event.remove(retention.engine.sync_engine, "after_cursor_execute", fail_after_delete)
        failure_installed = False
        assert (
            await cleaner.clean_user(
                user_id=state.user_id, cutoff=cutoff + timedelta(microseconds=1), batch_size=1
            )
            == 1
        )
        remaining = await _events(app, state)
        assert [row.event_type for row in remaining] == (
            ["oauth.refresh_started"] if kind == "unsatisfied" else []
        )
        await _assert_privileges(app, retention)
    finally:
        if failure_installed:
            event.remove(retention.engine.sync_engine, "after_cursor_execute", fail_after_delete)
        await retention.dispose()
        await app.dispose()


async def assert_oauth_fresh_group_recheck(
    *,
    app_url: str,
    retention_url: str,
    kind: str,
    duplicate_timing: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选读取后插入重复结果，过期、cutoff 当刻或更新结果都必须使完整组保留。"""
    app, retention = build_session_factory(app_url), build_session_factory(retention_url)
    try:
        state = await _seed_state(app)
        await (await _prepare_writer(state, kind))()
        initial = await _events(app, state)
        result = initial[-1]
        cutoff = NOW + timedelta(days=1)
        duplicate_created_at = {
            "expired": result.created_at + timedelta(microseconds=1),
            "at_cutoff": cutoff,
            "after_cutoff": cutoff + timedelta(microseconds=1),
        }[duplicate_timing]
        observed = []
        original_lock = oauth_lifecycle.lock_oauth_cleanup_identity

        async def insert_conflict_before_locks(session, *, user_id, connection_id):
            """不修改旧审计；app 的原有 INSERT 权限注入并发坏事实，完整组解析必须拒绝。"""
            if not observed:
                observed.append(True)
                async with app.begin() as writer:
                    writer.add(
                        AuditEventModel(
                            user_id=user_id,
                            task_id=None,
                            event_type=result.event_type,
                            actor_type="system",
                            actor_id=None,
                            event_metadata=dict(result.metadata),
                            created_at=duplicate_created_at,
                        )
                    )
            return await original_lock(session, user_id=user_id, connection_id=connection_id)

        monkeypatch.setattr(
            oauth_lifecycle, "lock_oauth_cleanup_identity", insert_conflict_before_locks
        )
        assert (
            await oauth_lifecycle.OAuthLifecycleCleanup(retention).clean_user(
                user_id=state.user_id,
                cutoff=cutoff,
                batch_size=1,
            )
            == 0
        )
        assert observed == [True]
        remaining = await _events(app, state)
        assert tuple(remaining[:-1]) == initial and len(remaining) == len(initial) + 1
    finally:
        await retention.dispose()
        await app.dispose()
