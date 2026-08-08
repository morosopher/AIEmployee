"""实现 Google Calendar 目录、分日历事件读取与严格错误分类。"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from ai_employee.application.ports.calendar import (
    CalendarCursorExpiredError,
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarSyncPage,
    ProviderCalendar,
)
from ai_employee.domain.errors import TransientProviderError, UserActionRequiredError

GOOGLE_CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
GOOGLE_CALENDAR_EVENTS_BASE_URL = "https://www.googleapis.com/calendar/v3/calendars"
# M1 的常量仍被旧契约和运维诊断导入；它只是 primary 的兼容 URL，不再是实现固定目标。
CALENDAR_EVENTS_URL = f"{GOOGLE_CALENDAR_EVENTS_BASE_URL}/primary/events"

_RefreshAccessToken = Callable[[], Awaitable[str]]
_MarkExpired = Callable[[], Awaitable[None]]


class GoogleCalendarAdapter:
    """以 httpx 调用 Google Calendar REST 只读端点并隔离供应商 JSON。

    适配器只负责网络和供应商字段规范化。目录同步游标固定使用 ``directory`` scope；事件
    游标由调用方以实际 provider calendar ID 作为 scope 保存，避免同一连接多个日历互相覆盖。
    """

    def __init__(
        self,
        *,
        access_token: str,
        user_timezone: str,
        now: Callable[[], datetime] | None = None,
        refresh_access_token: _RefreshAccessToken | None = None,
        mark_expired: _MarkExpired | None = None,
    ) -> None:
        """保存短生命周期令牌、明确用户时区和严格一次刷新回调。"""
        self._access_token = access_token
        self._timezone = ZoneInfo(user_timezone)
        self._now = now or (lambda: datetime.now(UTC))
        self._refresh_access_token = refresh_access_token
        self._mark_expired = mark_expired

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """增量读取可见日历目录，并在最终页返回目录 ``nextSyncToken``。"""
        parameters: dict[str, str] = {"showDeleted": "true"}
        if cursor is not None:
            if cursor == "":
                raise ValueError("calendar directory cursor must not be empty")
            parameters["syncToken"] = cursor
        page_token: str | None = None
        while True:
            values = dict(parameters)
            if page_token is not None:
                values["pageToken"] = page_token
            try:
                payload = self._record(
                    await self.execute_request(values, url=GOOGLE_CALENDAR_LIST_URL)
                )
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 410:
                    # 目录游标失效只影响目录发现；事件游标由应用层按 calendar ID 独立保留。
                    raise CalendarCursorExpiredError("google", "directory") from error
                raise
            page_token = self._optional_string(payload.get("nextPageToken"))
            items = payload.get("items")
            calendars = (
                tuple(
                    self._normalize_calendar(item)
                    for item in items
                    if isinstance(item, dict) and item.get("deleted") is not True
                )
                if isinstance(items, list)
                else ()
            )
            yield CalendarDirectoryPage(
                calendars=calendars,
                next_page_token=page_token,
                next_cursor=self._optional_string(payload.get("nextSyncToken")),
            )
            if page_token is None:
                return

    async def initial_pages(self, calendar_id: str = "primary") -> AsyncIterator[CalendarSyncPage]:
        """读取用户时区下过去一天至未来三十天的初始事件窗口。"""
        self._validate_calendar_id(calendar_id)
        local_now = self._now().astimezone(self._timezone)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        parameters = {
            "singleEvents": "true",
            "showDeleted": "true",
            "timeMin": (local_midnight - timedelta(days=1)).isoformat(),
            "timeMax": (local_midnight + timedelta(days=30)).isoformat(),
        }
        async for page in self._event_pages(calendar_id, parameters):
            yield page

    async def sync_pages(
        self, calendar_id: str, cursor: str | None = None
    ) -> AsyncIterator[CalendarSyncPage]:
        """通过单个日历自己的同步游标读取增量并保留删除 tombstone。

        ``cursor`` 为可选仅用于兼容 M1 的 ``sync_pages(cursor)`` 调用；新代码必须传入
        ``sync_pages(calendar_id, cursor)``，这样失效错误才能绑定到精确日历 scope。
        """
        if cursor is None:
            # 旧 M1 调用只有一个参数，且该参数必然是 primary 的 cursor。
            cursor, calendar_id = calendar_id, "primary"
        self._validate_calendar_id(calendar_id)
        if cursor == "":
            raise ValueError("calendar sync cursor must not be empty")
        async for page in self._event_pages(
            calendar_id,
            {"singleEvents": "true", "showDeleted": "true", "syncToken": cursor},
            expired_scope=calendar_id,
        ):
            yield page

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """精确读取一个当前事件，供提案和恢复准备进行版本核对。"""
        self._validate_calendar_id(calendar_id)
        if provider_event_id == "":
            raise ValueError("provider_event_id must not be empty")
        url = f"{self._events_url(calendar_id)}/{quote(provider_event_id, safe='')}"
        try:
            # ``singleEvents`` 仅属于 events.list；精确 events.get 携带它会被 Google
            # 作为未知查询参数拒绝，因此这里只发送认证 Header 和编码后的资源路径。
            payload = await self.execute_request({}, url=url)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise
        return self._normalize(self._record(payload), calendar_id=calendar_id)

    async def _event_pages(
        self,
        calendar_id: str,
        parameters: dict[str, str],
        *,
        expired_scope: str | None = None,
    ) -> AsyncIterator[CalendarSyncPage]:
        """按 pageToken 读取一个日历，保持请求选项并在最终页返回 opaque 游标。"""
        page_token: str | None = None
        url = self._events_url(calendar_id)
        while True:
            values = dict(parameters)
            if page_token is not None:
                values["pageToken"] = page_token
            try:
                payload = self._record(await self.execute_request(values, url=url))
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 410 and expired_scope is not None:
                    raise CalendarCursorExpiredError("google", expired_scope) from error
                raise
            page_token = self._optional_string(payload.get("nextPageToken"))
            items = payload.get("items")
            events = (
                tuple(
                    self._normalize(item, calendar_id=calendar_id)
                    for item in items
                    if isinstance(item, dict)
                )
                if isinstance(items, list)
                else ()
            )
            yield CalendarSyncPage(
                events,
                page_token,
                self._optional_string(payload.get("nextSyncToken")),
            )
            if page_token is None:
                return

    async def execute_request(
        self,
        parameters: Mapping[str, str],
        *,
        url: str = CALENDAR_EVENTS_URL,
    ) -> object:
        """执行只读请求，401 最多刷新并重试一次，其余暂态错误映射领域类型。"""
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
                    response = await client.get(
                        url,
                        params=dict(parameters),
                        headers={"Authorization": f"Bearer {self._access_token}"},
                    )
            except httpx.TimeoutException as error:
                raise TransientProviderError(
                    error_code="google_timeout", message="Google Calendar request timed out"
                ) from error
            except httpx.RequestError as error:
                raise TransientProviderError(
                    error_code="google_request_failed", message="Google Calendar request failed"
                ) from error
            if response.status_code == 401:
                if attempt == 0 and self._refresh_access_token is not None:
                    self._access_token = await self._refresh_access_token()
                    continue
                if self._mark_expired is not None:
                    await self._mark_expired()
                raise UserActionRequiredError(
                    error_code="google_reauthorization_required",
                    message="Google Calendar authorization requires user action",
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise TransientProviderError(
                    error_code="google_rate_limited"
                    if response.status_code == 429
                    else "google_service_unavailable",
                    message="Google Calendar is temporarily unavailable",
                    retry_after=self._retry_after(response),
                )
            response.raise_for_status()
            return response.json()
        raise AssertionError("Calendar request retry loop exhausted")

    def _normalize_calendar(self, payload: dict[str, object]) -> ProviderCalendar:
        """把 CalendarList 项收窄为目录领域模型，并对未知权限 fail closed。"""
        calendar_id = self._required(payload, "id")
        role = self._optional_string(payload.get("accessRole")) or "unknown"
        timezone = self._optional_string(payload.get("timeZone")) or str(self._timezone)
        display_name = (
            self._optional_string(payload.get("summaryOverride"))
            or self._optional_string(payload.get("summary"))
            or calendar_id
        )
        return ProviderCalendar(
            calendar_id=calendar_id,
            display_name=display_name,
            timezone=timezone,
            is_primary=payload.get("primary") is True,
            access_role=role,
            can_write=role in {"owner", "writer"},
            provider_url=self._optional_string(payload.get("htmlLink")),
        )

    def _normalize(
        self, payload: dict[str, object], *, calendar_id: str = "primary"
    ) -> CalendarEvent:
        """收窄供应商字段，日期事件按源时区当地午夜转为 UTC。"""
        event_id = self._required(payload, "id")
        status = self._optional_string(payload.get("status")) or "confirmed"
        recurring_event_id = self._optional_string(payload.get("recurringEventId"))
        etag = self._optional_string(payload.get("etag"))
        provider_url = self._optional_string(payload.get("htmlLink")) or ""
        updated_at = self._provider_updated_at(payload.get("updated"))
        start_value = payload.get("start")
        end_value = payload.get("end")
        if status == "cancelled" and (
            not isinstance(start_value, dict) or not isinstance(end_value, dict)
        ):
            return CalendarEvent(
                event_id,
                calendar_id,
                "",
                "",
                "",
                None,
                None,
                False,
                "opaque",
                status,
                str(self._timezone),
                recurring_event_id,
                etag,
                provider_url,
                updated_at,
                self._normalize_person(payload.get("organizer")),
                self._normalize_attendees(payload.get("attendees")),
                self._optional_string(payload.get("accessRole")),
                False,
            )
        start, end = self._record(start_value), self._record(end_value)
        timezone = (
            self._optional_string(start.get("timeZone"))
            or self._optional_string(end.get("timeZone"))
            or str(self._timezone)
        )
        all_day = isinstance(start.get("date"), str)
        starts_at = self._event_time(start, timezone, all_day)
        ends_at = self._event_time(end, timezone, all_day)
        return CalendarEvent(
            event_id,
            calendar_id,
            self._optional_string(payload.get("summary")) or "",
            self._optional_string(payload.get("description")) or "",
            self._optional_string(payload.get("location")) or "",
            starts_at,
            ends_at,
            all_day,
            self._optional_string(payload.get("transparency")) or "opaque",
            status,
            timezone,
            recurring_event_id,
            etag,
            provider_url,
            updated_at,
            self._normalize_person(payload.get("organizer")),
            self._normalize_attendees(payload.get("attendees")),
            self._optional_string(payload.get("accessRole")),
            self._event_can_edit(payload, status),
        )

    @staticmethod
    def _events_url(calendar_id: str) -> str:
        """按 RFC 3986 path segment 编码 opaque calendar ID，禁止斜杠改变资源路径。"""
        return f"{GOOGLE_CALENDAR_EVENTS_BASE_URL}/{quote(calendar_id, safe='')}/events"

    @staticmethod
    def _validate_calendar_id(calendar_id: str) -> None:
        """拒绝空日历标识，避免把无目标请求发送给供应商。"""
        if calendar_id == "":
            raise ValueError("calendar_id must not be empty")

    @staticmethod
    def _normalize_person(value: object) -> Mapping[str, str] | None:
        """仅保留可展示的组织者字段，并把供应商布尔 self 标记规范为文本。"""
        if not isinstance(value, dict):
            return None
        normalized: dict[str, str] = {}
        for key in ("email", "displayName", "responseStatus", "comment"):
            item = value.get(key)
            if isinstance(item, str):
                normalized[key] = item
        if isinstance(value.get("self"), bool):
            normalized["self"] = "true" if value["self"] else "false"
        return MappingProxyType(normalized) if normalized else None

    @classmethod
    def _normalize_attendees(cls, value: object) -> tuple[Mapping[str, str], ...]:
        """规范化参会人集合，跳过畸形项而不泄露原始供应商响应。"""
        if not isinstance(value, list):
            return ()
        return tuple(
            person for item in value if (person := cls._normalize_person(item)) is not None
        )

    @staticmethod
    def _event_can_edit(payload: dict[str, object], status: str) -> bool:
        """投影 Google 事件自身是否允许修改主字段。

        Google Event 资源没有 ``canEdit``；账户 ACL 来自 CalendarList，并在仓储层与本值
        取交集。事件侧唯一明确阻断主字段修改的事实是 ``locked=true``，取消 tombstone
        同样不可编辑。缺少 ``locked`` 按 Google 文档默认 false 处理，但若目录事实缺失，
        仓储仍会 fail closed 为不可编辑。
        """
        return status != "cancelled" and payload.get("locked") is not True

    @staticmethod
    def _provider_updated_at(value: object) -> datetime | None:
        """解析供应商 RFC3339 更新时间，保留增量事件的稳定版本事实。"""
        if not isinstance(value, str):
            return None
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _event_time(value: dict[str, object], timezone: str, all_day: bool) -> datetime:
        """解析 RFC3339 datetime 或 ``date`` 并以 UTC 返回，拒绝缺失时间。"""
        raw = value.get("date") if all_day else value.get("dateTime")
        if not isinstance(raw, str):
            raise TypeError("Calendar event time is invalid")
        if all_day:
            return datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        if parsed.tzinfo is None:
            # Google 允许 dateTime 在同时给出 timeZone 时省略 offset；内部事实仍必须统一
            # 转为 UTC，不能把带业务时区的 datetime 直接渗透到应用层。
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
        return parsed.astimezone(UTC)

    @staticmethod
    def _record(value: object) -> dict[str, object]:
        """验证供应商响应对象边界。"""
        if not isinstance(value, dict):
            raise TypeError("Calendar response is invalid")
        return value

    @staticmethod
    def _required(value: dict[str, object], key: str) -> str:
        """读取供应商必需字符串字段。"""
        result = value.get(key)
        if not isinstance(result, str) or result == "":
            raise TypeError(f"Calendar response missing {key}")
        return result

    @staticmethod
    def _optional_string(value: object) -> str | None:
        """将第三方任意值收窄为可选字符串。"""
        return value if isinstance(value, str) else None

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        """解析非负 Retry-After 秒数，畸形供应商头保持未知。"""
        try:
            value = int(response.headers.get("Retry-After", ""))
        except ValueError:
            return None
        return value if value >= 0 else None


# M1 import/factory compatibility; new code should use the explicit class name.
CalendarAdapter = GoogleCalendarAdapter

__all__ = [
    "CALENDAR_EVENTS_URL",
    "GOOGLE_CALENDAR_EVENTS_BASE_URL",
    "GOOGLE_CALENDAR_LIST_URL",
    "CalendarAdapter",
    "GoogleCalendarAdapter",
]
