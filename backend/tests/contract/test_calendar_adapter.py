"""验证 Google Calendar 目录、分日历事件读取与规范化边界。"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from ai_employee.application.ports.calendar import (
    CalendarCursorExpiredError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.integrations.google.calendar import (
    GOOGLE_CALENDAR_LIST_URL,
    CalendarAdapter,
    GoogleCalendarAdapter,
)

FIXTURES = Path(__file__).parent / "fixtures"


def calendar_list_fixture() -> dict[str, object]:
    """读取脱敏 CalendarList fixture，测试不得依赖真实账户数据。"""
    return json.loads((FIXTURES / "google_calendar_list.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
@respx.mock
async def test_google_calendar_directory_normalizes_each_calendar_and_write_role() -> None:
    """CalendarList 中每个 opaque ID 都必须独立输出，未知 accessRole 默认只读。"""
    route = respx.get(GOOGLE_CALENDAR_LIST_URL).mock(
        return_value=httpx.Response(200, json=calendar_list_fixture())
    )
    adapter = GoogleCalendarAdapter(access_token="synthetic", user_timezone="UTC")

    pages = [page async for page in adapter.directory_pages()]

    assert [(item.calendar_id, item.can_write) for item in pages[0].calendars] == [
        ("primary", True),
        ("readonly@example.test", False),
    ]
    assert pages[0].next_cursor == "directory-token-1"
    assert route.calls[0].request.url.params["showDeleted"] == "true"


@pytest.mark.asyncio
@respx.mock
async def test_google_calendar_directory_paginates_and_preserves_sync_token() -> None:
    """目录分页必须携带 pageToken，只有最终页承载目录 nextSyncToken。"""
    route = respx.get(GOOGLE_CALENDAR_LIST_URL).mock(
        side_effect=[
            httpx.Response(200, json={"items": [], "nextPageToken": "directory-page-2"}),
            httpx.Response(200, json={"items": [], "nextSyncToken": "directory-final"}),
        ]
    )
    adapter = GoogleCalendarAdapter(access_token="synthetic", user_timezone="UTC")

    pages = [page async for page in adapter.directory_pages("old-directory-token")]

    assert len(pages) == 2
    assert pages[0].next_cursor is None and pages[-1].next_cursor == "directory-final"
    assert route.calls[0].request.url.params["syncToken"] == "old-directory-token"
    assert route.calls[1].request.url.params["pageToken"] == "directory-page-2"


@pytest.mark.asyncio
@respx.mock
async def test_google_calendar_events_use_quoted_calendar_id_and_bounded_local_window() -> None:
    """事件 URL 必须按 calendar ID 编码，初始窗口为本地午夜前一天至后 30 天。"""
    calendar_id = "team/a@example.test"
    route = respx.get(
        "https://www.googleapis.com/calendar/v3/calendars/team%2Fa%40example.test/events"
    ).mock(return_value=httpx.Response(200, json={"items": [], "nextSyncToken": "event-token"}))
    adapter = GoogleCalendarAdapter(
        access_token="synthetic",
        user_timezone="Asia/Shanghai",
        now=lambda: datetime(2030, 1, 9, 16, tzinfo=UTC),
    )

    pages = [page async for page in adapter.initial_pages(calendar_id)]

    assert pages[0].next_cursor == "event-token"
    params = route.calls[0].request.url.params
    assert params["singleEvents"] == "true"
    assert params["showDeleted"] == "true"
    assert params["timeMin"].startswith("2030-01-09T00:00:00+08:00")
    assert params["timeMax"].startswith("2030-02-09T00:00:00+08:00")


@pytest.mark.asyncio
@respx.mock
async def test_google_calendar_event_normalizes_attendees_organizer_and_current_get() -> None:
    """事件列表与精确 GET 必须共享规范化，保留 ETag、组织者和参会人事实。"""
    calendar_id = "readonly@example.test"
    event_id = "event/with-slash"
    payload = {
        "id": event_id,
        "summary": "Synthetic event",
        "start": {"dateTime": "2030-01-02T09:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2030-01-02T10:00:00Z", "timeZone": "UTC"},
        "status": "confirmed",
        "locked": True,
        "etag": "etag-current",
        "updated": "2030-01-01T00:00:00Z",
        "htmlLink": "https://calendar.google.test/event/current",
        "organizer": {"email": "organizer@example.test", "displayName": "Organizer"},
        "attendees": [
            {"email": "attendee@example.test", "responseStatus": "accepted"},
        ],
    }
    route = respx.get(
        "https://www.googleapis.com/calendar/v3/calendars/readonly%40example.test/events/event%2Fwith-slash"
    ).mock(return_value=httpx.Response(200, json=payload))
    adapter = GoogleCalendarAdapter(access_token="synthetic", user_timezone="UTC")

    event = await adapter.get_current_event(calendar_id, event_id)

    assert event is not None
    assert event.calendar_id == calendar_id
    assert dict(event.organizer or {}) == payload["organizer"]
    assert [dict(item) for item in event.attendees] == payload["attendees"]
    assert event.etag == "etag-current"
    assert event.can_edit is False
    assert "singleEvents" not in route.calls[0].request.url.params


def test_google_calendar_naive_datetime_uses_explicit_event_timezone_and_returns_utc() -> None:
    """无 offset 的 Google dateTime 必须使用事件 IANA 时区解释后统一转为 UTC。"""
    event = GoogleCalendarAdapter(
        access_token="synthetic",
        user_timezone="UTC",
    )._normalize(
        {
            "id": "timezone-event",
            "start": {
                "dateTime": "2030-01-02T09:00:00",
                "timeZone": "Asia/Shanghai",
            },
            "end": {
                "dateTime": "2030-01-02T10:00:00",
                "timeZone": "Asia/Shanghai",
            },
        }
    )

    assert event.starts_at == datetime(2030, 1, 2, 1, tzinfo=UTC)
    assert event.ends_at == datetime(2030, 1, 2, 2, tzinfo=UTC)
    assert event.starts_at.tzinfo is UTC
    assert event.ends_at.tzinfo is UTC
    assert event.can_edit is True


@pytest.mark.asyncio
@respx.mock
async def test_google_calendar_directory_410_is_scoped_to_directory() -> None:
    """CalendarList 410 只能要求目录重置，不能伪装成某个事件日历失效。"""
    respx.get(GOOGLE_CALENDAR_LIST_URL).mock(return_value=httpx.Response(410))
    adapter = GoogleCalendarAdapter(access_token="synthetic", user_timezone="UTC")

    with pytest.raises(CalendarCursorExpiredError) as raised:
        [page async for page in adapter.directory_pages("old-directory-token")]

    assert raised.value.provider == "google"
    assert raised.value.scope_key == "directory"


@pytest.mark.asyncio
@respx.mock
async def test_google_calendar_event_410_is_scoped_to_one_calendar() -> None:
    """事件 syncToken 410 只能清除请求的单个日历 scope。"""
    calendar_id = "readonly@example.test"
    respx.get(
        "https://www.googleapis.com/calendar/v3/calendars/readonly%40example.test/events"
    ).mock(return_value=httpx.Response(410))
    adapter = GoogleCalendarAdapter(access_token="synthetic", user_timezone="UTC")

    with pytest.raises(CalendarCursorExpiredError) as raised:
        [page async for page in adapter.sync_pages(calendar_id, "old-event-token")]

    assert raised.value.provider == "google"
    assert raised.value.scope_key == calendar_id


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
        access_token="synthetic",
        user_timezone="Asia/Shanghai",
        now=lambda: datetime(2030, 1, 9, tzinfo=UTC),
    )

    pages = [page async for page in adapter.initial_pages()]

    params = route.calls[0].request.url.params
    assert params["singleEvents"] == "true"
    assert params["showDeleted"] == "true"
    assert params["timeMin"].startswith("2030-01-08T00:00:00+08:00")
    assert params["timeMax"].startswith("2030-02-08T00:00:00+08:00")
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
    pages = [
        page
        async for page in CalendarAdapter(access_token="x", user_timezone="UTC").initial_pages()
    ]
    assert (
        len(pages) == 2
        and pages[0].next_sync_token is None
        and pages[-1].next_sync_token == "final-token"
    )
    assert route.calls[1].request.url.params["pageToken"] == "page-2"


@pytest.mark.asyncio
@respx.mock
async def test_calendar_410_becomes_cursor_expired() -> None:
    """失效 syncToken 必须由用例识别为窗口重同步信号。"""
    respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
        return_value=httpx.Response(410)
    )
    with pytest.raises(CalendarCursorExpiredError):
        [
            page
            async for page in CalendarAdapter(access_token="x", user_timezone="UTC").sync_pages(
                "old"
            )
        ]


@pytest.mark.asyncio
@respx.mock
async def test_calendar_401_refreshes_once_then_requires_action() -> None:
    """资源 401 仅刷新一次，第二次拒绝会标记连接为需要重连。"""
    refreshed: list[bool] = []
    expired: list[bool] = []

    async def refresh() -> str:
        refreshed.append(True)
        return "new"

    async def mark_expired() -> None:
        expired.append(True)

    respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
        side_effect=[httpx.Response(401), httpx.Response(401)]
    )
    with pytest.raises(UserActionRequiredError):
        await CalendarAdapter(
            access_token="x",
            user_timezone="UTC",
            refresh_access_token=refresh,
            mark_expired=mark_expired,
        ).execute_request({})
    assert refreshed == [True] and expired == [True]


@pytest.mark.asyncio
@respx.mock
async def test_calendar_rate_limit_is_typed_transient_error() -> None:
    """429/Retry-After 映射 Durable Worker 可重试的领域错误。"""
    respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "12"})
    )
    with pytest.raises(TransientProviderError) as raised:
        await CalendarAdapter(access_token="x", user_timezone="UTC").execute_request({})
    assert raised.value.error_code == "google_rate_limited" and raised.value.retry_after == 12


def test_minimal_cancelled_tombstone_does_not_invent_event_times() -> None:
    """Google 增量删除可只给 ID；适配器必须保留 tombstone 而不能伪造旧日程时段。"""
    event = CalendarAdapter(access_token="x", user_timezone="UTC")._normalize(
        {"id": "deleted-1", "status": "cancelled", "recurringEventId": "series-1"}
    )
    assert event.status == "cancelled" and event.starts_at is None and event.ends_at is None
