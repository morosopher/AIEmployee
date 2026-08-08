"""为测试模式提供绝不联网的 Google 合成端口。"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from ai_employee.application.ports.calendar import (
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarSyncPage,
)
from ai_employee.application.ports.gmail import GmailSyncPage
from ai_employee.domain.errors import TransientProviderError, UserActionRequiredError
from ai_employee.integrations.google.calendar import CalendarAdapter
from ai_employee.integrations.google.oauth import GoogleAccount, GoogleTokenResponse

ScenarioConsumer = Callable[[UUID], Awaitable[str | None]]


class FakeGoogleOAuthClient:
    """返回固定合成帐户和 token，供 Playwright 稳定验证连接/重连状态。"""

    def __init__(
        self,
        *,
        scenario_consumer: ScenarioConsumer | None = None,
        user_id: UUID | None = None,
    ) -> None:
        """仅在测试组合根显式注入时消费当前用户的一次性故障场景。"""
        self._scenario_consumer = scenario_consumer
        self._user_id = user_id

    async def exchange_code(self, code: str, verifier: str) -> GoogleTokenResponse:
        """忽略浏览器往返内容，绝不向 Google 请求授权码。"""
        del code, verifier
        return GoogleTokenResponse("fake-access", "fake-refresh", 3600)

    async def fetch_account(self, access_token: str) -> GoogleAccount:
        """返回可预测且脱敏的测试帐户。"""
        del access_token
        if await self._consume_scenario() == "oauth_revoked":
            raise UserActionRequiredError(
                error_code="google_reauthorization_required",
                message="Google authorization requires user action",
            )
        return GoogleAccount("fake-google-subject", "test-mode@example.test")

    async def _consume_scenario(self) -> str | None:
        """通过调用方提供的 Redis GETDEL 边界一次性读取当前用户的测试值。"""
        if self._scenario_consumer is None or self._user_id is None:
            return None
        return await self._scenario_consumer(self._user_id)

    async def refresh_token(self, refresh_token: str) -> GoogleTokenResponse:
        """模拟连接恢复，不执行网络调用。"""
        del refresh_token
        return GoogleTokenResponse("fake-reconnected-access", "fake-refresh", 3600)

    async def revoke(self, token: str) -> None:
        """本地假撤销不产生外部副作用。"""
        del token


class FakeCalendarReader:
    """从仓库合成 fixture 装载目录和分日历页，永不创建 HTTP 客户端。"""

    def __init__(
        self,
        fixture: Path,
        *,
        directory_fixture: Path | None = None,
        scenario_consumer: ScenarioConsumer | None = None,
        user_id: UUID | None = None,
    ) -> None:
        self._fixture = fixture
        self._directory_fixture = directory_fixture or fixture.with_name(
            "google_calendar_list.json"
        )
        self._scenario_consumer = scenario_consumer
        self._user_id = user_id

    def for_user(self, user_id: UUID) -> "FakeCalendarReader":
        """为单次 Worker 执行绑定用户，避免共享 reader 串用 Redis 场景。"""
        return type(self)(
            self._fixture,
            directory_fixture=self._directory_fixture,
            scenario_consumer=self._scenario_consumer,
            user_id=user_id,
        )

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """读取本地 CalendarList fixture 并复用真实目录规范化逻辑。"""
        del cursor
        payload = json.loads(self._directory_fixture.read_text(encoding="utf-8"))
        adapter = CalendarAdapter(access_token="fake", user_timezone="UTC")
        yield CalendarDirectoryPage(
            tuple(
                adapter._normalize_calendar(item)
                for item in payload.get("items", [])
                if isinstance(item, dict)
            ),
            None,
            payload.get("nextSyncToken"),
        )

    async def initial_pages(self, calendar_id: str = "primary") -> AsyncIterator[CalendarSyncPage]:
        """读取 primary 事件 JSON；其他日历返回稳定空页，避免伪造供应商事件。"""
        if await self._consume_scenario() == "calendar_5xx":
            raise TransientProviderError(
                error_code="google_service_unavailable",
                message="Google Calendar is temporarily unavailable",
            )
        if calendar_id != "primary":
            # fixture 只描述 primary collection；为 secondary 伪造同一事件会制造错误身份。
            yield CalendarSyncPage((), None, f"fake-empty-{calendar_id}")
            return
        payload = json.loads(self._fixture.read_text(encoding="utf-8"))
        adapter = CalendarAdapter(
            access_token="fake", user_timezone="UTC", now=lambda: datetime(2030, 1, 9, tzinfo=UTC)
        )
        yield CalendarSyncPage(
            tuple(
                adapter._normalize(item, calendar_id=calendar_id)
                for item in payload.get("items", [])
                if isinstance(item, dict)
            ),
            None,
            payload.get("nextSyncToken"),
        )

    async def sync_pages(
        self, calendar_id: str, cursor: str | None = None
    ) -> AsyncIterator[CalendarSyncPage]:
        """primary 使用脱敏 fixture，其他日历返回稳定空页和合成游标。"""
        if cursor is None:
            cursor, calendar_id = calendar_id, "primary"
        del cursor
        async for page in self.initial_pages(calendar_id):
            yield page

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """仅从 primary fixture 精确读取当前事件，secondary 未知时安全返回 ``None``。"""
        if calendar_id != "primary":
            # 在检查 fixture 前拒绝 secondary，避免把 primary 事实投影到错误日历。
            return None
        payload = json.loads(self._fixture.read_text(encoding="utf-8"))
        adapter = CalendarAdapter(access_token="fake", user_timezone="UTC")
        for item in payload.get("items", []):
            if isinstance(item, dict) and item.get("id") == provider_event_id:
                return adapter._normalize(item, calendar_id=calendar_id)
        return None

    async def execute_request(self, parameters: dict[str, str]) -> object:
        """禁止通过 fake 绕过分页 API 访问网络。"""
        del parameters
        raise RuntimeError("test-mode Calendar reader never performs HTTP")

    async def _consume_scenario(self) -> str | None:
        """只在带用户的 test-support 调用中执行 Redis 原子消费。"""
        if self._scenario_consumer is None or self._user_id is None:
            return None
        return await self._scenario_consumer(self._user_id)


class FakeGmailReader:
    """从脱敏 Gmail fixture 读取游标页，绝不构造 HTTP 客户端。"""

    def __init__(
        self,
        fixture: Path,
        *,
        scenario_consumer: ScenarioConsumer | None = None,
        user_id: UUID | None = None,
    ) -> None:
        """保存仓库内 fixture 路径，不允许任何外部 URL 输入。"""
        self._fixture = fixture
        self._scenario_consumer = scenario_consumer
        self._user_id = user_id

    def for_user(self, user_id: UUID) -> "FakeGmailReader":
        """为单次 Worker 执行绑定用户，避免共享 reader 串用 Redis 场景。"""
        return type(self)(self._fixture, scenario_consumer=self._scenario_consumer, user_id=user_id)

    async def initial_pages(self) -> AsyncIterator[GmailSyncPage]:
        """返回合成的空消息页和 fixture history ID，供 Playwright 预测连接状态。"""
        scenario = await self._consume_scenario()
        if scenario == "oauth_revoked":
            raise UserActionRequiredError(
                error_code="google_reauthorization_required",
                message="Synthetic Google authorization was revoked",
            )
        if scenario == "partial_source":
            raise TransientProviderError(
                error_code="synthetic_partial_source",
                message="Synthetic Gmail source is temporarily unavailable",
            )
        if scenario == "gmail_429":
            raise TransientProviderError(
                error_code="google_rate_limited",
                message="Google Gmail is rate limited",
                retry_after=30,
            )
        payload = json.loads(self._fixture.read_text(encoding="utf-8"))
        history_id = payload.get("historyId")
        yield GmailSyncPage((), None, history_id if isinstance(history_id, str) else "")

    async def history_pages(self, cursor: str) -> AsyncIterator[GmailSyncPage]:
        """增量模式同样只读取固定 fixture。"""
        del cursor
        async for page in self.initial_pages():
            yield page

    async def execute_request(self, path: str, parameters: dict[str, str]) -> object:
        """明示测试模式不可联网。"""
        del path, parameters
        raise RuntimeError("test-mode Gmail reader never performs HTTP")

    async def _consume_scenario(self) -> str | None:
        """只在带用户的 test-support 调用中执行 Redis 原子消费。"""
        if self._scenario_consumer is None or self._user_id is None:
            return None
        return await self._scenario_consumer(self._user_id)
