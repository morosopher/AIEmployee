"""验证 Microsoft Graph CalendarView Delta 的安全 HTTP 与规范化契约。"""

from __future__ import annotations

import copy
import importlib
import json
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
import respx

from ai_employee.application.ports.calendar import CalendarCursorExpiredError, CalendarEvent
from ai_employee.application.use_cases import calendar_proposals
from ai_employee.domain.connections import CapabilityStatus, ConnectionStatus
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
CALENDARS_URL = f"{GRAPH_BASE_URL}/me/calendars"
CALENDAR_ID = "calendar-primary"
DELTA_URL = f"{CALENDARS_URL}/{CALENDAR_ID}/calendarView/delta"
NEXT_URL = f"{DELTA_URL}?$skiptoken=synthetic-next-1"
FINAL_DELTA_URL = f"{DELTA_URL}?$deltatoken=synthetic-delta-2"


def _module():
    """延迟导入待实现 adapter，使 RED 阶段保持可收集。"""
    return importlib.import_module("ai_employee.integrations.microsoft.calendar")


def _fixture(name: str) -> dict[str, object]:
    """读取脱敏 Graph fixture，并返回可安全修改的副本。"""
    value = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def _event_payload() -> dict[str, object]:
    """返回一条结构完整且可按单字段变异的合成 Graph event。"""
    payload = _fixture("calendar_view_delta_initial.json")
    values = payload["value"]
    assert isinstance(values, list) and isinstance(values[0], dict)
    return copy.deepcopy(values[0])


def _single_event_payload() -> dict[str, object]:
    """返回无重复关系的标准事件，供时间和普通字段边界测试。"""
    payload = _event_payload()
    payload["type"] = "singleInstance"
    payload.pop("seriesMasterId", None)
    payload.pop("recurrence", None)
    return payload


def _adapter(**kwargs: object):
    """构造固定合成 token 与显式用户时区的 adapter。"""
    return _module().MicrosoftCalendarAdapter(
        access_token="synthetic-access-token",
        user_timezone="Asia/Shanghai",
        now=lambda: datetime(2030, 1, 9, 8, tzinfo=UTC),
        **kwargs,
    )


async def _collect(pages):
    """完整消费异步迭代器，触发所有延迟分页校验。"""
    return tuple([page async for page in pages])


@pytest.mark.asyncio
@respx.mock
async def test_directory_uses_exact_select_and_projects_default_owner_capabilities() -> None:
    """目录必须保留能力字段，并把无 cursor 终页声明为完整快照。"""
    route = respx.get(CALENDARS_URL).respond(200, json=_fixture("calendars.json"))
    pages = await _collect(_adapter().directory_pages())
    assert len(pages) == 1
    primary, readonly = pages[0].calendars
    assert primary.is_primary is True and primary.can_write is True
    assert primary.can_share is True
    assert primary.hex_color == "#123456"
    assert primary.owner["email"] == "owner@example.test"
    assert readonly.is_primary is False and readonly.can_write is False
    assert readonly.can_share is False
    assert pages[0].next_page_token is None
    assert pages[0].next_cursor is None
    assert pages[0].full_snapshot is True
    assert (
        route.calls[0].request.url.params["$select"]
        == "id,name,isDefaultCalendar,canEdit,canShare,owner,hexColor"
    )


@pytest.mark.asyncio
@respx.mock
async def test_initial_delta_uses_local_window_and_normalizes_versions_people_recurrence() -> None:
    """初始窗口按用户时区计算，事件时间统一 UTC 并保留版本/人员/重复事实。"""
    first = respx.get(
        DELTA_URL,
        params={
            "startDateTime": "2030-01-08T00:00:00+08:00",
            "endDateTime": "2030-02-08T00:00:00+08:00",
        },
    ).respond(200, json=_fixture("calendar_view_delta_initial.json"))
    respx.get(NEXT_URL).respond(200, json=_fixture("calendar_view_delta_incremental.json"))
    pages = await _collect(_adapter().initial_pages(CALENDAR_ID))
    assert len(pages) == 2
    params = parse_qs(first.calls[0].request.url.query.decode("ascii"))
    assert params["startDateTime"][0].startswith("2030-01-08T00:00:00+08:00")
    assert params["endDateTime"][0].startswith("2030-02-08T00:00:00+08:00")
    event = pages[0].events[0]
    assert event.starts_at == datetime(2030, 1, 2, 1, tzinfo=UTC)
    assert event.timezone == "Asia/Shanghai"
    assert event.etag == 'W/"synthetic-etag"'
    assert event.change_key == "synthetic-change-key-1"
    assert event.recurring_event_id == "series-1"
    assert event.organizer == {"name": "Synthetic Organizer", "email": "organizer@example.test"}
    assert event.attendees[0]["email"] == "attendee@example.test"
    assert pages[0].next_page_token == NEXT_URL and pages[0].next_cursor is None
    assert pages[1].next_page_token is None and pages[1].next_cursor == FINAL_DELTA_URL


@pytest.mark.asyncio
@respx.mock
async def test_incremental_keeps_safe_tombstone_and_all_day_boundaries() -> None:
    """Graph removed 项只保留 ID/status；全天日期不得伪造供应商时间。"""
    respx.get(FINAL_DELTA_URL).respond(200, json=_fixture("calendar_view_delta_incremental.json"))
    pages = await _collect(_adapter().sync_pages(CALENDAR_ID, FINAL_DELTA_URL))
    tombstone, all_day = pages[0].events
    assert tombstone.status == "cancelled"
    assert tombstone.starts_at is None and tombstone.ends_at is None
    assert all_day.all_day is True
    assert all_day.starts_at == datetime(2030, 1, 3, 8, tzinfo=UTC)
    assert all_day.ends_at == datetime(2030, 1, 4, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field_name", "limit"),
    (
        ("id", 255),
        ("seriesMasterId", 255),
        ("@odata.etag", 255),
        ("changeKey", 255),
        ("showAs", 32),
        ("status", 32),
        ("accessRole", 32),
    ),
)
def test_event_scalar_lengths_match_shared_persistence_columns(
    field_name: str,
    limit: int,
) -> None:
    """受限 Graph 标量必须接受列上限并在多一个字符时永久失败。"""
    accepted = _event_payload()
    accepted[field_name] = "x" * limit
    _adapter()._normalize_event(accepted, calendar_id=CALENDAR_ID)

    rejected = _event_payload()
    rejected[field_name] = "x" * (limit + 1)
    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(rejected, calendar_id=CALENDAR_ID)
    assert raised.value.error_code == "microsoft_calendar_invalid_response"


def test_calendar_id_length_and_del_are_validated_against_directory_column() -> None:
    """calendar ID 接受 512 字符，但超长或 DEL 都不能越过适配器边界。"""
    payload = _fixture("calendars.json")
    values = payload["value"]
    assert isinstance(values, list) and isinstance(values[0], dict)
    values[0]["id"] = "c" * 512
    assert len(_adapter()._normalize_calendar(values[0]).calendar_id) == 512

    values[0]["id"] = "c" * 513
    with pytest.raises(PermanentProviderError):
        _adapter()._normalize_calendar(values[0])
    values[0]["id"] = "calendar\x7f"
    with pytest.raises(PermanentProviderError):
        _adapter()._normalize_calendar(values[0])


def test_calendar_timezone_projection_rejects_values_longer_than_shared_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使映射边界异常返回超长名称，目录适配器也必须在落库前 fail closed。"""
    monkeypatch.setattr(_module(), "to_iana_timezone", lambda _value: "z" * 65)
    payload = _fixture("calendars.json")
    values = payload["value"]
    assert isinstance(values, list) and isinstance(values[0], dict)

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_calendar(values[0])

    assert raised.value.error_code == "microsoft_calendar_invalid_response"


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("subject", "unsafe\x00title"),
        ("subject", "unsafe\nheader"),
        ("showAs", "busy\x7f"),
    ),
)
def test_display_scalars_reject_c0_del_and_nul(field_name: str, value: str) -> None:
    """展示标量不得通过替换或 trim 静默接受控制字符。"""
    payload = _single_event_payload()
    payload[field_name] = value

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"


def test_body_keeps_line_breaks_but_rejects_unrepresentable_controls() -> None:
    """纯文本正文保留换行/制表，NUL、其他 C0 与 DEL 必须拒绝。"""
    payload = _single_event_payload()
    body = payload["body"]
    assert isinstance(body, dict)
    body["content"] = "line one\nline two\r\n\tindented"
    event = _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)
    assert event.description == "line one\nline two\r\n\tindented"

    for invalid in ("nul\x00value", "control\x01value", "delete\x7fvalue"):
        body["content"] = invalid
        with pytest.raises(PermanentProviderError):
            _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)


async def _read_body_event(*, entry: str, body: object) -> CalendarEvent:
    """通过合成 HTTP 在四个真实读取入口返回事件，保留适配器完整请求与投影行为。"""
    payload = _single_event_payload()
    payload["body"] = body
    adapter = _adapter()
    if entry == "current":
        respx.get(f"{CALENDARS_URL}/{CALENDAR_ID}/events/event-1").respond(200, json=payload)
        event = await adapter.get_current_event(CALENDAR_ID, "event-1")
        assert event is not None
        return event

    response = {"value": [payload], "@odata.deltaLink": FINAL_DELTA_URL}
    if entry == "delta":
        respx.get(FINAL_DELTA_URL).respond(200, json=response)
        pages = await _collect(adapter.sync_pages(CALENDAR_ID, FINAL_DELTA_URL))
    else:
        initial_route = respx.get(
            DELTA_URL,
            params={
                "startDateTime": "2030-01-08T00:00:00+08:00",
                "endDateTime": "2030-02-08T00:00:00+08:00",
            },
        )
        if entry == "next":
            initial_route.respond(200, json={"value": [], "@odata.nextLink": NEXT_URL})
            respx.get(NEXT_URL).respond(200, json=response)
        else:
            assert entry == "initial"
            initial_route.respond(200, json=response)
        pages = await _collect(adapter.initial_pages(CALENDAR_ID))
    return pages[-1].events[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ("initial", "next", "delta", "current"))
@respx.mock
async def test_calendar_body_requests_plain_text_at_every_read_entry(entry: str) -> None:
    """初始、分页、持久 Delta 与精确 GET 都声明纯文本偏好并保留已有文本。"""
    event = await _read_body_event(
        entry=entry,
        body={"contentType": "text", "content": "  第一行\r\n\t第二行\n--\n> 日程引用  "},
    )

    assert event.description == "  第一行\r\n\t第二行\n--\n> 日程引用  "
    assert all(
        call.request.headers.get("Prefer") == 'outlook.body-content-type="text"'
        for call in respx.calls
    )
    assert len(respx.calls) == (2 if entry == "next" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ("initial", "next", "delta", "current"))
@pytest.mark.parametrize(
    ("html", "expected"),
    (
        pytest.param(
            '<html><head><meta charset="utf-8"><style>div { color: black; }</style>'
            "</head><body><div>合成描述 &amp; <span>讨论</span>&nbsp;流程"
            "<br>--<br>&gt; 仍属于日程</div></body></html>",
            "合成描述 & 讨论\u00a0流程\n--\n> 仍属于日程",
            id="graph-wrapper",
        ),
        pytest.param(
            "<div>第一行<div>第二行</div></div>",
            "第一行\n第二行",
            id="block-nested-start",
        ),
        pytest.param(
            "<div>第一行<div>第二行</div>第三行</div><div>第四行</div>",
            "第一行\n第二行\n第三行\n第四行",
            id="block-nested-tail-and-sibling",
        ),
        pytest.param(
            "<div><div>第一行</div></div><div><div>第二行</div></div>",
            "第一行\n第二行",
            id="block-nested-wrappers",
        ),
        pytest.param(
            "第一<span>行</span><p>第<b>二</b>行</p>第三<i>行</i>",
            "第一行\n第二行\n第三行",
            id="block-between-inline-text",
        ),
        pytest.param(
            "<div>第一行<br><br><div>\t第二行 &amp; 行内<span>连接</span></div></div>",
            "第一行\n\n\t第二行 & 行内连接",
            id="block-preserves-breaks-tabs-and-inline",
        ),
        pytest.param(
            '可见<span style="display:/**/none">合成内容</span>',
            "可见",
            id="css-comment-before-display-value",
        ),
        pytest.param(
            '可见<span style="visibility:/**/hidden">合成内容</span>',
            "可见",
            id="css-comment-before-visibility-value",
        ),
        pytest.param(
            '可见<span style="display:none !/**/important">合成内容</span>',
            "可见",
            id="css-comment-in-important",
        ),
        pytest.param(
            '可见<span style="color:black;/*;*/DISPLAY/**/:/**/none/**/!/**/important/**/;">'
            "合成内容</span>",
            "可见",
            id="css-comments-at-token-boundaries",
        ),
        pytest.param(
            '可见<span style="visibility:/*合成\n注释*/collapse">合成内容</span>',
            "可见",
            id="css-multiline-comment",
        ),
        pytest.param(
            '可见<span style="display:n/**/one">合成内容</span>',
            "可见合成内容",
            id="css-comment-cannot-join-value-identifier",
        ),
        pytest.param(
            '可见<span style="visibil/**/ity:hidden">合成内容</span>',
            "可见合成内容",
            id="css-comment-cannot-join-property-identifier",
        ),
        pytest.param(
            '可见<span style="/*;display:none;*/color:black">合成内容</span>',
            "可见合成内容",
            id="css-commented-declaration-stays-visible",
        ),
        pytest.param(
            "可见<span style=\"content:';display:/**/none;'\">合成内容</span>",
            "可见合成内容",
            id="css-quoted-comment-stays-visible",
        ),
        pytest.param(
            "可见<span style=\"content:'/*';display:/**/none\">合成内容</span>",
            "可见",
            id="css-quoted-comment-start-cannot-hide-declaration",
        ),
        pytest.param(
            '可见<span style=\'--start:"/*";display:none;--end:"*/"\'>合成内容</span>',
            "可见",
            id="css-comment-delimiters-in-strings-stay-opaque",
        ),
        pytest.param(
            '可见<span style="--start:url(/*);display:none;--end:url(*/)">合成隐藏内容</span>',
            "可见",
            id="css-url-token-comment-delimiters",
        ),
        pytest.param(
            '可见<span style="--start:URL(/*);display:none;--end:URL(*/)">合成隐藏内容</span>',
            "可见",
            id="css-url-uppercase-token",
        ),
        pytest.param(
            r'可见<span style="--start:u\72 l(/*);display:none;--end:u\72 l(*/)">合成隐藏内容</span>',
            "可见",
            id="css-url-escaped-function-name",
        ),
        pytest.param(
            '可见<span style="--info:func(/* );display:none; */);color:red">合成内容</span>',
            "可见合成内容",
            id="css-url-ordinary-function-comment",
        ),
        pytest.param(
            '可见<span style="--info:url(synthetic;display:none;value);color:red">合成内容</span>',
            "可见合成内容",
            id="css-url-token-contains-no-declaration",
        ),
        pytest.param(
            '可见<span style="--info:func(nested(value);display:none;value);color:red">合成内容</span>',
            "可见合成内容",
            id="css-url-ordinary-function-contains-no-declaration",
        ),
        pytest.param(
            r'可见<span style="--info:url(synthetic\);display:none;value);color:red">合成内容</span>',
            "可见合成内容",
            id="css-url-escaped-close-stays-inside-token",
        ),
        pytest.param(
            '可见<span style="--info:url(&quot;synthetic;display:none;value&quot;);color:red">'
            "合成内容</span>",
            "可见合成内容",
            id="css-url-quoted-value-stays-opaque",
        ),
        pytest.param(
            '可见<span style="--info:func(nested(url(/*)));display:none;--end:url(*/)">'
            "合成隐藏内容</span>",
            "可见",
            id="css-url-nested-function-preserves-following-declaration",
        ),
        # hash/at-keyword 的名称不能重当作 URL；括号中的真实注释必须屏蔽伪声明。
        pytest.param(
            '可见<span style="--start:#url(/*);display:none;--end:url(*/)">合成内容</span>',
            "可见合成内容",
            id="css-url-prefix-hash-token-keeps-comment",
        ),
        pytest.param(
            '可见<span style="--start:@url(/*);display:none;--end:url(*/)">合成内容</span>',
            "可见合成内容",
            id="css-url-prefix-at-keyword-keeps-comment",
        ),
        # 转义分号属于自定义属性值 token；仅未转义的顶层分号才能开启隐藏声明。
        pytest.param(
            r'可见<span style="--note:value\;display:none;">合成内容</span>',
            "可见合成内容",
            id="css-escaped-delimiter-display-stays-visible",
        ),
        pytest.param(
            r'可见<span style="--note:value\;visibility:hidden;">合成内容</span>',
            "可见合成内容",
            id="css-escaped-delimiter-visibility-stays-visible",
        ),
        pytest.param(
            '可见<span style="--note:value;display:none;">合成内容</span>',
            "可见",
            id="css-escaped-delimiter-real-semicolon-control",
        ),
    ),
)
@respx.mock
async def test_calendar_html_body_stays_plain_in_update_before_and_desired(
    entry: str, html: str, expected: str
) -> None:
    """块起止、行内连接与显式隐藏样式在真实 update before/desired 中保真，版本/时间不变。"""
    event = await _read_body_event(
        entry=entry,
        body={"contentType": "html", "content": html},
    )
    assert event.starts_at == datetime(2030, 1, 2, 1, tzinfo=UTC)
    assert event.ends_at == datetime(2030, 1, 2, 2, tzinfo=UTC)
    assert event.etag == 'W/"synthetic-etag"'
    assert event.change_key == "synthetic-change-key-1"
    # 只把供应商中立字段复制到本地快照端口；实际 before 和编辑计算仍由应用层执行。
    local_event = calendar_proposals.CalendarProposalEventSnapshot(
        event_id=UUID(int=1),
        connection_id=UUID(int=2),
        calendar_id=event.calendar_id,
        provider="microsoft",
        provider_event_id=event.event_id,
        title=event.title,
        description=event.description,
        location=event.location,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        all_day=event.all_day,
        timezone=event.timezone,
        attendees=tuple(person["email"] for person in event.attendees),
        recurring_event_id=event.recurring_event_id,
        etag=event.etag,
        status=event.status,
        can_edit=True,
    )
    before = calendar_proposals._content_from_local_event(local_event, operation_id=UUID(int=3))
    target = calendar_proposals.CalendarProposalTargetSnapshot(
        connection_id=UUID(int=2),
        provider="microsoft",
        timezone="Asia/Shanghai",
        connection_status=ConnectionStatus.CONNECTED,
        read_capability_status=CapabilityStatus.ENABLED,
        write_capability_status=CapabilityStatus.ENABLED,
        write_capability_error_code=None,
        calendar_id=CALENDAR_ID,
        can_write=True,
    )
    desired = calendar_proposals._apply_user_changes(
        before,
        {"title": "合成修改标题"},
        operation_kind="update",
        target=target,
        source_event_ids=(str(local_event.event_id),),
    )

    assert before.description == expected
    assert desired.description == expected
    assert desired.changed_fields == ("title",)


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        (None, ""),
        ({"contentType": "text"}, ""),
        ({"contentType": "text", "content": ""}, ""),
        ({"contentType": "TEXT", "content": "\t原样\n保留\r\n"}, "\t原样\n保留\r\n"),
        ({"contentType": "text", "content": "<literal> &amp;"}, "<literal> &amp;"),
        ({"contentType": "html", "content": ""}, ""),
        ({"contentType": "HTML", "content": "<div>合成内容</div>"}, "合成内容"),
        (
            {"contentType": "html", "content": "<div>&amp;lt;原样&amp;gt;</div>"},
            "&lt;原样&gt;",
        ),
        (
            {
                "contentType": "html",
                "content": (
                    "<head><title>hidden-head</title></head><div>可见 "
                    "<strong>内容</strong><script>hidden-script()</script>"
                    "<style>hidden-style</style><template>hidden-template</template>"
                    "<noscript>hidden-noscript</noscript><iframe>hidden-frame</iframe>"
                    "<object>hidden-object</object><span hidden>hidden-attribute</span>"
                    '<span aria-hidden="true">hidden-aria</span>'
                    '<span style="display: none !important">hidden-display</span>'
                    '<span style="visibility: hidden">hidden-visibility</span>'
                    "<!-- hidden-comment --></div>"
                    "<blockquote>&gt; 引用保留</blockquote>"
                    '<div class="signature">--<br>签名保留</div>'
                ),
            },
            "可见 内容\n> 引用保留\n--\n签名保留",
        ),
        ({"contentType": "html", "content": "<div>第一行<br>第二行"}, "第一行\n第二行"),
    ),
)
def test_calendar_body_normalization_preserves_only_declared_text(
    body: object, expected: str
) -> None:
    """按显式类型提取一次实体和可见文本，保留普通文本以及日程签名/引用语义。"""
    payload = _single_event_payload()
    payload["body"] = body

    assert _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID).description == expected


@pytest.mark.parametrize(
    "body",
    (
        {},
        {"content": "<div>synthetic-value</div>"},
        {"contentType": None, "content": "synthetic-value"},
        {"contentType": "", "content": "synthetic-value"},
        {"contentType": "unknownFutureValue", "content": "synthetic-value"},
        {"contentType": " text", "content": "synthetic-value"},
        {"contentType": "text/html", "content": "synthetic-value"},
        {"contentType": 1, "content": "synthetic-value"},
        {"contentType": [], "content": "synthetic-value"},
        {"contentType": {}, "content": "synthetic-value"},
        {"contentType": "text", "content": None},
        {"contentType": "html", "content": 1},
    ),
)
def test_calendar_body_rejects_unknown_or_malformed_types(body: object) -> None:
    """未知、缺失或畸形类型不能猜成纯文本，也不能把原始内容带入固定错误。"""
    payload = _single_event_payload()
    payload["body"] = body

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"
    assert "synthetic-value" not in str(raised.value)


@pytest.mark.parametrize("content_type", ("text", "html"))
@pytest.mark.parametrize("control", ("\x00", "\x01", "\x7f"))
def test_calendar_body_rejects_raw_controls_before_html_extraction(
    content_type: str, control: str
) -> None:
    """即使控制字符藏在主动节点内，也必须保持原始描述边界的拒绝语义。"""
    payload = _single_event_payload()
    payload["body"] = {
        "contentType": content_type,
        "content": f"<script>{control}</script>合成内容",
    }

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"


@pytest.mark.parametrize("entity", ("&#1;", "&#11;", "&#31;", "&#127;"))
def test_calendar_html_body_rejects_controls_decoded_from_entities(entity: str) -> None:
    """HTML 实体解码的 C0/DEL 必须在 trim 前拒绝，不能被当作边缘空白丢弃。"""
    payload = _single_event_payload()
    payload["body"] = {"contentType": "html", "content": f"<div>{entity}</div>"}

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"


@pytest.mark.parametrize("content_type", ("text", "html"))
def test_calendar_body_keeps_original_length_limit(content_type: str) -> None:
    """HTML 外壳也占用原始 16,384 字符预算，不因最终可见文本很短而绕过限制。"""
    payload = _single_event_payload()
    if content_type == "html":
        content = "<div>" + "x" * (16_384 - 11) + "</div>"
        expected = "x" * (16_384 - 11)
    else:
        content = "x" * 16_384
        expected = content
    payload["body"] = {"contentType": content_type, "content": content}
    assert _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID).description == expected

    payload["body"] = {"contentType": content_type, "content": content + "x"}
    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"


@pytest.mark.parametrize(
    "address",
    (
        "not-an-address",
        "Display Name <person@example.test>",
        "person@example.test\nbcc:other@example.test",
    ),
)
def test_organizer_address_uses_strict_mailbox_normalization(address: str) -> None:
    """组织者和参会人只能输出规范单一 addr-spec，不能保留 Header 文本。"""
    payload = _single_event_payload()
    organizer = payload["organizer"]
    assert isinstance(organizer, dict)
    email_address = organizer["emailAddress"]
    assert isinstance(email_address, dict)
    email_address["address"] = address

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"


@pytest.mark.parametrize(
    ("person_field", "address"),
    (
        ("organizer", "person\t@example.test"),
        ("attendee", "person\t@example.test"),
        ("organizer", "person\x01@example.test"),
        ("attendee", "person\x7f@example.test"),
    ),
)
def test_organizer_and_attendee_addresses_reject_controls_before_normalization(
    person_field: str,
    address: str,
) -> None:
    """原始人员地址的所有 C0/DEL 必须在共享 mailbox normalizer 前被拒绝。"""
    payload = _single_event_payload()
    if person_field == "organizer":
        person = payload["organizer"]
    else:
        attendees = payload["attendees"]
        assert isinstance(attendees, list) and isinstance(attendees[0], dict)
        person = attendees[0]
    assert isinstance(person, dict)
    email_address = person["emailAddress"]
    assert isinstance(email_address, dict)
    email_address["address"] = address

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"
    assert address not in str(raised.value)


@pytest.mark.parametrize(
    "address",
    (
        "owner\t@example.test",
        "owner\x01@example.test",
        "owner\x7f@example.test",
    ),
)
def test_calendar_owner_address_controls_fail_closed(address: str) -> None:
    """owner 复用人员边界；非法控制字符不能被规范后投影为可信 owner。"""
    payload = _fixture("calendars.json")
    values = payload["value"]
    assert isinstance(values, list) and isinstance(values[0], dict)
    owner = values[0]["owner"]
    assert isinstance(owner, dict)
    email_address = owner["emailAddress"]
    assert isinstance(email_address, dict)
    email_address["address"] = address

    calendar = _adapter()._normalize_calendar(values[0])

    assert calendar.owner is None


def test_mailbox_domain_is_canonicalized_without_changing_local_part() -> None:
    """有效 Graph 邮箱通过共享领域函数规范域名，同时保留 local-part 大小写。"""
    payload = _single_event_payload()
    organizer = payload["organizer"]
    assert isinstance(organizer, dict)
    email_address = organizer["emailAddress"]
    assert isinstance(email_address, dict)
    email_address["address"] = "Synthetic.Local@EXAMPLE.TEST"

    event = _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert event.organizer is not None
    assert event.organizer["email"] == "Synthetic.Local@example.test"


@pytest.mark.parametrize(
    "url",
    (
        "http://outlook.example.test/event",
        "https://user@outlook.example.test/event",
        "https://outlook.example.test/event#fragment",
        "https:///missing-host",
        "https://outlook.example.test/unsafe\x00path",
    ),
)
def test_provider_url_rejects_unsafe_absolute_values(url: str) -> None:
    """供应商 URL 一旦出现就必须是无 userinfo/fragment/control 的绝对 HTTPS。"""
    payload = _single_event_payload()
    payload["webLink"] = url

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"


@pytest.mark.parametrize(
    ("start_value", "end_value", "timezone_name", "expected_start"),
    (
        (
            "2030-11-03T01:30:00.123456789-08:00",
            "2030-11-03T02:30:00.987654321-08:00",
            "Pacific Standard Time",
            datetime(2030, 11, 3, 9, 30, 0, 123456, tzinfo=UTC),
        ),
        (
            "2030-01-02T09:00:00.123456789Z",
            "2030-01-02T10:00:00.987654321Z",
            "UTC",
            datetime(2030, 1, 2, 9, 0, 0, 123456, tzinfo=UTC),
        ),
    ),
)
def test_long_fractional_seconds_preserve_z_or_numeric_offset(
    start_value: str,
    end_value: str,
    timezone_name: str,
    expected_start: datetime,
) -> None:
    """截断七位以上小数时必须保留 Z/offset，不能改变事件 instant。"""
    payload = _single_event_payload()
    payload["start"] = {"dateTime": start_value, "timeZone": timezone_name}
    payload["end"] = {"dateTime": end_value, "timeZone": timezone_name}

    event = _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert event.starts_at == expected_start


@pytest.mark.parametrize(
    ("date_time", "timezone_name"),
    (
        ("2030-01-02T09:00:00+00:00", "China Standard Time"),
        ("2030-07-02T09:00:00-08:00", "Pacific Standard Time"),
    ),
)
def test_explicit_offset_must_match_declared_zone(date_time: str, timezone_name: str) -> None:
    """显式 offset 与 Windows/IANA zone 在该本地时刻不一致时必须拒绝。"""
    payload = _single_event_payload()
    payload["start"] = {"dateTime": date_time, "timeZone": timezone_name}
    payload["end"] = {"dateTime": date_time, "timeZone": timezone_name}

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"


@pytest.mark.parametrize(
    ("start_value", "end_value"),
    (
        ("2030-01-02T10:00:00", "2030-01-02T10:00:00"),
        ("2030-01-02T11:00:00", "2030-01-02T10:00:00"),
    ),
)
def test_event_end_must_be_strictly_after_start(start_value: str, end_value: str) -> None:
    """相等或倒序区间不能作为可持久化事件输出。"""
    payload = _single_event_payload()
    payload["start"] = {"dateTime": start_value, "timeZone": "UTC"}
    payload["end"] = {"dateTime": end_value, "timeZone": "UTC"}

    with pytest.raises(PermanentProviderError):
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)


def test_all_day_event_requires_same_zone_local_midnight_boundaries() -> None:
    """全天事件必须在同一声明时区的本地午夜开始和结束。"""
    payload = _single_event_payload()
    payload["isAllDay"] = True
    payload["start"] = {
        "dateTime": "2030-01-02T00:30:00",
        "timeZone": "China Standard Time",
    }
    payload["end"] = {
        "dateTime": "2030-01-03T00:30:00",
        "timeZone": "China Standard Time",
    }

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"


def test_series_master_exposes_read_only_recurrence_metadata() -> None:
    """seriesMaster 保留只读 recurrence，但仍以自身 ID 标记为重复事件。"""
    payload = _single_event_payload()
    payload["type"] = "seriesMaster"
    payload["recurrence"] = {
        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday"]},
        "range": {"type": "endDate", "startDate": "2030-01-02", "endDate": "2030-02-01"},
    }

    event = _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert event.recurring_event_id == event.event_id
    assert event.recurrence_metadata is not None
    assert event.recurrence_metadata["pattern_type"] == "weekly"


@pytest.mark.parametrize(
    ("event_type", "series_master_id", "recurrence"),
    (
        ("singleInstance", "series-1", None),
        ("singleInstance", None, {"pattern": {}, "range": {}}),
        ("occurrence", None, None),
        ("occurrence", "series-1", {"pattern": {}, "range": {}}),
        ("exception", None, None),
        ("seriesMaster", None, None),
        ("seriesMaster", "series-1", {"pattern": {}, "range": {}}),
        ("unknownType", None, None),
        (None, None, None),
    ),
)
def test_graph_event_type_rejects_recurrence_contradictions(
    event_type: str | None,
    series_master_id: str | None,
    recurrence: dict[str, object] | None,
) -> None:
    """Graph type、seriesMasterId 与 recurrence 的矛盾组合不能降级为普通事件。"""
    payload = _single_event_payload()
    if event_type is None:
        payload.pop("type", None)
    else:
        payload["type"] = event_type
    if series_master_id is not None:
        payload["seriesMasterId"] = series_master_id
    if recurrence is not None:
        payload["recurrence"] = recurrence

    with pytest.raises(PermanentProviderError) as raised:
        _adapter()._normalize_event(payload, calendar_id=CALENDAR_ID)

    assert raised.value.error_code == "microsoft_calendar_invalid_response"


@pytest.mark.asyncio
@respx.mock
async def test_current_event_get_404_returns_none_and_preserves_exact_path() -> None:
    """精确 GET 使用同一日历 scope，404 安全映射为 None。"""
    route = respx.get(f"{CALENDARS_URL}/{CALENDAR_ID}/events/missing-event").respond(404)
    assert await _adapter().get_current_event(CALENDAR_ID, "missing-event") is None
    assert route.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "identifier"),
    (
        ("initial", "."),
        ("initial", ".."),
        ("sync", "."),
        ("sync", ".."),
        ("current_calendar", "."),
        ("current_calendar", ".."),
        ("current_event", "."),
        ("current_event", ".."),
    ),
)
@respx.mock
async def test_caller_dot_segment_ids_fail_before_http(
    operation: str,
    identifier: str,
) -> None:
    """调用方 opaque ID 的精确 dot-segment 必须在 httpx 折叠 path 前固定拒绝。"""
    params: dict[str, str] | None = None
    if operation == "initial":
        raw_url = f"{CALENDARS_URL}/{identifier}/calendarView/delta"
        params = {
            "startDateTime": "2030-01-08T00:00:00+08:00",
            "endDateTime": "2030-02-08T00:00:00+08:00",
        }
        cursor = f"{raw_url}?$deltatoken=synthetic-dot-segment"
        response_payload = {"value": [], "@odata.deltaLink": cursor}
    elif operation == "sync":
        raw_url = f"{CALENDARS_URL}/{identifier}/calendarView/delta"
        cursor = f"{raw_url}?$deltatoken=synthetic-dot-segment"
        raw_url = cursor
        response_payload = {"value": [], "@odata.deltaLink": cursor}
    elif operation == "current_calendar":
        raw_url = f"{CALENDARS_URL}/{identifier}/events/synthetic-event"
        cursor = None
        response_payload = _single_event_payload()
    else:
        raw_url = f"{CALENDARS_URL}/{CALENDAR_ID}/events/{identifier}"
        cursor = None
        response_payload = _single_event_payload()
        response_payload["id"] = identifier

    # 用 httpx 自己解析测试路由，RED 会直接展示客户端真实折叠后的 path，而不是理论字符串。
    request_url = httpx.Request("GET", raw_url, params=params).url
    collapsed_route = respx.get(request_url).respond(200, json=response_payload)
    error_code: str | None = None
    try:
        adapter = _adapter()
        if operation == "initial":
            await _collect(adapter.initial_pages(identifier))
        elif operation == "sync":
            assert cursor is not None
            await _collect(adapter.sync_pages(identifier, cursor))
        elif operation == "current_calendar":
            await adapter.get_current_event(identifier, "synthetic-event")
        else:
            await adapter.get_current_event(CALENDAR_ID, identifier)
    except PermanentProviderError as error:
        error_code = error.error_code

    requested_paths = tuple(call.request.url.path for call in collapsed_route.calls)
    assert (
        error_code,
        collapsed_route.call_count,
        requested_paths,
    ) == (
        "microsoft_calendar_invalid_response",
        0,
        (),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", (".", ".."))
@respx.mock
async def test_provider_calendar_dot_segment_ids_fail_at_response_boundary(
    identifier: str,
) -> None:
    """Graph 目录返回 dot-segment ID 时必须在投影或后续日历请求前失败。"""
    payload = _fixture("calendars.json")
    values = payload["value"]
    assert isinstance(values, list) and isinstance(values[0], dict)
    values[0]["id"] = identifier
    source_route = respx.get(CALENDARS_URL).respond(200, json=payload)

    error_code: str | None = None
    try:
        await _collect(_adapter().directory_pages())
    except PermanentProviderError as error:
        error_code = error.error_code

    assert (error_code, source_route.call_count) == (
        "microsoft_calendar_invalid_response",
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "identifier"),
    (
        ("id", "."),
        ("id", ".."),
        ("seriesMasterId", "."),
        ("seriesMasterId", ".."),
    ),
)
@respx.mock
async def test_provider_event_dot_segment_ids_fail_at_response_boundary(
    field_name: str,
    identifier: str,
) -> None:
    """Graph event 与 series master 的 dot-segment ID 不能成为本地事件事实。"""
    event = _event_payload()
    event[field_name] = identifier
    payload = {"value": [event], "@odata.deltaLink": FINAL_DELTA_URL}
    source_route = respx.get(
        DELTA_URL,
        params={
            "startDateTime": "2030-01-08T00:00:00+08:00",
            "endDateTime": "2030-02-08T00:00:00+08:00",
        },
    ).respond(200, json=payload)

    error_code: str | None = None
    try:
        await _collect(_adapter().initial_pages(CALENDAR_ID))
    except PermanentProviderError as error:
        error_code = error.error_code

    assert (error_code, source_route.call_count) == (
        "microsoft_calendar_invalid_response",
        1,
    )


@pytest.mark.parametrize("identifier", (".abc", "...", "a..b"))
def test_non_segment_dots_remain_valid_in_opaque_ids(identifier: str) -> None:
    """只拒绝精确 dot-segment，普通含点 opaque calendar/event/series ID 仍合法。"""
    calendar_payload = _fixture("calendars.json")
    values = calendar_payload["value"]
    assert isinstance(values, list) and isinstance(values[0], dict)
    values[0]["id"] = identifier
    calendar = _adapter()._normalize_calendar(values[0])

    event_payload = _event_payload()
    event_payload["id"] = identifier
    event_payload["seriesMasterId"] = identifier
    event = _adapter()._normalize_event(event_payload, calendar_id=identifier)

    assert calendar.calendar_id == identifier
    assert event.event_id == identifier
    assert event.calendar_id == identifier
    assert event.recurring_event_id == identifier


@pytest.mark.asyncio
@respx.mock
async def test_delta_rejects_wrong_host_or_calendar_without_requesting_target() -> None:
    """next/delta URL 必须绑定 Graph 主机和精确 calendar path。"""
    payload = _fixture("calendar_view_delta_initial.json")
    payload["@odata.nextLink"] = "https://attacker.example.test/collect?token=secret"
    respx.get(DELTA_URL).respond(200, json=payload)
    attacker = respx.get("https://attacker.example.test/collect").respond(200, json={})
    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().initial_pages(CALENDAR_ID))
    assert raised.value.error_code == "microsoft_calendar_invalid_delta_url"
    assert not attacker.called and "secret" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_delta_cycle_or_missing_cursor_is_rejected() -> None:
    """循环分页、同时 next/delta 或最终缺 cursor 都不能推进同步。"""
    payload = _fixture("calendar_view_delta_initial.json")
    payload["@odata.nextLink"] = NEXT_URL
    respx.get(DELTA_URL).respond(200, json=payload)
    cycle = _fixture("calendar_view_delta_initial.json")
    cycle.pop("@odata.nextLink", None)
    cycle.pop("@odata.deltaLink", None)
    respx.get(NEXT_URL).respond(200, json=cycle)
    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().initial_pages(CALENDAR_ID))
    assert raised.value.error_code in {
        "microsoft_calendar_delta_missing_cursor",
        "microsoft_calendar_pagination_invalid",
    }


@pytest.mark.asyncio
@respx.mock
async def test_directory_follows_next_link_and_finishes_without_provider_cursor() -> None:
    """Graph 目录只跟随受限 nextLink，最终页不要求伪造 deltaLink。"""
    payload = _fixture("calendars.json")
    payload["@odata.nextLink"] = (
        "https://graph.microsoft.com/v1.0/me/calendars?$skiptoken=synthetic-next"
    )
    first = respx.get(
        CALENDARS_URL,
        params={"$select": "id,name,isDefaultCalendar,canEdit,canShare,owner,hexColor"},
    ).respond(200, json=payload)
    second = respx.get(
        "https://graph.microsoft.com/v1.0/me/calendars?$skiptoken=synthetic-next"
    ).respond(200, json={"value": []})

    pages = await _collect(_adapter().directory_pages())

    assert len(pages) == 2
    assert pages[0].next_page_token == (
        "https://graph.microsoft.com/v1.0/me/calendars?$skiptoken=synthetic-next"
    )
    assert pages[0].next_cursor is None
    assert pages[1].next_page_token is None and pages[1].next_cursor is None
    assert all(page.full_snapshot is True for page in pages)
    assert first.call_count == 1 and second.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden_field", ("@odata.deltaLink", "@removed"))
@respx.mock
async def test_directory_rejects_delta_link_and_tombstone(forbidden_field: str) -> None:
    """目录不支持 Delta 或 tombstone，畸形事实必须在持久化前永久失败。"""
    payload = _fixture("calendars.json")
    if forbidden_field == "@odata.deltaLink":
        payload[forbidden_field] = (
            "https://graph.microsoft.com/v1.0/me/calendars?$deltatoken=unsupported"
        )
    else:
        values = payload["value"]
        assert isinstance(values, list) and isinstance(values[0], dict)
        values[0][forbidden_field] = {"reason": "deleted"}
    route = respx.get(CALENDARS_URL).respond(200, json=payload)

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().directory_pages())

    assert raised.value.error_code in {
        "microsoft_calendar_invalid_response",
        "microsoft_calendar_pagination_invalid",
    }
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor",
    (
        "https://graph.microsoft.com/v1.0/me/calendars?$skiptoken=synthetic-valid-shape",
        "http://graph.microsoft.com/v1.0/me/calendars?$deltatoken=synthetic",
        " https://graph.microsoft.com/v1.0/me/calendars?$deltatoken=synthetic",
        "https://attacker.example.test/v1.0/me/calendars?$deltatoken=synthetic",
        "https://graph.microsoft.com/v1.0/me/messages?$deltatoken=synthetic",
    ),
)
@respx.mock
async def test_directory_cursor_must_be_validated_before_any_request(cursor: str) -> None:
    """Microsoft 目录没有 provider cursor，任意非空持久值都必须在 HTTP 前拒绝。"""
    route = respx.get(CALENDARS_URL).respond(200, json=_fixture("calendars.json"))
    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().directory_pages(cursor))
    assert raised.value.error_code == "microsoft_calendar_directory_cursor_unsupported"
    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_adapter_exposes_no_arbitrary_url_request_entry_point() -> None:
    """调用方提供的攻击者 URL 不能产生请求，更不能收到 Graph Bearer token。"""
    attacker_url = "https://attacker.example.test/collect"
    route = respx.get(attacker_url).respond(200, json={"value": []})
    adapter = _adapter()
    request_entry = getattr(adapter, "execute_request", None)

    if request_entry is not None:
        await request_entry(url=attacker_url)

    assert route.call_count == 0
    assert [call.request.headers.get("Authorization") for call in route.calls] == []
    assert request_entry is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", (404, 410))
@respx.mock
async def test_directory_404_and_410_are_permanent_request_errors(status_code: int) -> None:
    """固定 collection 的 404/410 不是 cursor expiry，不能触发目录回退重放。"""
    respx.get(CALENDARS_URL).respond(
        status_code,
        json={"error": {"code": "syncStateNotFound"}},
    )

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().directory_pages())

    assert not isinstance(raised.value, CalendarCursorExpiredError)
    assert raised.value.error_code == "microsoft_calendar_request_rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", (404, 410))
@respx.mock
async def test_initial_calendar_view_404_and_410_are_permanent(status_code: int) -> None:
    """初始固定窗口没有持久 cursor，404/410 不能触发 cursor reset。"""
    respx.get(
        DELTA_URL,
        params={
            "startDateTime": "2030-01-08T00:00:00+08:00",
            "endDateTime": "2030-02-08T00:00:00+08:00",
        },
    ).respond(status_code, json={"error": {"code": "syncStateNotFound"}})

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().initial_pages(CALENDAR_ID))

    assert not isinstance(raised.value, CalendarCursorExpiredError)
    assert raised.value.error_code == "microsoft_calendar_request_rejected"


@pytest.mark.asyncio
@respx.mock
async def test_calendar_view_next_link_410_is_not_persisted_cursor_expiry() -> None:
    """分页 nextLink 尚未提交为恢复位置，失效时必须永久失败而非清除旧 cursor。"""
    respx.get(
        DELTA_URL,
        params={
            "startDateTime": "2030-01-08T00:00:00+08:00",
            "endDateTime": "2030-02-08T00:00:00+08:00",
        },
    ).respond(200, json=_fixture("calendar_view_delta_initial.json"))
    respx.get(NEXT_URL).respond(410, json={"error": {"code": "syncStateNotFound"}})

    with pytest.raises(PermanentProviderError) as raised:
        await _collect(_adapter().initial_pages(CALENDAR_ID))

    assert not isinstance(raised.value, CalendarCursorExpiredError)
    assert raised.value.error_code == "microsoft_calendar_request_rejected"


@pytest.mark.asyncio
@respx.mock
async def test_persisted_calendar_cursor_sync_state_not_found_expires_only_that_scope() -> None:
    """Graph 稳定错误码只在已持久 CalendarView deltaLink 首请求上映射 cursor expiry。"""
    respx.get(FINAL_DELTA_URL).respond(
        400,
        json={"error": {"code": "syncStateNotFound"}},
    )

    with pytest.raises(CalendarCursorExpiredError) as raised:
        await _collect(_adapter().sync_pages(CALENDAR_ID, FINAL_DELTA_URL))

    assert raised.value.provider == "microsoft"
    assert raised.value.scope_key == CALENDAR_ID


@pytest.mark.asyncio
@respx.mock
async def test_error_mapping_refresh_403_429_5xx_and_cursor_expiry() -> None:
    """Graph 常见错误必须落入稳定领域错误类别。"""
    refresh_calls = 0

    async def refresh() -> str:
        nonlocal refresh_calls
        refresh_calls += 1
        return "synthetic-refreshed-token"

    route = respx.get(CALENDARS_URL).mock(side_effect=[httpx.Response(401), httpx.Response(401)])
    with pytest.raises(UserActionRequiredError) as raised:
        await _collect(_adapter(refresh_access_token=refresh).directory_pages())
    assert raised.value.error_code == "microsoft_reauthorization_required"
    assert refresh_calls == 1 and route.call_count == 2

    respx.get(CALENDARS_URL).respond(403, json={"error": {"code": "accessDenied"}})
    with pytest.raises(UserActionRequiredError) as raised:
        await _collect(_adapter().directory_pages())
    assert raised.value.error_code == "microsoft_calendar_permission_required"

    respx.get(CALENDARS_URL).respond(429, headers={"Retry-After": "7"})
    with pytest.raises(TransientProviderError) as raised:
        await _collect(_adapter().directory_pages())
    assert raised.value.error_code == "microsoft_calendar_rate_limited"
    assert raised.value.retry_after == 7

    respx.get(CALENDARS_URL).respond(503, text="sensitive-body")
    with pytest.raises(TransientProviderError) as raised:
        await _collect(_adapter().directory_pages())
    assert raised.value.error_code == "microsoft_calendar_service_unavailable"
    assert "sensitive-body" not in str(raised.value)

    respx.get(FINAL_DELTA_URL).respond(410, json={"error": {"code": "syncStateNotFound"}})
    with pytest.raises(CalendarCursorExpiredError) as raised:
        await _collect(_adapter().sync_pages(CALENDAR_ID, FINAL_DELTA_URL))
    assert raised.value.provider == "microsoft" and raised.value.scope_key == CALENDAR_ID


@pytest.mark.asyncio
@respx.mock
async def test_timeout_traceback_does_not_echo_cursor() -> None:
    """网络异常转换为暂态错误且不回显 opaque cursor 或底层文本。"""
    respx.get(FINAL_DELTA_URL).mock(
        side_effect=httpx.ReadTimeout(
            "sensitive-network", request=httpx.Request("GET", FINAL_DELTA_URL)
        )
    )
    with pytest.raises(TransientProviderError) as raised:
        await _collect(_adapter().sync_pages(CALENDAR_ID, FINAL_DELTA_URL))
    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.error_code == "microsoft_calendar_timeout"
    assert FINAL_DELTA_URL not in rendered and "sensitive-network" not in rendered
