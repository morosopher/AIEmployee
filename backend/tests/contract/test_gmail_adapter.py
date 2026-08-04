"""验证 Gmail REST 适配器只读取必要内容并收窄为稳定端口类型。"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from ai_employee.application.ports.gmail import UserActionRequiredError
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

    async def refresh() -> str:
        """返回合成轮换后的 access token 并记录刷新次数。"""
        refreshed.append("called")
        return "rotated-access"

    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        side_effect=[httpx.Response(401), httpx.Response(401)]
    )
    adapter = GmailAdapter(access_token="synthetic-access", refresh_access_token=refresh)

    with pytest.raises(UserActionRequiredError):
        [page async for page in adapter.initial_pages()]

    assert refreshed == ["called"]


@pytest.mark.asyncio
@respx.mock
async def test_refresh_aware_request_execution_is_public_port_operation() -> None:
    """调用方可通过稳定公开方法复用一次 401 刷新重试，不依赖适配器私有实现。"""
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        return_value=httpx.Response(200, json={"messages": [], "historyId": "101"})
    )

    payload = await GmailAdapter(access_token="synthetic-access").execute_request("/messages", {})

    assert payload == {"messages": [], "historyId": "101"}
