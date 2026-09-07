"""验证 Microsoft direct mail adapter 接入固定可信动作 registry。"""

from __future__ import annotations

import base64
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from ai_employee.application.ports.trusted_actions import ExecutionReference
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode, MailSendCommand, ReplyThreadHeaders
from ai_employee.infrastructure.db.models.sources import (
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.microsoft.mail_write import (
    MICROSOFT_SEND_MAIL_URL,
    MICROSOFT_SENT_MESSAGES_URL,
    MicrosoftMailWriteAdapter,
)
from ai_employee.integrations.registry import ProviderAdapterRegistry, build_trusted_action_registry
from tests.integration.m2.test_tool_execution_claim import _Seed, _seed_action, _settings

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


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("mode", [MailMode.REPLY, MailMode.REPLY_ALL])
@pytest.mark.parametrize("reply_to", ["", '"Synthetic, Reply" <reply@example.test>'])
async def test_production_registry_uses_exact_microsoft_reply_source(
    database_url: str,
    tmp_path: Path,
    mode: MailMode,
    reply_to: str,
) -> None:
    """生产解析器从本地已同步头字段派生 Graph 收件人，纯预检与直接回复均可用。"""
    seed = _Seed()
    seed.provider, seed.account_type = "microsoft", "work_school"
    seed.provider_tenant_id = "synthetic-tenant"
    seed.provider_account_id = "synthetic-tenant:synthetic-user"
    await _seed_action(database_url, seed)
    sessions = build_session_factory(database_url)
    root_file = tmp_path / "master"
    root_file.write_text(base64.urlsafe_b64encode(b"x" * 32).decode("ascii"))
    settings = _settings(
        provider="microsoft", tenant=seed.provider_tenant_id, account=seed.provider_account_id
    ).model_copy(update={"app_master_key_file": root_file})
    encrypted = AeadCipher(b"x" * 32).encrypt(
        b"synthetic-access",
        f"{seed.user_id}:{seed.connection_id}:access_token".encode(),
    )
    thread_id, message_id = uuid4(), uuid4()
    headers = {
        "from": "origin@example.test",
        "reply-to": reply_to,
        "to": "sender@example.test, other@example.test",
        "cc": "copy@example.test, other@example.test",
        "bcc": "hidden@example.test",
        "message-id": "<synthetic-source@example.test>",
    }
    target = "reply@example.test" if reply_to else "origin@example.test"
    command = replace(
        _command(),
        connection_id=seed.connection_id,
        mode=mode,
        source_thread_id="synthetic-source-thread",
        source_message_id="synthetic-source-message",
        to=(target, "other@example.test") if mode is MailMode.REPLY_ALL else (target,),
        cc=("copy@example.test",) if mode is MailMode.REPLY_ALL else (),
        bcc=(),
        thread_headers=ReplyThreadHeaders(
            "<synthetic-source@example.test>", ("<synthetic-source@example.test>",)
        ),
    )
    try:
        async with sessions.begin() as session:
            session.add(
                EncryptedCredentialModel(
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    credential_kind="access_token",
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                )
            )
            session.add(
                EmailThreadModel(
                    id=thread_id,
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    provider_thread_id="synthetic-source-thread",
                    subject="Synthetic source",
                    participants=[],
                    latest_message_at=datetime(2030, 1, 1, tzinfo=UTC),
                    provider_url="",
                )
            )
            await session.flush()
            session.add(
                EmailMessageModel(
                    id=message_id,
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    thread_id=thread_id,
                    provider_message_id="synthetic-source-message",
                    provider_conversation_id="synthetic-source-thread",
                    received_at=datetime(2030, 1, 1, tzinfo=UTC),
                    sender={"email": "origin@example.test"},
                    recipients=[],
                    subject="Synthetic source",
                    snippet="",
                    labels=[],
                    headers=headers,
                    provider_url="",
                )
            )
        registry = build_trusted_action_registry(session_factory=sessions, settings=settings)
        adapter = await registry.resolve_trusted_action_adapter(
            user_id=seed.user_id, provider="microsoft", command=command
        )
        adapter.validate_for_approval(command)
        assert len(respx.calls) == 0
        suffix = "replyAll" if mode is MailMode.REPLY_ALL else "reply"
        send = respx.post(
            f"https://graph.microsoft.com/v1.0/me/messages/synthetic-source-message/{suffix}"
        ).respond(202)
        result = await adapter.execute(command)
        assert result.kind is ProviderWriteOutcomeKind.UNKNOWN
        assert send.call_count == 1

        # 用户可编辑命令不能反向成为来源 proof；不同 source 四元绑定也不得借用此事实。
        for changed in (
            replace(command, to=("unapproved@example.test",)),
            replace(command, source_thread_id="synthetic-other-thread"),
            replace(command, source_message_id="synthetic-other-message"),
        ):
            with pytest.raises(StateConflictError):
                resolved = await registry.resolve_trusted_action_adapter(
                    user_id=seed.user_id, provider="microsoft", command=changed
                )
                resolved.validate_for_approval(changed)
        with pytest.raises(StateConflictError):
            await registry.resolve_trusted_action_adapter(
                user_id=uuid4(), provider="microsoft", command=command
            )
        async with sessions.begin() as session:
            source = await session.get(EmailMessageModel, message_id)
            assert source is not None
            source.headers = {key: value for key, value in headers.items() if key != "reply-to"}
        with pytest.raises(StateConflictError):
            resolved = await registry.resolve_trusted_action_adapter(
                user_id=seed.user_id, provider="microsoft", command=command
            )
            resolved.validate_for_approval(command)
        assert send.call_count == 1
    finally:
        await sessions.dispose()
