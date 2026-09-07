"""Microsoft Graph 邮件读取适配器的脱敏 HTTP 契约测试。

所有供应商请求都由 ``respx`` 在固定 Graph 主机处拦截，fixture 只含合成 ID、正文与
``.example.test`` 地址。测试覆盖 folder discovery、逐 folder Delta、不可变消息 ID、
受限错误分类与 SSRF/分页边界，绝不访问真实 Microsoft 账户。
"""

from __future__ import annotations

import importlib
import json
import traceback
from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Self
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from ai_employee.application.ports.mail import MailCursorExpiredError
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MAIL_FOLDERS_URL = f"{GRAPH_BASE_URL}/me/mailFolders"
INBOX_DELTA_URL = f"{GRAPH_BASE_URL}/me/mailFolders/synthetic-folder-inbox/messages/delta"
NEXT_URL = f"{INBOX_DELTA_URL}?$skiptoken=synthetic-next-1"
DELTA_URL = f"{INBOX_DELTA_URL}?$deltatoken=synthetic-delta-2"
IMMUTABLE_ID_PREFER = 'IdType="ImmutableId"'


def _mail_module() -> ModuleType:
    """延迟导入待实现模块，使 RED 表现为测试失败而不是收集期错误。"""
    return importlib.import_module("ai_employee.integrations.microsoft.mail")


def _adapter(**kwargs: object) -> object:
    """使用固定合成 token 构造待实现 Microsoft mail adapter。"""
    return _mail_module().MicrosoftMailAdapter(  # type: ignore[attr-defined]
        access_token="synthetic-access-token",
        **kwargs,
    )


def _fixture(name: str) -> dict[str, object]:
    """读取一个 JSON object fixture，并复制后交给测试安全修改。"""
    payload = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


async def _collect(pages: AsyncIterator[object]) -> tuple[object, ...]:
    """完整消费异步页，以触发延迟 URL 校验和 HTTP 请求。"""
    return tuple([page async for page in pages])


def _assert_immutable_request(request: httpx.Request) -> None:
    """证明每次 Graph 邮件读取都使用 Bearer 与不可变消息 ID。"""
    assert request.headers["Authorization"] == "Bearer synthetic-access-token"
    assert IMMUTABLE_ID_PREFER in request.headers["Prefer"]


@pytest.mark.asyncio
@respx.mock
async def test_folder_discovery_uses_exact_query_and_filters_excluded_folders() -> None:
    """目录读取保留 Sent，同时排除 Drafts/Deleted/Junk 并发送 ImmutableId header。"""
    route = respx.get(MAIL_FOLDERS_URL).respond(200, json=_fixture("mail_folders.json"))

    scopes = await _adapter().list_sync_scopes()  # type: ignore[attr-defined]

    assert tuple(scope.scope_key for scope in scopes) == (
        "synthetic-folder-inbox",
        "synthetic-folder-sent",
        "synthetic-folder-archive",
    )
    assert "sentitems" in {scope.well_known_name for scope in scopes}
    assert "drafts" not in {scope.well_known_name for scope in scopes}
    assert "deleteditems" not in {scope.well_known_name for scope in scopes}
    assert "junkemail" not in {scope.well_known_name for scope in scopes}
    request = route.calls[0].request
    assert dict(request.url.params) == {
        "includeHiddenFolders": "false",
        "$select": "id,displayName,wellKnownName",
    }
    _assert_immutable_request(request)


@pytest.mark.asyncio
@respx.mock
async def test_initial_delta_uses_explicit_utc_lower_bound_and_final_delta_link() -> None:
    """初始读取必须使用调用方 UTC 下界、跟随 nextLink，并只在末页输出 deltaLink。"""
    first = respx.get(
        INBOX_DELTA_URL,
        params={
            "$filter": "receivedDateTime ge 2030-01-01T08:15:00Z",
            "$select": "id,conversationId,internetMessageId,from,replyTo,toRecipients,ccRecipients,bccRecipients,subject,body,receivedDateTime,sentDateTime,lastModifiedDateTime,categories,webLink",
        },
    ).respond(200, json=_fixture("mail_delta_initial.json"))
    second = respx.get(NEXT_URL).respond(200, json=_fixture("mail_delta_incremental.json"))

    pages = await _collect(
        _adapter().initial_pages(  # type: ignore[attr-defined]
            "synthetic-folder-inbox",
            since=datetime(2030, 1, 1, 8, 15, tzinfo=UTC),
        )
    )

    assert len(pages) == 2
    first_page = pages[0]
    final_page = pages[1]
    assert first_page.next_page_token == NEXT_URL  # type: ignore[attr-defined]
    assert first_page.next_cursor is None  # type: ignore[attr-defined]
    assert final_page.next_page_token is None  # type: ignore[attr-defined]
    assert final_page.next_cursor == DELTA_URL  # type: ignore[attr-defined]
    assert [message.provider_message_id for message in first_page.messages] == [  # type: ignore[attr-defined]
        "synthetic-message-1"
    ]
    normalized = final_page.messages[0]  # type: ignore[attr-defined]
    assert normalized.provider_thread_id == "synthetic-conversation-1"
    assert normalized.provider_conversation_id == "synthetic-conversation-1"
    assert normalized.internet_message_id == "<synthetic-message-2@example.test>"
    assert normalized.sender == {"name": "Synthetic Sender", "email": "sender@example.test"}
    assert normalized.recipients == (
        {"name": "Synthetic Recipient", "email": "recipient@example.test"},
        {"name": "Synthetic Bcc", "email": "bcc@example.test"},
    )
    assert normalized.sanitized_body == "Synthetic incremental body"
    assert normalized.received_at == datetime(2030, 1, 8, 10, 30, tzinfo=UTC)
    assert normalized.provider_updated_at == datetime(2030, 1, 8, 10, 31, tzinfo=UTC)
    assert normalized.mailbox_scope_key == "synthetic-folder-inbox"
    assert [removal.provider_message_id for removal in final_page.removals] == [  # type: ignore[attr-defined]
        "synthetic-message-removed"
    ]
    assert final_page.removals[0].reason == "deleted"  # type: ignore[attr-defined]

    query = parse_qs(first.calls[0].request.url.query.decode("ascii"))
    assert query["$filter"] == ["receivedDateTime ge 2030-01-01T08:15:00Z"]
    assert "id" in query["$select"][0]
    assert "conversationId" in query["$select"][0]
    assert "body" in query["$select"][0]
    _assert_immutable_request(first.calls[0].request)
    _assert_immutable_request(second.calls[0].request)


@pytest.mark.asyncio
async def test_initial_delta_rejects_naive_or_non_utc_lower_bound() -> None:
    """七日下界必须是显式 UTC，不能依赖宿主机或隐式转换其他时区。"""
    adapter = _adapter()
    with pytest.raises(ValueError, match="UTC"):
        await _collect(
            adapter.initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC).replace(tzinfo=None),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor",
    (
        "http://graph.microsoft.com/v1.0/me/messages/delta?token=synthetic",
        "https://graph.microsoft.com:443/v1.0/me/messages/delta?token=synthetic",
        "https://graph.microsoft.com:invalid/v1.0/me/messages/delta?token=synthetic",
        "https://graph.microsoft.com.evil.example.test/v1.0/me/messages/delta?token=synthetic",
        "https://synthetic-user@graph.microsoft.com/v1.0/me/messages/delta?token=synthetic",
        "/v1.0/me/messages/delta?token=synthetic",
    ),
)
async def test_sync_cursor_rejects_noncanonical_absolute_graph_urls(cursor: str) -> None:
    """opaque Delta cursor 只能是无 userinfo/port 的精确 HTTPS Graph absolute URL。"""
    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            _adapter().sync_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                cursor,
            )
        )
    assert raised.value.error_code == "microsoft_mail_invalid_delta_url"
    assert cursor not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_delta_rejects_arbitrary_next_link_without_requesting_it() -> None:
    """分页 URL 的 host 被篡改时必须 fail closed，不能形成 SSRF 或 token 泄露。"""
    payload = _fixture("mail_delta_initial.json")
    payload["@odata.nextLink"] = (
        "https://attacker.example.test/collect?$skiptoken=synthetic-sensitive-next"
    )
    respx.get(INBOX_DELTA_URL).respond(200, json=payload)
    attacker = respx.get("https://attacker.example.test/collect").respond(200, json={})

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            _adapter().initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
    assert raised.value.error_code == "microsoft_mail_invalid_delta_url"
    assert not attacker.called
    assert "synthetic-sensitive-next" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_delta_rejects_same_host_next_link_for_another_folder() -> None:
    """同一 Graph 主机也不能把一个 folder 的 Delta 链切换到另一个 folder。"""
    payload = _fixture("mail_delta_initial.json")
    other_folder_link = (
        f"{GRAPH_BASE_URL}/me/mailFolders/synthetic-folder-sent/messages/delta"
        "?$skiptoken=synthetic-other-folder"
    )
    payload["@odata.nextLink"] = other_folder_link
    respx.get(INBOX_DELTA_URL).respond(200, json=payload)
    other_folder = respx.get(other_folder_link).respond(200, json={})

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            _adapter().initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    assert raised.value.error_code == "microsoft_mail_invalid_delta_url"
    assert not other_folder.called
    assert other_folder_link not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_collection_rejects_same_host_next_link_with_wrong_collection_path() -> None:
    """普通 collection 的同主机 nextLink 也必须绑定预期 path，不能只校验 host。"""
    payload = _fixture("mail_folders.json")
    wrong_path = f"{GRAPH_BASE_URL}/me/messages?$skiptoken=synthetic-wrong-path"
    payload["@odata.nextLink"] = wrong_path
    respx.get(MAIL_FOLDERS_URL).respond(200, json=payload)
    wrong_route = respx.get(wrong_path).respond(200, json={"value": []})

    with pytest.raises(PermanentProviderError) as raised:
        await _adapter().list_sync_scopes()  # type: ignore[attr-defined]

    assert raised.value.error_code == "microsoft_mail_invalid_collection_url"
    assert not wrong_route.called
    assert "synthetic-wrong-path" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_collection_next_link_preserves_opaque_query() -> None:
    """合法 collection nextLink 的 query 必须原样转发，不能重建或丢失 opaque 参数。"""
    payload = _fixture("mail_folders.json")
    opaque_next = (
        f"{MAIL_FOLDERS_URL}?$skiptoken=synthetic%2Bopaque%3Dvalue&opaque=keep%2Fexact"
    )
    payload["@odata.nextLink"] = opaque_next
    first = respx.get(
        MAIL_FOLDERS_URL,
        params={
            "includeHiddenFolders": "false",
            "$select": "id,displayName,wellKnownName",
        },
    ).respond(200, json=payload)
    second = respx.get(opaque_next).respond(200, json={"value": []})

    scopes = await _adapter().list_sync_scopes()  # type: ignore[attr-defined]

    assert scopes
    assert first.called and second.called
    assert str(second.calls[0].request.url) == opaque_next


class _ChunkedResponse:
    """只提供流式读取接口的合成响应，禁止实现退回完整 content 缓冲。"""

    status_code = 200
    headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}

    def __init__(
        self,
        chunks: tuple[bytes, ...],
        *,
        raw_download_totals: tuple[int, ...] | None = None,
    ) -> None:
        self._chunks = chunks
        self._raw_download_totals = raw_download_totals
        self._num_bytes_downloaded = 0
        self.closed = False

    @property
    def content(self) -> bytes:
        """若生产代码读取完整 body，测试应立即失败。"""
        raise AssertionError("streaming response must not access content")

    async def aiter_bytes(self):
        """返回 decoded chunk，并同步暴露 HTTPX raw 下载累计事实。"""
        for index, chunk in enumerate(self._chunks):
            if self._raw_download_totals is None:
                self._num_bytes_downloaded += len(chunk)
            else:
                self._num_bytes_downloaded = self._raw_download_totals[index]
            yield chunk

    @property
    def num_bytes_downloaded(self) -> int:
        """模拟 HTTPX 在内容解码前累计的实际下载字节数。"""
        return self._num_bytes_downloaded

    async def aclose(self) -> None:
        """记录 adapter 是否在超限时关闭响应。"""
        self.closed = True


class _StreamContext:
    """实现 AsyncClient.stream 所需的最小异步上下文。"""

    def __init__(self, response: _ChunkedResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _ChunkedResponse:
        return self.response

    async def __aexit__(self, *args: object) -> None:
        del args
        await self.response.aclose()


class _StreamingClient:
    """替代 HTTPX client，确保 adapter 使用 stream 而非完整缓冲 get。"""

    response: _ChunkedResponse

    def __init__(self, response: _ChunkedResponse) -> None:
        self.response = response

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    def stream(self, *args: object, **kwargs: object) -> _StreamContext:
        del args, kwargs
        return _StreamContext(self.response)

    async def get(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Microsoft mail reads must use AsyncClient.stream")


@pytest.mark.asyncio
async def test_streaming_response_aborts_and_closes_after_byte_limit(monkeypatch) -> None:
    """chunked body 超过单响应预算时应立即中止并关闭，而不是缓冲完整正文。"""
    module = _mail_module()
    monkeypatch.setattr(module, "MICROSOFT_MAIL_MAX_RESPONSE_BYTES", 8)
    response = _ChunkedResponse((b'{"value":', b"123456789", b"}"))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: _StreamingClient(response))

    with pytest.raises(PermanentProviderError) as raised:
        await module.MicrosoftMailAdapter(access_token="synthetic-token").list_sync_scopes()  # type: ignore[attr-defined]

    assert raised.value.error_code == "microsoft_mail_response_too_large"
    assert response.closed is True


@pytest.mark.asyncio
async def test_streaming_chain_budget_uses_raw_downloaded_bytes(monkeypatch) -> None:
    """decoded JSON 很小时，raw 下载超过链预算仍须立即关闭并安全失败。"""
    module = _mail_module()
    decoded = b'{"value":[]}'
    response = _ChunkedResponse(
        (decoded,),
        raw_download_totals=(module.MICROSOFT_MAIL_MAX_CHAIN_BYTES + 1,),  # type: ignore[attr-defined]
    )
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: _StreamingClient(response))

    with pytest.raises(PermanentProviderError) as raised:
        await module.MicrosoftMailAdapter(access_token="synthetic-token").list_sync_scopes()  # type: ignore[attr-defined]

    assert raised.value.error_code == "microsoft_mail_sync_budget_exceeded"
    assert response.closed is True


@pytest.mark.asyncio
async def test_streaming_normalized_budget_is_independent_from_wire_budget(monkeypatch) -> None:
    """只缩小规范化预算时也必须使用固定链容量错误并关闭响应。"""
    module = _mail_module()
    decoded = b'{"value":[]}'
    monkeypatch.setattr(module, "MICROSOFT_MAIL_MAX_NORMALIZED_BYTES", len(decoded) - 1)
    monkeypatch.setattr(module, "MICROSOFT_MAIL_MAX_CHAIN_BYTES", len(decoded) * 10)
    response = _ChunkedResponse((decoded,))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: _StreamingClient(response))

    with pytest.raises(PermanentProviderError) as raised:
        await module.MicrosoftMailAdapter(access_token="synthetic-token").list_sync_scopes()  # type: ignore[attr-defined]

    assert raised.value.error_code == "microsoft_mail_sync_budget_exceeded"
    assert response.closed is True


@pytest.mark.asyncio
@respx.mock
async def test_delta_chain_budget_fails_before_cursor_pages_are_returned(monkeypatch) -> None:
    """多页 wire 总量超过链预算时必须 fail closed，不能返回可推进的 cursor。"""
    module = _mail_module()
    monkeypatch.setattr(module, "MICROSOFT_MAIL_MAX_CHAIN_BYTES", 2_000, raising=False)
    first_payload = _fixture("mail_delta_initial.json")
    second_payload = _fixture("mail_delta_incremental.json")
    respx.get(
        INBOX_DELTA_URL,
        params={
            "$filter": "receivedDateTime ge 2030-01-01T00:00:00Z",
            "$select": (
                "id,conversationId,internetMessageId,from,replyTo,toRecipients,ccRecipients,"
                "bccRecipients,subject,body,receivedDateTime,sentDateTime,"
                "lastModifiedDateTime,categories,webLink"
            ),
        },
    ).respond(200, json=first_payload)
    respx.get(NEXT_URL).respond(200, json=second_payload)

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            module.MicrosoftMailAdapter(access_token="synthetic-token").initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    assert raised.value.error_code == "microsoft_mail_sync_budget_exceeded"


@pytest.mark.asyncio
@respx.mock
async def test_delta_pagination_cycle_is_rejected_without_logging_opaque_url(caplog) -> None:
    """重复 nextLink 必须在有限页内失败，异常与日志都不得包含 opaque URL。"""
    first_payload = _fixture("mail_delta_initial.json")
    cyclic_payload = deepcopy(first_payload)
    cyclic_payload["value"] = []
    cyclic_payload["@odata.nextLink"] = NEXT_URL
    respx.get(
        INBOX_DELTA_URL,
        params={
            "$filter": "receivedDateTime ge 2030-01-01T00:00:00Z",
            "$select": "id,conversationId,internetMessageId,from,replyTo,toRecipients,ccRecipients,bccRecipients,subject,body,receivedDateTime,sentDateTime,lastModifiedDateTime,categories,webLink",
        },
    ).respond(200, json=first_payload)
    cyclic = respx.get(NEXT_URL).respond(200, json=cyclic_payload)

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            _adapter().initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
    assert raised.value.error_code == "microsoft_mail_pagination_invalid"
    assert cyclic.call_count == 1
    assert NEXT_URL not in str(raised.value)
    assert NEXT_URL not in caplog.text


@pytest.mark.asyncio
@respx.mock
async def test_oversized_response_is_rejected_before_json_parsing() -> None:
    """单页响应超过固定上限时必须永久安全失败，避免无界内存与深层 JSON 处理。"""
    maximum = _mail_module().MICROSOFT_MAIL_MAX_RESPONSE_BYTES  # type: ignore[attr-defined]
    respx.get(MAIL_FOLDERS_URL).respond(
        200,
        content=b"x" * (maximum + 1),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(PermanentProviderError) as raised:
        await _adapter().list_sync_scopes()  # type: ignore[attr-defined]
    assert raised.value.error_code == "microsoft_mail_response_too_large"


@pytest.mark.asyncio
@respx.mock
async def test_redirect_is_not_followed() -> None:
    """Graph 3xx 必须按永久安全错误处理，禁止跟随供应商或代理给出的任意 Location。"""
    respx.get(MAIL_FOLDERS_URL).respond(
        302,
        headers={"Location": "https://redirect.example.test/synthetic-token-sink"},
    )
    redirected = respx.get("https://redirect.example.test/synthetic-token-sink").respond(
        200, json=_fixture("mail_folders.json")
    )
    with pytest.raises(PermanentProviderError) as raised:
        await _adapter().list_sync_scopes()  # type: ignore[attr-defined]
    assert raised.value.error_code == "microsoft_mail_request_rejected"
    assert not redirected.called


@pytest.mark.asyncio
@respx.mock
async def test_cursor_expiry_reports_only_provider_and_exact_folder_scope() -> None:
    """410 只标记当前 folder cursor 失效，错误绝不能携带 delta URL。"""
    respx.get(DELTA_URL).respond(410, json={"error": {"code": "syncStateNotFound"}})

    with pytest.raises(MailCursorExpiredError) as raised:
        await _collect(
            _adapter().sync_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                DELTA_URL,
            )
        )
    assert raised.value.provider == "microsoft"
    assert raised.value.scope_key == "synthetic-folder-inbox"
    assert raised.value.metadata == {
        "provider": "microsoft",
        "scope_key": "synthetic-folder-inbox",
    }
    assert DELTA_URL not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_401_refreshes_once_and_replays_read_with_immutable_id() -> None:
    """只读 folder 请求可在首个 401 后刷新一次，并以新 token 精确重放一次。"""
    refresh_calls = 0

    async def refresh() -> str:
        """返回合成轮换 token 并记录唯一刷新次数。"""
        nonlocal refresh_calls
        refresh_calls += 1
        return "synthetic-refreshed-token"

    route = respx.get(MAIL_FOLDERS_URL).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=_fixture("mail_folders.json")),
        ]
    )
    adapter = _adapter(refresh_access_token=refresh)

    scopes = await adapter.list_sync_scopes()  # type: ignore[attr-defined]

    assert scopes
    assert refresh_calls == 1
    assert route.call_count == 2
    assert route.calls[0].request.headers["Authorization"] == "Bearer synthetic-access-token"
    assert route.calls[1].request.headers["Authorization"] == "Bearer synthetic-refreshed-token"
    assert IMMUTABLE_ID_PREFER in route.calls[0].request.headers["Prefer"]
    assert IMMUTABLE_ID_PREFER in route.calls[1].request.headers["Prefer"]


@pytest.mark.asyncio
@respx.mock
async def test_second_401_requires_reauthorization_without_refresh_loop() -> None:
    """刷新后的第二个 401 必须停止并转用户操作，不能无限重放只读请求。"""
    refresh_calls = 0

    async def refresh() -> str:
        """记录刷新次数以证明最多一次。"""
        nonlocal refresh_calls
        refresh_calls += 1
        return "synthetic-refreshed-token"

    route = respx.get(MAIL_FOLDERS_URL).mock(side_effect=[httpx.Response(401), httpx.Response(401)])
    with pytest.raises(UserActionRequiredError) as raised:
        await _adapter(refresh_access_token=refresh).list_sync_scopes()  # type: ignore[attr-defined]
    assert raised.value.error_code == "microsoft_reauthorization_required"
    assert refresh_calls == 1
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_403_maps_to_mail_capability_action_required() -> None:
    """邮件 scope 被撤销时必须转 capability/action-required，而不是临时重试。"""
    respx.get(MAIL_FOLDERS_URL).respond(403, json={"error": {"code": "ErrorAccessDenied"}})
    with pytest.raises(UserActionRequiredError) as raised:
        await _adapter().list_sync_scopes()  # type: ignore[attr-defined]
    assert raised.value.error_code == "microsoft_mail_permission_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(("header", "expected"), (("37", 37.0), ("invalid", None)))
async def test_429_preserves_only_finite_retry_after(header: str, expected: float | None) -> None:
    """限流只暴露有限非负秒数，响应正文和请求 URL 不进入错误。"""
    with respx.mock:
        respx.get(MAIL_FOLDERS_URL).respond(429, headers={"Retry-After": header})
        with pytest.raises(TransientProviderError) as raised:
            await _adapter().list_sync_scopes()  # type: ignore[attr-defined]
    assert raised.value.error_code == "microsoft_mail_rate_limited"
    assert raised.value.retry_after == expected


@pytest.mark.asyncio
@respx.mock
async def test_5xx_is_transient_and_does_not_expose_body() -> None:
    """Graph 5xx 是可重试读取错误，错误文本不得复制供应商正文。"""
    respx.get(MAIL_FOLDERS_URL).respond(503, text="synthetic-sensitive-provider-body")
    with pytest.raises(TransientProviderError) as raised:
        await _adapter().list_sync_scopes()  # type: ignore[attr-defined]
    assert raised.value.error_code == "microsoft_mail_service_unavailable"
    assert "synthetic-sensitive-provider-body" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_network_error_traceback_does_not_expose_delta_cursor() -> None:
    """网络异常链也不能携带 opaque Delta URL 或底层异常原文。"""
    respx.get(DELTA_URL).mock(
        side_effect=httpx.ReadTimeout(
            "synthetic-sensitive-network-error",
            request=httpx.Request("GET", DELTA_URL),
        )
    )

    with pytest.raises(TransientProviderError) as raised:
        await _collect(
            _adapter().sync_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                DELTA_URL,
            )
        )

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.error_code == "microsoft_mail_timeout"
    assert DELTA_URL not in rendered
    assert "synthetic-sensitive-network-error" not in rendered


@pytest.mark.asyncio
@respx.mock
async def test_malformed_message_field_is_permanent_safe_error() -> None:
    """畸形第三方字段必须在 integrations 边界永久失败且不回显原值。"""
    payload = _fixture("mail_delta_incremental.json")
    messages = payload["value"]
    assert isinstance(messages, list)
    message = messages[0]
    assert isinstance(message, dict)
    message["conversationId"] = ["synthetic-sensitive-malformed-value"]
    respx.get(INBOX_DELTA_URL).respond(200, json=payload)

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            _adapter().initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
    assert raised.value.error_code == "microsoft_mail_invalid_response"
    assert "synthetic-sensitive-malformed-value" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_message_missing_last_modified_datetime_fails_closed() -> None:
    """Microsoft message 缺少供应商版本时不得创建不可排序的本地 projection。"""
    payload = _fixture("mail_delta_incremental.json")
    messages = payload["value"]
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    messages[0].pop("lastModifiedDateTime", None)
    respx.get(INBOX_DELTA_URL).respond(200, json=payload)

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            _adapter().initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    assert raised.value.error_code == "microsoft_mail_invalid_response"


@pytest.mark.asyncio
@respx.mock
async def test_message_malformed_last_modified_datetime_fails_closed() -> None:
    """畸形供应商版本必须永久失败，且错误不回显第三方字段内容。"""
    payload = _fixture("mail_delta_incremental.json")
    messages = payload["value"]
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    messages[0]["lastModifiedDateTime"] = "synthetic-sensitive-invalid-version"
    respx.get(INBOX_DELTA_URL).respond(200, json=payload)

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            _adapter().initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    assert raised.value.error_code == "microsoft_mail_invalid_response"
    assert "synthetic-sensitive-invalid-version" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_malformed_folder_display_name_is_permanent_safe_error() -> None:
    """空目录展示名不能作为本地 scope 事实，并且错误不回显供应商内容。"""
    payload = _fixture("mail_folders.json")
    folders = payload["value"]
    assert isinstance(folders, list) and isinstance(folders[0], dict)
    folders[0]["displayName"] = "\r\n"
    respx.get(MAIL_FOLDERS_URL).respond(200, json=payload)

    with pytest.raises(PermanentProviderError) as raised:
        await _adapter().list_sync_scopes()  # type: ignore[attr-defined]

    assert raised.value.error_code == "microsoft_mail_invalid_response"


@pytest.mark.asyncio
@respx.mock
async def test_malformed_recipient_entry_fails_closed() -> None:
    """收件人数组中的畸形条目必须使整页失败，不能被静默跳过。"""
    payload = _fixture("mail_delta_incremental.json")
    messages = payload["value"]
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    messages[0]["toRecipients"] = ["synthetic-sensitive-malformed-recipient"]
    respx.get(INBOX_DELTA_URL).respond(200, json=payload)

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            _adapter().initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    assert raised.value.error_code == "microsoft_mail_invalid_response"
    assert "synthetic-sensitive-malformed-recipient" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_invalid_graph_recipient_address_fails_closed() -> None:
    """Graph 地址必须通过共享邮箱规范化，不能把不可回复文本写入本地历史。"""
    payload = _fixture("mail_delta_incremental.json")
    messages = payload["value"]
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    recipients = messages[0]["toRecipients"]
    assert isinstance(recipients, list) and isinstance(recipients[0], dict)
    email_address = recipients[0]["emailAddress"]
    assert isinstance(email_address, dict)
    email_address["address"] = "synthetic-sensitive-invalid-address"
    respx.get(INBOX_DELTA_URL).respond(200, json=payload)

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            _adapter().initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    assert raised.value.error_code == "microsoft_mail_invalid_response"
    assert "synthetic-sensitive-invalid-address" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_message_identifier_larger_than_persistence_contract_is_rejected() -> None:
    """Graph message/conversation ID 不能超过共享数据库列的 255 字符边界。"""
    payload = _fixture("mail_delta_incremental.json")
    messages = payload["value"]
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    messages[0]["id"] = "m" * 256
    respx.get(INBOX_DELTA_URL).respond(200, json=payload)

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(
            _adapter().initial_pages(  # type: ignore[attr-defined]
                "synthetic-folder-inbox",
                since=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    assert raised.value.error_code == "microsoft_mail_invalid_response"


@pytest.mark.asyncio
@respx.mock
async def test_exact_source_message_read_uses_immutable_id_header() -> None:
    """按 folder/message 精确读取源邮件时也必须使用 ImmutableId，避免移动后 ID 漂移。"""
    payload = _fixture("mail_delta_initial.json")
    values = payload["value"]
    assert isinstance(values, list) and isinstance(values[0], dict)
    url = f"{GRAPH_BASE_URL}/me/mailFolders/synthetic-folder-inbox/messages/synthetic-message-1"
    route = respx.get(url).respond(200, json=values[0])

    message = await _adapter().get_message(  # type: ignore[attr-defined]
        "synthetic-folder-inbox",
        "synthetic-message-1",
    )

    assert message.provider_message_id == "synthetic-message-1"
    _assert_immutable_request(route.calls[0].request)
    assert route.calls[0].request.url.params["$select"]


@pytest.mark.asyncio
@respx.mock
async def test_sent_item_read_uses_immutable_id_header_and_utc_bound() -> None:
    """Sent Items 核对读取同样固定 ImmutableId，并使用显式 UTC 时间窗。"""
    payload = _fixture("mail_delta_initial.json")
    payload.pop("@odata.nextLink")
    route = respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/sentitems/messages").respond(
        200, json=payload
    )

    messages = await _adapter().list_sent_messages(  # type: ignore[attr-defined]
        since=datetime(2030, 1, 1, tzinfo=UTC),
        internet_message_id="<synthetic-message-1@example.test>",
    )

    assert [message.provider_message_id for message in messages] == ["synthetic-message-1"]
    _assert_immutable_request(route.calls[0].request)
    query = parse_qs(route.calls[0].request.url.query.decode("ascii"))
    assert query["$filter"] == [
        (
            "sentDateTime ge 2030-01-01T00:00:00Z and "
            "internetMessageId eq '<synthetic-message-1@example.test>'"
        )
    ]


def test_mail_fixtures_contain_only_synthetic_ids_bodies_and_example_addresses() -> None:
    """静态 fixture 必须持续满足脱敏约束，防止后续更新混入真实个人数据。"""
    combined = "\n".join(
        (FIXTURE_DIR / name).read_text(encoding="utf-8")
        for name in (
            "mail_folders.json",
            "mail_delta_initial.json",
            "mail_delta_incremental.json",
        )
    )
    for token in (
        "folder-inbox",
        "folder-sent",
        "folder-archive",
        "folder-drafts",
        "folder-deleted",
        "folder-junk",
        "message-1",
        "message-2",
        "message-removed",
        "conversation-1",
        "user",
        "next-1",
        "delta-2",
    ):
        assert f"synthetic-{token}" in combined
    assert '@example.com"' not in combined
    assert "@gmail.com" not in combined
    assert "@outlook.com" not in combined
    assert "Synthetic initial body" in combined
    assert "Synthetic incremental body" in combined
