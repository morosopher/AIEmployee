"""验证 Calendar 只读适配器的窗口请求与事件规范化。"""

from datetime import UTC, datetime

import httpx
import pytest
import respx

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
    assert params["timeMin"].startswith("2030-01-02T00:00:00+08:00")
    event = pages[0].events[0]
    assert event.all_day is True
    assert event.starts_at == datetime(2030, 1, 1, 16, tzinfo=UTC)
    assert event.status == "confirmed"
    assert event.transparency == "transparent"
