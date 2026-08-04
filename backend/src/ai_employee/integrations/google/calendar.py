"""实现 Google Calendar Events REST 读取与严格错误分类。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from ai_employee.application.ports.calendar import (
    CalendarCursorExpiredError,
    CalendarEvent,
    CalendarSyncPage,
    TransientProviderError,
    UserActionRequiredError,
)

CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_RefreshAccessToken = Callable[[], Awaitable[str]]
_MarkExpired = Callable[[], Awaitable[None]]


class CalendarAdapter:
    """以 httpx 调用 Calendar 只读事件端点并隔离供应商 JSON。"""

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
        self._access_token, self._timezone, self._now = (
            access_token,
            ZoneInfo(user_timezone),
            now or (lambda: datetime.now(UTC)),
        )
        self._refresh_access_token, self._mark_expired = refresh_access_token, mark_expired

    async def initial_pages(self) -> AsyncIterator[CalendarSyncPage]:
        """读取用户当地午夜起的七天 horizon，并让最终页携带 nextSyncToken。"""
        local_now = self._now().astimezone(self._timezone)
        start = (local_now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        parameters = {
            "singleEvents": "true",
            "showDeleted": "true",
            "timeMin": start.isoformat(),
            "timeMax": local_now.isoformat(),
        }
        async for page in self._pages(parameters):
            yield page

    async def sync_pages(self, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """通过同步游标读取变更；410 转为受控回退信号。"""
        try:
            async for page in self._pages(
                {"singleEvents": "true", "showDeleted": "true", "syncToken": cursor}
            ):
                yield page
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 410:
                raise CalendarCursorExpiredError from error
            raise

    async def _pages(self, parameters: dict[str, str]) -> AsyncIterator[CalendarSyncPage]:
        """按 pageToken 读取响应，保持初始窗口参数在每页一致。"""
        page_token: str | None = None
        while True:
            values = dict(parameters)
            if page_token is not None:
                values["pageToken"] = page_token
            payload = self._record(await self.execute_request(values))
            page_token = self._optional_string(payload.get("nextPageToken"))
            items = payload.get("items")
            events = (
                tuple(self._normalize(item) for item in items if isinstance(item, dict))
                if isinstance(items, list)
                else ()
            )
            yield CalendarSyncPage(
                events, page_token, self._optional_string(payload.get("nextSyncToken"))
            )
            if page_token is None:
                return

    async def execute_request(self, parameters: dict[str, str]) -> object:
        """执行只读请求，401 最多刷新并重试一次，其余暂态错误映射领域类型。"""
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
                    response = await client.get(
                        CALENDAR_EVENTS_URL,
                        params=parameters,
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

    def _normalize(self, payload: dict[str, object]) -> CalendarEvent:
        """收窄供应商字段，日期事件按源时区当地午夜转为 UTC。"""
        event_id, etag = self._required(payload, "id"), self._required(payload, "etag")
        start, end = self._record(payload.get("start")), self._record(payload.get("end"))
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
            "primary",
            self._optional_string(payload.get("summary")) or "",
            self._optional_string(payload.get("description")) or "",
            self._optional_string(payload.get("location")) or "",
            starts_at,
            ends_at,
            all_day,
            self._optional_string(payload.get("transparency")) or "opaque",
            self._optional_string(payload.get("status")) or "confirmed",
            timezone,
            self._optional_string(payload.get("recurringEventId")),
            etag,
            self._optional_string(payload.get("htmlLink")) or "",
        )

    @staticmethod
    def _event_time(value: dict[str, object], timezone: str, all_day: bool) -> datetime:
        """解析 RFC3339 datetime 或 ``date`` 并以 UTC 返回，拒绝缺失时间。"""
        raw = value.get("date") if all_day else value.get("dateTime")
        if not isinstance(raw, str):
            raise TypeError("Calendar event time is invalid")
        if all_day:
            return datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        return (
            parsed.replace(tzinfo=ZoneInfo(timezone))
            if parsed.tzinfo is None
            else parsed.astimezone(UTC)
        )

    @staticmethod
    def _record(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise TypeError("Calendar response is invalid")
        return value

    @staticmethod
    def _required(value: dict[str, object], key: str) -> str:
        result = value.get(key)
        if not isinstance(result, str):
            raise TypeError(f"Calendar response missing {key}")
        return result

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        try:
            value = int(response.headers.get("Retry-After", ""))
        except ValueError:
            return None
        return value if value >= 0 else None
