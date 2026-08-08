"""提供 Microsoft Graph Windows/IANA 时区的供应商边界。

Graph 的 dateTimeTimeZone.timeZone 使用 Windows 名称，而应用内部统一保存 IANA 名称。
本模块只读取 Babel 提供的 CLDR 映射并构造进程级不可变表；不读取宿主机时区、不尝试按
偏移猜测区域，也不把第三方异常文本带出 integrations 边界。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo

from babel.core import get_global

from ai_employee.domain.errors import PermanentProviderError

_UNSUPPORTED_CODE: Final[str] = "calendar_timezone_mapping_unsupported"


def _unsupported() -> PermanentProviderError:
    """创建不回显输入值的稳定时区映射错误。"""
    return PermanentProviderError(
        error_code=_UNSUPPORTED_CODE,
        message="Calendar timezone mapping is unsupported",
    )


def _canonical_iana(value: object) -> str:
    """验证并收敛 IANA alias，显式把所有 UTC 变体归一为 'UTC'。"""
    if not isinstance(value, str) or value == "" or value.strip() != value:
        raise _unsupported()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _unsupported()
    aliases = get_global("zone_aliases")
    current = value
    seen: set[str] = set()
    while isinstance(aliases, dict) and current in aliases:
        if current in seen:
            raise _unsupported()
        seen.add(current)
        target = aliases[current]
        if not isinstance(target, str) or target == "":
            raise _unsupported()
        current = target
    if current in {"UTC", "Etc/UTC", "Etc/GMT", "GMT"}:
        return "UTC"
    try:
        ZoneInfo(current)
    except (KeyError, ValueError):
        raise _unsupported() from None
    return current


def _build_mappings() -> tuple[dict[str, str], dict[str, str]]:
    """从 Babel CLDR 数据构造固定双向映射并选择稳定 canonical 值。"""
    raw_mapping = get_global("windows_zone_mapping")
    if not isinstance(raw_mapping, dict):
        raise TypeError("Babel windows timezone mapping is unavailable")
    windows_to_iana: dict[str, str] = {"UTC": "UTC"}
    iana_to_windows: dict[str, str] = {"UTC": "UTC"}
    for windows_name, raw_iana in raw_mapping.items():
        if not isinstance(windows_name, str) or not isinstance(raw_iana, str):
            continue
        try:
            iana_name = _canonical_iana(raw_iana)
        except PermanentProviderError:
            continue
        windows_to_iana.setdefault(windows_name, iana_name)
        # 同一 IANA 可能对应多个 Windows 区域，按字典序保留确定结果。
        previous = iana_to_windows.get(iana_name)
        if previous is None or windows_name < previous:
            iana_to_windows[iana_name] = windows_name

    # CLDR 的 UTC 映射通常是 Etc/UTC；显式覆盖供应商常用别名，避免输出 Etc/UTC。
    windows_to_iana["UTC"] = "UTC"
    iana_to_windows["UTC"] = "UTC"
    return windows_to_iana, iana_to_windows


_WINDOWS_TO_IANA, _IANA_TO_WINDOWS = _build_mappings()
WINDOWS_TO_IANA: Final = MappingProxyType(_WINDOWS_TO_IANA)
IANA_TO_WINDOWS: Final = MappingProxyType(_IANA_TO_WINDOWS)


def to_iana_timezone(value: object) -> str:
    """把 Graph Windows 时区转换为 canonical IANA 名称。

    Args:
        value: Graph dateTimeTimeZone.timeZone 字符串。

    Returns:
        稳定的 IANA 名称；UTC 的所有受支持变体返回 'UTC'。

    Raises:
        PermanentProviderError: 名称未知、畸形或 CLDR 映射不可用。
    """
    if not isinstance(value, str) or value == "" or value.strip() != value:
        raise _unsupported()
    if value in {"UTC", "Etc/UTC", "Etc/GMT", "GMT", "Coordinated Universal Time"}:
        return "UTC"
    result = WINDOWS_TO_IANA.get(value)
    if result is None:
        raise _unsupported()
    return result


def to_windows_timezone(value: object) -> str:
    """把 IANA 名称（含 CLDR canonical alias）转换为 Graph Windows 名称。

    Args:
        value: 应用内部 IANA 时区名称或受支持 alias。

    Returns:
        稳定的 Graph Windows 时区名称。

    Raises:
        PermanentProviderError: 名称未知、畸形或无法由 CLDR 表达。
    """
    canonical = _canonical_iana(value)
    result = IANA_TO_WINDOWS.get(canonical)
    if result is None:
        raise _unsupported()
    return result


__all__ = [
    "IANA_TO_WINDOWS",
    "WINDOWS_TO_IANA",
    "to_iana_timezone",
    "to_windows_timezone",
]
