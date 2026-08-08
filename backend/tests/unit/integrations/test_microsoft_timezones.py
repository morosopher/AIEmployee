"""验证 Microsoft Graph Windows/IANA 时区边界的确定性与安全错误。"""

import importlib

import pytest

from ai_employee.domain.errors import PermanentProviderError


def _module():
    """延迟导入待实现模块，让 RED 阶段表现为明确的缺失功能失败。"""
    return importlib.import_module("ai_employee.integrations.microsoft.timezones")


@pytest.mark.parametrize(
    ("iana", "windows"),
    (
        ("Asia/Shanghai", "China Standard Time"),
        ("America/Los_Angeles", "Pacific Standard Time"),
        ("UTC", "UTC"),
    ),
)
def test_bidirectional_mapping_is_deterministic(iana: str, windows: str) -> None:
    """常用 IANA/Windows 名称必须稳定双向转换。"""
    module = _module()
    assert module.to_windows_timezone(iana) == windows
    assert module.to_iana_timezone(windows) == iana


def test_canonical_iana_alias_is_normalized() -> None:
    """CLDR 返回的 canonical alias 应收敛到稳定 IANA 名称。"""
    module = _module()
    assert module.to_windows_timezone("US/Pacific") == "Pacific Standard Time"
    assert module.to_iana_timezone("Pacific Standard Time") == "America/Los_Angeles"


@pytest.mark.parametrize("value", ("", "Unknown/Zone", None, 123, "China Standard Time\nsecret"))
def test_unknown_or_malformed_zone_has_stable_non_echoing_error(value: object) -> None:
    """未知/畸形值不得回退宿主时区，也不得进入异常文本。"""
    module = _module()
    with pytest.raises(PermanentProviderError) as raised:
        module.to_iana_timezone(value)
    assert raised.value.error_code == "calendar_timezone_mapping_unsupported"
    if value:
        assert str(value) not in str(raised.value)
