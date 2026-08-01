"""实现不依赖 FastAPI、SQLAlchemy 或具体安全 SDK 的认证应用用例。"""

import asyncio
import secrets
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from uuid import UUID

from ai_employee.domain.identity import (
    MAX_SESSION_TTL_SECONDS,
    MIN_SESSION_TTL_SECONDS,
    AuthenticatedSession,
    ListedSession,
    NewAdmin,
    SessionAuthenticationRecord,
    SessionRecord,
    SessionSummary,
    UserCredential,
    UserIdentity,
    normalize_email,
)

LAST_SEEN_UPDATE_INTERVAL = timedelta(minutes=5)


class InvalidCredentialsError(Exception):
    """登录邮箱、密码或账户状态不能通过统一凭据校验。"""


class AuthenticationRequiredError(Exception):
    """请求未携带可用的活动会话。"""


class CsrfRejectedError(Exception):
    """修改类请求的 CSRF Cookie、Header 或持久摘要不匹配。"""


class SessionNotFoundError(Exception):
    """目标不是当前用户拥有的活动会话；错误不区分不存在或跨用户。"""


class AdminAlreadyExistsError(Exception):
    """数据库中已经存在管理员，且当前调用不满足同邮箱幂等条件。"""


class InvalidAdminEmailError(Exception):
    """管理员邮箱规范化后为空或缺少基本邮箱分隔符。"""


class EmptyAdminPasswordError(Exception):
    """管理员创建请求没有提供非空密码。"""


class Clock(Protocol):
    """提供可替换的显式 UTC 当前时间。"""

    def now(self) -> datetime:
        """返回当前 UTC 时间。"""


class PasswordVerifier(Protocol):
    """抽象密码验证能力，避免应用层依赖 Argon2 SDK。"""

    @property
    def fallback_password_hash(self) -> str:
        """返回用于不可用身份路径的有效密码哈希。"""

    def verify(self, password_hash: str, password: str) -> bool:
        """验证候选密码是否匹配编码哈希。"""


class PasswordHashFactory(Protocol):
    """抽象管理员密码哈希能力，避免应用层依赖 Argon2 SDK。"""

    def hash(self, password: str) -> str:
        """为明文密码生成带独立盐值的编码哈希。"""


class TokenFactory(Protocol):
    """生成只在 HTTP 边界短暂存在的高熵原始令牌。"""

    def __call__(self) -> str:
        """返回新的 URL-safe 原始令牌。"""


class TokenHasher(Protocol):
    """将原始认证令牌转换为固定长度持久摘要。"""

    def __call__(self, token: str) -> bytes:
        """返回令牌的不可逆摘要。"""


class IdentityRepository(Protocol):
    """定义认证用例所需且强制用户隔离的最小持久化端口。"""

    async def get_active_user_by_email(self, email: str) -> UserCredential | None:
        """按规范化邮箱读取活动用户凭据。"""

    async def lock_active_user_for_session_creation(
        self,
        *,
        user_id: UUID,
        email: str,
        password_hash: str,
    ) -> UserCredential | None:
        """锁定并复核用于创建会话的活动用户及已验证密码哈希。"""

    async def create_session(
        self,
        *,
        user_id: UUID,
        token_hash: bytes,
        csrf_hash: bytes,
        created_at: datetime,
        expires_at: datetime,
    ) -> SessionRecord:
        """创建只保存令牌摘要的新会话。"""

    async def get_session_authentication(
        self, token_hash: bytes
    ) -> SessionAuthenticationRecord | None:
        """按会话摘要读取联合身份记录。"""

    async def update_last_seen_if_due(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        last_seen_at: datetime,
        due_before_or_at: datetime,
    ) -> bool:
        """在用户隔离和时间阈值条件下原子更新最近访问时间。"""

    async def list_active_sessions(
        self, *, user_id: UUID, now: datetime
    ) -> tuple[SessionSummary, ...]:
        """列出指定用户当前仍活动的会话。"""

    async def revoke_active_session(
        self, *, user_id: UUID, session_id: UUID, revoked_at: datetime
    ) -> bool:
        """仅撤销指定用户拥有的活动会话。"""

    async def get_admin_for_creation(self) -> UserCredential | None:
        """串行化单管理员创建并返回现有管理员（若存在）。"""

    async def create_admin(self, admin: NewAdmin) -> UserIdentity:
        """创建唯一活动管理员并返回公开身份。"""


class IdentityRepositoryFactory(Protocol):
    """为一个用例调用提供提交/回滚受控的 Repository 事务上下文。"""

    def __call__(self) -> AbstractAsyncContextManager[IdentityRepository]:
        """创建在退出时提交成功写入、异常时回滚的事务上下文。"""


@dataclass(frozen=True, slots=True)
class LoginResult:
    """登录成功后返回公开用户与仅供 Cookie 写入的原始令牌。"""

    user: UserIdentity
    session: SessionRecord
    raw_session_token: str
    raw_csrf_token: str


@dataclass(frozen=True, slots=True)
class CreateAdminResult:
    """报告管理员创建是否写入新记录，不暴露密码哈希。"""

    user: UserIdentity
    created: bool


def _utc_now(clock: Clock) -> datetime:
    """读取并验证时钟返回显式 UTC，阻断宿主机本地时区渗透。"""
    now = clock.now()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("authentication clock must return an explicit UTC datetime")
    return now.astimezone(UTC)


class LoginUseCase:
    """验证活动管理员凭据并原子创建安全 Cookie 会话。"""

    def __init__(
        self,
        repositories: IdentityRepositoryFactory,
        password_verifier: PasswordVerifier,
        token_factory: TokenFactory,
        token_hasher: TokenHasher,
        clock: Clock,
        session_ttl_seconds: int,
    ) -> None:
        """注入认证事务、安全原语、可控时钟与会话 TTL。

        Raises:
            ValueError: TTL 小于一秒或超过批准的一年安全上限。
        """
        if not MIN_SESSION_TTL_SECONDS <= session_ttl_seconds <= MAX_SESSION_TTL_SECONDS:
            raise ValueError(
                "session_ttl_seconds must be between "
                f"{MIN_SESSION_TTL_SECONDS} and {MAX_SESSION_TTL_SECONDS}"
            )
        self._repositories = repositories
        self._password_verifier = password_verifier
        self._token_factory = token_factory
        self._token_hasher = token_hasher
        self._clock = clock
        self._session_ttl = timedelta(seconds=session_ttl_seconds)

    async def execute(self, *, email: str, password: str) -> LoginResult:
        """校验登录凭据并只持久化会话和 CSRF 摘要。

        Args:
            email: 登录请求提供的管理员邮箱。
            password: 登录请求提供的候选明文密码。

        Returns:
            公开用户资料、会话元数据以及两个仅供 Cookie 响应使用的原始令牌。

        Raises:
            InvalidCredentialsError: 用户不存在、已停用、无密码哈希或密码不匹配。
        """
        normalized_email = normalize_email(email)
        async with self._repositories() as repository:
            user = await repository.get_active_user_by_email(normalized_email)
        if user is None or not user.is_active or user.password_hash is None:
            authenticatable_user = None
            password_hash = self._password_verifier.fallback_password_hash
        else:
            authenticatable_user = user
            password_hash = user.password_hash
        # 先释放短读事务，再执行 CPU 密集的 Argon2id；不可用身份仍使用有效 fallback 哈希，
        # 既避免占用数据库连接，也保持未知邮箱与错误密码的主要计算工作量一致。
        password_matches = await asyncio.to_thread(
            self._password_verifier.verify,
            password_hash,
            password,
        )
        if authenticatable_user is None or not password_matches:
            raise InvalidCredentialsError

        raw_session_token = self._token_factory()
        raw_csrf_token = self._token_factory()
        token_hash = self._token_hasher(raw_session_token)
        csrf_hash = self._token_hasher(raw_csrf_token)
        now = _utc_now(self._clock)
        async with self._repositories() as repository:
            # Argon2id 期间可能发生停用、改密或管理员替换；写事务必须锁定并重新确认
            # 同一身份与同一密码哈希，不能用读事务留下的陈旧快照直接创建会话。
            locked_user = await repository.lock_active_user_for_session_creation(
                user_id=authenticatable_user.identity.id,
                email=normalized_email,
                password_hash=password_hash,
            )
            if locked_user is None:
                raise InvalidCredentialsError
            session = await repository.create_session(
                user_id=locked_user.identity.id,
                token_hash=token_hash,
                csrf_hash=csrf_hash,
                created_at=now,
                expires_at=now + self._session_ttl,
            )
        return LoginResult(
            user=locked_user.identity,
            session=session,
            raw_session_token=raw_session_token,
            raw_csrf_token=raw_csrf_token,
        )


class AuthenticateSessionUseCase:
    """验证会话 Cookie，并按五分钟窗口节流最近访问写入。"""

    def __init__(
        self,
        repositories: IdentityRepositoryFactory,
        token_hasher: TokenHasher,
        clock: Clock,
    ) -> None:
        """注入会话事务、摘要算法和确定性时钟。"""
        self._repositories = repositories
        self._token_hasher = token_hasher
        self._clock = clock

    async def execute(self, raw_session_token: str | None) -> AuthenticatedSession:
        """把原始会话 Cookie 解析为活动且用户仍启用的认证上下文。

        Args:
            raw_session_token: 请求 Cookie 中的原始会话令牌；可能缺失。

        Returns:
            不含密码哈希和原始令牌的认证上下文。

        Raises:
            AuthenticationRequiredError: Cookie 缺失、摘要未知、用户停用、会话过期或撤销。
        """
        if not raw_session_token:
            raise AuthenticationRequiredError
        now = _utc_now(self._clock)
        async with self._repositories() as repository:
            authentication = await repository.get_session_authentication(
                self._token_hasher(raw_session_token)
            )
            if (
                authentication is None
                or not authentication.user.is_active
                or not authentication.session.is_active_at(now)
            ):
                raise AuthenticationRequiredError

            session = authentication.session
            if now - session.last_seen_at >= LAST_SEEN_UPDATE_INTERVAL:
                updated = await repository.update_last_seen_if_due(
                    user_id=authentication.user.identity.id,
                    session_id=session.id,
                    last_seen_at=now,
                    due_before_or_at=now - LAST_SEEN_UPDATE_INTERVAL,
                )
                if updated:
                    session = replace(session, last_seen_at=now)
        return AuthenticatedSession(user=authentication.user.identity, session=session)


class ValidateCsrfUseCase:
    """以常量时间比较验证 CSRF Cookie、Header 与数据库摘要三方一致。"""

    def __init__(self, token_hasher: TokenHasher) -> None:
        """注入与会话创建相同的摘要算法。"""
        self._token_hasher = token_hasher

    def execute(
        self,
        authenticated: AuthenticatedSession,
        *,
        cookie_token: str | None,
        header_token: str | None,
    ) -> None:
        """校验修改类请求携带的双提交 CSRF 令牌。

        Args:
            authenticated: 已通过会话 Cookie 认证的请求上下文。
            cookie_token: 前端可读 CSRF Cookie 原始值。
            header_token: ``X-CSRF-Token`` Header 原始值。

        Raises:
            CsrfRejectedError: 任一值缺失、Cookie/Header 不同或持久摘要不匹配。
        """
        if cookie_token is None or header_token is None:
            raise CsrfRejectedError
        # 两次比较都使用常量时间函数：先阻断双提交值篡改，再证明其对应数据库事实。
        if not secrets.compare_digest(cookie_token.encode(), header_token.encode()):
            raise CsrfRejectedError
        provided_hash = self._token_hasher(header_token)
        if not secrets.compare_digest(provided_hash, authenticated.session.csrf_hash):
            raise CsrfRejectedError


class LogoutUseCase:
    """撤销当前认证会话，使原始 Cookie 即刻失效。"""

    def __init__(self, repositories: IdentityRepositoryFactory, clock: Clock) -> None:
        """注入用户隔离事务与撤销时间来源。"""
        self._repositories = repositories
        self._clock = clock

    async def execute(self, authenticated: AuthenticatedSession) -> None:
        """在当前用户条件下撤销当前会话。"""
        async with self._repositories() as repository:
            await repository.revoke_active_session(
                user_id=authenticated.user.id,
                session_id=authenticated.session.id,
                revoked_at=_utc_now(self._clock),
            )


class ListSessionsUseCase:
    """列出当前用户活动会话并标识当前请求会话。"""

    def __init__(self, repositories: IdentityRepositoryFactory, clock: Clock) -> None:
        """注入用户隔离事务与活动状态判断时间。"""
        self._repositories = repositories
        self._clock = clock

    async def execute(self, authenticated: AuthenticatedSession) -> tuple[ListedSession, ...]:
        """返回不含任何 Token 摘要的当前用户会话摘要。"""
        async with self._repositories() as repository:
            sessions = await repository.list_active_sessions(
                user_id=authenticated.user.id,
                now=_utc_now(self._clock),
            )
        return tuple(
            ListedSession(
                id=session.id,
                created_at=session.created_at,
                expires_at=session.expires_at,
                last_seen_at=session.last_seen_at,
                is_current=session.id == authenticated.session.id,
            )
            for session in sessions
        )


class RevokeSessionUseCase:
    """撤销当前用户拥有的指定活动会话并隐藏跨用户存在性。"""

    def __init__(self, repositories: IdentityRepositoryFactory, clock: Clock) -> None:
        """注入用户隔离事务与撤销时间来源。"""
        self._repositories = repositories
        self._clock = clock

    async def execute(self, authenticated: AuthenticatedSession, session_id: UUID) -> bool:
        """撤销目标会话并报告目标是否为当前会话。

        Args:
            authenticated: 当前请求的用户和会话上下文。
            session_id: 用户选择撤销的会话 ID。

        Returns:
            成功撤销且目标为当前会话时返回 ``True``。

        Raises:
            SessionNotFoundError: 目标不存在、已失效或不属于当前用户。
        """
        async with self._repositories() as repository:
            revoked = await repository.revoke_active_session(
                user_id=authenticated.user.id,
                session_id=session_id,
                revoked_at=_utc_now(self._clock),
            )
        if not revoked:
            # 不区分不存在与跨用户，避免通过响应探测其他用户的会话 ID。
            raise SessionNotFoundError
        return session_id == authenticated.session.id


class CreateAdminUseCase:
    """以数据库串行化边界创建唯一管理员或执行受限幂等 no-op。"""

    def __init__(
        self,
        repositories: IdentityRepositoryFactory,
        password_hasher: PasswordHashFactory,
    ) -> None:
        """注入单管理员事务与 Argon2id 哈希端口。"""
        self._repositories = repositories
        self._password_hasher = password_hasher

    async def execute(
        self,
        *,
        email: str,
        password: str,
        timezone: str,
        brief_time: str,
        if_absent: bool,
    ) -> CreateAdminResult:
        """创建管理员；仅同一规范化邮箱允许 ``--if-absent`` 成功 no-op。

        Args:
            email: CLI 提供的管理员邮箱。
            password: 从显式文件读取且仅在当前调用内存在的明文密码。
            timezone: 配置中的默认 IANA 时区。
            brief_time: 配置中的默认 ``HH:MM`` 字符串。
            if_absent: 已存在同邮箱管理员时是否允许成功且不修改任何字段。

        Returns:
            公开管理员身份和是否新建的标记。

        Raises:
            InvalidAdminEmailError: 邮箱规范化后不具备基本地址结构。
            EmptyAdminPasswordError: 密码为空或全为空白。
            AdminAlreadyExistsError: 已有管理员且不满足同邮箱幂等条件。
            ValueError: 默认简报时间不是 ``HH:MM``。
        """
        normalized_email = normalize_email(email)
        if not normalized_email or "@" not in normalized_email:
            raise InvalidAdminEmailError
        if not password or password.isspace():
            raise EmptyAdminPasswordError
        parsed_brief_time = time.fromisoformat(brief_time)

        async with self._repositories() as repository:
            existing = await repository.get_admin_for_creation()
            if existing is not None:
                same_email = normalize_email(existing.identity.email) == normalized_email
                if if_absent and same_email:
                    return CreateAdminResult(user=existing.identity, created=False)
                raise AdminAlreadyExistsError

            # 先取得跨进程创建锁，再执行 CPU 密集哈希；这样并发 CLI 不能创建第二个邮箱。
            password_hash = await asyncio.to_thread(self._password_hasher.hash, password)
            user = await repository.create_admin(
                NewAdmin(
                    email=normalized_email,
                    display_name="Administrator",
                    password_hash=password_hash,
                    timezone=timezone,
                    locale="zh-CN",
                    brief_time=parsed_brief_time,
                )
            )
        return CreateAdminResult(user=user, created=True)
