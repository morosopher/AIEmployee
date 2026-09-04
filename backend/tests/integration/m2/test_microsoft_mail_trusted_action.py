"""验证 Microsoft direct mail adapter 接入固定可信动作 registry。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
import respx

from ai_employee.application.ports.trusted_actions import ExecutionReference
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.mail_actions import MailMode, MailSendCommand
from ai_employee.integrations.microsoft.mail_write import (
    MICROSOFT_SEND_MAIL_URL,
    MICROSOFT_SENT_MESSAGES_URL,
    MicrosoftMailWriteAdapter,
)
from ai_employee.integrations.registry import ProviderAdapterRegistry

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="Microsoft mail trusted-action integration requires TEST_DATABASE_URL",
)


def _command() -> MailSendCommand:
    """构造最小合成新邮件命令。"""
    return MailSendCommand(
        schema_version="mail_send.v1",
        action="mail.send",
        operation_id=UUID("00000000-0000-0000-0000-000000000601"),
        connection_id=UUID("00000000-0000-0000-0000-000000000202"),
        draft_id=UUID("00000000-0000-0000-0000-000000000301"),
        draft_version=3,
        message_date=datetime(2030, 1, 1, tzinfo=UTC),
        mode=MailMode.NEW,
        source_thread_id=None,
        source_message_id=None,
        to=("recipient@example.test",),
        cc=(),
        bcc=(),
        subject="Synthetic subject",
        body_text="Synthetic body",
        thread_headers=None,
    )


def _execution() -> ExecutionReference:
    """构造只含标识的核对引用。"""
    return ExecutionReference(
        execution_id=UUID("00000000-0000-0000-0000-000000000404"),
        task_id=UUID("00000000-0000-0000-0000-000000000401"),
        step_id=UUID("00000000-0000-0000-0000-000000000402"),
        approval_id=UUID("00000000-0000-0000-0000-000000000403"),
        operation_id=UUID("00000000-0000-0000-0000-000000000601"),
        provider="microsoft",
        tool_name="mail.send",
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


@pytest.mark.asyncio
@respx.mock
async def test_registry_reentry_reconciles_without_second_graph_write() -> None:
    """固定 registry 返回 Microsoft adapter，unknown 重入只执行 Sent GET。"""
    send = respx.post(MICROSOFT_SEND_MAIL_URL).mock(return_value=httpx.Response(503))
    sent = respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    adapter = MicrosoftMailWriteAdapter(
        access_token="synthetic-access-token",
        account_email="sender@example.test",
    )
    registry = ProviderAdapterRegistry(microsoft_mail_action=adapter)
    resolved = registry.trusted_action_adapter(provider="microsoft", action="mail.send")

    first = await resolved.execute(_command())
    second = await resolved.reconcile(_command(), _execution())

    assert first.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert second.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert send.call_count == 1
    assert sent.call_count == 1
