"""在真实 PostgreSQL 上验证唯一 OAuth writer 的双行 CAS 与 matching audit 原子提交。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text

from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
from ai_employee.application.ports.oauth import (
    OAuthAccount,
    OAuthAuthorizationRequest,
    OAuthProvider,
    OAuthRevocationResult,
    OAuthTokenSet,
)
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
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStoreFactory
from ai_employee.infrastructure.db.repositories.credential_rotation import (
    SqlAlchemyCredentialRotationRepository,
    credential_snapshot,
)
from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
    SqlAlchemyOAuthRefreshCoordinator,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher

USER_ID = UUID("00000000-0000-0000-0000-000000000101")
OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000102")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000201")
NOW = datetime(2030, 1, 1, tzinfo=UTC)
SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    }
)


@dataclass
class FakeRefreshProvider:
    """仅替代网络；started/CAS/事务/lease 都使用真实 PostgreSQL 实现。"""

    response: OAuthTokenSet
    before_response: Callable[[], Awaitable[None]] | None = None
    calls: int = 0
    provider = OAuthProvider.GOOGLE

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """由 Fake 的明确能力映射给出 scope 覆盖，避免绕过 response 验证。"""
        scopes = set()
        if ConnectionCapability.MAIL_READ in capabilities:
            scopes.add("https://www.googleapis.com/auth/gmail.readonly")
        if ConnectionCapability.CALENDAR_READ in capabilities:
            scopes.add("https://www.googleapis.com/auth/calendar.readonly")
        return frozenset(scopes)

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """核对受控旧 token 后计数；按场景暂停或抛出网络边界故障。"""
        assert bool(refresh_token)
        self.calls += 1
        if self.before_response is not None:
            await self.before_response()
        return self.response


def token_response(refresh: str | None = None) -> OAuthTokenSet:
    """返回完整且只使用合成 token 的规范 OAuth 结果。"""
    return OAuthTokenSet("synthetic-new-access", refresh, 3600, SCOPES)


async def seed_oauth_connection(sessions: ManagedAsyncSessionMaker, cipher: AeadCipher) -> None:
    """建立两名合成用户、单一连接、两项读取能力与完整的两行 AEAD 凭据。"""
    async with sessions.begin() as session:
        for user_id in (USER_ID, OTHER_USER_ID):
            session.add(
                UserModel(
                    id=user_id,
                    email=f"owner-{user_id.int}@example.test",
                    display_name="Synthetic Owner",
                    password_hash=None,
                    timezone="UTC",
                    locale="zh-CN",
                    brief_time=time(8),
                    is_active=True,
                )
            )
        await session.flush()
        session.add(
            OAuthConnectionModel(
                id=CONNECTION_ID,
                user_id=USER_ID,
                provider="google",
                provider_account_id="synthetic-account-1",
                account_email="owner@example.test",
                scopes=sorted(SCOPES),
                status="connected",
                authorization_generation=2,
            )
        )
        await session.flush()
        for capability in ("mail.read", "calendar.read"):
            session.add(
                ConnectionCapabilityModel(
                    user_id=USER_ID,
                    connection_id=CONNECTION_ID,
                    capability=capability,
                    status="enabled",
                    actual_scopes=sorted(SCOPES),
                    last_verified_at=NOW,
                )
            )
        for index, kind, plaintext, expiry in (
            (701, "access_token", b"synthetic-old-access", NOW + timedelta(minutes=5)),
            (702, "refresh_token", b"synthetic-old-refresh", None),
        ):
            encrypted = cipher.encrypt(
                plaintext, f"{USER_ID}:{CONNECTION_ID}:{kind}".encode("ascii")
            )
            session.add(
                EncryptedCredentialModel(
                    id=UUID(int=index),
                    user_id=USER_ID,
                    connection_id=CONNECTION_ID,
                    credential_kind=kind,
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=expiry,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )


@pytest.fixture
async def oauth_state(
    database_url: str,
) -> AsyncIterator[tuple[ManagedAsyncSessionMaker, AeadCipher, SqlAlchemyOAuthRefreshCoordinator]]:
    """为每个测试建立并释放独立连接池；外层官方 fixture 负责数据库隔离。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(bytes(range(32)), key_version=7)
    await seed_oauth_connection(sessions, cipher)
    coordinator = SqlAlchemyOAuthRefreshCoordinator(
        session_factory=sessions,
        cipher=cipher,
        identity=OAuthRefreshIdentity(bytes(range(32)), key_version=cipher.key_version),
        clock=lambda: NOW,
    )
    try:
        yield sessions, cipher, coordinator
    finally:
        await sessions.dispose()


def refresh_request(**kwargs) -> OAuthRefreshRequest:
    """显式提供用户、连接与被刷新的能力，不依赖默认发送/读取账户。"""
    return OAuthRefreshRequest(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        capability=ConnectionCapability.MAIL_READ,
        **kwargs,
    )


async def persisted_credentials(sessions: ManagedAsyncSessionMaker):
    """通过真实物理 snapshot 对比全部九列，避免只断言解密明文。"""
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(EncryptedCredentialModel).where(
                    EncryptedCredentialModel.user_id == USER_ID,
                    EncryptedCredentialModel.connection_id == CONNECTION_ID,
                )
            )
        ).all()
        return {row.credential_kind: credential_snapshot(row) for row in rows}


@dataclass
class RecoveryOAuthAdapter:
    """仅替代 OAuth HTTP；真实用例、session lease、双行 CAS 和审计保持不变。"""

    response: OAuthTokenSet
    before_response: Callable[[], Awaitable[None]] | None = None
    request: OAuthAuthorizationRequest | None = None
    calls: int = 0
    failure: Exception | None = None
    account_id: str = "synthetic-account-1"
    provider = OAuthProvider.GOOGLE

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """使用完整 Gmail/Calendar 能力映射，使 scope 覆盖仍由生产用例判定。"""
        mapping = {
            ConnectionCapability.MAIL_READ: "https://www.googleapis.com/auth/gmail.readonly",
            ConnectionCapability.MAIL_SEND: "https://www.googleapis.com/auth/gmail.send",
            ConnectionCapability.CALENDAR_READ: "https://www.googleapis.com/auth/calendar.readonly",
            ConnectionCapability.CALENDAR_WRITE: "https://www.googleapis.com/auth/calendar.events",
        }
        return frozenset(mapping[value] for value in capabilities)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """仅在内存保存 state，不把一次性随机值写入日志或证据。"""
        self.request = request
        return "https://provider.example.test/authorize"

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """记录一次网络交换，并允许精确注入供应商故障或数据库交错。"""
        assert bool(code) and bool(verifier)
        self.calls += 1
        if self.before_response is not None:
            await self.before_response()
        if self.failure is not None:
            raise self.failure
        return self.response

    async def fetch_account(
        self, token: OAuthTokenSet, *, expected_nonce_hash: bytes | None
    ) -> OAuthAccount:
        """供应商返回完整规范化身份，身份不匹配测试只替换稳定账户键。"""
        del token, expected_nonce_hash
        return OAuthAccount(self.account_id, "owner@example.test", "", "google")

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """恢复回调不得借此发送第二个自动 refresh grant。"""
        del refresh_token
        raise AssertionError("recovery must not use automatic grant")

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """本 fixture 不执行供应商撤销。"""
        del token
        raise AssertionError("recovery must not revoke")


@dataclass(frozen=True)
class RecoveryClock:
    """同一固定时刻也必须靠微秒顺序生成严格 started/result 时间戳。"""

    def now(self) -> datetime:
        """返回测试 UTC 时钟，不依赖真实墙钟。"""
        return NOW


def recovery_use_case(oauth_state, adapter: RecoveryOAuthAdapter) -> ConnectionsUseCase:
    """为真实数据库恢复构造现有连接用例，供应商网络是唯一 fake 边界。"""
    sessions, cipher, coordinator = oauth_state
    return ConnectionsUseCase(
        SqlAlchemyConnectionStoreFactory(sessions),
        cipher,
        {"google": adapter},
        RecoveryClock(),
        identity=OAuthRefreshIdentity(bytes(range(32)), key_version=cipher.key_version),
        coordinator=coordinator,
    )


async def make_unknown_fence(coordinator: SqlAlchemyOAuthRefreshCoordinator) -> None:
    """通过真实自动 grant admission 加一次已分类传输失败留下原始 durable fence。"""

    async def failed_response() -> None:
        """未知网络结果不能被当成可安全重发的自动刷新。"""
        raise TransientProviderError(error_code="synthetic_oauth_timeout", message="Safe failure")

    provider = FakeRefreshProvider(token_response(), before_response=failed_response)
    with pytest.raises(OAuthRefreshError):
        await coordinator.refresh(refresh_request(), provider)
    assert provider.calls == 1


async def begin_recovery(use_case: ConnectionsUseCase, adapter: RecoveryOAuthAdapter) -> str:
    """显式在原连接发起 mail.send 依赖闭包，并返回只供本次 callback 使用的 state。"""
    await use_case.start_capability_enable(
        user_id=USER_ID, connection_id=CONNECTION_ID, capability=ConnectionCapability.MAIL_SEND
    )
    assert adapter.request is not None
    return adapter.request.state


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["provider_refresh", "calendar_aad_preflight"])
@pytest.mark.parametrize(
    "disposition,refresh",
    [("missing", None), ("same", "synthetic-old-refresh"), ("different", "synthetic-new-refresh")],
)
async def test_rotation_commits_fence_before_network_and_preserves_refresh_bytes(
    oauth_state,
    disposition: str,
    refresh: str | None,
    source: str,
) -> None:
    """真实网络边界前 fence 已提交且无业务行锁；三种合法响应都原子关闭本次 started。"""
    sessions, _, coordinator = oauth_state
    before = await persisted_credentials(sessions)

    async def inspect_committed_start() -> None:
        """另一个事务能读取 fence 并立即取得连接行锁，证明网络阶段无业务事务。"""
        async with sessions.begin() as session:
            assert (
                await session.scalar(select(AuditEventModel.event_type)) == "oauth.refresh_started"
            )
            assert (
                await session.scalar(select(OAuthConnectionModel.id).with_for_update(nowait=True))
                == CONNECTION_ID
            )
            idle_business = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                    "AND pid <> pg_backend_pid() AND state='idle in transaction'"
                )
            )
            assert idle_business == 0

    provider = FakeRefreshProvider(token_response(refresh), inspect_committed_start)
    request = (
        refresh_request()
        if source == "provider_refresh"
        else OAuthRefreshRequest(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            capability=ConnectionCapability.CALENDAR_READ,
            source="calendar_aad_preflight",
            rollout_digest_v1="a" * 64,
        )
    )
    ready = await coordinator.refresh(request, provider)
    after = await persisted_credentials(sessions)
    assert ready.refreshed and provider.calls == 1
    assert before["access_token"] != after["access_token"]
    assert (before["refresh_token"] == after["refresh_token"]) is (disposition != "different")
    async with sessions() as session:
        events = (await session.scalars(select(AuditEventModel).order_by(AuditEventModel.id))).all()
    assert [event.event_type for event in events] == [
        "oauth.refresh_started",
        "oauth.refresh_confirmed",
    ]
    assert events[1].created_at > events[0].created_at
    metadata = events[1].event_metadata
    assert metadata["refresh_token_disposition"] == disposition
    assert metadata["refresh_identity_changed"] is (disposition == "different")
    assert metadata["rollout_deadline_candidate"] == (
        None if source == "provider_refresh" else "2030-01-01T00:45:00.000000Z"
    )
    assert metadata["pre_generation"] == metadata["post_generation"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "generation",
        "capability",
        "access",
        "refresh",
        "access.id",
        "refresh.id",
        "access.ciphertext",
        "refresh.ciphertext",
        "access.nonce",
        "refresh.nonce",
        "access.key_version",
        "refresh.key_version",
        "access.token_expires_at",
    ],
)
async def test_rotation_cas_miss_never_overwrites_concurrent_facts_or_replays(
    oauth_state, mutation: str
) -> None:
    """provider 已调用后改变任一 CAS 条件，结果事务回滚且后续调用只读 unknown fence。"""
    sessions, _, coordinator = oauth_state

    async def mutate() -> None:
        """模拟网络阶段另一已授权事务改变 generation、能力或单行完整快照。"""
        async with sessions.begin() as session:
            if mutation == "generation":
                row = await session.get(OAuthConnectionModel, CONNECTION_ID)
                row.authorization_generation += 1
            elif mutation == "capability":
                row = await session.scalar(
                    select(ConnectionCapabilityModel).where(
                        ConnectionCapabilityModel.capability == "mail.read"
                    )
                )
                row.status = "disabled"
            else:
                kind, _, column = mutation.partition(".")
                row = await session.scalar(
                    select(EncryptedCredentialModel).where(
                        EncryptedCredentialModel.credential_kind == f"{kind}_token"
                    )
                )
                if column in {"ciphertext", "nonce"}:
                    value = getattr(row, column)
                    setattr(row, column, bytes([value[0] ^ 1]) + value[1:])
                elif column == "id":
                    row.id = UUID(int=row.id.int + 10)
                elif column == "key_version":
                    row.key_version += 1
                elif column == "token_expires_at":
                    row.token_expires_at += timedelta(seconds=1)
                else:
                    row.updated_at += timedelta(microseconds=1)

    provider = FakeRefreshProvider(token_response("synthetic-new-refresh"), mutate)
    with pytest.raises(OAuthRefreshError):
        await coordinator.refresh(refresh_request(), provider)
    after = await persisted_credentials(sessions)
    for _ in range(2):
        with pytest.raises(OAuthRefreshError):
            await coordinator.refresh(refresh_request(), provider)
    assert provider.calls == 1
    assert await persisted_credentials(sessions) == after
    async with sessions() as session:
        assert (await session.scalars(select(AuditEventModel.event_type))).all() == [
            "oauth.refresh_started"
        ]


@pytest.mark.asyncio
async def test_rotation_and_confirmed_rollback_together_on_post_mutation_failure(
    oauth_state, monkeypatch
) -> None:
    """在真实 writer 已 mutate/flush 后注入异常，不能留下 credential 或猜测 confirmed。"""
    sessions, _, coordinator = oauth_state
    before = await persisted_credentials(sessions)
    original = SqlAlchemyCredentialRotationRepository.confirm

    async def rollback_after_confirm(self, *args, **kwargs):
        """保留真实 CAS/flush 副作用，异常由外层真实事务回滚。"""
        await original(self, *args, **kwargs)
        raise RuntimeError("synthetic definite rollback")

    monkeypatch.setattr(SqlAlchemyCredentialRotationRepository, "confirm", rollback_after_confirm)
    provider = FakeRefreshProvider(token_response("synthetic-new-refresh"))
    for _ in range(2):
        with pytest.raises(OAuthRefreshError):
            await coordinator.refresh(refresh_request(), provider)
    assert provider.calls == 1
    assert await persisted_credentials(sessions) == before


@pytest.mark.asyncio
async def test_automatic_coordinator_app_role_preserves_append_only_audit_permissions(
    oauth_state, database_url: str
) -> None:
    """真正 app 登录可完成 coordinator；审计 UPDATE 与物理 FOR UPDATE 权限仍被拒绝。"""
    from sqlalchemy import event
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import DBAPIError

    _, cipher, _ = oauth_state
    app_url = make_url(database_url).set(
        username="ai_employee_app", password="app-role-integration-password"
    )
    app_sessions = build_session_factory(app_url.render_as_string(hide_password=False))
    denied_codes = []

    def capture_safe_sqlstate(context):
        """只保留 SQLSTATE，不输出 DBAPI 消息、SQL 参数或连接凭据。"""
        code = getattr(context.original_exception, "sqlstate", None)
        if code is not None:
            denied_codes.append(code)

    event.listen(app_sessions.engine.sync_engine, "handle_error", capture_safe_sqlstate)
    try:
        async with app_sessions() as session:
            assert await session.scalar(text("SELECT current_user")) == "ai_employee_app"
        coordinator = SqlAlchemyOAuthRefreshCoordinator(
            session_factory=app_sessions,
            cipher=cipher,
            identity=OAuthRefreshIdentity(bytes(range(32)), key_version=7),
            clock=lambda: NOW,
        )
        old = await coordinator.read_current(refresh_request())
        provider = FakeRefreshProvider(token_response())
        try:
            ready = await coordinator.refresh(refresh_request(), provider)
        except OAuthRefreshError:
            assert "42501" in denied_codes
            pytest.fail("app-role automatic refresh reached a forbidden audit row-lock privilege")
        assert ready.refreshed and provider.calls == 1
        assert (
            await coordinator.read_result(
                user_id=USER_ID, connection_id=CONNECTION_ID, attempt_id=ready.attempt_id
            )
            is not None
        )
        # 同一过期 access 快照重入只复用合法当前结果，不能产生第二次 grant。
        current = await coordinator.refresh(
            refresh_request(expected_access=old.snapshot.access), provider
        )
        assert not current.refreshed and provider.calls == 1
        with pytest.raises(DBAPIError):
            async with app_sessions.begin() as session:
                await session.execute(
                    text("UPDATE audit_events SET actor_type='system' WHERE false")
                )
        with pytest.raises(DBAPIError):
            async with app_sessions.begin() as session:
                await session.execute(text("SELECT id FROM audit_events FOR UPDATE"))
    finally:
        event.remove(app_sessions.engine.sync_engine, "handle_error", capture_safe_sqlstate)
        await app_sessions.dispose()


@pytest.mark.asyncio
async def test_recovery_app_role_can_start_close_unsatisfied_and_replace_without_audit_update(
    oauth_state,
    database_url: str,
) -> None:
    """真正 app 登录复用两个 audit mutex，拒绝/缺少 refresh/替换均只需 SELECT 与 INSERT。"""
    from sqlalchemy.engine import make_url

    from ai_employee.application.ports.credential_rotation import (
        CredentialReplacedV1,
        RecoveryUnsatisfiedV1,
    )
    from ai_employee.infrastructure.db.models.sources import OAuthAttemptModel

    _, cipher, _ = oauth_state
    app_url = make_url(database_url).set(
        username="ai_employee_app", password="app-role-integration-password"
    )
    app_sessions = build_session_factory(app_url.render_as_string(hide_password=False))
    coordinator = SqlAlchemyOAuthRefreshCoordinator(
        session_factory=app_sessions,
        cipher=cipher,
        identity=OAuthRefreshIdentity(bytes(range(32)), key_version=7),
        clock=lambda: NOW,
    )
    adapter = RecoveryOAuthAdapter(
        OAuthTokenSet(
            "synthetic-recovery-access",
            None,
            3600,
            SCOPES | {"https://www.googleapis.com/auth/gmail.send"},
        )
    )
    use_case = recovery_use_case((app_sessions, cipher, coordinator), adapter)
    try:
        async with app_sessions() as session:
            assert await session.scalar(text("SELECT current_user")) == "ai_employee_app"
            for privilege, expected in (
                ("SELECT", True),
                ("INSERT", True),
                ("UPDATE", False),
                ("DELETE", False),
            ):
                assert (
                    await session.scalar(
                        text(
                            "SELECT has_table_privilege(current_user, 'audit_events', :privilege)"
                        ),
                        {"privilege": privilege},
                    )
                    is expected
                )
        await make_unknown_fence(coordinator)
        for outcome in ("denial", "missing", "replacement"):
            if outcome == "replacement":
                adapter.response = OAuthTokenSet(
                    "synthetic-recovery-access",
                    "synthetic-different-refresh",
                    3600,
                    SCOPES | {"https://www.googleapis.com/auth/gmail.send"},
                )
            state = await begin_recovery(use_case, adapter)
            if outcome == "denial":
                await use_case.callback_error(provider="google", state=state)
            elif outcome == "missing":
                with pytest.raises(OAuthRefreshError):
                    await use_case.callback(code="synthetic-code", state=state)
            else:
                assert await use_case.callback(code="synthetic-code", state=state) == CONNECTION_ID
            async with app_sessions() as session:
                attempt = await session.scalar(
                    select(OAuthAttemptModel)
                    .where(
                        OAuthAttemptModel.user_id == USER_ID,
                        OAuthAttemptModel.target_connection_id == CONNECTION_ID,
                    )
                    .order_by(OAuthAttemptModel.target_authorization_generation.desc())
                )
                assert attempt is not None and attempt.consumed_at is not None
            result = await coordinator.read_result(
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
                attempt_id=attempt.id,
                recovery=True,
            )
            assert result is not None
            assert isinstance(
                result.metadata,
                CredentialReplacedV1 if outcome == "replacement" else RecoveryUnsatisfiedV1,
            )
        assert adapter.calls == 2 and app_sessions.engine.pool.checkedout() == 0
    finally:
        await app_sessions.dispose()
