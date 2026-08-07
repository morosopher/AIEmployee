"""实现 Google OAuth 连接、断开与手动同步的应用用例。"""

import base64
import hashlib
import secrets
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import httpx
from cryptography.exceptions import InvalidTag

from ai_employee.application.use_cases.tasks import CreateTaskBatchItem
from ai_employee.infrastructure.db.repositories.connections import ConnectionStore
from ai_employee.infrastructure.security.encryption import AeadCipher, EncryptedValue
from ai_employee.integrations.google.oauth import (
    GOOGLE_SCOPES,
    GoogleAccount,
    GoogleTokenResponse,
    build_authorization_url,
)

OAUTH_ATTEMPT_TTL = timedelta(minutes=10)


class Clock(Protocol):
    """定义连接流程所需的显式 UTC 时钟，测试可注入可控实现。"""

    def now(self) -> datetime: ...


class GoogleOAuthPort(Protocol):
    """定义连接用例所需 OAuth 行为，使测试模式可注入绝不联网的 fake。"""

    async def exchange_code(self, code: str, verifier: str) -> GoogleTokenResponse: ...
    async def fetch_account(self, access_token: str) -> GoogleAccount: ...
    async def refresh_token(self, refresh_token: str) -> GoogleTokenResponse: ...
    async def revoke(self, token: str) -> None: ...


class ConnectionStoreFactory(Protocol):
    """为一次连接用例提供事务边界，不让应用层直接依赖 ORM。"""

    def __call__(self) -> AbstractAsyncContextManager[ConnectionStore]: ...


class TaskCreator(Protocol):
    """定义手动同步创建两个可恢复任务的最小端口。"""

    async def execute_many(
        self,
        *,
        user_id: UUID,
        items: tuple[CreateTaskBatchItem, ...],
    ) -> tuple["CreatedTask", ...]: ...


class CreatedTask(Protocol):
    """定义同步接口需要读取的已创建任务标识。"""

    @property
    def task_id(self) -> UUID:
        """返回任务创建后可安全公开的稳定 UUID。"""


@dataclass(frozen=True, slots=True)
class ConnectionSummary:
    """API 可以公开的连接元数据，刻意排除 token、nonce 和供应商错误细节。"""

    id: UUID
    provider: str
    account_email: str
    scopes: tuple[str, ...]
    status: str
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class OAuthStartResult:
    """返回浏览器重定向用 URL，不把 state 写入 JSON 响应以外的持久化边界。"""

    authorization_url: str


@dataclass(frozen=True, slots=True)
class ManualSyncResult:
    """包含 Gmail 与 Calendar 各一个稳定任务标识的异步接受结果。"""

    gmail_task_id: UUID
    calendar_task_id: UUID


class OAuthStateRejectedError(Exception):
    """表示 state 不存在、已消费或已过期，统一映射为不泄露细节的 400。"""


class ConnectionNotFoundError(Exception):
    """表示指定连接不存在或不属于当前认证用户。"""


def _utc_now(clock: Clock) -> datetime:
    """读取并验证时钟返回显式 UTC，阻止本地时区进入 OAuth 过期判断。"""
    now = clock.now()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("connection clock must return an explicit UTC datetime")
    return now.astimezone(UTC)


def _b64url(value: bytes) -> str:
    """按 PKCE 规范输出无填充 Base64URL 文本。"""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class GoogleConnectionsUseCase:
    """协调记录绑定 AEAD、数据库事务和可替换 Google OAuth 客户端。"""

    def __init__(
        self,
        stores: ConnectionStoreFactory,
        cipher: AeadCipher,
        oauth: GoogleOAuthPort,
        clock: Clock,
        client_id: str,
        redirect_uri: str,
    ) -> None:
        """注入事务、加密、供应商和运行时配置；secret 仅保留在 OAuth 适配器中。"""
        self._stores = stores
        self._cipher = cipher
        self._oauth = oauth
        self._clock = clock
        self._client_id = client_id
        self._redirect_uri = redirect_uri

    async def start(self, *, user_id: UUID) -> OAuthStartResult:
        """生成 32 字节 state 与 64 字节 verifier，持久化加密 verifier 并返回授权 URL。"""
        now = _utc_now(self._clock)
        state = _b64url(secrets.token_bytes(32))
        verifier = _b64url(secrets.token_bytes(64))
        state_hash = hashlib.sha256(state.encode("ascii")).digest()
        # 预生成 attempt UUID 是 AAD 绑定所必需的，避免把 verifier 加密到无归属的临时 AAD。
        attempt_id = uuid4()
        verifier_value = self._cipher.encrypt(
            verifier.encode("ascii"), self._aad(user_id, attempt_id, "pkce_verifier")
        )
        async with self._stores() as store:
            await store.create_attempt(
                attempt_id=attempt_id,
                user_id=user_id,
                state_hash=state_hash,
                verifier=verifier_value,
                expires_at=now + OAUTH_ATTEMPT_TTL,
                created_at=now,
            )
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        return OAuthStartResult(
            build_authorization_url(
                client_id=self._client_id,
                redirect_uri=self._redirect_uri,
                state=state,
                code_challenge=challenge,
            )
        )

    @staticmethod
    def _aad(user_id: UUID, record_id: UUID, secret_kind: str) -> bytes:
        """生成固定 ``user_id:record_id:secret_kind`` AAD，禁止跨记录替换密文。"""
        return f"{user_id}:{record_id}:{secret_kind}".encode("ascii")

    async def callback(self, *, code: str, state: str) -> UUID:
        """原子消费 state，交换授权码并保存加密 Token 与初始同步游标。

        state 在供应商 I/O 前已消费，避免慢速或失败回调被攻击者重放；失败后用户必须重新
        发起授权流程，优先保证授权码与 verifier 的一次性安全语义。
        """
        now = _utc_now(self._clock)
        state_hash = hashlib.sha256(state.encode("ascii")).digest()
        async with self._stores() as store:
            consumed = await store.consume_attempt(state_hash=state_hash, now=now)
        if consumed is None:
            raise OAuthStateRejectedError
        attempt_id, user_id, encrypted_verifier = consumed
        verifier = self._cipher.decrypt(
            encrypted_verifier, self._aad(user_id, attempt_id, "pkce_verifier")
        ).decode("ascii")
        token = await self._oauth.exchange_code(code, verifier)
        account = await self._oauth.fetch_account(token.access_token)
        return await self._save_tokens(user_id=user_id, account=account, token=token, now=now)

    async def _save_tokens(
        self, *, user_id: UUID, account: GoogleAccount, token: GoogleTokenResponse, now: datetime
    ) -> UUID:
        """写入连接并用连接主键作为访问与刷新 token 的 AAD 归属记录。"""
        async with self._stores() as store:
            connection_id = await store.ensure_connection(
                user_id=user_id,
                provider_account_id=account.provider_account_id,
                account_email=account.email,
                scopes=GOOGLE_SCOPES,
            )
            access = self._cipher.encrypt(
                token.access_token.encode("utf-8"),
                self._aad(user_id, connection_id, "access_token"),
            )
            refresh = (
                self._cipher.encrypt(
                    token.refresh_token.encode("utf-8"),
                    self._aad(user_id, connection_id, "refresh_token"),
                )
                if token.refresh_token is not None
                else None
            )
            await store.save_connection_tokens(
                user_id=user_id,
                connection_id=connection_id,
                access_token=access,
                refresh_token=refresh,
                expires_at=now + timedelta(seconds=token.expires_in),
            )
        return connection_id

    async def list(self, *, user_id: UUID) -> tuple[ConnectionSummary, ...]:
        """返回当前用户的连接列表，绝不加载或公开 credential 行。"""
        async with self._stores() as store:
            rows = await store.list_connections(user_id=user_id)
        return tuple(
            ConnectionSummary(
                row.id,
                row.provider,
                row.account_email,
                tuple(row.scopes),
                row.status,
                row.last_error_code,
            )
            for row in rows
        )

    async def disconnect(self, *, user_id: UUID, connection_id: UUID) -> None:
        """先删除本地密文并标记断开，再尽力撤销远端刷新 token。"""
        async with self._stores() as store:
            found, refresh = await store.disconnect(user_id=user_id, connection_id=connection_id)
        if not found:
            raise ConnectionNotFoundError
        if refresh is None:
            return
        # 删除已提交后撤销远端，网络失败不能恢复本地访问能力或留下可用凭据。
        try:
            raw = self._cipher.decrypt(
                EncryptedValue(refresh.ciphertext, refresh.nonce, refresh.key_version),
                self._aad(user_id, connection_id, "refresh_token"),
            ).decode("utf-8")
            await self._oauth.revoke(raw)
        except (InvalidTag, UnicodeDecodeError, httpx.HTTPError):
            # 供应商撤销为尽力操作；本地密文已删除，禁止泄露或掩盖为连接仍有效。
            return

    async def start_manual_sync(
        self, *, user_id: UUID, connection_id: UUID, idempotency_key: str, tasks: TaskCreator
    ) -> ManualSyncResult:
        """为已连接且归属当前用户的帐号幂等创建邮件、日历两项异步任务。

        新任务必须使用供应商中立的 ``sync_mail`` 与显式 mailbox scope；幂等键仍保留
        M1 的 ``gmail`` 后缀，使升级前后重复提交收敛到既有任务。旧 ``sync_gmail`` kind
        只由 Worker 读取已持久化任务，不再从当前 API 创建。
        """
        async with self._stores() as store:
            connection = await store.get_connection(user_id=user_id, connection_id=connection_id)
        if connection is None or connection.status != "connected":
            raise ConnectionNotFoundError
        created = await tasks.execute_many(
            user_id=user_id,
            items=(
                CreateTaskBatchItem(
                    "sync_mail",
                    {
                        "connection_id": str(connection_id),
                        "scope_key": "mailbox",
                    },
                    f"{idempotency_key}:gmail",
                ),
                CreateTaskBatchItem(
                    "sync_calendar",
                    {"connection_id": str(connection_id)},
                    f"{idempotency_key}:calendar",
                ),
            ),
        )
        return ManualSyncResult(created[0].task_id, created[1].task_id)
