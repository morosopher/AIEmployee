"""验证 Microsoft Graph 直接 MIME 邮件写入与 Sent Items 核对契约。"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
import respx

from ai_employee.application.ports.trusted_actions import ExecutionReference
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode, MailSendCommand, ReplyThreadHeaders
from ai_employee.integrations.mail_mime import message_id_for
from ai_employee.integrations.microsoft.mail_write import (
    MICROSOFT_GRAPH_BASE_URL,
    MICROSOFT_SEND_MAIL_URL,
    MICROSOFT_SENT_MESSAGES_URL,
    MicrosoftMailWriteAdapter,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
OPERATION_ID = UUID("00000000-0000-0000-0000-000000000601")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000202")
DRAFT_ID = UUID("00000000-0000-0000-0000-000000000301")


def _fixture(name: str) -> dict[str, object]:
    """读取脱敏 Graph fixture，并把顶层收窄为 JSON object。"""
    value = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _command(*, mode: MailMode = MailMode.NEW) -> MailSendCommand:
    """构造完整、可重复的合成冻结命令。"""
    is_reply = mode is not MailMode.NEW
    return MailSendCommand(
        schema_version="mail_send.v1",
        action="mail.send",
        operation_id=OPERATION_ID,
        connection_id=CONNECTION_ID,
        draft_id=DRAFT_ID,
        draft_version=3,
        message_date=datetime(2030, 1, 1, tzinfo=UTC),
        mode=mode,
        source_thread_id="synthetic-thread-1" if is_reply else None,
        source_message_id="synthetic-message-1" if is_reply else None,
        to=("recipient@example.test",),
        cc=("copy@example.test",),
        bcc=("blind@example.test",),
        subject="Synthetic subject",
        body_text="Synthetic body\nSecond line",
        thread_headers=(
            ReplyThreadHeaders(
                in_reply_to="<source@example.test>",
                references=("<older@example.test>", "<source@example.test>"),
            )
            if is_reply
            else None
        ),
    )


def _reply_sets() -> dict[MailMode, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]]:
    """提供已由同步事实得到的 Graph reply/replyAll 收件人集合。"""
    return {
        MailMode.REPLY: (
            ("recipient@example.test",),
            ("copy@example.test",),
            ("blind@example.test",),
        ),
        MailMode.REPLY_ALL: (
            ("recipient@example.test",),
            ("copy@example.test",),
            ("blind@example.test",),
        ),
    }


def _adapter(**kwargs: object) -> MicrosoftMailWriteAdapter:
    """构造不含真实凭据的 Microsoft 写 adapter。"""
    return MicrosoftMailWriteAdapter(
        access_token="synthetic-access-token",
        account_email="sender@example.test",
        reply_recipient_sets=_reply_sets(),
        **kwargs,
    )


def _execution() -> ExecutionReference:
    """构造只含标识的只读核对引用。"""
    return ExecutionReference(
        execution_id=UUID("00000000-0000-0000-0000-000000000404"),
        task_id=UUID("00000000-0000-0000-0000-000000000401"),
        step_id=UUID("00000000-0000-0000-0000-000000000402"),
        approval_id=UUID("00000000-0000-0000-0000-000000000403"),
        operation_id=OPERATION_ID,
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


def _sent_payload(**updates: object) -> dict[str, object]:
    """返回一条可修改的合成 Sent 项，避免测试依赖真实供应商响应。"""
    payload = _fixture("sent_message.json")
    values = payload["value"]
    assert isinstance(values, list) and values and isinstance(values[0], dict)
    values[0].update(updates)
    return payload


def _assert_graph_headers(request: httpx.Request) -> None:
    """断言每次请求都带最小身份与不可变 ID 偏好。"""
    assert request.headers["Authorization"] == "Bearer synthetic-access-token"
    assert request.headers["Content-Type"] == "text/plain"
    assert request.headers["Prefer"] == 'IdType="ImmutableId"'


@pytest.mark.asyncio
@respx.mock
async def test_graph_send_202_requires_read_only_reconciliation() -> None:
    """202 空响应只能表示接受，必须保留 unknown 供只读核对。"""
    route = respx.post(MICROSOFT_SEND_MAIL_URL).mock(return_value=httpx.Response(202))

    outcome = await _adapter().execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert outcome.correlation_id.endswith("@ai-employee.invalid>")
    assert route.call_count == 1
    _assert_graph_headers(route.calls[0].request)


@pytest.mark.asyncio
@respx.mock
async def test_new_mail_uses_direct_base64_mime_not_json_wrapper() -> None:
    """新邮件请求体必须是标准 Base64 MIME，而不是 Graph JSON message 包装。"""
    route = respx.post(MICROSOFT_SEND_MAIL_URL).mock(return_value=httpx.Response(202))

    await _adapter().execute(_command())

    body = route.calls[0].request.content
    assert body and not body.startswith(b"{")
    decoded = base64.b64decode(body, validate=True)
    parsed = BytesParser(policy=policy.default).parsebytes(decoded)
    assert parsed.get_content_type() == "text/plain"
    assert parsed["From"] == "sender@example.test"
    assert parsed["Message-ID"] == message_id_for(OPERATION_ID)
    assert parsed.get_content().replace("\r\n", "\n").rstrip("\n") == (
        "Synthetic body\nSecond line"
    )


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("mode", "suffix"),
    ((MailMode.REPLY, "reply"), (MailMode.REPLY_ALL, "replyAll")),
)
async def test_reply_modes_use_corresponding_direct_graph_actions(
    mode: MailMode,
    suffix: str,
) -> None:
    """回复和全部回复只能命中对应直接动作，不能创建供应商草稿。"""
    route = respx.post(f"{MICROSOFT_GRAPH_BASE_URL}/me/messages/synthetic-message-1/{suffix}").mock(
        return_value=httpx.Response(202)
    )
    draft = respx.post(
        f"{MICROSOFT_GRAPH_BASE_URL}/me/messages/synthetic-message-1/create{suffix.title()}"
    ).mock(return_value=httpx.Response(500))

    await _adapter().execute(_command(mode=mode))

    assert route.call_count == 1
    assert not draft.called
    _assert_graph_headers(route.calls[0].request)


def test_preflight_is_pure_and_rejects_adjusted_reply_recipients() -> None:
    """无法证明 Graph 直接回复保留冻结收件人时，审批前必须稳定拒绝且不联网。"""
    adapter = MicrosoftMailWriteAdapter(
        access_token="synthetic-access-token",
        account_email="sender@example.test",
        reply_recipient_sets={
            MailMode.REPLY: (("source@example.test",), (), ()),
            MailMode.REPLY_ALL: (("source@example.test",), ("copy@example.test",), ()),
        },
    )
    command = _command(mode=MailMode.REPLY)

    with pytest.raises(StateConflictError) as raised:
        adapter.validate_for_approval(command)

    assert type(raised.value) is StateConflictError
    assert raised.value.error_code == "mail_thread_binding_conflict"


def test_preflight_accepts_a_proven_provider_recipient_set() -> None:
    """同步事实与冻结收件人完全一致时，直接回复可以进入审批。"""
    result = _adapter().validate_for_approval(_command(mode=MailMode.REPLY_ALL))
    assert result.warnings == ()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status_code", (400, 403, 404, 409, 429))
async def test_explicit_graph_rejection_is_confirmed_not_applied(status_code: int) -> None:
    """Graph 明确拒绝且尚未接受请求时不得进入 unknown；429 允许安全重试。"""
    route = respx.post(MICROSOFT_SEND_MAIL_URL).mock(
        return_value=httpx.Response(status_code, headers={"Retry-After": "12"})
    )

    outcome = await _adapter().execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.retryable is (status_code == 429)
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_401_is_not_refreshed_or_replayed_inside_adapter() -> None:
    """401 只能交给 coordinator，adapter 不调用 refresh callback 或第二次 POST。"""
    refresh_calls = 0

    async def refresh() -> str:
        nonlocal refresh_calls
        refresh_calls += 1
        return "synthetic-refreshed-token"

    route = respx.post(MICROSOFT_SEND_MAIL_URL).mock(return_value=httpx.Response(401))
    outcome = await MicrosoftMailWriteAdapter(
        access_token="synthetic-expired-token",
        account_email="sender@example.test",
        refresh_access_token=refresh,
    ).execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert refresh_calls == 0
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_connect_failure_is_safe_to_retry_but_not_replayed() -> None:
    """请求建立前连接失败可安全重试，但一次 adapter 调用仍只有一次尝试。"""
    route = respx.post(MICROSOFT_SEND_MAIL_URL).mock(
        side_effect=httpx.ConnectError(
            "synthetic-connect-failure",
            request=httpx.Request("POST", MICROSOFT_SEND_MAIL_URL),
        )
    )

    outcome = await _adapter().execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert outcome.retryable is True
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(503, text="synthetic-provider-body"),
        httpx.ReadTimeout(
            "synthetic-read-timeout",
            request=httpx.Request("POST", MICROSOFT_SEND_MAIL_URL),
        ),
    ),
)
async def test_post_send_ambiguity_is_unknown_and_not_replayed(
    response: httpx.Response | httpx.ReadTimeout,
) -> None:
    """发送后的 5xx/读取超时不能证明未应用，必须只返回 unknown。"""
    route = respx.post(MICROSOFT_SEND_MAIL_URL).mock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=None if isinstance(response, Exception) else response,
    )

    outcome = await _adapter().execute(_command())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert outcome.retryable is False
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_sent_reconciliation_uses_exact_filter_and_minimal_select() -> None:
    """核对必须按稳定 internetMessageId 精确过滤且只读取五个必要字段。"""
    route = respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_fixture("sent_message.json"))
    )

    outcome = await _adapter().reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert outcome.provider_resource_id == "synthetic-sent-message-id"
    assert (
        outcome.provider_url == "https://outlook.office.example.test/mail/synthetic-sent-message-id"
    )
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer synthetic-access-token"
    assert request.headers["Prefer"] == 'IdType="ImmutableId"'
    assert request.url.params["$filter"] == f"internetMessageId eq '{message_id_for(OPERATION_ID)}'"
    assert request.url.params["$select"] == (
        "id,conversationId,internetMessageId,sentDateTime,webLink"
    )


@pytest.mark.asyncio
@respx.mock
async def test_sent_reconciliation_accepts_missing_optional_web_link() -> None:
    """Graph 可省略 webLink；其余事实完整时仍确认发送且链接为空。"""
    payload = _sent_payload()
    values = payload["value"]
    assert isinstance(values, list) and isinstance(values[0], dict)
    values[0].pop("webLink", None)
    respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(return_value=httpx.Response(200, json=payload))

    outcome = await _adapter().reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert outcome.provider_resource_id == "synthetic-sent-message-id"
    assert outcome.provider_url is None


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "web_link",
    (
        "https://evil.example.test/mail/synthetic-sent-message-id",
        "https://evil.office.com/mail/synthetic-sent-message-id",
        "https://outlook.office.com.evil.example.test/mail/synthetic-sent-message-id",
        "https://outlook.office.example.test.evil.test/mail/synthetic-sent-message-id",
        "https://user:password@outlook.office.example.test/mail/synthetic-sent-message-id",
        "https://outlook.office.example.test:443/mail/synthetic-sent-message-id",
        "https://outlook.office.example.test/mail/synthetic-sent-message-id#fragment",
        "https://outlook.office.example.test\\mail\\synthetic-sent-message-id",
        "https://outlook.office.example.test/mail/synthetic-sent-message-id ",
        "https://outlook.office.example.test/mail/synthetic-sent-message-id?access_token=synthetic",
        "https://outlook.office.example.test/mail/synthetic-sent-message-id?next=%2e%2e%2fsecret",
        "https://outlook.office.example.test/mail/%2e%2e/secret",
        "https://outlook.office.example.test/mail/synthetic-sent-message-id%00",
    ),
)
async def test_sent_reconciliation_rejects_unsafe_or_spoofed_web_links(
    web_link: str,
) -> None:
    """webLink 只允许精确 Outlook 主机和安全 URL 形状。"""
    respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_sent_payload(webLink=web_link))
    )

    outcome = await _adapter().reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert outcome.provider_url is None


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "web_link",
    (
        "https://outlook.office.example.test/mail/synthetic-sent-message-id",
        "https://outlook.example.test/mail/synthetic-sent-message-id",
        "https://outlook.office.com/mail/synthetic-sent-message-id",
        "https://outlook.office365.com/mail/synthetic-sent-message-id",
        "https://outlook.live.com/mail/synthetic-sent-message-id",
        "https://outlook.com/mail/synthetic-sent-message-id",
    ),
)
async def test_sent_reconciliation_allows_exact_outlook_hosts(web_link: str) -> None:
    """测试域名与明确的 Outlook 生产主机均可作为展示链接。"""
    respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_sent_payload(webLink=web_link))
    )

    outcome = await _adapter().reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert outcome.provider_url == web_link


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("mode", (MailMode.REPLY, MailMode.REPLY_ALL))
async def test_reply_reconciliation_requires_matching_source_conversation(
    mode: MailMode,
) -> None:
    """回复核对必须证明 Sent 项 conversationId 等于冻结 source_thread_id。"""
    command = _command(mode=mode)
    payload = _sent_payload(conversationId="different-conversation-id")
    respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(return_value=httpx.Response(200, json=payload))

    outcome = await _adapter().reconcile(command, _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("mode", (MailMode.REPLY, MailMode.REPLY_ALL))
async def test_reply_reconciliation_accepts_matching_source_conversation(
    mode: MailMode,
) -> None:
    """回复 Sent 项 conversationId 与冻结来源一致时才能确认。"""
    command = _command(mode=mode)
    assert command.source_thread_id is not None
    payload = _sent_payload(conversationId=command.source_thread_id)
    respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(return_value=httpx.Response(200, json=payload))

    outcome = await _adapter().reconcile(command, _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "sent_date",
    (
        "2030-01-01 00:00:01Z",
        "2030-01-01T00:00:01",
        "2030-01-01T00:00:01+0000",
        "2030-01-01t00:00:01Z",
        "2030-01-01T00:00:01z",
        "2030-01-01T00:00:01.12345678Z",
        "2030-01-01T00:00:01,123Z",
        "2030-01-01T00:00:01+24:00",
    ),
)
async def test_sent_reconciliation_rejects_non_strict_rfc3339_datetime(
    sent_date: str,
) -> None:
    """sentDateTime 只接受严格 T、Z/带冒号 offset 与最多七位小数。"""
    respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_sent_payload(sentDateTime=sent_date))
    )

    outcome = await _adapter().reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "sent_date",
    (
        "2030-01-01T00:00:01Z",
        "2030-01-01T00:00:01.1234567+05:30",
        "2030-01-01T00:00:01-08:00",
    ),
)
async def test_sent_reconciliation_accepts_strict_rfc3339_datetime(sent_date: str) -> None:
    """合法 RFC3339 时间应保留可确认语义。"""
    respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=_sent_payload(sentDateTime=sent_date))
    )

    outcome = await _adapter().reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "next_link",
    (
        f" {MICROSOFT_SENT_MESSAGES_URL}?$skiptoken=synthetic-next",
        f"{MICROSOFT_SENT_MESSAGES_URL}?$skiptoken=synthetic-next ",
        f"\n{MICROSOFT_SENT_MESSAGES_URL}?$skiptoken=synthetic-next",
        f"{MICROSOFT_SENT_MESSAGES_URL}?$skiptoken=synthetic-next\n",
        f"{MICROSOFT_SENT_MESSAGES_URL}\x00?$skiptoken=synthetic-next",
        "https://graph.microsoft.com.evil.example.test/v1.0/me/mailFolders/sentitems/messages?$skiptoken=synthetic-next",
        "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages/other?$skiptoken=synthetic-next",
        "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages%2f?$skiptoken=synthetic-next",
        "https://graph.microsoft.com:443/v1.0/me/mailFolders/sentitems/messages?$skiptoken=synthetic-next",
    ),
)
async def test_sent_reconciliation_rejects_untrusted_next_link_before_request(
    next_link: str,
) -> None:
    """nextLink 先验证首尾空白/控制、精确 host/path，再决定是否跟随。"""
    requests: list[httpx.Request] = []

    class RecordingClient:
        """接受任意字符串 URL 的测试 client，用于证明 adapter 在校验前不发第二次 GET。"""

        async def get(self, url: str, **_: object) -> httpx.Response:
            requests.append(httpx.Request("GET", "https://recorded.example.test"))
            return httpx.Response(
                200,
                json={
                    "value": [],
                    "@odata.nextLink": next_link,
                }
                if len(requests) == 1
                else {"value": []},
            )

    adapter = _adapter(client=cast(httpx.AsyncClient, RecordingClient()))

    outcome = await adapter.reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert len(requests) == 1


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("source_id", (".", ".."))
async def test_reply_source_dot_segment_is_rejected_before_graph_request(source_id: str) -> None:
    """回复 source message ID 的精确 dot-segment 不能进入 Graph path。"""
    command = replace(_command(mode=MailMode.REPLY), source_message_id=source_id)
    route = respx.post(f"{MICROSOFT_GRAPH_BASE_URL}/me/messages/{source_id}/reply").mock(
        return_value=httpx.Response(202)
    )

    with pytest.raises(ValueError):
        await _adapter().execute(command)

    assert route.call_count == 0


@pytest.mark.parametrize("field_name", ("source_thread_id", "source_message_id"))
def test_preflight_source_dot_segment_raises_exact_state_conflict(field_name: str) -> None:
    """审批预检遇到不可绑定的 dot-segment 来源时必须抛出精确 StateConflictError。"""
    command = replace(_command(mode=MailMode.REPLY), **{field_name: ".."})

    with pytest.raises(StateConflictError) as raised:
        _adapter().validate_for_approval(command)

    assert type(raised.value) is StateConflictError
    assert raised.value.error_code == "mail_thread_binding_conflict"


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("provider_id", (".", ".."))
async def test_sent_reconciliation_rejects_provider_dot_segment_ids(provider_id: str) -> None:
    """Sent 候选的 provider ID 与 conversation ID 都拒绝精确 dot-segment。"""
    payload = _sent_payload(id=provider_id)
    respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(return_value=httpx.Response(200, json=payload))

    outcome = await _adapter().reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "payload",
    (
        {"value": []},
        {
            "value": [
                {
                    "id": "synthetic-one",
                    "conversationId": "synthetic-conversation",
                    "internetMessageId": message_id_for(OPERATION_ID),
                    "sentDateTime": "2030-01-01T00:00:01Z",
                    "webLink": "https://outlook.office.example.test/mail/one",
                },
                {
                    "id": "synthetic-two",
                    "conversationId": "synthetic-conversation",
                    "internetMessageId": message_id_for(OPERATION_ID),
                    "sentDateTime": "2030-01-01T00:00:02Z",
                    "webLink": "https://outlook.office.example.test/mail/two",
                },
            ]
        },
    ),
)
async def test_sent_reconciliation_without_unique_match_stays_unknown(
    payload: dict[str, object],
) -> None:
    """无匹配或矛盾重复匹配都不能伪造发送结论。"""
    route = respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=payload)
    )

    outcome = await _adapter().reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_reconciliation_follows_safe_pages_without_any_second_write() -> None:
    """分页核对只继续 GET，不能在首个候选后重新发送。"""
    next_url = f"{MICROSOFT_SENT_MESSAGES_URL}?$skiptoken=synthetic-next"
    first = respx.get(MICROSOFT_SENT_MESSAGES_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"value": [], "@odata.nextLink": next_url},
            ),
            httpx.Response(200, json=_fixture("sent_message.json")),
        ]
    )
    send = respx.post(MICROSOFT_SEND_MAIL_URL).mock(return_value=httpx.Response(500))

    outcome = await _adapter().reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
    assert first.call_count == 2
    assert send.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_reconciliation_rejects_attacker_next_link_before_request() -> None:
    """分页 URL 被篡改时必须停止，不能把 Bearer token 发给攻击者。"""
    attacker = "https://attacker.example.test/collect?token=synthetic"
    respx.get(MICROSOFT_SENT_MESSAGES_URL).respond(
        200,
        json={"value": [], "@odata.nextLink": attacker},
    )
    sink = respx.get(attacker).mock(return_value=httpx.Response(200, json={"value": []}))

    outcome = await _adapter().reconcile(_command(), _execution())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert not sink.called


@pytest.mark.asyncio
@respx.mock
async def test_coordinator_reentry_after_401_uses_new_adapter_once() -> None:
    """外部 coordinator 重组新 token 后可显式重入，单次重入不产生 adapter 内循环。"""
    route = respx.post(MICROSOFT_SEND_MAIL_URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(202)]
    )
    first = await MicrosoftMailWriteAdapter(
        access_token="synthetic-expired-token",
        account_email="sender@example.test",
    ).execute(_command())
    second = await MicrosoftMailWriteAdapter(
        access_token="synthetic-refreshed-token",
        account_email="sender@example.test",
    ).execute(_command())

    assert first.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
    assert second.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert route.call_count == 2


@pytest.mark.parametrize("account_email", ("personal@example.test", "work@example.test"))
def test_personal_and_work_accounts_share_the_same_safe_preflight(account_email: str) -> None:
    """个人与工作连接只由受控 From 事实区分，均使用同一 direct MIME 契约。"""
    adapter = MicrosoftMailWriteAdapter(
        access_token="synthetic-access-token",
        account_email=account_email,
    )
    assert adapter.validate_for_approval(_command()).warnings == ()
