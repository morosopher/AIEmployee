"""把 durable ``sync_mail`` 与 legacy ``sync_gmail`` 组合为受控邮件读取任务。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx

from ai_employee.application.use_cases.sync_mail import (
    MailConnectionNotFoundError,
    MailSyncStoreFactory,
    SyncMailUseCase,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.errors import TransientProviderError, UserActionRequiredError
from ai_employee.infrastructure.db.repositories.email import (
    SqlAlchemyMailSyncRepositoryFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics
from ai_employee.infrastructure.observability.sync import observe_google_sync
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.infrastructure.testing.scenarios import consume_test_scenario
from ai_employee.integrations.google.fake import FakeGmailReader, FakeGoogleOAuthClient
from ai_employee.integrations.google.gmail import GmailAdapter
from ai_employee.integrations.google.oauth import GoogleOAuthClient
from ai_employee.integrations.registry import LegacyGoogleMailReader, ProviderAdapterRegistry


class MailSyncTaskStep:
    """在 DurableTaskRunner 租约内执行一个 scoped ``sync_mail`` 任务。"""

    name = "sync_mail"

    def __init__(
        self,
        *,
        session_factory: ManagedAsyncSessionMaker,
        cipher: AeadCipher,
        oauth: GoogleOAuthClient | FakeGoogleOAuthClient,
        reader: LegacyGoogleMailReader | FakeGmailReader | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        """注入进程资源；每个数据库操作仍由独立短事务创建。"""
        self._stores = SqlAlchemyMailSyncRepositoryFactory(session_factory)
        self._cipher = cipher
        self._oauth = oauth
        self._reader = reader
        self._metrics = metrics

    async def execute(self, task: LeasedTask) -> None:
        """解密连接 token，组装固定 registry，并执行一个邮件 scope 同步。

        新任务必须携带非空 ``scope_key``。持久化 legacy ``sync_gmail`` 任务来自 M1 单邮箱
        架构，因此缺少该字段时只能回填可证明的固定 ``mailbox``，其他任务不得猜测 scope。
        Microsoft 适配器尚未在 Task 7 接入；Microsoft 连接会由 registry 安全拒绝，而不会
        被误送到 Google HTTP 端点。
        """
        raw_connection_id = task.input_payload.get("connection_id")
        if not isinstance(raw_connection_id, str) or task.user_id is None:
            raise ValueError("sync_mail requires connection_id")
        raw_scope_key = task.input_payload.get("scope_key")
        if raw_scope_key is None and task.kind == "sync_gmail":
            scope_key = "mailbox"
        elif isinstance(raw_scope_key, str) and raw_scope_key != "":
            scope_key = raw_scope_key
        else:
            raise ValueError("sync_mail requires scope_key")

        connection_id = UUID(raw_connection_id)
        user_id = task.user_id
        async with self._stores() as store:
            credentials = await store.get_credentials(
                user_id=user_id,
                connection_id=connection_id,
            )
        if credentials is None:
            raise MailConnectionNotFoundError
        access_token = self._cipher.decrypt(
            credentials.access_token,
            self._credential_aad(user_id, connection_id, "access_token"),
        ).decode("utf-8")
        refresh_token = (
            self._cipher.decrypt(
                credentials.refresh_token,
                self._credential_aad(user_id, connection_id, "refresh_token"),
            ).decode("utf-8")
            if credentials.refresh_token is not None
            else None
        )

        async def mark_expired() -> None:
            """在授权永久失效后独立提交降级状态，阻止未来后台读取。"""
            async with self._stores() as store:
                await store.mark_expired(user_id=user_id, connection_id=connection_id)

        async def refresh_access_token() -> str:
            """刷新 Google token 并原子轮换密文，按状态分类失败。

            Task 7 只保留既有 Google 刷新实现；Microsoft OAuth 客户端由后续批准任务接入。
            该闭包仅注册在 ``google`` reader 下，其他 provider 不可能调用它。
            """
            if refresh_token is None:
                raise MailConnectionNotFoundError
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

        google_reader: LegacyGoogleMailReader = (
            self._reader.for_user(user_id)
            if isinstance(self._reader, FakeGmailReader)
            else self._reader
        ) or GmailAdapter(
            access_token=access_token,
            refresh_access_token=refresh_access_token if refresh_token is not None else None,
            mark_expired=mark_expired,
        )
        registry = ProviderAdapterRegistry(google_mail=google_reader)
        try:
            await observe_google_sync(
                metrics=self._metrics,
                # 保留 M1 Google 指标 label，避免 Task 7 把指标协议迁移混入 source port 重构。
                resource="gmail",
                operation=lambda: SyncMailUseCase(
                    cast(MailSyncStoreFactory, self._stores),
                    registry,
                    self._cipher,
                ).execute(
                    user_id=user_id,
                    connection_id=connection_id,
                    scope_key=scope_key,
                ),
            )
        except UserActionRequiredError:
            # Fake 与真实适配器都经同一撤销事实入口，避免测试路径绕过持久化语义。
            await mark_expired()
            raise

    @staticmethod
    def _credential_aad(user_id: UUID, connection_id: UUID, kind: str) -> bytes:
        """保持与 OAuth callback 一致的 token AAD，阻止跨连接密文替换。"""
        return f"{user_id}:{connection_id}:{kind}".encode("ascii")

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        """解析 OAuth 响应的非负 Retry-After 秒数，畸形值返回 ``None``。"""
        try:
            retry_after = int(response.headers.get("Retry-After", ""))
        except ValueError:
            return None
        return retry_after if retry_after >= 0 else None


def build_mail_sync_task_step(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings,
    metrics: Metrics | None = None,
) -> MailSyncTaskStep:
    """从进程配置构造邮件 durable task step，不向任务载荷泄露 Secret。"""
    cipher = AeadCipher.from_file(settings.app_master_key_file)
    if settings.app_test_mode:
        fixture = (
            Path(__file__).parents[3] / "tests" / "contract" / "fixtures" / "gmail_initial.json"
        )
        return MailSyncTaskStep(
            session_factory=session_factory,
            cipher=cipher,
            oauth=FakeGoogleOAuthClient(),
            reader=FakeGmailReader(
                fixture,
                scenario_consumer=lambda user_id: consume_test_scenario(
                    redis_url=settings.redis_url,
                    user_id=user_id,
                ),
            ),
            metrics=metrics,
        )
    client_secret = settings.read_secret_file(settings.google_client_secret_file).get_secret_value()
    oauth = GoogleOAuthClient(
        settings.google_client_id,
        client_secret,
        settings.google_redirect_uri,
    )
    return MailSyncTaskStep(
        session_factory=session_factory,
        cipher=cipher,
        oauth=oauth,
        metrics=metrics,
    )


__all__ = ["MailSyncTaskStep", "build_mail_sync_task_step"]
