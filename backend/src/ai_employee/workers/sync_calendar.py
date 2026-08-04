"""将 durable ``sync_calendar`` 任务连接到 Calendar 同步用例。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx

from ai_employee.application.ports.calendar import CalendarReader
from ai_employee.application.use_cases.sync_calendar import (
    CalendarSyncStoreFactory,
    SyncCalendarUseCase,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.errors import TransientProviderError, UserActionRequiredError
from ai_employee.infrastructure.db.repositories.calendar import (
    SqlAlchemyCalendarSyncRepositoryFactory,
)
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyGmailSyncRepositoryFactory
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics
from ai_employee.infrastructure.observability.sync import observe_google_sync
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.calendar import CalendarAdapter
from ai_employee.integrations.google.fake import FakeCalendarReader, FakeGoogleOAuthClient
from ai_employee.integrations.google.oauth import GoogleOAuthClient


class CalendarSyncTaskStep:
    """在 DurableTaskRunner 租约内读取凭据并执行 Calendar 同步。"""

    name = "sync_calendar"

    def __init__(
        self,
        *,
        session_factory: ManagedAsyncSessionMaker,
        cipher: AeadCipher,
        oauth: GoogleOAuthClient | FakeGoogleOAuthClient,
        reader: CalendarReader | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        """注入进程资源；凭据读取和同步写入始终使用各自短事务。"""
        self._credential_stores = SqlAlchemyGmailSyncRepositoryFactory(session_factory)
        self._stores = SqlAlchemyCalendarSyncRepositoryFactory(session_factory)
        self._cipher, self._oauth = cipher, oauth
        self._reader = reader
        self._metrics = metrics

    async def execute(self, task: LeasedTask) -> None:
        """解密 token，构造只读适配器并把安全 AAD 留在应用/基础设施边界。"""
        raw_connection = task.input_payload.get("connection_id")
        if not isinstance(raw_connection, str) or task.user_id is None:
            raise ValueError("sync_calendar requires connection_id")
        connection_id, user_id = UUID(raw_connection), task.user_id
        async with self._credential_stores() as store:
            credentials = await store.get_credentials(user_id=user_id, connection_id=connection_id)
            timezone = await store.get_user_timezone(user_id=user_id)
        if credentials is None:
            from ai_employee.application.use_cases.sync_calendar import (
                CalendarConnectionNotFoundError,
            )

            raise CalendarConnectionNotFoundError
        if timezone is None:
            raise CalendarConnectionNotFoundError
        access = self._cipher.decrypt(
            credentials.access_token, self._credential_aad(user_id, connection_id, "access_token")
        ).decode()
        refresh = (
            self._cipher.decrypt(
                credentials.refresh_token,
                self._credential_aad(user_id, connection_id, "refresh_token"),
            ).decode()
            if credentials.refresh_token
            else None
        )

        async def refresh_access_token() -> str:
            """严格对齐 Gmail：一次 refresh 后 AEAD 轮换，按状态分类失败。"""
            if refresh is None:
                raise CalendarConnectionNotFoundError
            try:
                refreshed = await self._oauth.refresh_token(refresh)
            except httpx.HTTPStatusError as error:
                if error.response.status_code in {400, 401}:
                    await mark_expired()
                    raise UserActionRequiredError(
                        error_code="google_reauthorization_required",
                        message="Google Calendar authorization requires user action",
                    ) from error
                if error.response.status_code == 429 or error.response.status_code >= 500:
                    raise TransientProviderError(
                        error_code="google_rate_limited"
                        if error.response.status_code == 429
                        else "google_service_unavailable",
                        message="Google token refresh is temporarily unavailable",
                    ) from error
                raise UserActionRequiredError(
                    error_code="google_reauthorization_required",
                    message="Google Calendar authorization requires user action",
                ) from error
            except httpx.RequestError as error:
                raise TransientProviderError(
                    error_code="google_request_failed",
                    message="Google token refresh request failed",
                ) from error
            async with self._credential_stores() as store:
                await store.rotate_access_token(
                    user_id=user_id,
                    connection_id=connection_id,
                    access_token=self._cipher.encrypt(
                        refreshed.access_token.encode(),
                        self._credential_aad(user_id, connection_id, "access_token"),
                    ),
                    expires_at=datetime.now(UTC) + timedelta(seconds=refreshed.expires_in),
                    refresh_token=self._cipher.encrypt(
                        refreshed.refresh_token.encode(),
                        self._credential_aad(user_id, connection_id, "refresh_token"),
                    )
                    if refreshed.refresh_token
                    else None,
                )
            return refreshed.access_token

        async def mark_expired() -> None:
            """第二次资源 401 或 refresh 授权失效均独立提交 expired。"""
            async with self._credential_stores() as store:
                await store.mark_expired(user_id=user_id, connection_id=connection_id)

        adapter: CalendarReader = self._reader or CalendarAdapter(
            access_token=access,
            user_timezone=timezone,
            refresh_access_token=refresh_access_token if refresh else None,
            mark_expired=mark_expired,
        )
        await observe_google_sync(
            metrics=self._metrics,
            resource="calendar",
            operation=lambda: SyncCalendarUseCase(
                cast(CalendarSyncStoreFactory, self._stores), self._cipher, adapter
            ).execute(user_id=user_id, connection_id=connection_id),
        )

    @staticmethod
    def _credential_aad(user_id: UUID, connection_id: UUID, kind: str) -> bytes:
        """保持 OAuth token 使用连接主键绑定的固定 AAD。"""
        return f"{user_id}:{connection_id}:{kind}".encode("ascii")


def build_calendar_sync_task_step(
    *, session_factory: ManagedAsyncSessionMaker, settings: Settings, metrics: Metrics | None = None
) -> CalendarSyncTaskStep:
    """从受控配置构造日历任务步骤，不把 secret 放入队列输入。"""
    cipher = AeadCipher.from_file(settings.app_master_key_file)
    if settings.app_test_mode:
        fixture = Path(__file__).parents[3] / "tests" / "contract" / "fixtures" / "calendar_initial.json"
        return CalendarSyncTaskStep(session_factory=session_factory, cipher=cipher, oauth=FakeGoogleOAuthClient(), reader=FakeCalendarReader(fixture), metrics=metrics)
    oauth = GoogleOAuthClient(
        settings.google_client_id,
        settings.read_secret_file(settings.google_client_secret_file).get_secret_value(),
        settings.google_redirect_uri,
    )
    return CalendarSyncTaskStep(session_factory=session_factory, cipher=cipher, oauth=oauth, metrics=metrics)
