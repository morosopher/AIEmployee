"""提供 Microsoft Graph Windows/IANA 时区的供应商边界。

Graph 的 dateTimeTimeZone.timeZone 使用 Windows 名称，而应用内部统一保存 IANA 名称。
本模块只读取 Babel 提供的 CLDR 映射并构造进程级不可变表；不读取宿主机时区、不尝试按
偏移猜测区域，也不把第三方异常文本带出 integrations 边界。
"""

from __future__ import annotations

import importlib.resources
from importlib.resources.abc import Traversable
from types import MappingProxyType
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from babel.core import get_global

from ai_employee.domain.errors import PermanentProviderError

_UNSUPPORTED_CODE: Final[str] = "calendar_timezone_mapping_unsupported"
type _CldrMappingName = Literal["windows_zone_mapping", "zone_aliases"]


def _unsupported() -> PermanentProviderError:
    """创建不回显输入值的稳定时区映射错误。"""
    return PermanentProviderError(
        error_code=_UNSUPPORTED_CODE,
        message="Calendar timezone mapping is unsupported",
    )


def _validated_name(value: object) -> str:
    """验证 CLDR 或调用方时区名称，不在错误中回显原始内容。"""
    if not isinstance(value, str) or value == "" or value.strip() != value:
        raise _unsupported()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _unsupported()
    return value


def _cldr_mapping(name: _CldrMappingName) -> dict[str, str]:
    """读取并完整验证一个 Babel CLDR 字符串映射。

    CLDR 是供应商边界的静态事实。根对象缺失、任一键值类型错误、空白或控制字符都说明
    当前运行镜像中的映射不可证明完整；此时必须整体失败，不能跳过坏项后生成部分表。
    """
    try:
        raw_mapping = get_global(name)
    except (LookupError, OSError, TypeError, ValueError):
        raise _unsupported() from None
    if not isinstance(raw_mapping, dict) or not raw_mapping:
        raise _unsupported()
    validated: dict[str, str] = {}
    for raw_key, raw_value in raw_mapping.items():
        key = _validated_name(raw_key)
        value = _validated_name(raw_value)
        validated[key] = value
    return validated


def _tzdata_root() -> Traversable:
    """返回锁定的 Python tzdata 资源根，拒绝宿主 TZPATH 隐式兜底。"""
    try:
        root = importlib.resources.files("tzdata.zoneinfo")
        # UTC 会在 canonical 分支提前返回，因此必须在根校验时主动解析其 TZif 内容；只做
        # 文件存在检查会让损坏包借宿主 TZPATH 掩盖错误。
        _validate_packaged_zone(root, "UTC")
    except PermanentProviderError:
        raise
    except (ModuleNotFoundError, OSError, TypeError, ValueError):
        raise _unsupported() from None
    return root


def _validate_packaged_zone(root: Traversable, zone_name: str) -> None:
    """从 Python tzdata 文件本身验证 IANA zone，不读取或修改进程 TZPATH。"""
    try:
        resource = root.joinpath(*zone_name.split("/"))
        if not resource.is_file():
            raise _unsupported()
        with resource.open("rb") as zone_file:
            ZoneInfo.from_file(zone_file, key=zone_name)
    except PermanentProviderError:
        raise
    except (EOFError, OSError, TypeError, ValueError, ZoneInfoNotFoundError):
        raise _unsupported() from None


def _canonical_iana(
    value: object,
    *,
    aliases: dict[str, str] | None = None,
    tzdata_root: Traversable | None = None,
) -> str:
    """验证并收敛 IANA alias，显式把所有 UTC 变体归一为 'UTC'。"""
    current = _validated_name(value)
    alias_mapping = aliases if aliases is not None else _cldr_mapping("zone_aliases")
    seen: set[str] = set()
    while current in alias_mapping:
        if current in seen:
            raise _unsupported()
        seen.add(current)
        current = alias_mapping[current]
    if current in {"UTC", "Etc/UTC", "Etc/GMT", "GMT"}:
        return "UTC"
    _validate_packaged_zone(tzdata_root or _tzdata_root(), current)
    return current


def _build_mappings() -> tuple[dict[str, str], dict[str, str]]:
    """从完整 CLDR 与锁定 tzdata 构造固定双向映射。"""
    raw_mapping = _cldr_mapping("windows_zone_mapping")
    aliases = _cldr_mapping("zone_aliases")
    tzdata_root = _tzdata_root()
    windows_to_iana: dict[str, str] = {"UTC": "UTC"}
    iana_to_windows: dict[str, str] = {"UTC": "UTC"}
    for windows_name, raw_iana in raw_mapping.items():
        iana_name = _canonical_iana(
            raw_iana,
            aliases=aliases,
            tzdata_root=tzdata_root,
        )
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
