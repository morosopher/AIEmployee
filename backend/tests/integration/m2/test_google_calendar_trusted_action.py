"""验证 Google Calendar adapter 接入固定可信动作 registry 的边界。"""

from __future__ import annotations

import os
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
from ai_employee.integrations.google.calendar_write import (
    GOOGLE_CALENDAR_API_BASE_URL,
    GoogleCalendarWriteAdapter,
)
from ai_employee.integrations.registry import ProviderAdapterRegistry

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="Google Calendar trusted-action integration requires TEST_DATABASE_URL",
)


def _command() -> CalendarCreateCommand:
    """构造最小合成日历创建命令。"""
    from datetime import UTC, datetime

    return CalendarCreateCommand(
        schema_version="calendar_create.v1",
        action="calendar.create",
        operation_id=UUID("00000000-0000-0000-0000-000000000011"),
        connection_id=UUID("00000000-0000-0000-0000-000000000012"),
        calendar_id="primary",
        title="Synthetic event",
        description=None,
        location=None,
        starts_at=datetime(2030, 1, 1, 9, tzinfo=UTC),
        ends_at=datetime(2030, 1, 1, 10, tzinfo=UTC),
        timezone="UTC",
        all_day=False,
        attendees=(),
        notification_policy=NotificationPolicy.ALL,
        client_event_id=calendar_client_event_id(UUID("00000000-0000-0000-0000-000000000011")),
    )


def _execution() -> ExecutionReference:
    """构造只读核对引用。"""
    return ExecutionReference(
        execution_id=UUID("00000000-0000-0000-0000-000000000014"),
        task_id=UUID("00000000-0000-0000-0000-000000000015"),
        step_id=UUID("00000000-0000-0000-0000-000000000016"),
        approval_id=UUID("00000000-0000-0000-0000-000000000017"),
        operation_id=UUID("00000000-0000-0000-0000-000000000011"),
        provider="google",
        tool_name="calendar.create",
        idempotency_key="synthetic-idempotency",
        request_payload_hash="a" * 64,
        status=ToolExecutionStatus.RECONCILING,
        result_summary=None,
        request_started_at=None,
        write_attempt_count=1,
        provider_resource_id=None,
        provider_request_id=None,
        correlation_id=None,
    )


@pytest.mark.asyncio
@respx.mock
async def test_registry_exposes_calendar_action_and_reconciliation_is_read_only() -> None:
    """固定 registry 返回同一 Google Calendar adapter，核对不再次 POST。"""
    post = respx.post(f"{GOOGLE_CALENDAR_API_BASE_URL}/calendars/primary/events").mock(
        return_value=httpx.Response(503)
    )
    get = respx.get(
        f"{GOOGLE_CALENDAR_API_BASE_URL}/calendars/primary/events/{calendar_client_event_id(_command().operation_id)}"
    ).mock(return_value=httpx.Response(404))
    adapter = GoogleCalendarWriteAdapter(access_token="synthetic-access-token")
    registry = ProviderAdapterRegistry(google_calendar_action=adapter)

    assert registry.trusted_action_adapter(provider="google", action="calendar.create") is adapter
    first = await adapter.execute(_command())
    second = await adapter.reconcile(_command(), _execution())

    assert first.kind is ProviderWriteOutcomeKind.UNKNOWN
    # 写请求已可能到达供应商；随后精确 GET 的 404 不能排除事件写入后又被删除，
    # 因此只能保留 unknown 并交给有界核对/人工确认，不能伪造“从未应用”。
    assert second.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert post.call_count == 1
    assert get.call_count == 1
