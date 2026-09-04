"""验证 Microsoft Calendar adapter 通过固定 registry 显式注入可信动作 slot。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
import respx

from ai_employee.application.ports.trusted_actions import ExecutionReference
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.calendar_actions import (
    CalendarCreateCommand,
    NotificationPolicy,
    calendar_client_event_id,
)
from ai_employee.integrations.microsoft.calendar_write import (
    MICROSOFT_GRAPH_BASE_URL,
    MicrosoftCalendarWriteAdapter,
)
from ai_employee.integrations.registry import ProviderAdapterRegistry

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="Microsoft Calendar trusted-action integration requires TEST_DATABASE_URL",
)

OPERATION_ID = UUID("00000000-0000-0000-0000-000000000011")
CALENDAR_ID = "calendar-primary"
EVENTS_URL = f"{MICROSOFT_GRAPH_BASE_URL}/me/calendars/{CALENDAR_ID}/events"
CALENDAR_VIEW_URL = f"{MICROSOFT_GRAPH_BASE_URL}/me/calendars/{CALENDAR_ID}/calendarView"


def _command() -> CalendarCreateCommand:
    """构造无需邀请通知映射的最小合成创建命令。"""
    return CalendarCreateCommand(
        schema_version="calendar_create.v1",
        action="calendar.create",
        operation_id=OPERATION_ID,
        connection_id=UUID("00000000-0000-0000-0000-000000000012"),
        calendar_id=CALENDAR_ID,
        title="Synthetic event",
        description=None,
        location=None,
        starts_at=datetime(2030, 1, 1, 9, tzinfo=UTC),
        ends_at=datetime(2030, 1, 1, 10, tzinfo=UTC),
        timezone="UTC",
        all_day=False,
        attendees=(),
        notification_policy=NotificationPolicy.NONE,
        client_event_id=calendar_client_event_id(OPERATION_ID),
    )


def _execution() -> ExecutionReference:
    """构造创建响应丢失后的只读核对引用。"""
    return ExecutionReference(
        execution_id=UUID("00000000-0000-0000-0000-000000000014"),
        task_id=UUID("00000000-0000-0000-0000-000000000015"),
        step_id=UUID("00000000-0000-0000-0000-000000000016"),
        approval_id=UUID("00000000-0000-0000-0000-000000000017"),
        operation_id=OPERATION_ID,
        provider="microsoft",
        tool_name="calendar.create",
        idempotency_key="synthetic-idempotency",
        request_payload_hash="a" * 64,
        status=ToolExecutionStatus.RECONCILING,
        result_summary=None,
        request_started_at=None,
        write_attempt_count=1,
        provider_resource_id=None,
        provider_request_id=None,
        correlation_id=str(OPERATION_ID),
    )


@pytest.mark.asyncio
@respx.mock
async def test_registry_reentry_reconciles_unknown_create_without_second_post() -> None:
    """三个 Microsoft 日历动作共享固定 adapter，unknown 重入只能读 calendarView。"""
    create_route = respx.post(EVENTS_URL).mock(return_value=httpx.Response(503))
    view_route = respx.get(CALENDAR_VIEW_URL).mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    adapter = MicrosoftCalendarWriteAdapter(access_token="synthetic-access-token")
    registry = ProviderAdapterRegistry(microsoft_calendar_action=adapter)

    for action in ("calendar.create", "calendar.update", "calendar.restore"):
        assert registry.trusted_action_adapter(provider="microsoft", action=action) is adapter
        assert registry.trusted_action_preflight(provider="microsoft", action=action) is adapter

    resolved = registry.trusted_action_adapter(
        provider="microsoft",
        action="calendar.create",
    )
    first = await resolved.execute(_command())
    second = await resolved.reconcile(_command(), _execution())

    assert first.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert second.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert create_route.call_count == 1
    assert view_route.call_count == 1
