"""验证 Gmail 可信邮件写入适配器的 HTTP、MIME 与三态结果契约。"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import respx

from ai_employee.application.ports.trusted_actions import ExecutionReference
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.mail_actions import MailMode, MailSendCommand, ReplyThreadHeaders
from ai_employee.integrations.google.gmail_write import (
    GMAIL_MESSAGES_URL,
    GMAIL_SEND_URL,
    GmailWriteAdapter,
)
from ai_employee.integrations.mail_mime import build_mail_mime, message_id_for

FIXTURES = Path(__file__).parent / "fixtures"
OPERATION_ID = UUID("00000000-0000-0000-0000-000000000001")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000002")
DRAFT_ID = UUID("00000000-0000-0000-0000-000000000003")


def _fixture(name: str) -> dict[str, object]:
    """读取仓库内脱敏 Gmail 响应 fixture。"""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _command(*, mode: MailMode = MailMode.NEW) -> MailSendCommand:
    """创建可重复使用的合成冻结邮件命令。"""
    reply = mode is not MailMode.NEW
    return MailSendCommand(
        schema_version="mail_send.v1",
        action="mail.send",
        operation_id=OPERATION_ID,
        connection_id=CONNECTION_ID,
        draft_id=DRAFT_ID,
        draft_version=1,
        message_date=datetime(2026, 8, 6, 9, 30, tzinfo=UTC),
        mode=mode,
        source_thread_id="synthetic-thread-id" if reply else None,
        source_message_id="synthetic-message-id" if reply else None,
        to=("recipient@example.test",),
        cc=("copy@example.test",),
        bcc=("blind@example.test",),
        subject="Synthetic subject",
        body_text="Synthetic body",
        thread_headers=(
            ReplyThreadHeaders(
                in_reply_to="<source-message@example.test>",
                references=("<older@example.test>", "<source-message@example.test>"),
            )
            if reply
            else None
        ),
    )


def _execution() -> ExecutionReference:
    """构造只含标识和状态的核对引用。"""
    return ExecutionReference(
        execution_id=UUID("00000000-0000-0000-0000-000000000004"),
        task_id=UUID("00000000-0000-0000-0000-000000000005"),
        step_id=UUID("00000000-0000-0000-0000-000000000006"),
        approval_id=UUID("00000000-0000-0000-0000-000000000007"),
        operation_id=OPERATION_ID,
        provider="google",
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


def test_reply_mime_contains_frozen_thread_headers_and_plain_text_only() -> None:
    """回复 MIME 只能包含冻结引用头和 UTF-8 纯文本正文。"""
    raw = build_mail_mime(_command(mode=MailMode.REPLY), from_address="sender@example.test")
    message = BytesParser(policy=policy.default).parsebytes(raw)

    assert message.get_content_type() == "text/plain"
    assert message["From"] == "sender@example.test"
    assert message["In-Reply-To"] == "<source-message@example.test>"
    assert message["References"] == "<older@example.test> <source-message@example.test>"
    assert message["Message-ID"] == message_id_for(OPERATION_ID)
    assert message.get_body(preferencelist=("html",)) is None
    assert message.get_content().rstrip("\r\n") == "Synthetic body"


def test_mime_is_byte_deterministic_for_the_same_frozen_command() -> None:
    """同一冻结命令重复预检必须生成逐字相同的 RFC 2822 bytes。"""
    command = _command(mode=MailMode.REPLY_ALL)

    first = build_mail_mime(command, from_address="sender@example.test")
    second = build_mail_mime(command, from_address="sender@example.test")

    assert first == second


def test_validate_for_approval_is_pure_and_does_not_call_gmail() -> None:
    """审批预检只做本地表达能力检查，不应触发任何 Gmail HTTP 请求。"""
    with respx.mock:
        route = respx.route().mock(return_value=httpx.Response(500))
        result = GmailWriteAdapter(
            access_token="synthetic-access-token", account_email="sender@example.test"
        ).validate_for_approval(_command())

    assert result.warnings == ()
    assert route.call_count == 0


def test_adapter_rejects_header_injection_and_domain_recipient_limit() -> None:
    """连接地址与领域收件人上限在供应商边界都必须 fail closed。"""
    with pytest.raises(ValueError):
        GmailWriteAdapter(
            access_token="synthetic-access-token",
            account_email="sender@example.test\r\nBcc: attacker@example.test",
        )

    with pytest.raises(ValueError):
        MailSendCommand(
            schema_version="mail_send.v1",
            action="mail.send",
            operation_id=OPERATION_ID,
            connection_id=CONNECTION_ID,
            draft_id=DRAFT_ID,
            draft_version=1,
            message_date=datetime(2026, 8, 6, 9, 30, tzinfo=UTC),
            mode=MailMode.NEW,
            source_thread_id=None,
            source_message_id=None,
            to=tuple(f"recipient-{index}@example.test" for index in range(51)),
            cc=(),
            bcc=(),
            subject="Synthetic subject",
            body_text="Synthetic body",
            thread_headers=None,
        )


@pytest.mark.asyncio
@respx.mock
async def test_send_uses_base64url_and_normalizes_success_response() -> None:
    """新邮件必须发送 raw Base64URL，成功响应只映射为稳定 ID。"""
    route = respx.post(GMAIL_SEND_URL).mock(
        return_value=httpx.Response(200, json=_fixture("gmail_send_success.json"))
    )
    adapter = GmailWriteAdapter(
        access_token="synthetic-access-token",
        account_email="sender@example.test",
    )

    outcome = await adapter.execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert outcome.provider_resource_id == "synthetic-sent-message-id"
    assert outcome.correlation_id == message_id_for(OPERATION_ID)
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    encoded = body["raw"]
    assert isinstance(encoded, str)
    assert "=" not in encoded
    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    parsed = BytesParser(policy=policy.default).parsebytes(decoded)
    assert parsed["From"] == "sender@example.test"


@pytest.mark.asyncio
@respx.mock
async def test_reply_sends_frozen_thread_id_and_never_calls_draft_api() -> None:
    """回复请求携带冻结 threadId，且不会触达 Gmail Draft API。"""
    route = respx.post(GMAIL_SEND_URL).mock(
        return_value=httpx.Response(
            200,
            json={"id": "synthetic-sent-message-id", "threadId": "synthetic-thread-id"},
        )
    )
    draft_route = respx.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
    ).mock(return_value=httpx.Response(500))

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).execute(_command(mode=MailMode.REPLY_ALL))

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert json.loads(route.calls[0].request.content)["threadId"] == "synthetic-thread-id"
    assert not draft_route.called


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("returned_thread_id", ("different-thread-id", None))
async def test_reply_success_requires_the_frozen_thread_id(returned_thread_id: str | None) -> None:
    """回复 200 响应的 threadId 必须与冻结来源严格一致。"""
    response: dict[str, object] = {"id": "synthetic-sent-message-id"}
    if returned_thread_id is not None:
        response["threadId"] = returned_thread_id
    respx.post(GMAIL_SEND_URL).mock(return_value=httpx.Response(200, json=response))

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).execute(_command(mode=MailMode.REPLY))

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert outcome.retryable is False


@pytest.mark.asyncio
@respx.mock
async def test_sent_reconciliation_searches_by_stable_message_id() -> None:
    """核对只读搜索 Sent 的稳定 Message-ID，并返回不含地址的链接。"""
    search = respx.get(GMAIL_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_fixture("gmail_sent_search.json"))
    )
    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert outcome.provider_resource_id == "synthetic-sent-message-id"
    assert outcome.provider_url is not None
    assert "@" not in outcome.provider_url
    assert "Synthetic subject" not in outcome.provider_url
    query = str(search.calls[0].request.url.params["q"])
    assert query == f"in:sent rfc822msgid:{message_id_for(OPERATION_ID)}"


@pytest.mark.asyncio
@respx.mock
async def test_sent_reconciliation_without_a_match_is_unknown() -> None:
    """单轮 Sent 无匹配不能证明未发送，必须保留 unknown。"""
    search = respx.get(GMAIL_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"messages": []})
    )

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert outcome.retryable is False
    assert search.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_sent_reconciliation_rejects_multiple_matching_messages() -> None:
    """多个符合稳定关联条件的 Sent 消息不能被首个候选掩盖。"""
    search = respx.get(GMAIL_MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "messages": [
                    {"id": "synthetic-message-one", "threadId": "synthetic-thread-one"},
                    {"id": "synthetic-message-two", "threadId": "synthetic-thread-two"},
                ]
            },
        )
    )

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert outcome.retryable is False
    assert search.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_sent_reconciliation_follows_pages_before_declaring_unique() -> None:
    """有 nextPageToken 时必须读取后续有限页面，避免首个结果伪造唯一性。"""
    search = respx.get(GMAIL_MESSAGES_URL)
    search.side_effect = [
        httpx.Response(
            200,
            json={
                "messages": [
                    {"id": "synthetic-message-one", "threadId": "synthetic-thread-one"}
                ],
                "nextPageToken": "synthetic-page-two",
            },
        ),
        httpx.Response(
            200,
            json={
                "messages": [
                    {"id": "synthetic-message-two", "threadId": "synthetic-thread-two"}
                ]
            },
        ),
    ]

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert search.call_count == 2
    assert search.calls[1].request.url.params["pageToken"] == "synthetic-page-two"


@pytest.mark.asyncio
@respx.mock
async def test_sent_reconciliation_malformed_candidate_is_unknown() -> None:
    """缺少供应商 ID/thread 事实的候选不能被当作已发送。"""
    respx.get(GMAIL_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "only-id"}]})
    )

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status_code", (400, 403, 429))
async def test_documented_4xx_is_confirmed_not_applied(status_code: int) -> None:
    """供应商明确拒绝的 4xx 不得被误报为 unknown。"""
    respx.post(GMAIL_SEND_URL).mock(
        return_value=httpx.Response(status_code, headers={"Retry-After": "12"})
    )

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.retryable is (status_code == 429)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status_code", (408, 425))
async def test_undocumented_transient_4xx_is_not_safe_to_retry(status_code: int) -> None:
    """没有 Gmail 未应用证明的 408/425 不能授权再次写入。"""
    respx.post(GMAIL_SEND_URL).mock(return_value=httpx.Response(status_code))

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.retryable is False


@pytest.mark.asyncio
@respx.mock
async def test_connect_failure_is_retryable_without_a_second_send() -> None:
    """连接建立前失败可安全重试，但单次 adapter 调用绝不自动重放。"""
    route = respx.post(GMAIL_SEND_URL).mock(
        side_effect=httpx.ConnectError(
            "synthetic-connect-failure",
            request=httpx.Request("POST", GMAIL_SEND_URL),
        )
    )

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.retryable is True
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(503, text="synthetic-provider-body"),
        httpx.Response(200, json={"id": "missing-thread-id"}),
    ),
)
async def test_ambiguous_or_malformed_success_is_unknown(response: httpx.Response) -> None:
    """发送后语义不明的 5xx 或缺 ID 成功响应必须停在 unknown。"""
    route = respx.post(GMAIL_SEND_URL).mock(return_value=response)

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert outcome.retryable is False
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_read_timeout_after_send_is_unknown_and_not_replayed() -> None:
    """请求开始后的读取超时表示未知，绝不能再次 POST。"""
    route = respx.post(GMAIL_SEND_URL).mock(
        side_effect=httpx.ReadTimeout(
            "synthetic-read-timeout",
            request=httpx.Request("POST", GMAIL_SEND_URL),
        )
    )

    outcome = await GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    ).execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_401_is_not_retried_inside_adapter_and_coordinator_can_refresh_once() -> None:
    """401 只返回可分类结果；一次 refresh/re-entry 由外部 coordinator 控制。"""
    route = respx.post(GMAIL_SEND_URL).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=_fixture("gmail_send_success.json")),
        ]
    )
    first = GmailWriteAdapter(
        access_token="synthetic-expired-token", account_email="sender@example.test"
    )
    first_outcome = await first.execute(_command())
    assert first_outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert route.call_count == 1

    # coordinator 在持久化 refresh 事实后重新组装 adapter；adapter 自身没有 refresh callback。
    second_outcome = await GmailWriteAdapter(
        access_token="synthetic-refreshed-token", account_email="sender@example.test"
    ).execute(_command())
    assert second_outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_401_never_calls_an_injected_refresh_callback() -> None:
    """401 的 refresh/re-entry 只能由外部 coordinator 控制。"""
    refresh_calls = 0

    async def refresh() -> str:
        nonlocal refresh_calls
        refresh_calls += 1
        return "synthetic-refreshed-token"

    route = respx.post(GMAIL_SEND_URL).mock(return_value=httpx.Response(401))
    outcome = await GmailWriteAdapter(
        access_token="synthetic-expired-token",
        account_email="sender@example.test",
        refresh_access_token=refresh,
    ).execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert refresh_calls == 0
    assert route.call_count == 1
