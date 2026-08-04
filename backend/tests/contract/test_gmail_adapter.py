"""验证 Gmail REST 适配器只读取必要内容并收窄为稳定端口类型。"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from ai_employee.application.ports.gmail import (
    GmailMessage,
    GmailSyncPage,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.integrations.google.gmail import GmailAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def _message(message_id: str, thread_id: str, history_id: str) -> dict[str, object]:
    """构造包含纯文本、HTML、引用历史和无效块的合成 Gmail full 格式消息。"""
    return {
        "id": message_id,
        "threadId": thread_id,
        "historyId": history_id,
        "internalDate": "1785571200000",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "synthetic snippet",
        "payload": {
            "headers": [
                {"name": "From", "value": "Ada <ada@example.test>"},
                {"name": "To", "value": "Grace <grace@example.test>"},
                {"name": "Subject", "value": "Status"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": "Q3VycmVudCB1cGRhdGUuCgotLSAKQWRh"},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": "PHA-SWdub3JlPC9wPg"},
                },
            ],
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_initial_page_uses_recent_query_and_fetches_normalized_message() -> None:
    """初次同步仅列出七日内消息，获取 full 消息时不请求任何附件端点。"""
    listed = json.loads((FIXTURES / "gmail_initial.json").read_text(encoding="utf-8"))
    list_route = respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        return_value=httpx.Response(200, json=listed)
    )
    message_route = respx.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/message-1"
    ).mock(return_value=httpx.Response(200, json=_message("message-1", "thread-1", "101")))
    adapter = GmailAdapter(access_token="synthetic-access")

    pages = [page async for page in adapter.initial_pages()]

    assert list_route.called
    assert list_route.calls[0].request.url.params["q"] == "newer_than:7d"
    assert message_route.called
    assert len(pages) == 1
    assert pages[0].messages[0].normalized_body == "Current update."
    assert all("attachments" not in str(call.request.url) for call in respx.calls)


@pytest.mark.asyncio
@respx.mock
async def test_html_fallback_removes_unsafe_and_quoted_content() -> None:
    """没有纯文本时 HTML 会去除脚本、像素、签名、引用历史和空白块。"""
    html_message = _message("message-1", "thread-1", "101")
    payload = html_message["payload"]
    assert isinstance(payload, dict)
    payload["parts"] = [
        {
            "mimeType": "text/html",
            "body": {
                "data": "PGRpdj5IZWxsbzwvZGl2PjxzY3JpcHQ-YmFkKCk8L3NjcmlwdD48aW1nIHdpZHRoPSIxIiBoZWlnaHQ9IjEiIHNyYz0icHgiPjxibG9ja3F1b3RlPlF1b3RlZDwvYmxvY2txdW90ZT48ZGl2Pi0tIDxici9TaWduYXR1cmU8L2Rpdj4="
            },
        }
    ]
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "message-1"}], "historyId": "101"})
    )
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages/message-1").mock(
        return_value=httpx.Response(200, json=html_message)
    )

    pages = [page async for page in GmailAdapter(access_token="synthetic-access").initial_pages()]

    assert pages[0].messages[0].normalized_body == "Hello"


@pytest.mark.asyncio
@respx.mock
async def test_html_fallback_removes_tracking_pixel_without_dimensions() -> None:
    """HTML 的远端像素即使没有宽高属性，也不得残留为同步正文。"""
    html_message = _message("message-1", "thread-1", "101")
    payload = html_message["payload"]
    assert isinstance(payload, dict)
    payload["parts"] = [
        {
            "mimeType": "text/html",
            "body": {
                "data": "PGRpdj5IZWxsbzwvZGl2PjxpbWcgYWx0PSJ0cmFja2VkIHBpeGVsIiBzcmM9Imh0dHBzOi8vdHJhY2suZXhhbXBsZS50ZXN0L29wZW4/dWlkPTEiPjxkaXY+Rm9sbG93LXVwPC9kaXY+"
            },
        }
    ]
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "message-1"}], "historyId": "101"})
    )
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages/message-1").mock(
        return_value=httpx.Response(200, json=html_message)
    )

    pages = [page async for page in GmailAdapter(access_token="synthetic-access").initial_pages()]

    assert pages[0].messages[0].normalized_body == "Hello\nFollow-up"


@pytest.mark.asyncio
@respx.mock
async def test_text_attachment_is_not_normalized_as_message_body() -> None:
    """具有 filename 或 attachment disposition 的文本 part 必须跳过，且不额外下载附件。"""
    attachment_message = _message("message-1", "thread-1", "101")
    payload = attachment_message["payload"]
    assert isinstance(payload, dict)
    payload["parts"] = [
        {
            "mimeType": "text/plain",
            "filename": "private-notes.txt",
            "headers": [
                {"name": "Content-Disposition", "value": "attachment; filename=private-notes.txt"}
            ],
            "body": {"data": "QVRUQUNITUVOVF9TRU5USU5FTA=="},
        },
        {
            "mimeType": "text/plain",
            "headers": [{"name": "Content-Disposition", "value": "attachment"}],
            "body": {"data": "RElTUE9TSVRJT05fU0VOVElORUw="},
        },
        {"mimeType": "text/plain", "body": {"data": "VmlzaWJsZSBtZXNzYWdlIGJvZHk="}},
    ]
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "message-1"}], "historyId": "101"})
    )
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages/message-1").mock(
        return_value=httpx.Response(200, json=attachment_message)
    )

    pages = [page async for page in GmailAdapter(access_token="synthetic-access").initial_pages()]

    assert pages[0].messages[0].normalized_body == "Visible message body"
    assert all("attachments" not in str(call.request.url) for call in respx.calls)


@pytest.mark.asyncio
@respx.mock
async def test_history_maps_added_messages_and_label_changes() -> None:
    """增量 history 同时产生新增邮件及标签变化涉及的消息读取。"""
    history = json.loads((FIXTURES / "gmail_history.json").read_text(encoding="utf-8"))
    route = respx.get("https://gmail.googleapis.com/gmail/v1/users/me/history").mock(
        return_value=httpx.Response(200, json=history)
    )
    for message_id in ("message-1", "message-2"):
        respx.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}").mock(
            return_value=httpx.Response(200, json=_message(message_id, "thread-1", "102"))
        )

    pages = [page async for page in GmailAdapter(access_token="synthetic-access").history_pages("101")]

    assert route.calls[0].request.url.params["startHistoryId"] == "101"
    assert {message.message_id for message in pages[0].messages} == {"message-1", "message-2"}


@pytest.mark.asyncio
@respx.mock
async def test_first_401_refreshes_once_and_second_401_requires_user_action() -> None:
    """401 只触发一次刷新重试，重试仍拒绝时连接必须转为需用户操作。"""
    refreshed: list[str] = []
    expired: list[str] = []

    async def refresh() -> str:
        """返回合成轮换后的 access token 并记录刷新次数。"""
        refreshed.append("called")
        return "rotated-access"

    async def mark_expired() -> None:
        """记录第二次 401 应持久化的连接过期回调。"""
        expired.append("called")

    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        side_effect=[httpx.Response(401), httpx.Response(401)]
    )
    adapter = GmailAdapter(
        access_token="synthetic-access",
        refresh_access_token=refresh,
        mark_expired=mark_expired,
    )

    with pytest.raises(UserActionRequiredError):
        [page async for page in adapter.initial_pages()]

    assert refreshed == ["called"]
    assert expired == ["called"]


@pytest.mark.asyncio
@respx.mock
async def test_transient_google_failure_uses_domain_error_and_retry_after() -> None:
    """429 必须映射为 Durable Worker 可捕获的领域临时错误及 Retry-After。"""
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "37"})
    )

    with pytest.raises(TransientProviderError) as raised:
        await GmailAdapter(access_token="synthetic-access").execute_request("/messages", {})

    assert raised.value.error_code == "google_rate_limited"
    assert raised.value.retry_after == 37


def test_normalized_message_collections_are_deeply_immutable() -> None:
    """端口消息冻结 sender、recipient 和 header 映射，调用方不能事后篡改同步事实。"""
    message = GmailAdapter._normalize_message(_message("message-1", "thread-1", "101"))

    with pytest.raises(TypeError):
        message.sender["email"] = "changed@example.test"  # type: ignore[index]
    with pytest.raises(TypeError):
        message.recipients[0]["email"] = "changed@example.test"  # type: ignore[index]
    with pytest.raises(TypeError):
        message.headers["subject"] = "Changed"  # type: ignore[index]


def test_message_labels_and_page_messages_are_copied_to_tuples() -> None:
    """即使不可信调用方传入列表，端口对象也必须复制为不可变 tuple。"""
    labels = ["INBOX"]
    messages = [GmailAdapter._normalize_message(_message("message-1", "thread-1", "101"))]
    message = GmailAdapter._normalize_message(_message("message-2", "thread-2", "102"))
    mutable_message = GmailMessage(
        message_id=message.message_id,
        thread_id=message.thread_id,
        history_id=message.history_id,
        received_at=message.received_at,
        sender=message.sender,
        recipients=message.recipients,
        subject=message.subject,
        snippet=message.snippet,
        normalized_body=message.normalized_body,
        labels=labels,
        headers=message.headers,
        provider_url=message.provider_url,
    )
    page = GmailSyncPage(
        messages=messages,
        next_page_token=None,
        latest_history_id="102",
    )

    labels.append("STARRED")
    messages.append(mutable_message)

    assert mutable_message.labels == ("INBOX",)
    assert page.messages == (messages[0],)


@pytest.mark.asyncio
@respx.mock
async def test_non_timeout_http_request_error_is_transient_provider_error() -> None:
    """连接、TLS 或协议错误同样必须映射为可重试且脱敏的领域供应商错误。"""
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        side_effect=httpx.ConnectError("synthetic connection failure")
    )

    with pytest.raises(TransientProviderError) as raised:
        await GmailAdapter(access_token="synthetic-access").execute_request("/messages", {})

    assert raised.value.error_code == "google_request_failed"


@pytest.mark.asyncio
@respx.mock
async def test_refresh_aware_request_execution_is_public_port_operation() -> None:
    """调用方可通过稳定公开方法复用一次 401 刷新重试，不依赖适配器私有实现。"""
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        return_value=httpx.Response(200, json={"messages": [], "historyId": "101"})
    )

    payload = await GmailAdapter(access_token="synthetic-access").execute_request("/messages", {})

    assert payload == {"messages": [], "historyId": "101"}
