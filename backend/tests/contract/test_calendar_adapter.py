"""验证 Calendar 只读适配器的窗口请求与事件规范化。"""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from ai_employee.application.ports.calendar import (
    CalendarCursorExpiredError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.integrations.google.calendar import CalendarAdapter


@pytest.mark.asyncio
@respx.mock
async def test_initial_page_uses_primary_events_window_and_normalizes_all_day() -> None:
    """初次读取传递 Google 必需参数，并将全天事件转换为 UTC 午夜边界。"""
    route = respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "synthetic-all-day",
                        "summary": "Synthetic holiday",
                        "description": "private description",
                        "location": "private location",
                        "start": {"date": "2030-01-02"},
                        "end": {"date": "2030-01-03"},
                        "status": "confirmed",
                        "transparency": "transparent",
                        "etag": "etag-1",
                        "htmlLink": "https://calendar.google.test/event",
                    }
                ],
                "nextSyncToken": "sync-101",
            },
        )
    )
    adapter = CalendarAdapter(
        access_token="synthetic", user_timezone="Asia/Shanghai", now=lambda: datetime(2030, 1, 9, tzinfo=UTC)
    )

    pages = [page async for page in adapter.initial_pages()]

    params = route.calls[0].request.url.params
    assert params["singleEvents"] == "true"
    assert params["showDeleted"] == "true"
    assert params["timeMin"].startswith("2030-01-09T00:00:00+08:00")
    assert params["timeMax"].startswith("2030-01-16T00:00:00+08:00")
    event = pages[0].events[0]
    assert event.all_day is True
    assert event.starts_at == datetime(2030, 1, 1, 16, tzinfo=UTC)
    assert event.status == "confirmed"
    assert event.transparency == "transparent"


@pytest.mark.asyncio
@respx.mock
async def test_calendar_paginates_and_only_final_page_has_sync_token() -> None:
    """分页请求保留初始窗口参数，最终 token 不会被前页提前使用。"""
    route = respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
        side_effect=[
            httpx.Response(200, json={"items": [], "nextPageToken": "page-2"}),
            httpx.Response(200, json={"items": [], "nextSyncToken": "final-token"}),
        ]
    )
    pages = [page async for page in CalendarAdapter(access_token="x", user_timezone="UTC").initial_pages()]
    assert len(pages) == 2 and pages[0].next_sync_token is None and pages[-1].next_sync_token == "final-token"
    assert route.calls[1].request.url.params["pageToken"] == "page-2"


@pytest.mark.asyncio
@respx.mock
async def test_calendar_410_becomes_cursor_expired() -> None:
    """失效 syncToken 必须由用例识别为窗口重同步信号。"""
    respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(return_value=httpx.Response(410))
    with pytest.raises(CalendarCursorExpiredError):
        [page async for page in CalendarAdapter(access_token="x", user_timezone="UTC").sync_pages("old")]


@pytest.mark.asyncio
@respx.mock
async def test_calendar_401_refreshes_once_then_requires_action() -> None:
    """资源 401 仅刷新一次，第二次拒绝会标记连接为需要重连。"""
    refreshed: list[bool] = []
    expired: list[bool] = []
    async def refresh() -> str:
        refreshed.append(True); return "new"
    async def mark_expired() -> None:
        expired.append(True)
    respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(side_effect=[httpx.Response(401), httpx.Response(401)])
    with pytest.raises(UserActionRequiredError):
        await CalendarAdapter(access_token="x", user_timezone="UTC", refresh_access_token=refresh, mark_expired=mark_expired).execute_request({})
    assert refreshed == [True] and expired == [True]


@pytest.mark.asyncio
@respx.mock
async def test_calendar_rate_limit_is_typed_transient_error() -> None:
    """429/Retry-After 映射 Durable Worker 可重试的领域错误。"""
    respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(return_value=httpx.Response(429, headers={"Retry-After": "12"}))
    with pytest.raises(TransientProviderError) as raised:
        await CalendarAdapter(access_token="x", user_timezone="UTC").execute_request({})
    assert raised.value.error_code == "google_rate_limited" and raised.value.retry_after == 12
