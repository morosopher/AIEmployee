"""验证连接能力仓储的唯一性、身份绑定、替换语义与 token 保留不变量。"""

import asyncio
import base64
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.oauth import (
    OAuthAccount,
    OAuthAuthorizationRequest,
    OAuthProvider,
    OAuthRevocationResult,
    OAuthRevocationStatus,
    OAuthTokenSet,
)
from ai_employee.application.use_cases.connections import (
    CapabilityDisableResult,
    ConnectionsUseCase,
)
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.connections import (
    SqlAlchemyConnectionStore,
    SqlAlchemyConnectionStoreFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher

_CALLBACK_STATE = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class FixedClock:
    """为真实 Repository callback 提供确定性 UTC 时间。"""

    current: datetime = datetime(2030, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        """返回冻结时间。"""
        return self.current


@dataclass(slots=True)
class RepositoryCallbackOAuthAdapter:
    """通过真实事务触发 callback 保存路径的合成 Microsoft adapter。"""

    account: OAuthAccount
    token: OAuthTokenSet
    provider: OAuthProvider = OAuthProvider.MICROSOFT

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """把内部能力映射为合成 scope。"""
        return frozenset(f"scope:{capability.value}" for capability in capabilities)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """Repository callback 测试不发起授权。"""
        del request
        raise AssertionError("build_authorization_url must not be called")

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """验证一次性 callback 参数并返回合成 token。"""
        assert code == "synthetic-code"
        assert verifier == "synthetic-verifier"
        return self.token

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """返回合成规范身份，不读取真实供应商。"""
        assert token is self.token
        assert expected_nonce_hash == b"n" * 32
        return self.account

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """callback 测试不刷新 token。"""
        del refresh_token
        raise AssertionError("refresh must not be called")

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """callback 测试不撤销 token。"""
        del token
        raise AssertionError("revoke must not be called")


@dataclass(slots=True)
class ProgressiveOAuthAdapter:
    """记录渐进授权 state，并让 callback 返回可切换的合成账户。"""

    account: OAuthAccount
    token: OAuthTokenSet
    provider: OAuthProvider = OAuthProvider.MICROSOFT
    authorization_states: list[str] = field(default_factory=list)
    fetch_started: asyncio.Event | None = None
    release_fetch: asyncio.Event | None = None

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """把能力闭包映射为稳定合成 scope。"""
        return frozenset(f"scope:{capability.value}" for capability in capabilities)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """记录 state，测试无需从日志或数据库读取明文。"""
        self.authorization_states.append(request.state)
        return "https://provider.example.test/authorize"

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """验证 callback 只传递本地解密后的 verifier。"""
        assert code == "synthetic-code"
        assert verifier != ""
        return self.token

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """返回当前测试指定的账户身份。"""
        assert token is self.token
        assert expected_nonce_hash is not None
        if self.fetch_started is not None:
            self.fetch_started.set()
        if self.release_fetch is not None:
            await self.release_fetch.wait()
        return self.account

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """本组测试不刷新 token。"""
        del refresh_token
        raise AssertionError("refresh must not be called")

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """无 refresh token 的断开路径不会调用撤销；保留完整端口实现。"""
        del token
        return OAuthRevocationResult(OAuthRevocationStatus.REVOKED)


@dataclass(slots=True)
class RevocationFailureOAuthAdapter:
    """让断开测试注入供应商撤销失败，验证本地提交不依赖网络结果。"""

    provider: OAuthProvider = OAuthProvider.MICROSOFT

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """断开路径不会读取 scope；保留完整 adapter 端口以便组合根校验。"""
        del capabilities
        return frozenset()

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """断开路径不发起授权。"""
        del request
        raise AssertionError("build_authorization_url must not be called")

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """断开路径不交换授权码。"""
        del code, verifier
        raise AssertionError("exchange_code must not be called")

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """断开路径不读取账户资料。"""
        del token, expected_nonce_hash
        raise AssertionError("fetch_account must not be called")

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """断开路径不刷新 token。"""
        del refresh_token
        raise AssertionError("refresh must not be called")

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """模拟供应商网络/传输失败，不能伪装成撤销成功。"""
        assert token == "synthetic-refresh"
        raise RuntimeError("synthetic provider revoke failure")


@dataclass(slots=True)
class InitialOAuthAdapter:
    """记录首次 OAuth state，并可在 token 交换阶段暂停制造并发竞态。"""

    account: OAuthAccount
    token: OAuthTokenSet
    provider: OAuthProvider = OAuthProvider.GOOGLE
    authorization_states: list[str] = field(default_factory=list)
    exchange_started: asyncio.Event | None = None
    release_exchange: asyncio.Event | None = None

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """把首次授权能力映射为确定性的合成 scope。"""
        return frozenset(f"scope:{capability.value}" for capability in capabilities)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """保存 state 原文仅在测试内存中，避免从数据库读取敏感 state。"""
        self.authorization_states.append(request.state)
        return "https://provider.example.test/authorize"

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """在可选屏障处暂停，确保 callback 已消费 state 后再执行断开。"""
        assert code == "synthetic-code"
        assert verifier != ""
        if self.exchange_started is not None:
            self.exchange_started.set()
        if self.release_exchange is not None:
            await self.release_exchange.wait()
        return self.token

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """返回预置规范身份，验证 OIDC nonce 摘要仍沿用原 attempt。"""
        assert token is self.token
        assert expected_nonce_hash is not None
        return self.account

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """测试不刷新 token。"""
        del refresh_token
        raise AssertionError("refresh must not be called")

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """首次连接竞态测试不保存 refresh token，断开无需撤销。"""
        del token
        return OAuthRevocationResult(OAuthRevocationStatus.REVOKED)


@dataclass(slots=True)
class DisjointInitialOAuthAdapter:
    """让两个首次 Google callback 在保存前同时返回互不重叠的读取 scope。"""

    account: OAuthAccount
    tokens_by_code: dict[str, OAuthTokenSet]
    provider: OAuthProvider = OAuthProvider.GOOGLE
    exchange_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_exchange: asyncio.Event = field(default_factory=asyncio.Event)
    fetch_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_fetch: asyncio.Event = field(default_factory=asyncio.Event)
    authorization_states: list[str] = field(default_factory=list)
    _exchange_count: int = 0
    _fetch_count: int = 0

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """把本次首次授权意图映射为合成 scope，保持能力集合精确。"""
        return frozenset(f"scope:{capability.value}" for capability in capabilities)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """首次授权测试只需持久化 state，URL 内容不参与断言。"""
        self.authorization_states.append(request.state)
        return "https://provider.example.test/authorize"

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """等待两个 callback 都消费完 state，确保保存阶段存在并发竞争。"""
        assert verifier != ""
        self._exchange_count += 1
        if self._exchange_count == 2:
            self.exchange_started.set()
        await self.release_exchange.wait()
        return self.tokens_by_code[code]

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """等待两个 callback 都完成供应商读取，再同时进入本地保存。"""
        assert token in self.tokens_by_code.values()
        assert expected_nonce_hash is not None
        self._fetch_count += 1
        if self._fetch_count == 2:
            self.fetch_started.set()
        await self.release_fetch.wait()
        return self.account

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """首次 callback 不刷新 token。"""
        del refresh_token
        raise AssertionError("refresh must not be called")

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """首次连接竞态测试不调用撤销端点。"""
        del token
        return OAuthRevocationResult(OAuthRevocationStatus.REVOKED)


def _user(email: str) -> UserModel:
    """构造满足当前身份 Schema 的合成用户，不依赖认证或真实个人资料。"""
    return UserModel(
        email=email,
        display_name="Synthetic Repository User",
        password_hash="synthetic-password-hash",
        timezone="UTC",
        locale="zh-CN",
        brief_time=time(8, 0),
        is_active=True,
    )


async def _create_callback_attempt(
    store: SqlAlchemyConnectionStore,
    *,
    cipher: AeadCipher,
    user_id: UUID,
    requested_capabilities: frozenset[ConnectionCapability],
) -> None:
    """写入与 ``_CALLBACK_STATE`` 匹配且 verifier 使用真实 AAD 的 OAuth attempt。"""
    attempt_id = uuid4()
    await store.create_attempt(
        attempt_id=attempt_id,
        user_id=user_id,
        provider="microsoft",
        state_hash=hashlib.sha256(_CALLBACK_STATE.encode("ascii")).digest(),
        verifier=cipher.encrypt(
            b"synthetic-verifier",
            f"{user_id}:{attempt_id}:pkce_verifier".encode("ascii"),
        ),
        requested_capabilities=requested_capabilities,
        oidc_nonce_hash=b"n" * 32,
        expires_at=datetime(2030, 1, 1, tzinfo=UTC) + timedelta(minutes=10),
        created_at=datetime(2030, 1, 1, tzinfo=UTC),
    )


async def _seed_progressive_connection(
    session_factory: ManagedAsyncSessionMaker,
    *,
    email: str,
    account_key: str,
) -> tuple[UUID, UUID]:
    """创建启用 mail.read 的 Microsoft 连接，返回用户与连接标识。"""
    async with session_factory.begin() as session:
        user = _user(email)
        session.add(user)
        await session.flush()
        user_id = user.id
        store = SqlAlchemyConnectionStore(session)
        connection_id = await store.ensure_connection(
            user_id=user_id,
            provider="microsoft",
            provider_account_id=account_key,
            provider_tenant_id="tenant-a",
            account_type="work_school",
            account_email=email,
            scopes=frozenset({"scope:mail.read"}),
        )
        await store.save_capability_state(
            user_id=user_id,
            connection_id=connection_id,
            capability=ConnectionCapability.MAIL_READ,
            status=CapabilityStatus.ENABLED,
            actual_scopes=frozenset({"scope:mail.read"}),
            last_verified_at=datetime(2030, 1, 1, tzinfo=UTC),
            last_error_code=None,
        )
    return user_id, connection_id


async def _seed_initial_google_connection(
    session_factory: ManagedAsyncSessionMaker,
    *,
    email: str,
) -> tuple[UUID, UUID]:
    """创建与首次 OAuth 返回身份相同的 Google 连接，供断开竞态测试使用。"""
    async with session_factory.begin() as session:
        user = _user(email)
        session.add(user)
        await session.flush()
        connection_id = await SqlAlchemyConnectionStore(session).ensure_connection(
            user_id=user.id,
            provider="google",
            provider_account_id="google-initial-subject",
            provider_tenant_id="",
            account_type="google",
            account_email=email,
            scopes=frozenset({"scope:mail.read"}),
        )
        return user.id, connection_id


def _initial_google_adapter(
    *,
    email: str,
    exchange_started: asyncio.Event | None = None,
    release_exchange: asyncio.Event | None = None,
) -> InitialOAuthAdapter:
    """构造首次 Google OAuth 的合成 adapter，避免测试重复拼接身份/token。"""
    return InitialOAuthAdapter(
        account=OAuthAccount(
            provider_account_id="google-initial-subject",
            account_email=email,
            provider_tenant_id="",
            account_type="google",
        ),
        token=OAuthTokenSet(
            access_token="initial-access",
            refresh_token="initial-refresh",
            expires_in=3600,
            granted_scopes=frozenset({"scope:mail.read"}),
        ),
        exchange_started=exchange_started,
        release_exchange=release_exchange,
    )


@pytest.mark.asyncio
async def test_concurrent_targetless_google_callbacks_reject_disjoint_token_scopes(
    database_url: str,
) -> None:
    """同一帐号的 disjoint token 只能提交一个，最终能力必须与可用凭据一致。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"w" * 32)
    email = "concurrent-disjoint-google@example.test"
    adapter = DisjointInitialOAuthAdapter(
        account=OAuthAccount(
            provider_account_id="google-disjoint-subject",
            account_email=email,
            provider_tenant_id="",
            account_type="google",
        ),
        tokens_by_code={
            "mail-code": OAuthTokenSet(
                access_token="mail-access",
                refresh_token="mail-refresh",
                expires_in=3600,
                granted_scopes=frozenset({"scope:mail.read"}),
            ),
            "calendar-code": OAuthTokenSet(
                access_token="calendar-access",
                refresh_token="calendar-refresh",
                expires_in=3600,
                granted_scopes=frozenset({"scope:calendar.read"}),
            ),
        },
    )
    try:
        async with session_factory.begin() as session:
            user = _user(email)
            session.add(user)
            await session.flush()
            user_id = user.id

        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(session_factory),
            cipher,
            {"google": adapter},
            FixedClock(),
        )
        await use_case.start(
            user_id=user_id,
            provider=OAuthProvider.GOOGLE,
            capabilities=frozenset({ConnectionCapability.MAIL_READ}),
        )
        mail_state = adapter.authorization_states[-1]
        await use_case.start(
            user_id=user_id,
            provider=OAuthProvider.GOOGLE,
            capabilities=frozenset({ConnectionCapability.CALENDAR_READ}),
        )
        calendar_state = adapter.authorization_states[-1]

        callbacks = asyncio.gather(
            use_case.callback(code="mail-code", state=mail_state),
            use_case.callback(code="calendar-code", state=calendar_state),
            return_exceptions=True,
        )
        await asyncio.wait_for(adapter.exchange_started.wait(), timeout=5)
        adapter.release_exchange.set()
        await asyncio.wait_for(adapter.fetch_started.wait(), timeout=5)
        adapter.release_fetch.set()
        results = await asyncio.wait_for(callbacks, timeout=5)
        successes = tuple(
            (index, result) for index, result in enumerate(results) if isinstance(result, UUID)
        )
        conflicts = tuple(result for result in results if isinstance(result, StateConflictError))
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0].error_code == "oauth_authorization_scope_conflict"
        success_index, connection_id = successes[0]
        winner_code = ("mail-code", "calendar-code")[success_index]
        winner_capability = (
            ConnectionCapability.MAIL_READ,
            ConnectionCapability.CALENDAR_READ,
        )[success_index]
        loser_capability = (
            ConnectionCapability.CALENDAR_READ,
            ConnectionCapability.MAIL_READ,
        )[success_index]
        winner_token = adapter.tokens_by_code[winner_code]

        async with session_factory() as session:
            connection = await session.get(OAuthConnectionModel, connection_id)
            rows = tuple(
                (
                    await session.scalars(
                        select(ConnectionCapabilityModel)
                        .where(ConnectionCapabilityModel.connection_id == connection_id)
                        .order_by(ConnectionCapabilityModel.capability)
                    )
                ).all()
            )
            credentials = tuple(
                (
                    await session.scalars(
                        select(EncryptedCredentialModel)
                        .where(EncryptedCredentialModel.connection_id == connection_id)
                        .order_by(EncryptedCredentialModel.credential_kind)
                    )
                ).all()
            )

        assert connection is not None
        assert set(connection.scopes) == set(winner_token.granted_scopes)
        by_capability = {row.capability: row for row in rows}
        assert by_capability[winner_capability.value].status == (CapabilityStatus.ENABLED.value)
        assert set(by_capability[winner_capability.value].actual_scopes) == set(
            winner_token.granted_scopes
        )
        assert by_capability[loser_capability.value].status == CapabilityStatus.DISABLED.value
        assert by_capability[loser_capability.value].actual_scopes == []
        assert by_capability[ConnectionCapability.MAIL_SEND.value].status == (
            CapabilityStatus.DISABLED.value
        )
        assert by_capability[ConnectionCapability.CALENDAR_WRITE.value].status == (
            CapabilityStatus.DISABLED.value
        )
        by_kind = {row.credential_kind: row for row in credentials}
        assert set(by_kind) == {"access_token", "refresh_token"}
        for kind, expected in (
            ("access_token", winner_token.access_token),
            ("refresh_token", winner_token.refresh_token),
        ):
            assert expected is not None
            row = by_kind[kind]
            decrypted = cipher.decrypt(
                EncryptedValue(row.ciphertext, row.nonce, row.key_version),
                f"{user_id}:{connection_id}:{kind}".encode("ascii"),
            ).decode("utf-8")
            assert decrypted == expected
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_initial_oauth_state_is_invalidated_when_connection_disconnects_before_callback(
    database_url: str,
) -> None:
    """首次 state 在同账户连接断开后不得重新 connected 或写入凭据。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"u" * 32)
    email = "initial-before-callback@example.test"
    try:
        user_id, connection_id = await _seed_initial_google_connection(
            session_factory,
            email=email,
        )
        adapter = _initial_google_adapter(email=email)
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(session_factory),
            cipher,
            {"google": adapter},
            FixedClock(),
        )
        await use_case.start(
            user_id=user_id,
            provider=OAuthProvider.GOOGLE,
            capabilities=frozenset({ConnectionCapability.MAIL_READ}),
        )
        stale_state = adapter.authorization_states[-1]
        await use_case.disconnect(user_id=user_id, connection_id=connection_id)

        with pytest.raises(StateConflictError) as raised:
            await use_case.callback(code="synthetic-code", state=stale_state)
        assert raised.value.error_code == "oauth_attempt_invalidated"

        async with session_factory() as session:
            connection = await session.get(OAuthConnectionModel, connection_id)
            credentials = tuple((await session.scalars(select(EncryptedCredentialModel))).all())
        assert connection is not None
        assert connection.status == "disconnected"
        assert credentials == ()
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_initial_oauth_callback_and_disconnect_interleaving_cannot_resurrect_connection(
    database_url: str,
) -> None:
    """state 已消费后若断开先提交，callback 保存阶段仍必须读到持久失效标记。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"v" * 32)
    email = "initial-interleaving@example.test"
    exchange_started = asyncio.Event()
    release_exchange = asyncio.Event()
    try:
        user_id, connection_id = await _seed_initial_google_connection(
            session_factory,
            email=email,
        )
        adapter = _initial_google_adapter(
            email=email,
            exchange_started=exchange_started,
            release_exchange=release_exchange,
        )
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(session_factory),
            cipher,
            {"google": adapter},
            FixedClock(),
        )
        await use_case.start(
            user_id=user_id,
            provider=OAuthProvider.GOOGLE,
            capabilities=frozenset({ConnectionCapability.MAIL_READ}),
        )
        stale_state = adapter.authorization_states[-1]
        callback_task = asyncio.create_task(
            use_case.callback(code="synthetic-code", state=stale_state)
        )
        await asyncio.wait_for(exchange_started.wait(), timeout=5)

        await use_case.disconnect(user_id=user_id, connection_id=connection_id)
        release_exchange.set()
        with pytest.raises(StateConflictError) as raised:
            await callback_task
        assert raised.value.error_code == "oauth_attempt_invalidated"

        async with session_factory() as session:
            connection = await session.get(OAuthConnectionModel, connection_id)
            credentials = tuple((await session.scalars(select(EncryptedCredentialModel))).all())
        assert connection is not None
        assert connection.status == "disconnected"
        assert credentials == ()
    finally:
        if not release_exchange.is_set():
            release_exchange.set()
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_fresh_initial_oauth_state_reconnects_after_prior_disconnect(
    database_url: str,
) -> None:
    """断开后新建的首次 OAuth state 仍可安全重连同一规范账户。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"w" * 32)
    email = "initial-fresh-reconnect@example.test"
    try:
        user_id, connection_id = await _seed_initial_google_connection(
            session_factory,
            email=email,
        )
        adapter = _initial_google_adapter(email=email)
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(session_factory),
            cipher,
            {"google": adapter},
            FixedClock(),
        )
        await use_case.start(
            user_id=user_id,
            provider=OAuthProvider.GOOGLE,
            capabilities=frozenset({ConnectionCapability.MAIL_READ}),
        )
        stale_state = adapter.authorization_states[-1]
        await use_case.disconnect(user_id=user_id, connection_id=connection_id)

        await use_case.start(
            user_id=user_id,
            provider=OAuthProvider.GOOGLE,
            capabilities=frozenset({ConnectionCapability.MAIL_READ}),
        )
        fresh_state = adapter.authorization_states[-1]
        assert fresh_state != stale_state
        result = await use_case.callback(code="synthetic-code", state=fresh_state)
        assert result == connection_id

        async with session_factory() as session:
            connection = await session.get(OAuthConnectionModel, connection_id)
            credentials = tuple((await session.scalars(select(EncryptedCredentialModel))).all())
        assert connection is not None
        assert connection.status == "connected"
        assert len(credentials) == 2
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_capability_upsert_replaces_actual_scopes_and_keeps_unique_row(
    database_url: str,
) -> None:
    """同一用户/连接/能力只能有一行，后续验证必须替换而非累积实际 scope。"""
    session_factory = build_session_factory(database_url)
    verified_at = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            user = _user("repository-owner@example.test")
            session.add(user)
            await session.flush()
            store = SqlAlchemyConnectionStore(session)
            connection_id = await store.ensure_connection(
                user_id=user.id,
                provider="google",
                provider_account_id="repository-account",
                provider_tenant_id="",
                account_type="google",
                account_email="repository-owner@example.test",
                scopes=frozenset({"scope:mail.read", "scope:mail.send"}),
            )

            await store.save_capability_state(
                user_id=user.id,
                connection_id=connection_id,
                capability=ConnectionCapability.MAIL_SEND,
                status=CapabilityStatus.AUTHORIZING,
                actual_scopes=frozenset(),
                last_verified_at=None,
                last_error_code=None,
            )
            await store.save_capability_state(
                user_id=user.id,
                connection_id=connection_id,
                capability=ConnectionCapability.MAIL_SEND,
                status=CapabilityStatus.ENABLED,
                actual_scopes=frozenset({"scope:mail.read", "scope:mail.send"}),
                last_verified_at=verified_at,
                last_error_code=None,
            )
            await store.save_capability_state(
                user_id=user.id,
                connection_id=connection_id,
                capability=ConnectionCapability.MAIL_SEND,
                status=CapabilityStatus.ACTION_REQUIRED,
                actual_scopes=frozenset({"scope:mail.read"}),
                last_verified_at=verified_at + timedelta(minutes=1),
                last_error_code="connection_scope_missing",
            )

            row_count = await session.scalar(
                select(func.count())
                .select_from(ConnectionCapabilityModel)
                .where(
                    ConnectionCapabilityModel.user_id == user.id,
                    ConnectionCapabilityModel.connection_id == connection_id,
                    ConnectionCapabilityModel.capability == "mail.send",
                )
            )
            capability = await session.scalar(
                select(ConnectionCapabilityModel).where(
                    ConnectionCapabilityModel.user_id == user.id,
                    ConnectionCapabilityModel.connection_id == connection_id,
                    ConnectionCapabilityModel.capability == "mail.send",
                )
            )

        assert row_count == 1
        assert capability is not None
        assert capability.status == "action_required"
        assert capability.actual_scopes == ["scope:mail.read"]
        assert capability.last_error_code == "connection_scope_missing"
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_missing_rotated_refresh_token_preserves_existing_ciphertext(
    database_url: str,
) -> None:
    """callback 未返回新 refresh token 时只能轮换 access token，不能清空既有密文。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _user("refresh-owner@example.test")
            session.add(user)
            await session.flush()
            store = SqlAlchemyConnectionStore(session)
            connection_id = await store.ensure_connection(
                user_id=user.id,
                provider="google",
                provider_account_id="refresh-account",
                provider_tenant_id="",
                account_type="google",
                account_email="refresh-owner@example.test",
                scopes=frozenset({"scope:mail.read"}),
            )
            original_refresh = EncryptedValue(b"old-refresh-ciphertext", b"r" * 12, 1)
            await store.save_connection_tokens(
                user_id=user.id,
                connection_id=connection_id,
                access_token=EncryptedValue(b"old-access-ciphertext", b"a" * 12, 1),
                refresh_token=original_refresh,
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
            await store.save_connection_tokens(
                user_id=user.id,
                connection_id=connection_id,
                access_token=EncryptedValue(b"new-access-ciphertext", b"b" * 12, 1),
                refresh_token=None,
                expires_at=datetime(2030, 1, 2, tzinfo=UTC),
            )
            refresh = await session.scalar(
                select(EncryptedCredentialModel).where(
                    EncryptedCredentialModel.connection_id == connection_id,
                    EncryptedCredentialModel.credential_kind == "refresh_token",
                )
            )

        assert refresh is not None
        assert refresh.ciphertext == original_refresh.ciphertext
        assert refresh.nonce == original_refresh.nonce
        assert refresh.key_version == original_refresh.key_version
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_cross_user_token_save_rejected_without_credential_or_cursor_mutation(
    database_url: str,
) -> None:
    """其他用户不能利用已知 connection ID 覆写 token，失败前后凭据和游标必须完全相同。"""
    session_factory = build_session_factory(database_url)
    original_access = EncryptedValue(b"owner-access", b"a" * 12, 1)
    original_refresh = EncryptedValue(b"owner-refresh", b"r" * 12, 1)
    try:
        async with session_factory.begin() as session:
            owner = _user("credential-owner@example.test")
            other = _user("credential-other@example.test")
            session.add_all((owner, other))
            await session.flush()
            owner_id, other_id = owner.id, other.id
            store = SqlAlchemyConnectionStore(session)
            connection_id = await store.ensure_connection(
                user_id=owner_id,
                provider="google",
                provider_account_id="credential-owner-account",
                provider_tenant_id="",
                account_type="google",
                account_email="credential-owner@example.test",
                scopes=frozenset({"scope:mail.read"}),
            )
            await store.save_connection_tokens(
                user_id=owner_id,
                connection_id=connection_id,
                access_token=original_access,
                refresh_token=original_refresh,
                expires_at=datetime(2030, 1, 2, tzinfo=UTC),
            )

        async with session_factory.begin() as session:
            store = SqlAlchemyConnectionStore(session)
            with pytest.raises(StateConflictError) as raised:
                await store.save_connection_tokens(
                    user_id=other_id,
                    connection_id=connection_id,
                    access_token=EncryptedValue(b"foreign-access", b"b" * 12, 1),
                    refresh_token=EncryptedValue(b"foreign-refresh", b"c" * 12, 1),
                    expires_at=datetime(2030, 1, 3, tzinfo=UTC),
                )
            assert raised.value.error_code == "connection_credential_ownership_conflict"

            credentials = tuple(
                (
                    await session.scalars(
                        select(EncryptedCredentialModel)
                        .where(EncryptedCredentialModel.connection_id == connection_id)
                        .order_by(EncryptedCredentialModel.credential_kind)
                    )
                ).all()
            )
            cursors = tuple(
                (
                    await session.scalars(
                        select(SyncCursorModel)
                        .where(SyncCursorModel.connection_id == connection_id)
                        .order_by(SyncCursorModel.resource_kind, SyncCursorModel.scope_key)
                    )
                ).all()
            )

        assert {
            row.credential_kind: (
                row.user_id,
                row.ciphertext,
                row.nonce,
                row.key_version,
                row.token_expires_at,
            )
            for row in credentials
        } == {
            "access_token": (
                owner_id,
                original_access.ciphertext,
                original_access.nonce,
                original_access.key_version,
                datetime(2030, 1, 2, tzinfo=UTC),
            ),
            "refresh_token": (
                owner_id,
                original_refresh.ciphertext,
                original_refresh.nonce,
                original_refresh.key_version,
                None,
            ),
        }
        assert [(row.resource_kind, row.scope_key, row.cursor) for row in cursors] == [
            ("calendar", "primary", None),
            ("mail", "mailbox", None),
        ]
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_attempt_and_capability_snapshot_are_typed_and_user_scoped(
    database_url: str,
) -> None:
    """仓储须保留授权意图并只向连接拥有者投影能力与规范化日历目录。"""
    session_factory = build_session_factory(database_url)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            owner = _user("snapshot-owner@example.test")
            other = _user("snapshot-other@example.test")
            session.add_all((owner, other))
            await session.flush()
            store = SqlAlchemyConnectionStore(session)
            connection_id = await store.ensure_connection(
                user_id=owner.id,
                provider="google",
                provider_account_id="snapshot-account",
                provider_tenant_id="",
                account_type="google",
                account_email="snapshot-owner@example.test",
                scopes=frozenset({"scope:calendar.read"}),
            )
            await store.save_capability_state(
                user_id=owner.id,
                connection_id=connection_id,
                capability=ConnectionCapability.CALENDAR_READ,
                status=CapabilityStatus.ENABLED,
                actual_scopes=frozenset({"scope:calendar.read"}),
                last_verified_at=now,
                last_error_code=None,
            )
            session.add(
                ProviderCalendarModel(
                    user_id=owner.id,
                    connection_id=connection_id,
                    provider_calendar_id="snapshot-calendar",
                    name="Snapshot Calendar",
                    timezone="UTC",
                    is_primary=True,
                    access_role="reader",
                    can_write=False,
                    provider_url=None,
                )
            )
            attempt_id = await store.create_attempt(
                attempt_id=owner.id,
                user_id=owner.id,
                provider="google",
                state_hash=b"s" * 32,
                verifier=EncryptedValue(b"verifier-ciphertext", b"v" * 12, 1),
                requested_capabilities=frozenset({ConnectionCapability.CALENDAR_READ}),
                oidc_nonce_hash=b"n" * 32,
                expires_at=now + timedelta(minutes=10),
                created_at=now,
            )
            owner_snapshot = await store.get_capability_snapshot(
                user_id=owner.id,
                connection_id=connection_id,
            )
            foreign_snapshot = await store.get_capability_snapshot(
                user_id=other.id,
                connection_id=connection_id,
            )
            attempt = await session.get(OAuthAttemptModel, attempt_id)

        assert attempt is not None
        assert attempt.provider == "google"
        assert attempt.requested_capabilities == ["calendar.read"]
        assert attempt.oidc_nonce_hash == b"n" * 32
        assert owner_snapshot is not None
        assert owner_snapshot.provider == "google"
        assert owner_snapshot.provider_calendars[0].id == "snapshot-calendar"
        assert foreign_snapshot is None
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_distinct_normalized_provider_keys_keep_tenant_accounts_separate(
    database_url: str,
) -> None:
    """Task 10 规范键包含 tenant 时，现有唯一键必须允许同一 Graph ID 的两个租户。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _user("normalized-keys@example.test")
            session.add(user)
            await session.flush()
            store = SqlAlchemyConnectionStore(session)
            first_id = await store.ensure_connection(
                user_id=user.id,
                provider="microsoft",
                provider_account_id="tenant-a:graph-user",
                provider_tenant_id="tenant-a",
                account_type="work_school",
                account_email="normalized-keys@example.test",
                scopes=frozenset({"scope:mail.read"}),
            )
            second_id = await store.ensure_connection(
                user_id=user.id,
                provider="microsoft",
                provider_account_id="tenant-b:graph-user",
                provider_tenant_id="tenant-b",
                account_type="work_school",
                account_email="normalized-keys@example.test",
                scopes=frozenset({"scope:mail.read"}),
            )
            rows = tuple(
                (
                    await session.scalars(
                        select(OAuthConnectionModel)
                        .where(OAuthConnectionModel.user_id == user.id)
                        .order_by(OAuthConnectionModel.provider_account_id)
                    )
                ).all()
            )

        assert first_id != second_id
        assert [(row.provider_account_id, row.provider_tenant_id) for row in rows] == [
            ("tenant-a:graph-user", "tenant-a"),
            ("tenant-b:graph-user", "tenant-b"),
        ]
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_capabilities", "granted_scopes", "expected_states"),
    (
        (
            frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND}),
            frozenset({"scope:mail.send"}),
            {
                ConnectionCapability.CALENDAR_READ: ("disabled", None),
                ConnectionCapability.CALENDAR_WRITE: ("disabled", None),
                ConnectionCapability.MAIL_READ: (
                    "action_required",
                    "connection_scope_missing",
                ),
                ConnectionCapability.MAIL_SEND: (
                    "action_required",
                    "connection_scope_missing",
                ),
            },
        ),
        (
            frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND}),
            frozenset({"scope:mail.read", "scope:mail.send"}),
            {
                ConnectionCapability.CALENDAR_READ: ("disabled", None),
                ConnectionCapability.CALENDAR_WRITE: ("disabled", None),
                ConnectionCapability.MAIL_READ: ("enabled", None),
                ConnectionCapability.MAIL_SEND: ("enabled", None),
            },
        ),
        (
            frozenset(
                {
                    ConnectionCapability.MAIL_READ,
                    ConnectionCapability.CALENDAR_READ,
                    ConnectionCapability.CALENDAR_WRITE,
                }
            ),
            frozenset({"scope:mail.read", "scope:calendar.write"}),
            {
                ConnectionCapability.CALENDAR_READ: (
                    "action_required",
                    "connection_scope_missing",
                ),
                ConnectionCapability.CALENDAR_WRITE: (
                    "action_required",
                    "connection_scope_missing",
                ),
                ConnectionCapability.MAIL_READ: ("enabled", None),
                ConnectionCapability.MAIL_SEND: ("disabled", None),
            },
        ),
    ),
)
async def test_real_callback_persists_dependency_closed_capability_states(
    database_url: str,
    requested_capabilities: frozenset[ConnectionCapability],
    granted_scopes: frozenset[str],
    expected_states: dict[ConnectionCapability, tuple[str, str | None]],
) -> None:
    """真实 PostgreSQL callback 对仅写、完整闭包和 mixed scope 都保存可信能力状态。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"d" * 32)
    try:
        async with session_factory.begin() as session:
            user = _user("callback-dependencies@example.test")
            session.add(user)
            await session.flush()
            user_id = user.id
            await _create_callback_attempt(
                SqlAlchemyConnectionStore(session),
                cipher=cipher,
                user_id=user_id,
                requested_capabilities=requested_capabilities,
            )

        adapter = RepositoryCallbackOAuthAdapter(
            account=OAuthAccount(
                provider_account_id="tenant-a:callback-dependencies",
                account_email="callback-dependencies@example.test",
                provider_tenant_id="tenant-a",
                account_type="work_school",
            ),
            token=OAuthTokenSet(
                access_token="callback-access",
                refresh_token="callback-refresh",
                expires_in=3600,
                granted_scopes=granted_scopes,
            ),
        )
        connection_id = await ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(session_factory),
            cipher,
            {"microsoft": adapter},
            FixedClock(),
        ).callback(code="synthetic-code", state=_CALLBACK_STATE)

        async with session_factory() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(ConnectionCapabilityModel)
                        .where(ConnectionCapabilityModel.connection_id == connection_id)
                        .order_by(ConnectionCapabilityModel.capability)
                    )
                ).all()
            )

        assert {
            ConnectionCapability(row.capability): (row.status, row.last_error_code) for row in rows
        } == expected_states
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_tenant_id", "callback_account_type"),
    (
        ("tenant-b", "work_school"),
        ("tenant-a", "personal"),
    ),
)
async def test_callback_rejects_normalized_key_identity_rebinding_without_mutation(
    database_url: str,
    callback_tenant_id: str,
    callback_account_type: str,
) -> None:
    """同一规范账户键不能静默改绑 tenant/type，失败 callback 也不能轮换既有 token。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"i" * 32)
    original_access = EncryptedValue(b"original-access", b"a" * 12, 1)
    original_refresh = EncryptedValue(b"original-refresh", b"r" * 12, 1)
    try:
        async with session_factory.begin() as session:
            user = _user("identity-binding@example.test")
            session.add(user)
            await session.flush()
            user_id = user.id
            store = SqlAlchemyConnectionStore(session)
            connection_id = await store.ensure_connection(
                user_id=user_id,
                provider="microsoft",
                provider_account_id="tenant-a:graph-user",
                provider_tenant_id="tenant-a",
                account_type="work_school",
                account_email="identity-binding@example.test",
                scopes=frozenset({"scope:mail.read"}),
            )
            await store.save_connection_tokens(
                user_id=user_id,
                connection_id=connection_id,
                access_token=original_access,
                refresh_token=original_refresh,
                expires_at=datetime(2030, 1, 2, tzinfo=UTC),
            )
            await _create_callback_attempt(
                store,
                cipher=cipher,
                user_id=user_id,
                requested_capabilities=frozenset({ConnectionCapability.MAIL_READ}),
            )

        adapter = RepositoryCallbackOAuthAdapter(
            account=OAuthAccount(
                provider_account_id="tenant-a:graph-user",
                account_email="rebound@example.test",
                provider_tenant_id=callback_tenant_id,
                account_type=callback_account_type,
            ),
            token=OAuthTokenSet(
                access_token="replacement-access",
                refresh_token="replacement-refresh",
                expires_in=3600,
                granted_scopes=frozenset({"scope:mail.read"}),
            ),
        )
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(session_factory),
            cipher,
            {"microsoft": adapter},
            FixedClock(),
        )

        with pytest.raises(StateConflictError) as raised:
            await use_case.callback(code="synthetic-code", state=_CALLBACK_STATE)
        assert raised.value.error_code == "connection_identity_conflict"

        async with session_factory() as session:
            connection = await session.get(OAuthConnectionModel, connection_id)
            credentials = tuple(
                (
                    await session.scalars(
                        select(EncryptedCredentialModel)
                        .where(EncryptedCredentialModel.connection_id == connection_id)
                        .order_by(EncryptedCredentialModel.credential_kind)
                    )
                ).all()
            )
            capability_statuses = tuple(
                await session.scalars(
                    select(ConnectionCapabilityModel.status)
                    .where(ConnectionCapabilityModel.connection_id == connection_id)
                    .order_by(ConnectionCapabilityModel.capability)
                )
            )

        assert connection is not None
        assert connection.provider_tenant_id == "tenant-a"
        assert connection.account_type == "work_school"
        assert connection.account_email == "identity-binding@example.test"
        assert connection.scopes == ["scope:mail.read"]
        assert {
            row.credential_kind: (row.ciphertext, row.nonce, row.key_version) for row in credentials
        } == {
            "access_token": (
                original_access.ciphertext,
                original_access.nonce,
                original_access.key_version,
            ),
            "refresh_token": (
                original_refresh.ciphertext,
                original_refresh.nonce,
                original_refresh.key_version,
            ),
        }
        assert capability_statuses == ("disabled", "disabled", "disabled", "disabled")
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_disconnect_commits_local_state_before_provider_revoke_failure(
    database_url: str,
) -> None:
    """供应商撤销失败时，本地连接仍保持断开且所有凭据密文已删除。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"r" * 32)
    try:
        async with session_factory.begin() as session:
            user = _user("disconnect-revoke-failure@example.test")
            session.add(user)
            await session.flush()
            user_id = user.id
            store = SqlAlchemyConnectionStore(session)
            connection_id = await store.ensure_connection(
                user_id=user_id,
                provider="microsoft",
                provider_account_id="tenant-a:disconnect-revoke-failure",
                provider_tenant_id="tenant-a",
                account_type="work_school",
                account_email="disconnect-revoke-failure@example.test",
                scopes=frozenset({"scope:mail.read"}),
            )
            await store.save_connection_tokens(
                user_id=user_id,
                connection_id=connection_id,
                access_token=cipher.encrypt(
                    b"synthetic-access",
                    f"{user_id}:{connection_id}:access_token".encode("ascii"),
                ),
                refresh_token=cipher.encrypt(
                    b"synthetic-refresh",
                    f"{user_id}:{connection_id}:refresh_token".encode("ascii"),
                ),
                expires_at=datetime(2030, 1, 2, tzinfo=UTC),
            )

        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(session_factory),
            cipher,
            {"microsoft": RevocationFailureOAuthAdapter()},
            FixedClock(),
        )
        with pytest.raises(RuntimeError, match="synthetic provider revoke failure"):
            await use_case.disconnect(user_id=user_id, connection_id=connection_id)

        async with session_factory() as session:
            connection = await session.get(OAuthConnectionModel, connection_id)
            credentials = tuple(
                (
                    await session.scalars(
                        select(EncryptedCredentialModel).where(
                            EncryptedCredentialModel.connection_id == connection_id
                        )
                    )
                ).all()
            )

        assert connection is not None
        assert connection.status == "disconnected"
        assert connection.authorization_generation == 1
        assert credentials == ()
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_progressive_callback_cannot_land_on_a_different_connection(
    database_url: str,
) -> None:
    """绑定连接 A 的 state 即使返回连接 B 的身份，也不能覆写 B 的 token 或能力。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"b" * 32)
    original_access: EncryptedValue | None = None
    original_refresh: EncryptedValue | None = None
    try:
        user_id, target_connection_id = await _seed_progressive_connection(
            session_factory,
            email="progressive-owner@example.test",
            account_key="tenant-a:target-a",
        )
        async with session_factory.begin() as session:
            store = SqlAlchemyConnectionStore(session)
            other_connection_id = await store.ensure_connection(
                user_id=user_id,
                provider="microsoft",
                provider_account_id="tenant-a:target-b",
                provider_tenant_id="tenant-a",
                account_type="work_school",
                account_email="progressive-other@example.test",
                scopes=frozenset({"scope:mail.read"}),
            )
            original_access = cipher.encrypt(
                b"original-access",
                f"{user_id}:{other_connection_id}:access_token".encode("ascii"),
            )
            original_refresh = cipher.encrypt(
                b"original-refresh",
                f"{user_id}:{other_connection_id}:refresh_token".encode("ascii"),
            )
            await store.save_connection_tokens(
                user_id=user_id,
                connection_id=other_connection_id,
                access_token=original_access,
                refresh_token=original_refresh,
                expires_at=datetime(2030, 1, 2, tzinfo=UTC),
            )

        adapter = ProgressiveOAuthAdapter(
            account=OAuthAccount(
                provider_account_id="tenant-a:target-b",
                account_email="progressive-other@example.test",
                provider_tenant_id="tenant-a",
                account_type="work_school",
            ),
            token=OAuthTokenSet(
                access_token="replacement-access",
                refresh_token="replacement-refresh",
                expires_in=3600,
                granted_scopes=frozenset({"scope:mail.read", "scope:mail.send"}),
            ),
        )
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(session_factory),
            cipher,
            {"microsoft": adapter},
            FixedClock(),
        )
        await use_case.start_capability_enable(
            user_id=user_id,
            connection_id=target_connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )
        state = adapter.authorization_states[-1]

        with pytest.raises(StateConflictError) as raised:
            await use_case.callback(code="synthetic-code", state=state)
        assert raised.value.error_code == "oauth_attempt_invalidated"

        async with session_factory() as session:
            credentials = tuple(
                (
                    await session.scalars(
                        select(EncryptedCredentialModel)
                        .where(EncryptedCredentialModel.connection_id == other_connection_id)
                        .order_by(EncryptedCredentialModel.credential_kind)
                    )
                ).all()
            )
            target_credentials = tuple(
                (
                    await session.scalars(
                        select(EncryptedCredentialModel).where(
                            EncryptedCredentialModel.connection_id == target_connection_id
                        )
                    )
                ).all()
            )

        assert original_access is not None
        assert original_refresh is not None
        assert {
            row.credential_kind: (row.ciphertext, row.nonce, row.key_version) for row in credentials
        } == {
            "access_token": (
                original_access.ciphertext,
                original_access.nonce,
                original_access.key_version,
            ),
            "refresh_token": (
                original_refresh.ciphertext,
                original_refresh.nonce,
                original_refresh.key_version,
            ),
        }
        assert target_credentials == ()
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_disable_capability_cannot_race_progressive_callback_into_broken_dependency(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭读能力与渐进回调交错时，回调必须因授权代际变化而失效。

    测试先让 callback 在真实事务中消费 state 并暂停于合成身份请求；disable 随后沿
    全部 Task→User→Approval→ToolExecution→本地动作锁序扫描，在 Connection 行锁后读取
    enabled 快照并暂停。释放身份请求后，callback 保存必须等待同一用户/连接屏障，并在
    disable 提交后因代际变化而失效，不能形成缺少 ``mail.read`` 的 ``mail.send=enabled``。
    """
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"p" * 32)
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    enabled_read_observed = asyncio.Event()
    release_enabled_read = asyncio.Event()
    disable_task: asyncio.Task[CapabilityDisableResult] | None = None
    callback_task: asyncio.Task[UUID] | None = None
    email = "disable-callback-race@example.test"
    try:
        user_id, connection_id = await _seed_progressive_connection(
            session_factory,
            email=email,
            account_key="tenant-a:disable-callback-race",
        )
        adapter = ProgressiveOAuthAdapter(
            account=OAuthAccount(
                provider_account_id="tenant-a:disable-callback-race",
                account_email=email,
                provider_tenant_id="tenant-a",
                account_type="work_school",
            ),
            token=OAuthTokenSet(
                access_token="race-access",
                refresh_token="race-refresh",
                expires_in=3600,
                granted_scopes=frozenset({"scope:mail.read", "scope:mail.send"}),
            ),
            fetch_started=fetch_started,
            release_fetch=release_fetch,
        )
        store_factory = SqlAlchemyConnectionStoreFactory(session_factory)
        use_case = ConnectionsUseCase(
            store_factory,
            cipher,
            {"microsoft": adapter},
            FixedClock(),
        )
        await use_case.start_capability_enable(
            user_id=user_id,
            connection_id=connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )
        state = adapter.authorization_states[-1]

        # state 消费本身也同步 User；先进入无事务的网络窗口，再与关闭结果保存制造交错。
        callback_task = asyncio.create_task(use_case.callback(code="synthetic-code", state=state))
        await asyncio.wait_for(fetch_started.wait(), timeout=5)

        original_get_enabled = SqlAlchemyConnectionStore.get_enabled_capabilities

        async def blocked_get_enabled(
            store: SqlAlchemyConnectionStore,
            *,
            user_id: UUID,
            connection_id: UUID,
        ) -> frozenset[ConnectionCapability] | None:
            """在连接行锁取得后暂停，保持 callback 必须等待的真实事务屏障。"""
            enabled = await original_get_enabled(
                store,
                user_id=user_id,
                connection_id=connection_id,
            )
            enabled_read_observed.set()
            await release_enabled_read.wait()
            return enabled

        # 只拦截 disable 的快照读取；callback 保存路径不会读取 enabled 集合。
        monkeypatch.setattr(
            SqlAlchemyConnectionStore, "get_enabled_capabilities", blocked_get_enabled
        )
        disable_task = asyncio.create_task(
            ConnectionsUseCase(
                store_factory,
                cipher,
                {"microsoft": adapter},
                FixedClock(),
            ).disable_capability(
                user_id=user_id,
                connection_id=connection_id,
                capability=ConnectionCapability.MAIL_READ,
            )
        )
        await asyncio.wait_for(enabled_read_observed.wait(), timeout=5)

        release_fetch.set()

        # 已在途的回调结果不能越过同事务用户/连接屏障，必须等待 disable 提交再检查代际。
        callback_completed_before_disable_release = False
        try:
            await asyncio.wait_for(asyncio.shield(callback_task), timeout=2)
            callback_completed_before_disable_release = True
        except TimeoutError:
            pass

        release_enabled_read.set()
        disable_result = await disable_task
        assert disable_result.status is CapabilityStatus.DISABLED

        if callback_completed_before_disable_release:
            pytest.fail(
                "progressive callback committed before disable released its user and connection locks"
            )
        with pytest.raises(StateConflictError) as raised:
            await callback_task
        assert raised.value.error_code == "oauth_attempt_invalidated"

        async with session_factory() as session:
            statuses = {
                ConnectionCapability(row.capability): row.status
                for row in (
                    await session.scalars(
                        select(ConnectionCapabilityModel).where(
                            ConnectionCapabilityModel.connection_id == connection_id
                        )
                    )
                )
            }
        assert statuses[ConnectionCapability.MAIL_READ] == CapabilityStatus.DISABLED.value
        assert not (
            statuses[ConnectionCapability.MAIL_SEND] == CapabilityStatus.ENABLED.value
            and statuses[ConnectionCapability.MAIL_READ] != CapabilityStatus.ENABLED.value
        )
    finally:
        release_fetch.set()
        release_enabled_read.set()
        pending_tasks = tuple(
            task for task in (disable_task, callback_task) if task is not None and not task.done()
        )
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation", ("disable", "disconnect", "new_authorization"))
async def test_progressive_callback_rejects_stale_target_generation(
    database_url: str,
    invalidation: str,
) -> None:
    """关闭、断开或更新授权代际后，旧 state 均不能写 token 或恢复能力。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"g" * 32)
    try:
        user_id, connection_id = await _seed_progressive_connection(
            session_factory,
            email=f"stale-{invalidation}@example.test",
            account_key=f"tenant-a:stale-{invalidation}",
        )
        adapter = ProgressiveOAuthAdapter(
            account=OAuthAccount(
                provider_account_id=f"tenant-a:stale-{invalidation}",
                account_email=f"stale-{invalidation}@example.test",
                provider_tenant_id="tenant-a",
                account_type="work_school",
            ),
            token=OAuthTokenSet(
                access_token="stale-access",
                refresh_token="stale-refresh",
                expires_in=3600,
                granted_scopes=frozenset({"scope:mail.read", "scope:mail.send"}),
            ),
        )
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(session_factory),
            cipher,
            {"microsoft": adapter},
            FixedClock(),
        )
        await use_case.start_capability_enable(
            user_id=user_id,
            connection_id=connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )
        stale_state = adapter.authorization_states[-1]

        if invalidation == "disable":
            await use_case.disable_capability(
                user_id=user_id,
                connection_id=connection_id,
                capability=ConnectionCapability.MAIL_READ,
            )
        elif invalidation == "disconnect":
            await use_case.disconnect(user_id=user_id, connection_id=connection_id)
        else:
            await use_case.start_capability_enable(
                user_id=user_id,
                connection_id=connection_id,
                capability=ConnectionCapability.MAIL_SEND,
            )
            assert adapter.authorization_states[-1] != stale_state

        with pytest.raises(StateConflictError) as raised:
            await use_case.callback(code="synthetic-code", state=stale_state)
        assert raised.value.error_code == "oauth_attempt_invalidated"

        async with session_factory() as session:
            connection = await session.get(OAuthConnectionModel, connection_id)
            credentials = tuple(
                (
                    await session.scalars(
                        select(EncryptedCredentialModel).where(
                            EncryptedCredentialModel.connection_id == connection_id
                        )
                    )
                ).all()
            )
            mail_read_status = await session.scalar(
                select(ConnectionCapabilityModel.status).where(
                    ConnectionCapabilityModel.connection_id == connection_id,
                    ConnectionCapabilityModel.capability == ConnectionCapability.MAIL_READ.value,
                )
            )

        assert connection is not None
        assert credentials == ()
        if invalidation == "disconnect":
            assert connection.status == "disconnected"
        elif invalidation == "disable":
            assert mail_read_status == "disabled"
        else:
            assert mail_read_status == "authorizing"
    finally:
        await session_factory.dispose()
