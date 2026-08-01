"""验证认证应用用例在并发与可替换端口下保持确定性。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest

from ai_employee.application.use_cases.auth import (
    AuthenticateSessionUseCase,
    IdentityRepository,
    InvalidCredentialsError,
    LoginUseCase,
)
from ai_employee.domain.identity import (
    NewAdmin,
    SessionAuthenticationRecord,
    SessionRecord,
    SessionSummary,
    UserCredential,
    UserIdentity,
)
from ai_employee.infrastructure.security.tokens import hash_token

FALLBACK_PASSWORD_HASH = "synthetic-fallback-argon2id-hash"
CANDIDATE_PASSWORD = "synthetic-candidate-password"


@dataclass(slots=True)
class FixedClock:
    """返回固定 UTC 时刻的认证测试时钟。"""

    current: datetime

    def now(self) -> datetime:
        """返回不依赖宿主机时间的当前值。"""
        return self.current


@dataclass(slots=True)
class LoginRepositoryState:
    """保存登录测试要返回的凭据以及禁止发生的会话创建次数。"""

    user: UserCredential | None
    create_session_calls: int = 0


class LoginIdentityRepository:
    """为登录用例提供确定性凭据，并拒绝无关 Repository 操作。"""

    def __init__(self, state: LoginRepositoryState) -> None:
        """绑定当前测试场景的身份状态。"""
        self._state = state

    async def get_active_user_by_email(self, email: str) -> UserCredential | None:
        """返回场景指定凭据，并验证邮箱已在应用层规范化。"""
        assert email == "owner@example.com"
        return self._state.user

    async def create_session(
        self,
        *,
        user_id: UUID,
        token_hash: bytes,
        csrf_hash: bytes,
        created_at: datetime,
        expires_at: datetime,
    ) -> SessionRecord:
        """记录并拒绝无效凭据场景不应发生的会话创建。"""
        self._state.create_session_calls += 1
        raise AssertionError("unexpected create_session call")

    async def get_session_authentication(
        self, token_hash: bytes
    ) -> SessionAuthenticationRecord | None:
        """拒绝登录测试不应触发的会话认证读取。"""
        raise AssertionError("unexpected get_session_authentication call")

    async def update_last_seen_if_due(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        last_seen_at: datetime,
        due_before_or_at: datetime,
    ) -> bool:
        """拒绝登录测试不应触发的最近访问更新时间节流。"""
        raise AssertionError("unexpected update_last_seen_if_due call")

    async def list_active_sessions(
        self, *, user_id: UUID, now: datetime
    ) -> tuple[SessionSummary, ...]:
        """拒绝登录测试不应触发的活动会话查询。"""
        raise AssertionError("unexpected list_active_sessions call")

    async def revoke_active_session(
        self, *, user_id: UUID, session_id: UUID, revoked_at: datetime
    ) -> bool:
        """拒绝登录测试不应触发的会话撤销。"""
        raise AssertionError("unexpected revoke_active_session call")

    async def get_admin_for_creation(self) -> UserCredential | None:
        """拒绝登录测试不应触发的管理员创建锁定查询。"""
        raise AssertionError("unexpected get_admin_for_creation call")

    async def create_admin(self, admin: NewAdmin) -> UserIdentity:
        """拒绝登录测试不应触发的管理员创建。"""
        raise AssertionError("unexpected create_admin call")


class LoginRepositoryFactory:
    """为一次登录调用提供同一个确定性 Repository 事务上下文。"""

    def __init__(self, repository: LoginIdentityRepository) -> None:
        """保存登录测试 Repository。"""
        self._repository = repository

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[IdentityRepository]:
        """返回不执行外部 I/O 的登录测试事务上下文。"""
        yield self._repository


@dataclass(slots=True)
class RecordingPasswordVerifier:
    """记录密码验证参数，并可模拟持久哈希损坏异常。"""

    fallback_password_hash: str
    failure: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def verify(self, password_hash: str, password: str) -> bool:
        """记录一次等价验证工作，必要时抛出场景指定异常。"""
        self.calls.append((password_hash, password))
        if self.failure is not None:
            raise self.failure
        return False


def _login_credential(
    *, is_active: bool = True, password_hash: str | None = "synthetic-stored-password-hash"
) -> UserCredential:
    """构造不含真实个人数据的登录测试凭据。"""
    return UserCredential(
        identity=UserIdentity(
            id=uuid4(),
            email="owner@example.com",
            display_name="Synthetic Owner",
            timezone="UTC",
            locale="zh-CN",
            brief_time=time(8, 0),
        ),
        password_hash=password_hash,
        is_active=is_active,
    )


@pytest.mark.parametrize(
    "user",
    [
        pytest.param(None, id="unknown-user"),
        pytest.param(_login_credential(is_active=False), id="inactive-user"),
        pytest.param(_login_credential(password_hash=None), id="missing-password-hash"),
    ],
)
@pytest.mark.asyncio
async def test_unavailable_login_identity_still_performs_fallback_password_verification(
    user: UserCredential | None,
) -> None:
    """未知、停用或无哈希身份都必须执行一次等价密码验证后统一拒绝。"""
    state = LoginRepositoryState(user=user)
    verifier = RecordingPasswordVerifier(FALLBACK_PASSWORD_HASH)
    use_case = LoginUseCase(
        LoginRepositoryFactory(LoginIdentityRepository(state)),
        verifier,
        lambda: "unused-token",
        hash_token,
        FixedClock(datetime(2030, 1, 1, 8, 0, tzinfo=UTC)),
        3600,
    )

    with pytest.raises(InvalidCredentialsError):
        await use_case.execute(
            email="  OWNER@EXAMPLE.COM  ",
            password=CANDIDATE_PASSWORD,
        )

    assert verifier.calls == [(FALLBACK_PASSWORD_HASH, CANDIDATE_PASSWORD)]
    assert state.create_session_calls == 0


@pytest.mark.asyncio
async def test_active_login_preserves_damaged_password_hash_exception() -> None:
    """活动身份的持久哈希损坏必须继续上抛，不能伪装为普通凭据失败。"""
    damaged_hash = "synthetic-damaged-password-hash"
    state = LoginRepositoryState(user=_login_credential(password_hash=damaged_hash))
    verifier_error = RuntimeError("stored password hash is damaged")
    verifier = RecordingPasswordVerifier(FALLBACK_PASSWORD_HASH, failure=verifier_error)
    use_case = LoginUseCase(
        LoginRepositoryFactory(LoginIdentityRepository(state)),
        verifier,
        lambda: "unused-token",
        hash_token,
        FixedClock(datetime(2030, 1, 1, 8, 0, tzinfo=UTC)),
        3600,
    )

    with pytest.raises(RuntimeError, match="stored password hash is damaged"):
        await use_case.execute(email="owner@example.com", password=CANDIDATE_PASSWORD)

    assert verifier.calls == [(damaged_hash, CANDIDATE_PASSWORD)]
    assert state.create_session_calls == 0


@dataclass(slots=True)
class ConcurrentSessionState:
    """保存两个并发认证调用共享的会话与写入计数。"""

    authentication: SessionAuthenticationRecord
    update_count: int
    read_count: int
    both_read: asyncio.Event
    update_lock: asyncio.Lock


class ConcurrentIdentityRepository:
    """强制两个调用先读取同一旧快照，再观察更新方法是否具备原子节流。"""

    def __init__(self, state: ConcurrentSessionState) -> None:
        """绑定共享状态，使两个事务可重现竞争窗口。"""
        self._state = state

    async def get_active_user_by_email(self, email: str) -> UserCredential | None:
        """拒绝并发认证测试不应触发的邮箱凭据查询。"""
        raise AssertionError("unexpected get_active_user_by_email call")

    async def create_session(
        self,
        *,
        user_id: UUID,
        token_hash: bytes,
        csrf_hash: bytes,
        created_at: datetime,
        expires_at: datetime,
    ) -> SessionRecord:
        """拒绝并发认证测试不应触发的会话创建。"""
        raise AssertionError("unexpected create_session call")

    async def get_session_authentication(
        self, token_hash: bytes
    ) -> SessionAuthenticationRecord | None:
        """等待两个调用都取得旧快照后再允许任一调用更新。"""
        assert token_hash == hash_token("runtime-generated-token")
        snapshot = self._state.authentication
        self._state.read_count += 1
        if self._state.read_count == 2:
            self._state.both_read.set()
        await self._state.both_read.wait()
        return snapshot

    async def update_last_seen(
        self, *, user_id: UUID, session_id: UUID, last_seen_at: datetime
    ) -> None:
        """模拟旧的读后无条件写入，以证明并发时会产生两次更新。"""
        async with self._state.update_lock:
            assert user_id == self._state.authentication.user.identity.id
            assert session_id == self._state.authentication.session.id
            self._state.update_count += 1
            self._state.authentication = replace(
                self._state.authentication,
                session=replace(
                    self._state.authentication.session,
                    last_seen_at=last_seen_at,
                ),
            )

    async def update_last_seen_if_due(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        last_seen_at: datetime,
        due_before_or_at: datetime,
    ) -> bool:
        """模拟数据库条件 UPDATE；只有首个到达的调用能跨过时间阈值。"""
        async with self._state.update_lock:
            assert user_id == self._state.authentication.user.identity.id
            assert session_id == self._state.authentication.session.id
            if self._state.authentication.session.last_seen_at > due_before_or_at:
                return False
            self._state.update_count += 1
            self._state.authentication = replace(
                self._state.authentication,
                session=replace(
                    self._state.authentication.session,
                    last_seen_at=last_seen_at,
                ),
            )
            return True

    async def list_active_sessions(
        self, *, user_id: UUID, now: datetime
    ) -> tuple[SessionSummary, ...]:
        """拒绝并发认证测试不应触发的活动会话查询。"""
        raise AssertionError("unexpected list_active_sessions call")

    async def revoke_active_session(
        self, *, user_id: UUID, session_id: UUID, revoked_at: datetime
    ) -> bool:
        """拒绝并发认证测试不应触发的会话撤销。"""
        raise AssertionError("unexpected revoke_active_session call")

    async def get_admin_for_creation(self) -> UserCredential | None:
        """拒绝并发认证测试不应触发的管理员创建锁定查询。"""
        raise AssertionError("unexpected get_admin_for_creation call")

    async def create_admin(self, admin: NewAdmin) -> UserIdentity:
        """拒绝并发认证测试不应触发的管理员创建。"""
        raise AssertionError("unexpected create_admin call")


class ConcurrentRepositoryFactory:
    """为每个并发调用提供共享状态上的窄事务上下文。"""

    def __init__(self, repository: ConcurrentIdentityRepository) -> None:
        """保存测试 Repository。"""
        self._repository = repository

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[IdentityRepository]:
        """返回不执行外部 I/O 的测试事务上下文。"""
        yield self._repository


@pytest.mark.asyncio
async def test_concurrent_authentication_updates_last_seen_only_once_per_window() -> None:
    """两个同时越过五分钟阈值的请求也只能产生一次持久化更新。"""
    user_id = uuid4()
    now = datetime(2030, 1, 1, 8, 5, tzinfo=UTC)
    state = ConcurrentSessionState(
        authentication=SessionAuthenticationRecord(
            user=UserCredential(
                identity=UserIdentity(
                    id=user_id,
                    email="owner@example.com",
                    display_name="Synthetic Owner",
                    timezone="UTC",
                    locale="zh-CN",
                    brief_time=time(8, 0),
                ),
                password_hash=None,
                is_active=True,
            ),
            session=SessionRecord(
                id=uuid4(),
                user_id=user_id,
                csrf_hash=b"c" * 32,
                created_at=now - timedelta(hours=1),
                expires_at=now + timedelta(hours=1),
                last_seen_at=now - timedelta(minutes=5),
                revoked_at=None,
            ),
        ),
        update_count=0,
        read_count=0,
        both_read=asyncio.Event(),
        update_lock=asyncio.Lock(),
    )
    use_case = AuthenticateSessionUseCase(
        ConcurrentRepositoryFactory(ConcurrentIdentityRepository(state)),
        hash_token,
        FixedClock(now),
    )

    await asyncio.gather(
        use_case.execute("runtime-generated-token"),
        use_case.execute("runtime-generated-token"),
    )

    assert state.update_count == 1
