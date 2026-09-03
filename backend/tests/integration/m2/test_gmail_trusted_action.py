"""验证 Gmail adapter 接入固定可信动作 registry 后的幂等核对边界。"""

import os
from uuid import UUID

import httpx
import pytest
import respx

from ai_employee.application.ports.trusted_actions import ExecutionReference
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.mail_actions import MailMode, MailSendCommand
from ai_employee.integrations.google.gmail_write import GMAIL_SEND_URL, GmailWriteAdapter
from ai_employee.integrations.registry import ProviderAdapterRegistry

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="Gmail trusted-action integration requires TEST_DATABASE_URL",
)


def _command() -> MailSendCommand:
    """构造最小合成新邮件命令。"""
    from datetime import UTC, datetime

    return MailSendCommand(
        schema_version="mail_send.v1",
        action="mail.send",
        operation_id=UUID("00000000-0000-0000-0000-000000000011"),
        connection_id=UUID("00000000-0000-0000-0000-000000000012"),
        draft_id=UUID("00000000-0000-0000-0000-000000000013"),
        draft_version=1,
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
    """构造 unknown 恢复所需的内容无关执行投影。"""
    return ExecutionReference(
        execution_id=UUID("00000000-0000-0000-0000-000000000014"),
        task_id=UUID("00000000-0000-0000-0000-000000000015"),
        step_id=UUID("00000000-0000-0000-0000-000000000016"),
        approval_id=UUID("00000000-0000-0000-0000-000000000017"),
        operation_id=UUID("00000000-0000-0000-0000-000000000011"),
        provider="google",
        tool_name="mail.send",
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
async def test_registry_exposes_gmail_action_and_unknown_reentry_is_read_only() -> None:
    """固定 registry 可取出 Gmail adapter，核对重入不会再次调用 send。"""
    send = respx.post(GMAIL_SEND_URL).mock(return_value=httpx.Response(503))
    search = respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        return_value=httpx.Response(200, json={"messages": []})
    )
    adapter = GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    )
    registry = ProviderAdapterRegistry(google_mail_action=adapter)
    resolved = registry.trusted_action_adapter(provider="google", action="mail.send")

    first = await resolved.execute(_command())
    second = await resolved.reconcile(_command(), _execution())

    assert first.kind is ProviderWriteOutcomeKind.UNKNOWN
    # 单轮 Sent 无匹配不能证明请求未应用；上层必须继续有界只读核对或转人工关注。
    assert second.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert send.call_count == 1
    assert search.call_count == 1
