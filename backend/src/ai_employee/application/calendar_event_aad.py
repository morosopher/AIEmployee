"""构造 CalendarEvent 敏感字段使用的版本化记录绑定 AAD。"""

from typing import Literal, NoReturn
from uuid import UUID

_DOMAIN_V2 = b"AIEMPLOYEE/calendar-event-field-aad/v2\x00"
_FORMAT_ERROR = "invalid calendar event field AAD input"


class CalendarEventFieldAadFormatError(ValueError):
    """表示 CalendarEvent 字段 AAD 输入不满足冻结格式。

    该异常只提供稳定、无内容的错误消息，避免把用户、连接或供应商对象标识符
    泄露到日志、审计或上层错误响应中。
    """


def _fail() -> NoReturn:
    """以唯一稳定错误拒绝格式不合法的 AAD 输入。"""
    raise CalendarEventFieldAadFormatError(_FORMAT_ERROR)


def _uuid_bytes(value: str) -> bytes:
    """验证小写 canonical UUID 文本并返回其 ASCII 字节。

    Args:
        value: 待验证的用户或连接 UUID 文本。

    Returns:
        与 canonical UUID 文本完全一致的 ASCII 字节。

    Raises:
        CalendarEventFieldAadFormatError: 类型、UUID 形状或 ASCII 表示不符合冻结契约。
    """
    if type(value) is not str:
        _fail()

    parsed: UUID | None
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        parsed = None
    if parsed is None or str(parsed) != value:
        _fail()

    raw: bytes | None
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError:
        raw = None
    if raw is None:
        _fail()
    return raw


def _opaque_bytes(value: str, *, maximum_scalars: int) -> bytes:
    """验证 opaque ID 的标量边界并保留其严格 UTF-8 字节。

    Args:
        value: 供应商日历或事件的原始字符串标识符。
        maximum_scalars: 该类标识符允许的最大 Unicode 标量数量。

    Returns:
        未做正规化、大小写转换或分隔符处理的严格 UTF-8 字节。

    Raises:
        CalendarEventFieldAadFormatError: 类型、长度或 UTF-8 编码不合法。
    """
    if type(value) is not str or not 1 <= len(value) <= maximum_scalars:
        _fail()

    raw: bytes | None
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raw = None
    if raw is None:
        _fail()
    return raw


def _frame(raw: bytes) -> bytes:
    """以四字节大端无符号长度前缀封装一个原始字段。"""
    prefix: bytes | None
    try:
        prefix = len(raw).to_bytes(4, "big")
    except OverflowError:
        prefix = None
    if prefix is None:
        _fail()
    return prefix + raw


def calendar_event_field_aad_v2(
    *,
    user_id: str,
    connection_id: str,
    calendar_id: str,
    provider_event_id: str,
    field: Literal["description", "location"],
) -> bytes:
    """返回严格验证、版本化且无歧义的 CalendarEvent 字段 AAD。

    五个身份字段按固定顺序分别加上 ``uint32_be`` 长度前缀。Opaque ID
    保留原始 Unicode 标量与 UTF-8 表示，不进行 normalization；因此视觉相同但
    字节不同的供应商标识符仍绑定到不同密文。

    Args:
        user_id: 小写 canonical 用户 UUID。
        connection_id: 小写 canonical OAuth 连接 UUID。
        calendar_id: 长度为 1..512 个标量的供应商日历 ID。
        provider_event_id: 长度为 1..255 个标量的供应商事件 ID。
        field: 仅允许 ``description`` 或 ``location``。

    Returns:
        以固定 v2 domain 开头、包含五个独立 frame 的 canonical AAD 字节。

    Raises:
        CalendarEventFieldAadFormatError: 任一输入不符合冻结格式或 frame 长度溢出。
    """
    if type(field) is not str or field not in ("description", "location"):
        _fail()

    raw_values = (
        _uuid_bytes(user_id),
        _uuid_bytes(connection_id),
        _opaque_bytes(calendar_id, maximum_scalars=512),
        _opaque_bytes(provider_event_id, maximum_scalars=255),
        field.encode("ascii"),
    )
    return _DOMAIN_V2 + b"".join(_frame(raw) for raw in raw_values)
