"""验证 Microsoft Graph CalendarView Delta 的安全 HTTP 与规范化契约。"""

from __future__ import annotations

import copy
import importlib
import json
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from ai_employee.application.ports.calendar import CalendarCursorExpiredError
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
CALENDARS_URL = f"{GRAPH_BASE_URL}/me/calendars"
CALENDAR_ID = "calendar-primary"
DELTA_URL = f"{CALENDARS_URL}/{CALENDAR_ID}/calendarView/delta"
NEXT_URL = f"{DELTA_URL}?$skiptoken=synthetic-next-1"
FINAL_DELTA_URL = f"{DELTA_URL}?$deltatoken=synthetic-delta-2"


def _module():
    """延迟导入待实现 adapter，使 RED 阶段保持可收集。"""
    return importlib.import_module("ai_employee.integrations.microsoft.calendar")


def _fixture(name: str) -> dict[str, object]:
    """读取脱敏 Graph fixture，并返回可安全修改的副本。"""
    value = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def _adapter(**kwargs: object):
    """构造固定合成 token 与显式用户时区的 adapter。"""
    return _module().MicrosoftCalendarAdapter(
        access_token="synthetic-access-token",
        user_timezone="Asia/Shanghai",
        now=lambda: datetime(2030, 1, 9, 8, tzinfo=UTC),
        **kwargs,
    )


async def _collect(pages):
    """完整消费异步迭代器，触发所有延迟分页校验。"""
    return tuple([page async for page in pages])


@pytest.mark.asyncio
@respx.mock
async def test_directory_uses_exact_select_and_projects_default_owner_capabilities() -> None:
    """目录必须保留 default、owner、hexColor，并对能力字段 fail closed。"""
    route = respx.get(CALENDARS_URL).respond(200, json=_fixture("calendars.json"))
    pages = await _collect(_adapter().directory_pages())
    assert len(pages) == 1
    primary, readonly = pages[0].calendars
    assert primary.is_primary is True and primary.can_write is True
    assert primary.can_share is True
    assert primary.hex_color == "#123456"
    assert primary.owner["email"] == "owner@example.test"
    assert readonly.is_primary is False and readonly.can_write is False
    assert readonly.can_share is False
    assert (
        pages[0].next_cursor
        == "https://graph.microsoft.com/v1.0/me/calendars?$deltatoken=synthetic-directory-final"
    )
    assert (
        route.calls[0].request.url.params["$select"]
        == "id,name,isDefaultCalendar,canEdit,canShare,owner,hexColor"
    )


@pytest.mark.asyncio
@respx.mock
async def test_initial_delta_uses_local_window_and_normalizes_versions_people_recurrence() -> None:
    """初始窗口按用户时区计算，事件时间统一 UTC 并保留版本/人员/重复事实。"""
    first = respx.get(
        DELTA_URL,
        params={
            "startDateTime": "2030-01-08T00:00:00+08:00",
            "endDateTime": "2030-02-08T00:00:00+08:00",
        },
    ).respond(200, json=_fixture("calendar_view_delta_initial.json"))
    respx.get(NEXT_URL).respond(200, json=_fixture("calendar_view_delta_incremental.json"))
    pages = await _collect(_adapter().initial_pages(CALENDAR_ID))
    assert len(pages) == 2
    params = parse_qs(first.calls[0].request.url.query.decode("ascii"))
    assert params["startDateTime"][0].startswith("2030-01-08T00:00:00+08:00")
    assert params["endDateTime"][0].startswith("2030-02-08T00:00:00+08:00")
    event = pages[0].events[0]
    assert event.starts_at == datetime(2030, 1, 2, 1, tzinfo=UTC)
    assert event.timezone == "Asia/Shanghai"
    assert event.etag == 'W/"synthetic-etag"'
    assert event.change_key == "synthetic-change-key-1"
    assert event.recurring_event_id == "series-1"
    assert event.organizer == {"name": "Synthetic Organizer", "email": "organizer@example.test"}
    assert event.attendees[0]["email"] == "attendee@example.test"
    assert pages[0].next_page_token == NEXT_URL and pages[0].next_cursor is None
    assert pages[1].next_page_token is None and pages[1].next_cursor == FINAL_DELTA_URL


@pytest.mark.asyncio
@respx.mock
async def test_incremental_keeps_safe_tombstone_and_all_day_boundaries() -> None:
    """Graph removed 项只保留 ID/status；全天日期不得伪造供应商时间。"""
    respx.get(FINAL_DELTA_URL).respond(200, json=_fixture("calendar_view_delta_incremental.json"))
    pages = await _collect(_adapter().sync_pages(CALENDAR_ID, FINAL_DELTA_URL))
    tombstone, all_day = pages[0].events
    assert tombstone.status == "cancelled"
    assert tombstone.starts_at is None and tombstone.ends_at is None
    assert all_day.all_day is True
    assert all_day.starts_at == datetime(2030, 1, 3, 8, tzinfo=UTC)
    assert all_day.ends_at == datetime(2030, 1, 4, 8, tzinfo=UTC)


@pytest.mark.asyncio
@respx.mock
async def test_current_event_get_404_returns_none_and_preserves_exact_path() -> None:
    """精确 GET 使用同一日历 scope，404 安全映射为 None。"""
    route = respx.get(f"{CALENDARS_URL}/{CALENDAR_ID}/events/missing-event").respond(404)
    assert await _adapter().get_current_event(CALENDAR_ID, "missing-event") is None
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_delta_rejects_wrong_host_or_calendar_without_requesting_target() -> None:
    """next/delta URL 必须绑定 Graph 主机和精确 calendar path。"""
    payload = _fixture("calendar_view_delta_initial.json")
    payload["@odata.nextLink"] = "https://attacker.example.test/collect?token=secret"
    respx.get(DELTA_URL).respond(200, json=payload)
    attacker = respx.get("https://attacker.example.test/collect").respond(200, json={})
    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().initial_pages(CALENDAR_ID))
    assert raised.value.error_code == "microsoft_calendar_invalid_delta_url"
    assert not attacker.called and "secret" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_delta_cycle_or_missing_cursor_is_rejected() -> None:
    """循环分页、同时 next/delta 或最终缺 cursor 都不能推进同步。"""
    payload = _fixture("calendar_view_delta_initial.json")
    payload["@odata.nextLink"] = NEXT_URL
    respx.get(DELTA_URL).respond(200, json=payload)
    cycle = _fixture("calendar_view_delta_initial.json")
    cycle.pop("@odata.nextLink", None)
    cycle.pop("@odata.deltaLink", None)
    respx.get(NEXT_URL).respond(200, json=cycle)
    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().initial_pages(CALENDAR_ID))
    assert raised.value.error_code in {
        "microsoft_calendar_delta_missing_cursor",
        "microsoft_calendar_pagination_invalid",
    }


@pytest.mark.asyncio
@respx.mock
async def test_directory_rejects_next_and_delta_links_in_same_page() -> None:
    """目录页不能同时声明分页和最终游标，避免游标语义歧义。"""
    payload = _fixture("calendars.json")
    payload["@odata.nextLink"] = (
        "https://graph.microsoft.com/v1.0/me/calendars?$skiptoken=synthetic-next"
    )
    route = respx.get(CALENDARS_URL).respond(200, json=payload)
    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().directory_pages())
    assert raised.value.error_code == "microsoft_calendar_pagination_invalid"
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor",
    (
        "http://graph.microsoft.com/v1.0/me/calendars?$deltatoken=synthetic",
        " https://graph.microsoft.com/v1.0/me/calendars?$deltatoken=synthetic",
        "https://attacker.example.test/v1.0/me/calendars?$deltatoken=synthetic",
        "https://graph.microsoft.com/v1.0/me/messages?$deltatoken=synthetic",
    ),
)
@respx.mock
async def test_directory_cursor_must_be_validated_before_any_request(cursor: str) -> None:
    """目录增量 cursor 必须是 Graph 最终 deltaLink，不能降级为任意 token。"""
    route = respx.get(CALENDARS_URL).respond(200, json=_fixture("calendars.json"))
    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().directory_pages(cursor))
    assert raised.value.error_code == "microsoft_calendar_invalid_directory_url"
    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_error_mapping_refresh_403_429_5xx_and_cursor_expiry() -> None:
    """Graph 常见错误必须落入稳定领域错误类别。"""
    refresh_calls = 0

    async def refresh() -> str:
        nonlocal refresh_calls
        refresh_calls += 1
        return "synthetic-refreshed-token"

    route = respx.get(CALENDARS_URL).mock(side_effect=[httpx.Response(401), httpx.Response(401)])
    with pytest.raises(UserActionRequiredError) as raised:
        await _collect(_adapter(refresh_access_token=refresh).directory_pages())
    assert raised.value.error_code == "microsoft_reauthorization_required"
    assert refresh_calls == 1 and route.call_count == 2

    respx.get(CALENDARS_URL).respond(403, json={"error": {"code": "accessDenied"}})
    with pytest.raises(UserActionRequiredError) as raised:
        await _collect(_adapter().directory_pages())
    assert raised.value.error_code == "microsoft_calendar_permission_required"

    respx.get(CALENDARS_URL).respond(429, headers={"Retry-After": "7"})
    with pytest.raises(TransientProviderError) as raised:
        await _collect(_adapter().directory_pages())
    assert raised.value.error_code == "microsoft_calendar_rate_limited"
    assert raised.value.retry_after == 7

    respx.get(CALENDARS_URL).respond(503, text="sensitive-body")
    with pytest.raises(TransientProviderError) as raised:
        await _collect(_adapter().directory_pages())
    assert raised.value.error_code == "microsoft_calendar_service_unavailable"
    assert "sensitive-body" not in str(raised.value)

    respx.get(FINAL_DELTA_URL).respond(410, json={"error": {"code": "syncStateNotFound"}})
    with pytest.raises(CalendarCursorExpiredError) as raised:
        await _collect(_adapter().sync_pages(CALENDAR_ID, FINAL_DELTA_URL))
    assert raised.value.provider == "microsoft" and raised.value.scope_key == CALENDAR_ID


@pytest.mark.asyncio
@respx.mock
async def test_timeout_traceback_does_not_echo_cursor() -> None:
    """网络异常转换为暂态错误且不回显 opaque cursor 或底层文本。"""
    respx.get(FINAL_DELTA_URL).mock(
        side_effect=httpx.ReadTimeout(
            "sensitive-network", request=httpx.Request("GET", FINAL_DELTA_URL)
        )
    )
    with pytest.raises(TransientProviderError) as raised:
        await _collect(_adapter().sync_pages(CALENDAR_ID, FINAL_DELTA_URL))
    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.error_code == "microsoft_calendar_timeout"
    assert FINAL_DELTA_URL not in rendered and "sensitive-network" not in rendered
