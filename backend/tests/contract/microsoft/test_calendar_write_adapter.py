"""验证 Microsoft Graph Calendar 写入、条件更新与只读核对契约。"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
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
from ai_employee.domain.errors import StateConflictError
from ai_employee.integrations.microsoft.calendar_write import (
    MICROSOFT_GRAPH_BASE_URL,
    MicrosoftCalendarWriteAdapter,
    validate_graph_notification_policy,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
OPERATION_ID = UUID("00000000-0000-0000-0000-000000000001")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000002")
SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000003")
CALENDAR_ID = "calendar-primary"
EVENT_ID = "event-1"
EVENTS_URL = f"{MICROSOFT_GRAPH_BASE_URL}/me/calendars/{CALENDAR_ID}/events"
EVENT_URL = f"{EVENTS_URL}/{EVENT_ID}"
CALENDAR_VIEW_URL = f"{MICROSOFT_GRAPH_BASE_URL}/me/calendars/{CALENDAR_ID}/calendarView"


def _fixture() -> dict[str, object]:
    """读取完整合成 Graph event，并返回可安全变异的深拷贝。"""
    value = json.loads((FIXTURE_DIR / "calendar_event_write.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return copy.deepcopy(cast(dict[str, object], value))


def _create(
    *,
    attendees: tuple[str, ...] = ("attendee@example.test",),
    policy: NotificationPolicy = NotificationPolicy.ALL,
) -> CalendarCreateCommand:
    """构造带上海业务时区的定时创建命令。"""
    return CalendarCreateCommand(
        schema_version="calendar_create.v1",
        action="calendar.create",
        operation_id=OPERATION_ID,
        connection_id=CONNECTION_ID,
        calendar_id=CALENDAR_ID,
        title="Synthetic calendar event",
        description="Synthetic description",
        location="Synthetic room",
        starts_at=datetime(2030, 1, 2, 1, tzinfo=UTC),
        ends_at=datetime(2030, 1, 2, 2, tzinfo=UTC),
        timezone="Asia/Shanghai",
        all_day=False,
        attendees=attendees,
        notification_policy=policy,
        client_event_id=calendar_client_event_id(OPERATION_ID),
    )


def _all_day_create() -> CalendarCreateCommand:
    """构造洛杉矶时区的全天个人事件，日期不得经 UTC 平移。"""
    return CalendarCreateCommand(
        schema_version="calendar_create.v1",
        action="calendar.create",
        operation_id=OPERATION_ID,
        connection_id=CONNECTION_ID,
        calendar_id=CALENDAR_ID,
        title="Synthetic all-day event",
        description=None,
        location=None,
        starts_at=date(2030, 1, 2),
        ends_at=date(2030, 1, 3),
        timezone="America/Los_Angeles",
        all_day=True,
        attendees=(),
        notification_policy=NotificationPolicy.NONE,
        client_event_id=calendar_client_event_id(OPERATION_ID),
    )


def _update(*, base_etag: str = 'W/"etag-1"') -> CalendarUpdateCommand:
    """构造完整期望状态的条件更新命令。"""
    create = _create()
    return CalendarUpdateCommand(
        schema_version="calendar_update.v1",
        action="calendar.update",
        operation_id=create.operation_id,
        connection_id=create.connection_id,
        calendar_id=create.calendar_id,
        title=create.title,
        description=create.description,
        location=create.location,
        starts_at=create.starts_at,
        ends_at=create.ends_at,
        timezone=create.timezone,
        all_day=create.all_day,
        attendees=create.attendees,
        notification_policy=create.notification_policy,
        provider_event_id=EVENT_ID,
        base_etag=base_etag,
        before_snapshot_id=SNAPSHOT_ID,
        changed_fields=("title",),
    )


def _restore(*, base_etag: str = 'W/"etag-1"') -> CalendarRestoreCommand:
    """构造使用新 operation 与当前版本的恢复命令。"""
    update = _update(base_etag=base_etag)
    return CalendarRestoreCommand(
        schema_version="calendar_restore.v1",
        action="calendar.restore",
        operation_id=update.operation_id,
        connection_id=update.connection_id,
        calendar_id=update.calendar_id,
        title=update.title,
        description=update.description,
        location=update.location,
        starts_at=update.starts_at,
        ends_at=update.ends_at,
        timezone=update.timezone,
        all_day=update.all_day,
        attendees=update.attendees,
        notification_policy=update.notification_policy,
        provider_event_id=update.provider_event_id,
        base_etag=update.base_etag,
        before_snapshot_id=update.before_snapshot_id,
        changed_fields=update.changed_fields,
    )


def _current_event(*, etag: str = 'W/"etag-1"') -> dict[str, object]:
    """返回执行前精确 GET 应观察到的可写非重复事件。"""
    payload = _fixture()
    payload["@odata.etag"] = etag
    payload["changeKey"] = "etag-1"
    return payload


def _all_day_response() -> dict[str, object]:
    """返回与全天冻结命令严格一致的 Graph 成功响应。"""
    payload = _fixture()
    payload.update(
        subject="Synthetic all-day event",
        body={"contentType": "text", "content": ""},
        location={"displayName": ""},
        start={"dateTime": "2030-01-02T00:00:00", "timeZone": "Pacific Standard Time"},
        end={"dateTime": "2030-01-03T00:00:00", "timeZone": "Pacific Standard Time"},
        isAllDay=True,
        attendees=[],
    )
    return payload


def _execution(
    command: CalendarCreateCommand | CalendarUpdateCommand | CalendarRestoreCommand,
    *,
    provider_resource_id: str | None = None,
) -> ExecutionReference:
    """构造只含持久标识的核对引用，可模拟已保存的响应资源 ID。"""
    return ExecutionReference(
        execution_id=UUID("00000000-0000-0000-0000-000000000004"),
        task_id=UUID("00000000-0000-0000-0000-000000000005"),
        step_id=UUID("00000000-0000-0000-0000-000000000006"),
        approval_id=UUID("00000000-0000-0000-0000-000000000007"),
        operation_id=command.operation_id,
        provider="microsoft",
        tool_name=command.action,
        idempotency_key="synthetic-idempotency-key",
        request_payload_hash="a" * 64,
        status=ToolExecutionStatus.RECONCILING,
        result_summary=None,
        request_started_at=None,
        write_attempt_count=1,
        provider_resource_id=provider_resource_id,
        provider_request_id=None,
        correlation_id=str(command.operation_id),
    )


def _adapter() -> MicrosoftCalendarWriteAdapter:
    """构造只使用合成 bearer token 的写入 adapter。"""
    return MicrosoftCalendarWriteAdapter(access_token="synthetic-access-token")


def test_graph_rejects_notification_none_when_attendees_exist() -> None:
    """Graph 无法静默邀请参会人，none 策略必须在审批前稳定拒绝。"""
    with pytest.raises(StateConflictError) as raised:
        validate_graph_notification_policy(
            attendees=("person@example.test",),
            policy=NotificationPolicy.NONE,
        )

    assert raised.value.error_code == "calendar_notification_mapping_unsupported"


def test_validate_for_approval_is_pure_and_accepts_only_lossless_policies() -> None:
    """审批预检不发网络；个人 none 与有参会人 all 均可无损表达。"""
    with respx.mock:
        route = respx.route().mock(return_value=httpx.Response(500))
        personal = _adapter().validate_for_approval(
            _create(attendees=(), policy=NotificationPolicy.NONE)
        )
        invited = _adapter().validate_for_approval(_create())

    assert personal.warnings == ()
    assert invited.warnings == ()
    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_create_posts_transaction_id_to_exact_calendar_with_graph_timezone() -> None:
    """创建必须用精确 calendar path、operation transactionId 和 Windows 墙上时间。"""
    route = respx.post(EVENTS_URL).mock(
        return_value=httpx.Response(201, json=_fixture(), headers={"request-id": "request-1"})
    )

    outcome = await _adapter().execute(_create())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert outcome.provider_resource_id == EVENT_ID
    request = route.calls[0].request
    assert dict(request.url.params) == {}
    payload = json.loads(request.content)
    assert payload["transactionId"] == str(OPERATION_ID)
    assert payload["start"] == {
        "dateTime": "2030-01-02T09:00:00",
        "timeZone": "China Standard Time",
    }
    assert payload["end"] == {
        "dateTime": "2030-01-02T10:00:00",
        "timeZone": "China Standard Time",
    }
    assert payload["isAllDay"] is False
    assert payload["attendees"] == [
        {
            "emailAddress": {"address": "attendee@example.test"},
            "type": "required",
        }
    ]
    assert "recurrence" not in payload
    assert "isOnlineMeeting" not in payload


@pytest.mark.asyncio
@respx.mock
async def test_all_day_create_keeps_local_dates_without_utc_shift() -> None:
    """全天日期必须直接成为目标 Windows 时区午夜，不能先转换成 UTC 日界线。"""
    route = respx.post(EVENTS_URL).mock(return_value=httpx.Response(201, json=_all_day_response()))

    outcome = await _adapter().execute(_all_day_create())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    payload = json.loads(route.calls[0].request.content)
    assert payload["start"] == {
        "dateTime": "2030-01-02T00:00:00",
        "timeZone": "Pacific Standard Time",
    }
    assert payload["end"] == {
        "dateTime": "2030-01-03T00:00:00",
        "timeZone": "Pacific Standard Time",
    }
    assert payload["isAllDay"] is True


@pytest.mark.asyncio
@respx.mock
async def test_update_gets_exact_event_then_patches_complete_state_with_if_match() -> None:
    """更新必须先读当前事实，再以 If-Match PATCH 全部冻结可写字段。"""
    get_route = respx.get(EVENT_URL).mock(return_value=httpx.Response(200, json=_current_event()))
    patch_route = respx.patch(EVENT_URL).mock(return_value=httpx.Response(200, json=_fixture()))

    outcome = await _adapter().execute(_update())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert get_route.call_count == 1
    request = patch_route.calls[0].request
    assert request.headers["If-Match"] == 'W/"etag-1"'
    payload = json.loads(request.content)
    assert set(payload) == {
        "subject",
        "body",
        "location",
        "start",
        "end",
        "isAllDay",
        "attendees",
    }
    assert payload["subject"] == "Synthetic calendar event"
    assert "transactionId" not in payload


@pytest.mark.asyncio
@respx.mock
async def test_update_accepts_change_key_when_odata_etag_is_absent() -> None:
    """Graph 仅返回 changeKey 时也必须规范为可比较版本并原样用于 If-Match。"""
    current = _current_event()
    current.pop("@odata.etag")
    current["changeKey"] = "change-key-1"
    respx.get(EVENT_URL).mock(return_value=httpx.Response(200, json=current))
    patch_route = respx.patch(EVENT_URL).mock(return_value=httpx.Response(200, json=_fixture()))

    outcome = await _adapter().execute(_update(base_etag="change-key-1"))

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert patch_route.calls[0].request.headers["If-Match"] == "change-key-1"


@pytest.mark.asyncio
@respx.mock
async def test_update_version_mismatch_stops_before_patch() -> None:
    """当前 ETag 与冻结 base 不同时必须终态冲突，且不得发送 PATCH。"""
    respx.get(EVENT_URL).mock(return_value=httpx.Response(200, json=_current_event()))
    patch_route = respx.patch(EVENT_URL).mock(return_value=httpx.Response(200, json=_fixture()))

    outcome = await _adapter().execute(_update(base_etag='W/"other-etag"'))

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.error_code == "calendar_event_version_conflict"
    assert patch_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("changes", "error_code"),
    (
        ({"isCancelled": True}, "microsoft_calendar_event_deleted"),
        (
            {"type": "occurrence", "seriesMasterId": "series-1"},
            "calendar_recurring_event_unsupported",
        ),
        (
            {"isOrganizer": False, "canEdit": False},
            "microsoft_calendar_event_not_editable",
        ),
    ),
)
async def test_update_rejects_deleted_recurring_or_permission_loss_before_patch(
    changes: dict[str, object],
    error_code: str,
) -> None:
    """删除、重复或权限丢失事实均必须在写入前精确拒绝。"""
    current = _current_event()
    current.update(changes)
    respx.get(EVENT_URL).mock(return_value=httpx.Response(200, json=current))
    patch_route = respx.patch(EVENT_URL).mock(return_value=httpx.Response(200, json=_fixture()))

    outcome = await _adapter().execute(_update())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.error_code == error_code
    assert patch_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_restore_uses_same_current_get_and_conditional_patch_path() -> None:
    """恢复是新操作，仍必须重新读取当前版本并带 If-Match 写完整状态。"""
    get_route = respx.get(EVENT_URL).mock(return_value=httpx.Response(200, json=_current_event()))
    patch_route = respx.patch(EVENT_URL).mock(return_value=httpx.Response(200, json=_fixture()))

    outcome = await _adapter().execute(_restore())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert get_route.call_count == 1
    assert patch_route.calls[0].request.headers["If-Match"] == 'W/"etag-1"'


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status_code", (401, 403, 429))
async def test_documented_create_rejections_are_confirmed_not_applied(
    status_code: int,
) -> None:
    """Graph 明确 4xx 拒绝不能伪装成功；仅 429 可授权安全重试。"""
    route = respx.post(EVENTS_URL).mock(
        return_value=httpx.Response(status_code, headers={"Retry-After": "12"})
    )

    outcome = await _adapter().execute(_create())

    assert route.call_count == 1
    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.retryable is (status_code == 429)
    assert outcome.retry_after_seconds == (12 if status_code == 429 else None)


@pytest.mark.asyncio
@respx.mock
async def test_create_409_is_terminal_and_never_reposts() -> None:
    """明确冲突只返回未应用；adapter 内不得借 transactionId 自动第二次 POST。"""
    route = respx.post(EVENTS_URL).mock(return_value=httpx.Response(409))

    outcome = await _adapter().execute(_create())

    assert route.call_count == 1
    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.retryable is False


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status_code", (409, 412))
async def test_patch_conflicts_are_terminal_not_applied(status_code: int) -> None:
    """PATCH 的 409/412 均证明本次条件写未应用，不得内部重试。"""
    respx.get(EVENT_URL).mock(return_value=httpx.Response(200, json=_current_event()))
    patch_route = respx.patch(EVENT_URL).mock(return_value=httpx.Response(status_code))

    outcome = await _adapter().execute(_update())

    assert patch_route.call_count == 1
    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.error_code == (
        "calendar_event_version_conflict" if status_code == 412 else "microsoft_calendar_conflict"
    )


@pytest.mark.asyncio
@respx.mock
async def test_post_write_read_timeout_is_unknown_and_never_replayed() -> None:
    """POST 发出后的读取超时不能证明未应用，必须停在 unknown。"""
    route = respx.post(EVENTS_URL).mock(
        side_effect=httpx.ReadTimeout(
            "synthetic-sensitive-timeout",
            request=httpx.Request("POST", EVENTS_URL),
        )
    )

    outcome = await _adapter().execute(_create())

    assert route.call_count == 1
    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert outcome.retryable is False
    assert "synthetic-sensitive-timeout" not in str(outcome)


@pytest.mark.asyncio
@respx.mock
async def test_write_5xx_is_unknown_and_does_not_propagate_provider_body() -> None:
    """语义不明的 5xx 保持 unknown，原始响应正文不得进入规范结果。"""
    route = respx.post(EVENTS_URL).mock(
        return_value=httpx.Response(503, text="synthetic-sensitive-provider-body")
    )

    outcome = await _adapter().execute(_create())

    assert route.call_count == 1
    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert "synthetic-sensitive-provider-body" not in str(outcome)


@pytest.mark.asyncio
@respx.mock
async def test_malformed_create_success_is_unknown() -> None:
    """201 缺少安全 ID、关联或版本事实时不能确认外部副作用。"""
    route = respx.post(EVENTS_URL).mock(return_value=httpx.Response(201, json={"id": "event-1"}))

    outcome = await _adapter().execute(_create())

    assert route.call_count == 1
    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN


@pytest.mark.asyncio
@respx.mock
async def test_success_rejects_offset_inconsistent_with_declared_graph_timezone() -> None:
    """显式 offset 若与 Windows 区域的同一墙上时间矛盾，不能确认字段精确匹配。"""
    payload = _fixture()
    payload["start"] = {
        "dateTime": "2030-01-02T01:00:00+00:00",
        "timeZone": "China Standard Time",
    }
    payload["end"] = {
        "dateTime": "2030-01-02T02:00:00+00:00",
        "timeZone": "China Standard Time",
    }
    respx.post(EVENTS_URL).mock(return_value=httpx.Response(201, json=payload))

    outcome = await _adapter().execute(_create())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN


@pytest.mark.asyncio
@respx.mock
async def test_reconcile_prefers_persisted_resource_id_and_never_writes() -> None:
    """已保存响应 ID 时核对必须精确 GET 该资源，且绝不发 POST/PATCH。"""
    get_route = respx.get(EVENT_URL).mock(return_value=httpx.Response(200, json=_fixture()))
    post_route = respx.post().mock(return_value=httpx.Response(500))
    patch_route = respx.patch().mock(return_value=httpx.Response(500))
    command = _create()

    outcome = await _adapter().reconcile(
        command,
        _execution(command, provider_resource_id=EVENT_ID),
    )

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert get_route.call_count == 1
    assert post_route.call_count == 0
    assert patch_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_lost_create_response_uses_bounded_calendar_view_client_side_match() -> None:
    """创建响应丢失时只查目标时间窗，并在本地按 transactionId 取唯一精确项。"""
    unrelated = _fixture()
    unrelated["id"] = "event-other"
    unrelated["transactionId"] = str(UUID(int=99))
    route = respx.get(CALENDAR_VIEW_URL).mock(
        return_value=httpx.Response(200, json={"value": [unrelated, _fixture()]})
    )
    command = _create()

    outcome = await _adapter().reconcile(command, _execution(command))

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    request = route.calls[0].request
    assert request.url.path == "/v1.0/me/calendars/calendar-primary/calendarView"
    assert not request.url.path.endswith("/delta")
    assert "$filter" not in request.url.params
    assert "transactionId" in request.url.params["$select"]
    assert request.url.params["startDateTime"] == "2030-01-02T01:00:00+00:00"
    assert request.url.params["endDateTime"] == "2030-01-02T02:00:00+00:00"
    assert request.headers["Prefer"] == (
        'outlook.timezone="China Standard Time", outlook.body-content-type="text"'
    )


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("case", ("empty", "multiple", "conflicting"))
async def test_window_reconciliation_requires_one_exact_transaction_match(case: str) -> None:
    """窗口内零个、多个或字段冲突的 transactionId 候选都必须保持 unknown。"""
    matching = _fixture()
    if case == "empty":
        values: list[object] = []
    elif case == "multiple":
        duplicate = copy.deepcopy(matching)
        duplicate["id"] = "event-duplicate"
        values = [matching, duplicate]
    else:
        matching["subject"] = "Conflicting provider title"
        values = [matching]
    route = respx.get(CALENDAR_VIEW_URL).mock(
        return_value=httpx.Response(200, json={"value": values})
    )
    command = _create()

    outcome = await _adapter().reconcile(command, _execution(command))

    assert route.call_count == 1
    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN


@pytest.mark.asyncio
@respx.mock
async def test_reconcile_update_requires_new_version_and_complete_desired_state() -> None:
    """更新核对只有目标状态完整且版本已离开 base 时才能确认已应用。"""
    command = _update()
    respx.get(EVENT_URL).mock(return_value=httpx.Response(200, json=_fixture()))

    outcome = await _adapter().reconcile(command, _execution(command))

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED


@pytest.mark.asyncio
@respx.mock
async def test_reconcile_same_update_version_remains_unknown() -> None:
    """字段碰巧相同但版本仍是 base 不能证明本次 PATCH 已应用。"""
    command = _update()
    respx.get(EVENT_URL).mock(return_value=httpx.Response(200, json=_current_event()))

    outcome = await _adapter().reconcile(command, _execution(command))

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN


@pytest.mark.asyncio
@respx.mock
async def test_provider_identifier_dot_segment_fails_before_any_request() -> None:
    """会被 HTTP client 折叠的 dot-segment 日历 ID 必须在附加 token 前拒绝。"""
    command = replace(_create(), calendar_id=".")
    route = respx.route().mock(return_value=httpx.Response(500))

    with pytest.raises(ValueError):
        await _adapter().execute(command)

    assert route.call_count == 0
