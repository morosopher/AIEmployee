"""将 durable ``sync_calendar`` 任务连接到 Calendar 同步用例。"""

import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from ai_employee.application.ports.calendar import (
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarReader,
    CalendarSyncPage,
)
from ai_employee.application.ports.encryption import Encryption
from ai_employee.application.ports.oauth import OAuthProviderAdapter
from ai_employee.application.ports.oauth_refresh import (
    OAuthRefreshCoordinator,
    OAuthRefreshLease,
    OAuthRefreshProvider,
    OAuthRefreshRequest,
)
from ai_employee.application.use_cases.calendar_aad_preflight import CalendarAadAdapters
from ai_employee.application.use_cases.calendar_aad_recovery import CalendarAadTaskBinding
from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadGuard,
    CalendarAadPair,
    CalendarAadRolloutError,
)
from ai_employee.application.use_cases.sync_calendar import (
    CalendarConnectionNotFoundError,
    CalendarSyncStoreFactory,
    SyncCalendarUseCase,
)
from ai_employee.application.use_cases.sync_mail import CoordinatedAccessTokenRefresh
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import (
    PermanentProviderError,
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
from ai_employee.integrations.google.oauth import GoogleOAuthAdapter, GoogleOAuthClient
from ai_employee.integrations.microsoft.calendar import MicrosoftCalendarAdapter
from ai_employee.integrations.microsoft.fake import FakeMicrosoftOAuthAdapter
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter
from ai_employee.integrations.registry import (
    ProviderAdapterRegistry,
    build_oauth_security_services,
)


class _FakeMicrosoftCalendarReader:
    """从脱敏 Graph fixture 提供离线 Microsoft Calendar 端口。

    测试模式必须覆盖 provider 选择和 scoped cursor，但绝不能因为默认配置误触真实 Graph。
    该 reader 复用 Microsoft adapter 的字段规范化函数，确保 fake 与真实响应共享边界。
    """

    def __init__(self, directory_fixture: Path, event_fixture: Path) -> None:
        """保存仓库内 fixture 路径，并冻结事件 fixture 的唯一日历归属。"""
        self._directory_fixture = directory_fixture
        self._event_fixture = event_fixture
        self._event_calendar_id = self._primary_calendar_id()

    def for_user(self, user_id: UUID) -> "_FakeMicrosoftCalendarReader":
        """按用户返回独立 reader；fixture 本身不含用户数据。"""
        del user_id
        return type(self)(self._directory_fixture, self._event_fixture)

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """读取合成完整目录；测试模式同样不得制造 Microsoft provider cursor。"""
        if cursor is not None:
            raise PermanentProviderError(
                error_code="microsoft_calendar_directory_cursor_unsupported",
                message="Microsoft calendar directory cursor is unsupported",
            )
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
        if payload.get("@odata.deltaLink") is not None or any(
            item.get("@removed") is not None for item in values if isinstance(item, dict)
        ):
            raise PermanentProviderError(
                error_code="microsoft_calendar_invalid_response",
                message="Microsoft calendar response is invalid",
            )
        yield CalendarDirectoryPage(calendars, None, None, full_snapshot=True)

    async def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """读取合成事件并以 calendar ID 绑定事件事实。"""
        if calendar_id != self._event_calendar_id:
            # 单份事件 fixture 只描述目录中的 primary collection；未知或只读日历必须返回
            # 独立空页，不能通过改写 calendar_id 复制同一供应商事实。
            yield CalendarSyncPage((), None, f"fake-microsoft-calendar-{calendar_id}-v1")
            return
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
        if calendar_id != self._event_calendar_id:
            # exact GET 的稳定身份是 (calendar_id, event_id)，不能只按 event ID 命中。
            return None
        payload = self._load(self._event_fixture)
        values = payload.get("value", [])
        if not isinstance(values, list):
            return None
        adapter = MicrosoftCalendarAdapter(access_token="fake", user_timezone="UTC")
        for item in values:
            if isinstance(item, dict) and item.get("id") == provider_event_id:
                return adapter._normalize_event(item, calendar_id=calendar_id)
        return None

    def _primary_calendar_id(self) -> str:
        """从目录 fixture 解析事件 fixture 唯一对应的 primary calendar ID。

        测试模式不得猜测或复制日历归属。目录缺少唯一 primary、ID 为空或类型异常时，组合
        根必须以稳定供应商响应错误失败，而不是把事件投影到调用方任意传入的 calendar ID。
        """
        payload = self._load(self._directory_fixture)
        values = payload.get("value", [])
        if not isinstance(values, list):
            raise PermanentProviderError(
                error_code="microsoft_calendar_invalid_response",
                message="Microsoft calendar response is invalid",
            )
        primary_ids = [
            item.get("id")
            for item in values
            if isinstance(item, dict) and item.get("isDefaultCalendar") is True
        ]
        if len(primary_ids) != 1 or not isinstance(primary_ids[0], str) or primary_ids[0] == "":
            raise PermanentProviderError(
                error_code="microsoft_calendar_invalid_response",
                message="Microsoft calendar response is invalid",
            )
        return primary_ids[0]

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


class CalendarSyncTaskStep:
    """在 DurableTaskRunner 租约内读取凭据并执行 Calendar 同步。"""

    name = "sync_calendar"

    def __init__(
        self,
        *,
        session_factory: ManagedAsyncSessionMaker,
        cipher: AeadCipher,
        oauth: OAuthRefreshProvider | GoogleOAuthClient | FakeGoogleOAuthClient,
        reader: CalendarReader | FakeCalendarReader | None = None,
        metrics: Metrics | None = None,
        refresh_coordinator: OAuthRefreshCoordinator | None = None,
        microsoft_oauth: OAuthProviderAdapter | None = None,
        microsoft_reader: CalendarReader | _FakeMicrosoftCalendarReader | None = None,
    ) -> None:
        """注入进程资源；凭据读取和同步写入始终使用各自短事务。"""
        self._credential_stores = SqlAlchemyMailSyncRepositoryFactory(session_factory)
        self._stores = SqlAlchemyCalendarSyncRepositoryFactory(session_factory)
        self._cipher, self._oauth = cipher, oauth
        self._refresh_coordinator = refresh_coordinator
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
        # 每个 CalendarView 的 401 都绑定当时 access 快照；不能在闭包缓存旧 refresh。
        provider = (
            self._microsoft_oauth
            if credentials.provider == "microsoft"
            else (
                None
                if isinstance(self._oauth, (GoogleOAuthClient, FakeGoogleOAuthClient))
                else self._oauth
            )
        )
        refresh_access_token = CoordinatedAccessTokenRefresh(
            coordinator=self._refresh_coordinator,
            provider=provider,
            request=OAuthRefreshRequest(
                user_id=user_id,
                connection_id=connection_id,
                capability=ConnectionCapability.CALENDAR_READ,
                expected_access=credentials.access_snapshot,
            ),
        )

        async def mark_expired() -> None:
            """第二次资源 401 或 refresh 授权失效均独立提交 expired。"""
            async with self._credential_stores() as store:
                await store.mark_expired(user_id=user_id, connection_id=connection_id)

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
                refresh_access_token=(refresh_access_token),
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
                refresh_access_token=refresh_access_token,
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
    security = build_oauth_security_services(
        session_factory=session_factory, master_key_file=settings.app_master_key_file
    )
    cipher = security.cipher
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
            refresh_coordinator=security.coordinator,
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
    oauth = GoogleOAuthAdapter(
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
        refresh_coordinator=security.coordinator,
        oauth=oauth,
        metrics=metrics,
        microsoft_oauth=microsoft_oauth,
    )


class CalendarAadReadAdapters:
    """封闭维护窗口复用普通 Calendar adapter 类型，移除自动 401 refresh 和能力状态副作用。"""

    def __init__(self, settings: Settings, *, clock: Callable[[], datetime]) -> None:
        """仅保存配置；按实际 affected provider 延迟读取对应 OAuth Secret，不联系真实账号。"""
        self._settings, self._clock = settings, clock

    def oauth(self, provider: str) -> OAuthRefreshProvider:
        """构造与 Calendar Worker 相同的最小 provider-neutral OAuth refresh adapter。"""
        settings = self._settings
        if settings.app_test_mode:
            # 维护 CLI 不从测试模式推断真实账号授权；合成测试显式注入 FakeAdapters。
            raise PermanentProviderError(
                error_code="calendar_aad_fake_adapter_required",
                message="Calendar AAD requires an injected test adapter",
            )
        if provider == "google":
            return GoogleOAuthAdapter(
                settings.google_client_id,
                settings.read_secret_file(settings.google_client_secret_file).get_secret_value(),
                settings.google_redirect_uri,
            )
        if (
            provider == "microsoft"
            and settings.microsoft_client_id
            and settings.microsoft_redirect_uri
        ):
            return MicrosoftOAuthAdapter(
                settings.microsoft_client_id,
                settings.read_secret_file(settings.microsoft_client_secret_file).get_secret_value(),
                settings.microsoft_redirect_uri,
            )
        raise PermanentProviderError(
            error_code="provider_read_adapter_unavailable",
            message="Calendar read adapter is unavailable",
        )

    def calendar_reader(self, pair: CalendarAadPair, access_token: str) -> CalendarReader:
        """仅在受控内存把当前 token 与精确 owner timezone 交给现有只读 Calendar adapter。"""
        if self._settings.app_test_mode:
            raise PermanentProviderError(
                error_code="calendar_aad_fake_adapter_required",
                message="Calendar AAD requires an injected test adapter",
            )
        if pair.provider == "google":
            return GoogleCalendarAdapter(
                access_token=access_token, user_timezone=pair.timezone, now=self._clock
            )
        if pair.provider == "microsoft":
            return MicrosoftCalendarAdapter(
                access_token=access_token, user_timezone=pair.timezone, now=self._clock
            )
        raise PermanentProviderError(
            error_code="provider_read_adapter_unavailable",
            message="Calendar read adapter is unavailable",
        )


class _CalendarAadLeasedGuard:
    """把连接 lease 加到同一 artifact/current-deadline guard，供每页与最终提交复用。"""

    def __init__(self, guard: CalendarAadGuard, lease: OAuthRefreshLease) -> None:
        """租约只由该 recovery task 持有，不缓存 guard 的通过结果。"""
        self._guard, self._lease = guard, lease

    async def verify(self) -> datetime | None:
        """在当前读取前后确认连接 lease；底层 guard 另行验证 revision-global lease。"""
        await self._lease.assert_owned()
        deadline = await self._guard.verify()
        await self._lease.assert_owned()
        return deadline


class CalendarAadRecoveryTaskStep:
    """为 recovery-only Runner 组合 exact marker、当前双 credential 和只读 provider。

    输入先验证，marker 已清时不解析 credential/adapter。所有认证或供应商错误仅由
    durable runner 记录稳定失败，保持连接能力与 marker；从不发起第二次 OAuth refresh。
    """

    name = "calendar_aad_0019_resync"

    def __init__(
        self,
        *,
        sessions: ManagedAsyncSessionMaker,
        cipher: Encryption,
        coordinator: OAuthRefreshCoordinator,
        adapters: CalendarAadAdapters,
        guard: CalendarAadGuard,
        clock: Callable[[], datetime],
    ) -> None:
        """注入同 Secret 的唯一 coordinator/cipher 与 one-off 已持有的真实 rollout guard。"""
        self._stores = SqlAlchemyCalendarSyncRepositoryFactory(sessions)
        self._cipher, self._coordinator, self._adapters = cipher, coordinator, adapters
        self._guard, self._clock = guard, clock

    async def execute(self, task: LeasedTask) -> None:
        """持共享连接 lease 读取当前 readiness，再运行同一事务边界的 marked scope 用例。"""
        binding = CalendarAadTaskBinding.from_task(task)
        await self._guard.verify()
        async with self._stores() as store:
            state = await store.get_marked_state(binding=binding, now=self._clock())
        if state is None:
            return
        async with self._coordinator.explicit_recovery_lease(
            user_id=binding.user_id,
            connection_id=binding.input.connection_id,
        ) as lease:
            guard = _CalendarAadLeasedGuard(self._guard, lease)
            await guard.verify()
            ready = await self._coordinator.read_current(
                OAuthRefreshRequest(
                    user_id=binding.user_id,
                    connection_id=binding.input.connection_id,
                    capability=ConnectionCapability.CALENDAR_READ,
                )
            )
            if ready.snapshot.authorization_generation != state.pair.authorization_generation:
                raise CalendarAadRolloutError("calendar_aad_recovery_state_changed")
            await guard.verify()
            reader = self._adapters.calendar_reader(state.pair, ready.access_token)
            registry = ProviderAdapterRegistry(
                google_calendar=reader if state.pair.provider == "google" else None,
                microsoft_calendar=reader if state.pair.provider == "microsoft" else None,
            )
            await SyncCalendarUseCase(
                cast(CalendarSyncStoreFactory, self._stores),
                registry,
                self._cipher,
            ).execute_marked_scope(binding=binding, guard=guard, clock=self._clock)
