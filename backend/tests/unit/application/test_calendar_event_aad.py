"""验证 CalendarEvent 字段 AAD v2 的冻结 framing 与失败关闭边界。"""

import base64
import hashlib
from collections.abc import Callable
from typing import Literal, cast

import pytest

from ai_employee.application import calendar_event_aad
from ai_employee.application.calendar_event_aad import (
    CalendarEventFieldAadFormatError,
    calendar_event_field_aad_v2,
)

USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONNECTION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DOMAIN = b"AIEMPLOYEE/calendar-event-field-aad/v2\x00"

VECTOR_A_BASE64 = (
    "QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEt"
    "NGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJi"
    "YmJiYmIAAAADYTpiAAAAAWMAAAALZGVzY3JpcHRpb24="
)
VECTOR_A_SHA256 = "67ba9e40f2157a49de2d987e1f4d8e61c2a78a7d71d4da7ce13421fde58a8e70"
VECTOR_A_BYTES = (
    DOMAIN
    + b"\x00\x00\x00$"
    + USER_ID.encode("ascii")
    + b"\x00\x00\x00$"
    + CONNECTION_ID.encode("ascii")
    + b"\x00\x00\x00\x03a:b"
    + b"\x00\x00\x00\x01c"
    + b"\x00\x00\x00\x0bdescription"
)

VECTOR_B_BASE64 = (
    "QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEt"
    "NGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJi"
    "YmJiYmIAAAABYQAAAANiOmMAAAALZGVzY3JpcHRpb24="
)
VECTOR_B_SHA256 = "52e707318449a55779a023931f5914909d662944f5364545e1795edbcf555dad"
VECTOR_B_BYTES = (
    DOMAIN
    + b"\x00\x00\x00$"
    + USER_ID.encode("ascii")
    + b"\x00\x00\x00$"
    + CONNECTION_ID.encode("ascii")
    + b"\x00\x00\x00\x01a"
    + b"\x00\x00\x00\x03b:c"
    + b"\x00\x00\x00\x0bdescription"
)

UNICODE_BASE64 = (
    "QUlFTVBMT1lFRS9jYWxlbmRhci1ldmVudC1maWVsZC1hYWQvdjIAAAAAJGFhYWFhYWFhLWFhYWEt"
    "NGFhYS04YWFhLWFhYWFhYWFhYWFhYQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJi"
    "YmJiYmIAAAAJ5pel5Y6GL86xAAAACeS6i+S7tjrDqQAAAAhsb2NhdGlvbg=="
)
UNICODE_SHA256 = "0bdf656c1b037423e71c950df9f6bb5c56a737bcef8fe7d6e58e26d7dee04370"
UNICODE_BYTES = (
    DOMAIN
    + b"\x00\x00\x00$"
    + USER_ID.encode("ascii")
    + b"\x00\x00\x00$"
    + CONNECTION_ID.encode("ascii")
    + b"\x00\x00\x00\x09\xe6\x97\xa5\xe5\x8e\x86/\xce\xb1"
    + b"\x00\x00\x00\x09\xe4\xba\x8b\xe4\xbb\xb6:\xc3\xa9"
    + b"\x00\x00\x00\x08location"
)

_FORMAT_ERROR = "invalid calendar event field AAD input"


def _assert_exact_frames(canonical: bytes, raw_values: tuple[bytes, ...]) -> None:
    """逐字段验证四字节大端长度前缀和原始字节未被改写。"""
    offset = len(DOMAIN)
    for raw in raw_values:
        assert canonical[offset : offset + 4] == len(raw).to_bytes(4, "big")
        offset += 4
        assert canonical[offset : offset + len(raw)] == raw
        offset += len(raw)
    assert offset == len(canonical)


def _assert_format_error(call: Callable[[], bytes]) -> None:
    """验证所有格式失败只暴露同一个无输入内容的稳定异常。"""
    with pytest.raises(CalendarEventFieldAadFormatError) as raised:
        call()

    assert str(raised.value) == _FORMAT_ERROR
    assert raised.value.args == (_FORMAT_ERROR,)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def _encode_candidate(
    *,
    user_id: object = USER_ID,
    connection_id: object = CONNECTION_ID,
    calendar_id: object = "calendar-a",
    provider_event_id: object = "event-a",
    field: object = "description",
) -> bytes:
    """绕过静态类型收窄，把恶意边界值送入公共运行时校验。"""
    return calendar_event_field_aad_v2(
        user_id=cast(str, user_id),
        connection_id=cast(str, connection_id),
        calendar_id=cast(str, calendar_id),
        provider_event_id=cast(str, provider_event_id),
        field=cast(Literal["description", "location"], field),
    )


def test_vector_a_uses_exact_big_endian_frames_and_frozen_digest() -> None:
    """向量 A 必须匹配独立冻结的 bytes、Base64、SHA 与每个 frame 前缀。"""
    raw_values = (
        USER_ID.encode("ascii"),
        CONNECTION_ID.encode("ascii"),
        b"a:b",
        b"c",
        b"description",
    )

    canonical = calendar_event_field_aad_v2(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id="a:b",
        provider_event_id="c",
        field="description",
    )

    assert len(DOMAIN) == 39
    assert tuple(len(raw) for raw in raw_values) == (36, 36, 3, 1, 11)
    assert len(canonical) == 146
    assert canonical == VECTOR_A_BYTES
    assert base64.b64encode(canonical).decode("ascii") == VECTOR_A_BASE64
    assert hashlib.sha256(canonical).hexdigest() == VECTOR_A_SHA256
    _assert_exact_frames(canonical, raw_values)


def test_vector_b_separates_delimiter_collision_and_matches_frozen_digest() -> None:
    """向量 B 即使冒号拼接可与 A 混淆，也必须生成不同的 canonical bytes。"""
    raw_values = (
        USER_ID.encode("ascii"),
        CONNECTION_ID.encode("ascii"),
        b"a",
        b"b:c",
        b"description",
    )

    canonical = calendar_event_field_aad_v2(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id="a",
        provider_event_id="b:c",
        field="description",
    )

    assert tuple(len(raw) for raw in raw_values) == (36, 36, 1, 3, 11)
    assert len(canonical) == 146
    assert canonical == VECTOR_B_BYTES
    assert base64.b64encode(canonical).decode("ascii") == VECTOR_B_BASE64
    assert hashlib.sha256(canonical).hexdigest() == VECTOR_B_SHA256
    assert b":".join((b"a:b", b"c")) == b":".join((b"a", b"b:c"))
    assert canonical != VECTOR_A_BYTES
    _assert_exact_frames(canonical, raw_values)


def test_unicode_vector_preserves_strict_utf8_opaque_identifiers() -> None:
    """非 ASCII opaque ID 必须保留原始 UTF-8 标量字节并匹配冻结向量。"""
    calendar_raw = "日历/α".encode()
    event_raw = "事件:é".encode()
    raw_values = (
        USER_ID.encode("ascii"),
        CONNECTION_ID.encode("ascii"),
        calendar_raw,
        event_raw,
        b"location",
    )

    canonical = calendar_event_field_aad_v2(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id="日历/α",
        provider_event_id="事件:é",
        field="location",
    )

    assert calendar_raw.hex() == "e697a5e58e862fceb1"
    assert event_raw.hex() == "e4ba8be4bbb63ac3a9"
    assert tuple(len(raw) for raw in raw_values) == (36, 36, 9, 9, 8)
    assert len(canonical) == 157
    assert canonical == UNICODE_BYTES
    assert base64.b64encode(canonical).decode("ascii") == UNICODE_BASE64
    assert hashlib.sha256(canonical).hexdigest() == UNICODE_SHA256
    _assert_exact_frames(canonical, raw_values)


def test_composed_and_decomposed_unicode_are_not_normalized() -> None:
    """视觉相同的 composed/decomposed ID 必须继续表示不同供应商身份。"""
    composed = calendar_event_field_aad_v2(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id="calendar-a",
        provider_event_id="event-é",
        field="description",
    )
    decomposed = calendar_event_field_aad_v2(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id="calendar-a",
        provider_event_id="event-e\u0301",
        field="description",
    )

    assert "é".encode() != "e\u0301".encode()
    assert composed != decomposed


def test_opaque_id_scalar_limits_accept_the_exact_inclusive_upper_bounds() -> None:
    """日历 512、事件 255 个标量的精确上界必须成功且按原始 UTF-8 framing。"""
    calendar_id = "日" * 512
    provider_event_id = "é" * 255
    raw_values = (
        USER_ID.encode("ascii"),
        CONNECTION_ID.encode("ascii"),
        calendar_id.encode(),
        provider_event_id.encode(),
        b"location",
    )

    canonical = calendar_event_field_aad_v2(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id=calendar_id,
        provider_event_id=provider_event_id,
        field="location",
    )

    assert len(calendar_id) == 512
    assert len(provider_event_id) == 255
    _assert_exact_frames(canonical, raw_values)


@pytest.mark.parametrize(
    ("overrides"),
    (
        pytest.param({"user_id": None}, id="missing-user-id"),
        pytest.param({"user_id": ""}, id="empty-user-id"),
        pytest.param({"connection_id": None}, id="missing-connection-id"),
        pytest.param({"connection_id": ""}, id="empty-connection-id"),
        pytest.param({"calendar_id": None}, id="missing-calendar-id"),
        pytest.param({"calendar_id": ""}, id="empty-calendar-id"),
        pytest.param({"provider_event_id": None}, id="missing-event-id"),
        pytest.param({"provider_event_id": ""}, id="empty-event-id"),
        pytest.param({"calendar_id": b"calendar-a"}, id="non-string-calendar-id"),
        pytest.param({"provider_event_id": 7}, id="non-string-event-id"),
        pytest.param({"user_id": USER_ID.upper()}, id="uppercase-user-uuid"),
        pytest.param({"connection_id": CONNECTION_ID.upper()}, id="uppercase-connection-uuid"),
        pytest.param({"user_id": "{" + USER_ID + "}"}, id="braced-user-uuid"),
        pytest.param(
            {"connection_id": "{" + CONNECTION_ID + "}"},
            id="braced-connection-uuid",
        ),
        pytest.param({"user_id": USER_ID.replace("-", "")}, id="compact-user-uuid"),
        pytest.param(
            {"connection_id": CONNECTION_ID.replace("-", "")},
            id="compact-connection-uuid",
        ),
        pytest.param({"calendar_id": "calendar-\ud800"}, id="calendar-surrogate"),
        pytest.param({"provider_event_id": "event-\udfff"}, id="event-surrogate"),
        pytest.param({"calendar_id": "c" * 513}, id="calendar-over-512-scalars"),
        pytest.param({"provider_event_id": "e" * 256}, id="event-over-255-scalars"),
        pytest.param({"field": "subject"}, id="unknown-field"),
        pytest.param({"field": "Description"}, id="case-changed-field"),
        pytest.param({"field": None}, id="non-string-field"),
    ),
)
def test_invalid_inputs_fail_closed_with_one_content_free_error(
    overrides: dict[str, object],
) -> None:
    """所有输入边界失败必须在任何加密调用前折叠为同一稳定错误。"""
    values: dict[str, object] = {
        "user_id": USER_ID,
        "connection_id": CONNECTION_ID,
        "calendar_id": "calendar-a",
        "provider_event_id": "event-a",
        "field": "description",
    }
    values.update(overrides)

    _assert_format_error(
        lambda: _encode_candidate(
            user_id=values["user_id"],
            connection_id=values["connection_id"],
            calendar_id=values["calendar_id"],
            provider_event_id=values["provider_event_id"],
            field=values["field"],
        )
    )


class _OversizedRaw(bytes):
    """模拟无法放入 uint32 frame 的 raw bytes，而不分配超大内存。"""

    def __len__(self) -> int:
        """返回首个不可由四字节无符号整数表达的长度。"""
        return 1 << 32


def test_raw_byte_length_over_uint32_fails_with_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """raw byte 长度溢出必须转换为统一格式错误，不能泄漏 OverflowError。"""

    def oversized_opaque_bytes(value: str, *, maximum_scalars: int) -> bytes:
        """保留 helper 调用形状并返回零内存成本的溢出长度测试替身。"""
        del value, maximum_scalars
        return cast(bytes, _OversizedRaw(b"x"))

    monkeypatch.setattr(calendar_event_aad, "_opaque_bytes", oversized_opaque_bytes)

    _assert_format_error(lambda: _encode_candidate())
