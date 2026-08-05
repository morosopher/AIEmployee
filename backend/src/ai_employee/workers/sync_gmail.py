"""把 durable ``sync_gmail`` 任务组合为受控 Gmail 读取、刷新和原子同步。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx

from ai_employee.application.ports.gmail import GmailReader
from ai_employee.application.use_cases.sync_gmail import (
    GmailConnectionNotFoundError,
    GmailSyncStoreFactory,
    SyncGmailUseCase,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.errors import TransientProviderError, UserActionRequiredError
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyGmailSyncRepositoryFactory
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics
from ai_employee.infrastructure.observability.sync import observe_google_sync
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.infrastructure.testing.scenarios import consume_test_scenario
from ai_employee.integrations.google.fake import FakeGmailReader, FakeGoogleOAuthClient
from ai_employee.integrations.google.gmail import GmailAdapter
from ai_employee.integrations.google.oauth import GoogleOAuthClient


class GmailSyncTaskStep:
    """在 DurableTaskRunner 的租约内执行一个 ``sync_gmail`` 任务。"""

    name = "sync_gmail"

    def __init__(
        self,
        *,
        session_factory: ManagedAsyncSessionMaker,
        cipher: AeadCipher,
        oauth: GoogleOAuthClient | FakeGoogleOAuthClient,
        reader: GmailReader | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        """注入进程级资源；每个实际数据库操作仍由短事务 factory 创建。"""
        self._stores = SqlAlchemyGmailSyncRepositoryFactory(session_factory)
        self._cipher = cipher
        self._oauth = oauth
        self._reader = reader
        self._metrics = metrics

    async def execute(self, task: LeasedTask) -> None:
        """解密当前连接 token，在事务外读取 Gmail，并以精确 AAD 加密落库。

        Args:
            task: Durable runner 已租约的同步任务；输入必须只含 ``connection_id`` 字符串。

        Raises:
            GmailConnectionNotFoundError: 连接不属于任务用户、已断开或缺少可读凭据时抛出。
            ValueError: task 输入不是稳定 UUID 时抛出，交给 Runner 写安全失败。
        """
        raw_connection_id = task.input_payload.get("connection_id")
        if not isinstance(raw_connection_id, str) or task.user_id is None:
            raise ValueError("sync_gmail requires connection_id")
        connection_id = UUID(raw_connection_id)
        user_id = task.user_id
        async with self._stores() as store:
            credentials = await store.get_credentials(
                user_id=user_id, connection_id=connection_id
            )
        if credentials is None:
            raise GmailConnectionNotFoundError
        access_token = self._cipher.decrypt(
            credentials.access_token, self._credential_aad(user_id, connection_id, "access_token")
        ).decode("utf-8")
        refresh_token = (
            self._cipher.decrypt(
                credentials.refresh_token,
                self._credential_aad(user_id, connection_id, "refresh_token"),
            ).decode("utf-8")
            if credentials.refresh_token is not None
            else None
        )

        async def refresh_access_token() -> str:
            """刷新并原子轮换密文，同时把 OAuth 网络失败收窄为可恢复领域错误。

            refresh token 被 Google 以 400/401 拒绝时，连接已不再可由后台自行恢复，必须
            先持久化 ``expired`` 再提示用户重新授权。429、5xx 与全部 HTTP 传输异常则保留
            连接状态，让 DurableTaskRunner 根据稳定错误码及 ``Retry-After`` 安排重试。
            """
            if refresh_token is None:
                raise GmailConnectionNotFoundError
            try:
                refreshed = await self._oauth.refresh_token(refresh_token)
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                if status_code in {400, 401}:
                    await mark_expired()
                    raise UserActionRequiredError(
                        error_code="google_reauthorization_required",
                        message="Google Gmail authorization requires user action",
                    ) from error
                if status_code == 429 or status_code >= 500:
                    raise TransientProviderError(
                        error_code=(
                            "google_rate_limited"
                            if status_code == 429
                            else "google_service_unavailable"
                        ),
                        message="Google token refresh is temporarily unavailable",
                        retry_after=self._retry_after(error.response),
                    ) from error
                raise UserActionRequiredError(
                    error_code="google_reauthorization_required",
                    message="Google Gmail authorization requires user action",
                ) from error
            except httpx.RequestError as error:
                raise TransientProviderError(
                    error_code="google_request_failed",
                    message="Google token refresh request failed",
                ) from error
            encrypted_access = self._cipher.encrypt(
                refreshed.access_token.encode("utf-8"),
                self._credential_aad(user_id, connection_id, "access_token"),
            )
            encrypted_refresh = (
                self._cipher.encrypt(
                    refreshed.refresh_token.encode("utf-8"),
                    self._credential_aad(user_id, connection_id, "refresh_token"),
                )
                if refreshed.refresh_token is not None
                else None
            )
            async with self._stores() as store:
                await store.rotate_access_token(
                    user_id=user_id,
                    connection_id=connection_id,
                    access_token=encrypted_access,
                    expires_at=datetime.now(UTC) + timedelta(seconds=refreshed.expires_in),
                    refresh_token=encrypted_refresh,
                )
            return refreshed.access_token

        async def mark_expired() -> None:
            """在第二次 401 后独立提交 expired 状态，禁止未来后台读取。"""
            async with self._stores() as store:
                await store.mark_expired(user_id=user_id, connection_id=connection_id)

        adapter: GmailReader = (
            self._reader.for_user(user_id)
            if isinstance(self._reader, FakeGmailReader)
            else self._reader
        ) or GmailAdapter(
            access_token=access_token,
            refresh_access_token=refresh_access_token if refresh_token is not None else None,
            mark_expired=mark_expired,
        )
        await observe_google_sync(
            metrics=self._metrics,
            resource="gmail",
            operation=lambda: SyncGmailUseCase(
                cast(GmailSyncStoreFactory, self._stores), self._cipher, adapter
            ).execute(user_id=user_id, connection_id=connection_id),
        )

    @staticmethod
    def _credential_aad(user_id: UUID, connection_id: UUID, kind: str) -> bytes:
        """保持与 OAuth 回调一致的 token AAD，阻止跨连接密文替换。"""
        return f"{user_id}:{connection_id}:{kind}".encode("ascii")

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        """解析 OAuth 响应的非负秒数提示，畸形值交由耐久退避策略使用默认等待。

        Args:
            response: 已由 httpx 绑定请求上下文的 Google OAuth 响应。

        Returns:
            合法 ``Retry-After`` 的秒数；缺失、日期格式或非法值时返回 ``None``。
        """
        try:
            retry_after = int(response.headers.get("Retry-After", ""))
        except ValueError:
            return None
        return retry_after if retry_after >= 0 else None


def build_gmail_sync_task_step(
    *, session_factory: ManagedAsyncSessionMaker, settings: Settings, metrics: Metrics | None = None
) -> GmailSyncTaskStep:
    """从进程配置构造 Gmail durable task step，不向任务载荷泄露任何 Secret。"""
    cipher = AeadCipher.from_file(settings.app_master_key_file)
    if settings.app_test_mode:
        fixture = Path(__file__).parents[3] / "tests" / "contract" / "fixtures" / "gmail_initial.json"
        return GmailSyncTaskStep(
            session_factory=session_factory,
            cipher=cipher,
            oauth=FakeGoogleOAuthClient(),
            reader=FakeGmailReader(
                fixture,
                scenario_consumer=lambda user_id: consume_test_scenario(
                    redis_url=settings.redis_url, user_id=user_id
                ),
            ),
            metrics=metrics,
        )
    client_secret = settings.read_secret_file(settings.google_client_secret_file).get_secret_value()
    oauth = GoogleOAuthClient(settings.google_client_id, client_secret, settings.google_redirect_uri)
    return GmailSyncTaskStep(
        session_factory=session_factory,
        cipher=cipher,
        oauth=oauth,
        metrics=metrics,
    )
