"""复现 Graph 返回的括号式 Delta URL，并验证资源身份与 opaque query 边界。

供应商 HTTP 全部使用合成响应；这些测试直接消费两个正式适配器的初始/增量页面，既防止
合法括号式链接被拒，也防止兼容修复放宽到其他账户、其他资源或错误的 OData 字符串键。
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal

import pytest
import respx

from ai_employee.application.ports.calendar import CalendarSyncPage
from ai_employee.application.ports.mail import MailSyncPage
from ai_employee.domain.errors import PermanentProviderError
from ai_employee.integrations.microsoft.calendar import MicrosoftCalendarAdapter
from ai_employee.integrations.microsoft.mail import MicrosoftMailAdapter

_Kind = Literal["mail", "calendar"]
_Adapter = MicrosoftMailAdapter | MicrosoftCalendarAdapter
_Page = MailSyncPage | CalendarSyncPage
_BASE = "https://graph.microsoft.com/v1.0/me"
_NOW = datetime(2030, 1, 9, tzinfo=UTC)


def _adapter(kind: _Kind) -> _Adapter:
    """以固定合成凭据和时间构造供应商真实读适配器，不接触数据库或真实账号。"""
    if kind == "mail":
        return MicrosoftMailAdapter(access_token="synthetic-access-token")
    return MicrosoftCalendarAdapter(
        access_token="synthetic-access-token", user_timezone="UTC", now=lambda: _NOW
    )


def _initial(adapter: _Adapter, scope: str) -> AsyncIterator[_Page]:
    """适配两种初始窗口签名，保留正式实现的 URL 构造和安全校验。"""
    if isinstance(adapter, MicrosoftMailAdapter):
        return adapter.initial_pages(scope, since=_NOW)
    return adapter.initial_pages(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("mail", "calendar"))
@pytest.mark.parametrize(
    ("scope", "path_id", "odata_id"),
    (
        ("synthetic-scope", "synthetic-scope", "synthetic-scope"),
        pytest.param(
            "synthetic-scope==", "synthetic-scope%3D%3D", "synthetic-scope==",
            id="unescaped_base64_padding",
        ),
        ("synthetic+A/B=C%", "synthetic%2BA%2FB%3DC%25", "synthetic%2BA%2FB%3DC%25"),
        ("synthetic'scope", "synthetic%27scope", "synthetic%27%27scope"),
    ),
)
@respx.mock
async def test_initial_and_incremental_accept_exact_odata_key_with_opaque_query(
    kind: _Kind, scope: str, path_id: str, odata_id: str
) -> None:
    """合法括号式 next/deltaLink 可推进并恢复，编码后的查询字节保持原样。"""
    collection, suffix = (
        ("mailFolders", "messages/delta")
        if kind == "mail"
        else ("calendars", "calendarView/delta")
    )
    initial_url = f"{_BASE}/{collection}/{path_id}/{suffix}"
    keyed_url = f"{_BASE}/{collection}('{odata_id}')/{suffix}"
    next_url = f"{keyed_url}?$skiptoken=synthetic%2Bopaque%3D&opaque=keep%2Fexact"
    final_url = f"{keyed_url}?$deltatoken=synthetic%2Fopaque%3D&opaque=keep%2Bexact"
    respx.get(initial_url).respond(200, json={"value": [], "@odata.nextLink": next_url})
    next_route = respx.get(next_url).respond(
        200, json={"value": [], "@odata.deltaLink": final_url}
    )
    final_route = respx.get(final_url).respond(
        200, json={"value": [], "@odata.deltaLink": final_url}
    )
    adapter = _adapter(kind)

    pages = [page async for page in _initial(adapter, scope)]
    resumed = [page async for page in adapter.sync_pages(scope, final_url)]

    assert len(pages) == 2 and len(resumed) == 1
    assert pages[0].next_page_token == next_url
    assert pages[1].next_cursor == final_url
    assert resumed[0].next_cursor == final_url
    assert str(next_route.calls[0].request.url) == next_url
    assert str(final_route.calls[0].request.url) == final_url
    assert next_route.calls[0].request.headers["Authorization"] == "Bearer synthetic-access-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("mail", "calendar"))
@pytest.mark.parametrize(
    "path_template",
    (
        "/v1.0/me/{collection}('other-scope')/{suffix}",
        "/v1.0/me/{collection}('SYNTHETIC-scope')/{suffix}",
        "/v1.0/users('synthetic-user')/{collection}('synthetic-scope')/{suffix}",
        "/v1.0/me/{collection}(id='synthetic-scope')/{suffix}",
        "/v1.0/me/{collection}('synthetic-scope')/unexpected/{suffix}",
        "/v1.0/me/{collection}('synthetic-scope')/../{suffix}",
        "/v1.0/me/{collection}('synthetic%252Dscope')/{suffix}",
        "/v1.0/me/{collection}%28%27synthetic-scope%27%29/{suffix}",
    ),
)
@respx.mock
async def test_odata_cursor_rejects_other_identity_or_route_before_http(
    kind: _Kind, path_template: str
) -> None:
    """仅兼容精确键的两种 path 表示，未知账户/资源/语法不得收到任何请求。"""
    collection, suffix = (
        ("mailFolders", "messages/delta")
        if kind == "mail"
        else ("calendars", "calendarView/delta")
    )
    path = path_template.format(collection=collection, suffix=suffix)
    cursor = f"https://graph.microsoft.com{path}?$deltatoken=synthetic-sensitive-cursor"

    with pytest.raises(PermanentProviderError) as raised:
        [page async for page in _adapter(kind).sync_pages("synthetic-scope", cursor)]

    assert raised.value.error_code == f"microsoft_{kind}_invalid_delta_url"
    assert "synthetic-sensitive-cursor" not in str(raised.value)
    assert not respx.calls
