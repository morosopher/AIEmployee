"""验证 Microsoft Graph Windows/IANA 时区边界的确定性与安全错误。"""

import importlib
import importlib.resources
import importlib.util
import io
import zoneinfo

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


def test_packaged_tzdata_dependency_is_available() -> None:
    """时区验证必须锁定 Python tzdata，不能只依赖宿主镜像可选数据库。"""
    assert importlib.util.find_spec("tzdata") is not None


@pytest.mark.parametrize(
    "windows_mapping",
    (
        None,
        {},
        {123: "UTC"},
        {"Pacific Standard Time": 123},
        {"": "America/Los_Angeles"},
    ),
)
def test_missing_or_malformed_windows_cldr_items_fail_stably(
    monkeypatch: pytest.MonkeyPatch,
    windows_mapping: object,
) -> None:
    """Windows 映射的根对象或任一条目畸形时不得静默跳过。"""
    module = _module()
    original_tzpath = tuple(zoneinfo.TZPATH)

    def fake_get_global(name: str) -> object:
        """只替换构表所需 CLDR 数据，精确隔离畸形 Windows mapping。"""
        if name == "windows_zone_mapping":
            return windows_mapping
        if name == "zone_aliases":
            return {"US/Pacific": "America/Los_Angeles"}
        raise AssertionError(f"unexpected CLDR global: {name}")

    monkeypatch.setattr(module, "get_global", fake_get_global)
    with pytest.raises(PermanentProviderError) as raised:
        module._build_mappings()

    assert raised.value.error_code == "calendar_timezone_mapping_unsupported"
    assert tuple(zoneinfo.TZPATH) == original_tzpath


def test_missing_zone_alias_cldr_mapping_fails_stably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLDR alias 根对象缺失时不能借 ZoneInfo 对 alias 的宿主支持继续运行。"""
    module = _module()
    original_tzpath = tuple(zoneinfo.TZPATH)

    def fake_get_global(name: str) -> object:
        """提供有效 Windows 条目但移除 alias 表，触发独立缺失边界。"""
        if name == "windows_zone_mapping":
            return {"Pacific Standard Time": "America/Los_Angeles"}
        if name == "zone_aliases":
            return None
        raise AssertionError(f"unexpected CLDR global: {name}")

    monkeypatch.setattr(module, "get_global", fake_get_global)
    with pytest.raises(PermanentProviderError) as raised:
        module._build_mappings()

    assert raised.value.error_code == "calendar_timezone_mapping_unsupported"
    assert tuple(zoneinfo.TZPATH) == original_tzpath


def test_missing_packaged_tzdata_fails_without_mutating_tzpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使宿主 ZoneInfo 可用，缺失 Python tzdata 也必须在映射边界稳定失败。"""
    module = _module()
    original_tzpath = tuple(zoneinfo.TZPATH)

    def missing_files(_package: object) -> object:
        """模拟锁定依赖未安装，禁止回退到宿主时区数据库。"""
        raise ModuleNotFoundError("synthetic missing tzdata")

    monkeypatch.setattr(importlib.resources, "files", missing_files)
    with pytest.raises(PermanentProviderError) as raised:
        module._build_mappings()

    assert raised.value.error_code == "calendar_timezone_mapping_unsupported"
    assert "synthetic missing tzdata" not in str(raised.value)
    assert tuple(zoneinfo.TZPATH) == original_tzpath


def test_malformed_packaged_tzdata_fails_without_mutating_tzpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tzdata 包缺少目标 zone 文件时不得借宿主 TZPATH 掩盖损坏。"""
    module = _module()
    original_tzpath = tuple(zoneinfo.TZPATH)

    class MissingZoneResource:
        """模拟可导入但内容损坏的 tzdata Traversable。"""

        def joinpath(self, *_descendants: str) -> "MissingZoneResource":
            """保持链式路径接口并始终指向不存在的资源。"""
            return self

        def is_file(self) -> bool:
            """声明所有 zone 文件均缺失。"""
            return False

    monkeypatch.setattr(
        importlib.resources,
        "files",
        lambda _package: MissingZoneResource(),
    )
    with pytest.raises(PermanentProviderError) as raised:
        module._build_mappings()

    assert raised.value.error_code == "calendar_timezone_mapping_unsupported"
    assert tuple(zoneinfo.TZPATH) == original_tzpath


def test_corrupt_packaged_utc_file_fails_without_host_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UTC 资源存在但不是有效 TZif 时也必须失败，不能因特殊分支跳过解析。"""
    module = _module()
    original_tzpath = tuple(zoneinfo.TZPATH)

    class CorruptZoneResource:
        """模拟路径完整但内容损坏的 tzdata zone 文件。"""

        def joinpath(self, *_descendants: str) -> "CorruptZoneResource":
            """保持 Traversable 路径接口。"""
            return self

        def is_file(self) -> bool:
            """声明资源存在，使测试进入 TZif 内容校验。"""
            return True

        def open(self, _mode: str) -> io.BytesIO:
            """返回不含 TZif header 的合成损坏内容。"""
            return io.BytesIO(b"synthetic-corrupt-zoneinfo")

    def fake_get_global(name: str) -> object:
        """只提供 UTC 映射，确保损坏发生在 UTC 特殊分支。"""
        if name == "windows_zone_mapping":
            return {"Synthetic UTC": "UTC"}
        if name == "zone_aliases":
            return {"Etc/UCT": "Etc/UTC"}
        raise AssertionError(f"unexpected CLDR global: {name}")

    monkeypatch.setattr(module, "get_global", fake_get_global)
    monkeypatch.setattr(
        importlib.resources,
        "files",
        lambda _package: CorruptZoneResource(),
    )
    with pytest.raises(PermanentProviderError) as raised:
        module._build_mappings()

    assert raised.value.error_code == "calendar_timezone_mapping_unsupported"
    assert tuple(zoneinfo.TZPATH) == original_tzpath
