"""验证 Google Calendar 可信写入适配器的 HTTP、ETag 与三态结果契约。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import respx

from ai_employee.application.ports.trusted_actions import ExecutionReference
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.calendar_actions import (
    CalendarCreateCommand,
    CalendarRestoreCommand,
    CalendarUpdateCommand,
    NotificationPolicy,
    calendar_client_event_id,
)
from ai_employee.integrations.google.calendar_write import (
    GOOGLE_CALENDAR_API_BASE_URL,
    GoogleCalendarWriteAdapter,
)

FIXTURES = Path(__file__).parent / "fixtures"
OPERATION_ID = UUID("00000000-0000-0000-0000-000000000001")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000002")
SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000003")
CALENDAR_ID = "primary"
EVENT_ID = "event-1"
GOOGLE_EVENT_URL = f"{GOOGLE_CALENDAR_API_BASE_URL}/calendars/{CALENDAR_ID}/events/{EVENT_ID}"


def _fixture(name: str = "calendar_event_write.json") -> dict[str, object]:
    """读取合成 Google Calendar 写入响应 fixture。"""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _common(*, policy: NotificationPolicy = NotificationPolicy.ALL) -> dict[str, object]:
    """构造三类日历命令共享的完整期望状态。"""
    return {
        "schema_version": "calendar_create.v1",
        "action": "calendar.create",
        "operation_id": OPERATION_ID,
        "connection_id": CONNECTION_ID,
        "calendar_id": CALENDAR_ID,
        "title": "Synthetic calendar event",
        "description": "Synthetic description",
        "location": "Synthetic room",
        "starts_at": datetime(2030, 1, 2, 9, tzinfo=UTC),
        "ends_at": datetime(2030, 1, 2, 10, tzinfo=UTC),
        "timezone": "UTC",
        "all_day": False,
        "attendees": ("attendee@example.test",),
        "notification_policy": policy,
    }


def _create(*, policy: NotificationPolicy = NotificationPolicy.ALL) -> CalendarCreateCommand:
    """构造稳定创建命令。"""
    return CalendarCreateCommand(
        **_common(policy=policy),
        client_event_id=calendar_client_event_id(OPERATION_ID),
    )


def _update(
    *,
    base_etag: str = '"etag-1"',
    policy: NotificationPolicy = NotificationPolicy.ALL,
    action: str = "calendar.update",
) -> CalendarUpdateCommand:
    """构造条件修改命令。"""
    values = _common(policy=policy)
    values.update(
        schema_version="calendar_update.v1",
        action=action,
        provider_event_id=EVENT_ID,
        base_etag=base_etag,
        before_snapshot_id=SNAPSHOT_ID,
        changed_fields=("title",),
    )
    values.pop("client_event_id", None)
    return CalendarUpdateCommand(**values)


def _restore(
    *, base_etag: str = '"etag-1"', policy: NotificationPolicy = NotificationPolicy.ALL
) -> CalendarRestoreCommand:
    """构造恢复命令。"""
    values = _common(policy=policy)
    values.update(
        schema_version="calendar_restore.v1",
        action="calendar.restore",
        provider_event_id=EVENT_ID,
        base_etag=base_etag,
        before_snapshot_id=SNAPSHOT_ID,
        changed_fields=("title",),
    )
    return CalendarRestoreCommand(**values)


def _execution(
    command: CalendarCreateCommand | CalendarUpdateCommand | CalendarRestoreCommand,
) -> ExecutionReference:
    """构造只读核对所需的内容无关执行引用。"""
    return ExecutionReference(
        execution_id=UUID("00000000-0000-0000-0000-000000000004"),
        task_id=UUID("00000000-0000-0000-0000-000000000005"),
        step_id=UUID("00000000-0000-0000-0000-000000000006"),
        approval_id=UUID("00000000-0000-0000-0000-000000000007"),
        operation_id=command.operation_id,
        provider="google",
        tool_name=command.action,
        idempotency_key="synthetic-idempotency-key",
        request_payload_hash="a" * 64,
        status=ToolExecutionStatus.RECONCILING,
        result_summary=None,
        request_started_at=None,
        write_attempt_count=1,
        provider_resource_id=None,
        provider_request_id=None,
        correlation_id=None,
    )


def _adapter() -> GoogleCalendarWriteAdapter:
    """构造使用合成 bearer token 的 adapter。"""
    return GoogleCalendarWriteAdapter(access_token="synthetic-access-token")


def test_calendar_client_id_is_deterministic_google_base32hex() -> None:
    """创建标识必须稳定且只使用 Google 允许的 base32hex 字符。"""
    first = calendar_client_event_id(OPERATION_ID)
    second = calendar_client_event_id(OPERATION_ID)

    assert first == second
    assert re.fullmatch(r"[0-9a-v]+", first)
    assert "-" not in first


def test_validate_for_approval_is_pure_and_warns_for_send_updates_none() -> None:
    """预检不发 HTTP，并将 Google none 通知语义显式变成 warning。"""
    with respx.mock:
        route = respx.route().mock(return_value=httpx.Response(500))
        result = _adapter().validate_for_approval(_create(policy=NotificationPolicy.NONE))

    assert [warning.value for warning in result.warnings] == [
        "google_send_updates_none_external_sync"
    ]
    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_create_posts_stable_id_target_calendar_and_full_body() -> None:
    """创建必须定位精确日历、传稳定 ID、扩展关联属性和完整状态。"""
    route = respx.post(f"{GOOGLE_CALENDAR_API_BASE_URL}/calendars/{CALENDAR_ID}/events").mock(
        return_value=httpx.Response(200, json=_fixture())
    )

    outcome = await _adapter().execute(_create())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    request = route.calls[0].request
    assert request.url.params["sendUpdates"] == "all"
    payload = json.loads(request.content)
    assert payload["id"] == calendar_client_event_id(OPERATION_ID)
    assert payload["extendedProperties"]["private"]["ai_employee_operation_id"] == str(OPERATION_ID)
    assert payload["summary"] == "Synthetic calendar event"
    assert payload["start"] == {"dateTime": "2030-01-02T09:00:00+00:00", "timeZone": "UTC"}
    assert payload["end"] == {"dateTime": "2030-01-02T10:00:00+00:00", "timeZone": "UTC"}
    assert "recurrence" not in payload
    assert "conferenceData" not in payload


@pytest.mark.asyncio
@respx.mock
async def test_create_none_uses_send_updates_none_and_preserves_warning_metadata() -> None:
    """``notification_policy=none`` 必须映射 Google 查询参数而非静默丢失语义。"""
    route = respx.post(f"{GOOGLE_CALENDAR_API_BASE_URL}/calendars/{CALENDAR_ID}/events").mock(
        return_value=httpx.Response(200, json=_fixture())
    )

    outcome = await _adapter().execute(_create(policy=NotificationPolicy.NONE))

    assert route.calls[0].request.url.params["sendUpdates"] == "none"
    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED


@pytest.mark.asyncio
@respx.mock
async def test_update_reads_and_compares_etag_before_put() -> None:
    """当前 ETag 与冻结版本不同时不得 PUT，并返回终态版本冲突。"""
    get_route = respx.get(GOOGLE_EVENT_URL).mock(
        return_value=httpx.Response(200, json={"id": EVENT_ID, "etag": '"etag-2"'})
    )
    put_route = respx.put(GOOGLE_EVENT_URL).mock(
        return_value=httpx.Response(200, json={**_fixture(), "id": EVENT_ID})
    )

    outcome = await _adapter().execute(_update(base_etag='"etag-1"'))

    assert get_route.called
    assert put_route.called is False
    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.error_code == "calendar_event_version_conflict"


@pytest.mark.asyncio
@respx.mock
async def test_update_puts_complete_desired_state_with_if_match() -> None:
    """ETag 相同时更新必须带 If-Match、通知参数和完整期望状态。"""
    respx.get(GOOGLE_EVENT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"id": EVENT_ID, "etag": '"etag-1"', "status": "confirmed"},
        )
    )
    put_route = respx.put(GOOGLE_EVENT_URL).mock(
        return_value=httpx.Response(200, json={**_fixture(), "id": EVENT_ID})
    )

    outcome = await _adapter().execute(_update())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    request = put_route.calls[0].request
    assert request.headers["If-Match"] == '"etag-1"'
    assert request.url.params["sendUpdates"] == "all"
    payload = json.loads(request.content)
    assert "id" not in payload
    assert payload["summary"] == "Synthetic calendar event"
    assert payload["extendedProperties"]["private"]["ai_employee_operation_id"] == str(OPERATION_ID)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("field", "response"),
    (
        ("deleted", {"id": EVENT_ID, "etag": '"etag-1"', "deleted": True}),
        ("recurring", {"id": EVENT_ID, "etag": '"etag-1"', "recurrence": ["RRULE:FREQ=DAILY"]}),
        ("locked", {"id": EVENT_ID, "etag": '"etag-1"', "locked": True}),
    ),
)
async def test_update_rejects_deleted_recurring_or_uneditable_event(
    field: str, response: dict[str, object]
) -> None:
    """不支持删除、重复或不可编辑事实时必须在 PUT 前停止。"""
    del field
    respx.get(GOOGLE_EVENT_URL).mock(return_value=httpx.Response(200, json=response))
    put_route = respx.put(GOOGLE_EVENT_URL).mock(return_value=httpx.Response(200, json=_fixture()))

    outcome = await _adapter().execute(_update())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert put_route.called is False


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status_code", (401, 403, 429))
async def test_documented_calendar_4xx_is_not_applied(status_code: int) -> None:
    """明确拒绝不能变成成功；仅 429 可携带安全重试许可。"""
    route = respx.post(f"{GOOGLE_CALENDAR_API_BASE_URL}/calendars/{CALENDAR_ID}/events").mock(
        return_value=httpx.Response(status_code, headers={"Retry-After": "12"})
    )

    outcome = await _adapter().execute(_create())

    assert route.call_count == 1
    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.retryable is (status_code == 429)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(503, text="provider unavailable"),
        httpx.Response(200, json={"id": "missing-etag"}),
    ),
)
async def test_timeout_or_ambiguous_success_is_unknown(response: httpx.Response) -> None:
    """超时、5xx 和畸形成功响应都必须停在 unknown。"""
    route = respx.post(f"{GOOGLE_CALENDAR_API_BASE_URL}/calendars/{CALENDAR_ID}/events").mock(
        return_value=response
    )
    outcome = await _adapter().execute(_create())
    assert route.call_count == 1
    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN


@pytest.mark.asyncio
@respx.mock
async def test_create_409_reconciles_exact_event_without_second_post() -> None:
    """稳定 ID 冲突只允许精确 GET 核对，绝不第二次 POST。"""
    create_route = respx.post(
        f"{GOOGLE_CALENDAR_API_BASE_URL}/calendars/{CALENDAR_ID}/events"
    ).mock(return_value=httpx.Response(409))
    reconcile_route = respx.get(
        f"{GOOGLE_CALENDAR_API_BASE_URL}/calendars/{CALENDAR_ID}/events/{calendar_client_event_id(OPERATION_ID)}"
    ).mock(return_value=httpx.Response(200, json=_fixture()))

    outcome = await _adapter().execute(_create())

    assert create_route.call_count == 1
    assert reconcile_route.call_count == 1
    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED


@pytest.mark.asyncio
@respx.mock
async def test_reconcile_gets_exact_event_and_never_writes() -> None:
    """只读核对验证 operation 关联和期望字段，且不会发出 POST/PUT。"""
    get_route = respx.get(GOOGLE_EVENT_URL).mock(
        return_value=httpx.Response(200, json={**_fixture(), "id": EVENT_ID})
    )
    post_route = respx.post().mock(return_value=httpx.Response(500))
    put_route = respx.put().mock(return_value=httpx.Response(500))

    outcome = await _adapter().reconcile(_update(), _execution(_update()))

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert get_route.call_count == 1
    assert post_route.call_count == 0
    assert put_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_reconcile_mismatched_operation_is_unknown() -> None:
    """精确事件存在但 operation 关联不匹配时不能伪造已应用。"""
    payload = {**_fixture(), "id": EVENT_ID}
    payload["extendedProperties"] = {"private": {"ai_employee_operation_id": str(UUID(int=99))}}
    respx.get(GOOGLE_EVENT_URL).mock(return_value=httpx.Response(200, json=payload))

    outcome = await _adapter().reconcile(_update(), _execution(_update()))

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN


@pytest.mark.asyncio
@respx.mock
async def test_restore_uses_same_conditional_update_path() -> None:
    """恢复必须重新读取当前 ETag 并使用 PUT，而不是绕过并发保护。"""
    respx.get(GOOGLE_EVENT_URL).mock(
        return_value=httpx.Response(200, json={"id": EVENT_ID, "etag": '"etag-1"'})
    )
    put_route = respx.put(GOOGLE_EVENT_URL).mock(
        return_value=httpx.Response(200, json={**_fixture(), "id": EVENT_ID})
    )

    outcome = await _adapter().execute(_restore())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert put_route.called
