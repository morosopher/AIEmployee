"""将 durable ``sync_calendar`` 任务连接到 Calendar 同步用例。"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import httpx

from ai_employee.application.ports.calendar import (
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarReader,
    CalendarSyncPage,
)
from ai_employee.application.ports.oauth import OAuthProviderAdapter
from ai_employee.application.use_cases.sync_calendar import (
    CalendarConnectionNotFoundError,
    CalendarSyncStoreFactory,
    SyncCalendarUseCase,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.infrastructure.db.repositories.calendar import (
    SqlAlchemyCalendarSyncRepositoryFactory,
)
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyMailSyncRepositoryFactory
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics
from ai_employee.infrastructure.observability.sync import (
    observe_google_sync,
    observe_provider_sync,
)
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.infrastructure.testing.scenarios import consume_test_scenario
from ai_employee.integrations.google.calendar import GoogleCalendarAdapter
from ai_employee.integrations.google.fake import FakeCalendarReader, FakeGoogleOAuthClient
from ai_employee.integrations.google.oauth import GoogleOAuthClient
from ai_employee.integrations.microsoft.calendar import MicrosoftCalendarAdapter
from ai_employee.integrations.microsoft.fake import FakeMicrosoftOAuthAdapter
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter
from ai_employee.integrations.registry import (
    ProviderAdapterRegistry,
)


class _FakeMicrosoftCalendarReader:
    """从脱敏 Graph fixture 提供离线 Microsoft Calendar 端口。

    测试模式必须覆盖 provider 选择和 scoped cursor，但绝不能因为默认配置误触真实 Graph。
    该 reader 复用 Microsoft adapter 的字段规范化函数，确保 fake 与真实响应共享边界。
    """

    def __init__(self, directory_fixture: Path, event_fixture: Path) -> None:
        """保存仓库内 fixture 路径，不接受外部 URL。"""
        self._directory_fixture = directory_fixture
        self._event_fixture = event_fixture

    def for_user(self, user_id: UUID) -> "_FakeMicrosoftCalendarReader":
        """按用户返回独立 reader；fixture 本身不含用户数据。"""
        del user_id
        return type(self)(self._directory_fixture, self._event_fixture)

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """读取合成目录并保留 fixture 的最终 deltaLink。"""
        del cursor
        payload = self._load(self._directory_fixture)
        adapter = MicrosoftCalendarAdapter(access_token="fake", user_timezone="UTC")
        values = payload.get("value", [])
        if not isinstance(values, list):
            raise PermanentProviderError(
                error_code="microsoft_calendar_invalid_response",
                message="Microsoft calendar response is invalid",
            )
        calendars = tuple(
            adapter._normalize_calendar(item) for item in values if isinstance(item, dict)
        )
        delta = payload.get("@odata.deltaLink")
        if not isinstance(delta, str) or delta == "":
            raise PermanentProviderError(
                error_code="microsoft_calendar_delta_missing_cursor",
                message="Microsoft calendar delta response is missing a cursor",
            )
        yield CalendarDirectoryPage(calendars, None, delta)

    async def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """读取合成事件并以 calendar ID 绑定事件事实。"""
        payload = self._load(self._event_fixture)
        adapter = MicrosoftCalendarAdapter(access_token="fake", user_timezone="UTC")
        values = payload.get("value", [])
        if not isinstance(values, list):
            raise PermanentProviderError(
                error_code="microsoft_calendar_invalid_response",
                message="Microsoft calendar response is invalid",
            )
        events = tuple(
            adapter._normalize_event(item, calendar_id=calendar_id)
            for item in values
            if isinstance(item, dict)
        )
        yield CalendarSyncPage(events, None, f"fake-microsoft-calendar-{calendar_id}-v1")

    async def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """返回幂等空增量页，避免测试模式重复写入 fixture 事件。"""
        del cursor
        yield CalendarSyncPage((), None, f"fake-microsoft-calendar-{calendar_id}-v1")

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """从同一 fixture 精确读取当前事件，未知 ID 安全返回 None。"""
        payload = self._load(self._event_fixture)
        values = payload.get("value", [])
        if not isinstance(values, list):
            return None
        adapter = MicrosoftCalendarAdapter(access_token="fake", user_timezone="UTC")
        for item in values:
            if isinstance(item, dict) and item.get("id") == provider_event_id:
                return adapter._normalize_event(item, calendar_id=calendar_id)
        return None

    @staticmethod
    def _load(path: Path) -> dict[str, object]:
        """读取并验证 fixture 根对象。"""
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise PermanentProviderError(
                error_code="microsoft_calendar_invalid_response",
                message="Microsoft calendar response is invalid",
            )
        return value


class _RefreshedTokens(Protocol):
    """收窄 Google/Microsoft token response 的共同安全字段。"""

    @property
    def access_token(self) -> str: ...

    @property
    def refresh_token(self) -> str | None: ...

    @property
    def expires_in(self) -> int: ...


class CalendarSyncTaskStep:
    """在 DurableTaskRunner 租约内读取凭据并执行 Calendar 同步。"""

    name = "sync_calendar"

    def __init__(
        self,
        *,
        session_factory: ManagedAsyncSessionMaker,
        cipher: AeadCipher,
        oauth: GoogleOAuthClient | FakeGoogleOAuthClient,
        reader: CalendarReader | FakeCalendarReader | None = None,
        metrics: Metrics | None = None,
        microsoft_oauth: OAuthProviderAdapter | None = None,
        microsoft_reader: CalendarReader | _FakeMicrosoftCalendarReader | None = None,
    ) -> None:
        """注入进程资源；凭据读取和同步写入始终使用各自短事务。"""
        self._credential_stores = SqlAlchemyMailSyncRepositoryFactory(session_factory)
        self._stores = SqlAlchemyCalendarSyncRepositoryFactory(session_factory)
        self._cipher, self._oauth = cipher, oauth
        self._reader = reader
        self._metrics = metrics
        self._microsoft_oauth = microsoft_oauth
        self._microsoft_reader = microsoft_reader

    async def execute(self, task: LeasedTask) -> None:
        """解密 token，构造只读适配器并把安全 AAD 留在应用/基础设施边界。"""
        raw_connection = task.input_payload.get("connection_id")
        if not isinstance(raw_connection, str) or task.user_id is None:
            raise ValueError("sync_calendar requires connection_id")
        scope_key = self._resolve_scope_key(task.input_payload.get("scope_key"))
        connection_id, user_id = UUID(raw_connection), task.user_id
        async with self._credential_stores() as store:
            credentials = await store.get_credentials(user_id=user_id, connection_id=connection_id)
            timezone = await store.get_user_timezone(user_id=user_id)
        if credentials is None:
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

        async def refresh_microsoft_access_token() -> str:
            """刷新 Microsoft delegated token，并原子轮换 AEAD 密文。"""
            if refresh is None or self._microsoft_oauth is None:
                raise UserActionRequiredError(
                    error_code="microsoft_reauthorization_required",
                    message="Microsoft authorization requires user action",
                )
            try:
                refreshed: _RefreshedTokens = await self._microsoft_oauth.refresh(refresh)
            except TransientProviderError:
                raise
            except PermanentProviderError as error:
                if error.error_code != "microsoft_oauth_rejected":
                    raise
                await mark_expired()
                raise UserActionRequiredError(
                    error_code="microsoft_reauthorization_required",
                    message="Microsoft authorization requires user action",
                ) from error
            if (
                not isinstance(refreshed.access_token, str)
                or refreshed.access_token == ""
                or type(refreshed.expires_in) is not int
                or refreshed.expires_in <= 0
                or (
                    refreshed.refresh_token is not None
                    and (
                        not isinstance(refreshed.refresh_token, str)
                        or refreshed.refresh_token == ""
                    )
                )
            ):
                raise PermanentProviderError(
                    error_code="calendar_token_refresh_invalid",
                    message="Calendar token refresh response is invalid",
                )
            async with self._credential_stores() as store:
                await store.rotate_access_token(
                    user_id=user_id,
                    connection_id=connection_id,
                    access_token=self._cipher.encrypt(
                        refreshed.access_token.encode("utf-8"),
                        self._credential_aad(user_id, connection_id, "access_token"),
                    ),
                    expires_at=datetime.now(UTC) + timedelta(seconds=refreshed.expires_in),
                    refresh_token=(
                        self._cipher.encrypt(
                            refreshed.refresh_token.encode("utf-8"),
                            self._credential_aad(user_id, connection_id, "refresh_token"),
                        )
                        if refreshed.refresh_token is not None
                        else None
                    ),
                )
            return refreshed.access_token

        async def mark_calendar_permission_required(error_code: str) -> None:
            """只降级 calendar.read，避免 Graph 403 误断开整个连接。"""
            async with self._stores() as store:
                await store.mark_calendar_capability_action_required(
                    user_id=user_id,
                    connection_id=connection_id,
                    error_code=error_code,
                )

        if credentials.provider == "microsoft":
            if self._microsoft_oauth is None:
                raise PermanentProviderError(
                    error_code="provider_read_adapter_unavailable",
                    message="Microsoft calendar read adapter is unavailable",
                )
            microsoft_reader = (
                self._microsoft_reader.for_user(user_id)
                if isinstance(self._microsoft_reader, _FakeMicrosoftCalendarReader)
                else self._microsoft_reader
            ) or MicrosoftCalendarAdapter(
                access_token=access,
                user_timezone=timezone,
                refresh_access_token=(
                    refresh_microsoft_access_token if refresh is not None else None
                ),
                mark_expired=mark_expired,
            )
            registry = ProviderAdapterRegistry(
                microsoft_calendar=cast(CalendarReader, microsoft_reader)
            )
            try:
                await observe_provider_sync(
                    provider="microsoft",
                    metrics=self._metrics,
                    resource="calendar",
                    operation=lambda: SyncCalendarUseCase(
                        cast(CalendarSyncStoreFactory, self._stores),
                        registry,
                        self._cipher,
                    ).execute(
                        user_id=user_id,
                        connection_id=connection_id,
                        scope_key=scope_key,
                    ),
                )
            except UserActionRequiredError as error:
                if error.error_code == "microsoft_calendar_permission_required":
                    await mark_calendar_permission_required(error.error_code)
                elif error.error_code == "microsoft_reauthorization_required":
                    await mark_expired()
                raise
            return

        adapter = cast(
            CalendarReader,
            (
                self._reader.for_user(user_id)
                if isinstance(self._reader, FakeCalendarReader)
                else self._reader
            )
            or GoogleCalendarAdapter(
                access_token=access,
                user_timezone=timezone,
                refresh_access_token=refresh_access_token if refresh else None,
                mark_expired=mark_expired,
            ),
        )
        registry = ProviderAdapterRegistry(google_calendar=adapter)
        await observe_google_sync(
            metrics=self._metrics,
            resource="calendar",
            operation=lambda: SyncCalendarUseCase(
                cast(CalendarSyncStoreFactory, self._stores),
                registry,
                self._cipher,
            ).execute(
                user_id=user_id,
                connection_id=connection_id,
                scope_key=scope_key,
            ),
        )

    @staticmethod
    def _resolve_scope_key(raw_scope_key: object) -> str:
        """解析任务 scope，并让历史缺字段任务安全经过 ``directory`` owner。

        Task 12 之后所有新建手动/周期任务都必须显式携带 ``directory``；但数据库中可能仍有
        Task 7 前固定 primary 架构创建的 ``sync_calendar`` 任务。同一 kind 无法区分版本，
        因此字段完全缺失时也先同步目录，由当前 ProviderCalendar 事实证明后再访问 primary；
        显式空串或非字符串继续拒绝。
        """
        if raw_scope_key is None:
            return "directory"
        if isinstance(raw_scope_key, str) and raw_scope_key != "":
            return raw_scope_key
        raise ValueError("sync_calendar requires scope_key")

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
        fixture = (
            Path(__file__).parents[3] / "tests" / "contract" / "fixtures" / "calendar_initial.json"
        )
        microsoft_fixture_dir = (
            Path(__file__).parents[3] / "tests" / "contract" / "microsoft" / "fixtures"
        )
        return CalendarSyncTaskStep(
            session_factory=session_factory,
            cipher=cipher,
            oauth=FakeGoogleOAuthClient(),
            reader=FakeCalendarReader(
                fixture,
                scenario_consumer=lambda user_id: consume_test_scenario(
                    redis_url=settings.redis_url, user_id=user_id
                ),
            ),
            metrics=metrics,
            microsoft_oauth=FakeMicrosoftOAuthAdapter(
                client_id=settings.microsoft_client_id or "test-mode-microsoft-client",
                redirect_uri=settings.microsoft_redirect_uri
                or "https://app.example.test/microsoft/callback",
            ),
            microsoft_reader=_FakeMicrosoftCalendarReader(
                microsoft_fixture_dir / "calendars.json",
                microsoft_fixture_dir / "calendar_view_delta_initial.json",
            ),
        )
    oauth = GoogleOAuthClient(
        settings.google_client_id,
        settings.read_secret_file(settings.google_client_secret_file).get_secret_value(),
        settings.google_redirect_uri,
    )
    microsoft_oauth: MicrosoftOAuthAdapter | None = None
    if settings.microsoft_client_id and settings.microsoft_redirect_uri:
        microsoft_secret = settings.read_secret_file(
            settings.microsoft_client_secret_file
        ).get_secret_value()
        microsoft_oauth = MicrosoftOAuthAdapter(
            settings.microsoft_client_id,
            microsoft_secret,
            settings.microsoft_redirect_uri,
        )
    return CalendarSyncTaskStep(
        session_factory=session_factory,
        cipher=cipher,
        oauth=oauth,
        metrics=metrics,
        microsoft_oauth=microsoft_oauth,
    )
