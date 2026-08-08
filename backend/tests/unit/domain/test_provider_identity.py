"""验证连接身份键在 Trusted Action 账户门禁中的严格规范。"""

import pytest

from ai_employee.domain.connections import (
    canonical_provider_identity_key,
    parse_provider_identity_key,
)


class _ExplodingString(str):
    """模拟不可信字符串子类，确保 Google tenant 校验先做精确类型收窄。"""

    def __ne__(self, other: object) -> bool:
        """拒绝任何值比较；若被提前调用会暴露边界顺序错误。"""
        raise RuntimeError("untrusted tenant comparison must not run")


def test_google_identity_rejects_string_subclass_without_running_custom_comparison() -> None:
    """Google 的空 tenant 必须是普通空字符串，不能执行不可信子类的比较钩子。"""
    with pytest.raises(ValueError, match="provider identity key is invalid") as exc_info:
        canonical_provider_identity_key(
            "google",
            _ExplodingString(""),
            "google-user-1",
        )

    assert str(exc_info.value) == "provider identity key is invalid"


@pytest.mark.parametrize(
    ("provider", "tenant", "account", "expected"),
    (
        (
            "microsoft",
            "tenant-a",
            "tenant-a:graph-user-1",
            "microsoft:tenant-a:graph-user-1",
        ),
        ("google", "", "google-user-1", "google::google-user-1"),
    ),
)
def test_canonical_provider_identity_key_uses_strict_three_segment_format(
    provider: str,
    tenant: str,
    account: str,
    expected: str,
) -> None:
    """Microsoft 同时绑定 tenant/Graph ID，Google 保留空 tenant 兼容格式。"""
    identity_key = canonical_provider_identity_key(provider, tenant, account)

    assert identity_key == expected
    assert len(identity_key.split(":")) == 3
    assert parse_provider_identity_key(identity_key) == (provider, tenant, account)


@pytest.mark.parametrize(
    ("provider", "tenant", "account"),
    (
        ("microsoft", "tenant-a", "tenant-b:graph-user-1"),
        ("microsoft", "tenant-a", "tenant-a:graph:user-1"),
        ("microsoft", "tenant-a", "graph-user-1"),
        ("microsoft", "", "tenant-a:graph-user-1"),
        ("microsoft", "tenant a", "tenant a:graph-user-1"),
        ("microsoft", "tenant-a", "tenant-a:graph user-1"),
        ("microsoft", "tenant-a", "tenant-a:graph\x00user-1"),
        ("microsoft", "tenant-a", "tenant-a:graph@example.test"),
        ("google", "tenant-a", "google-user-1"),
        ("google", "", "google:user-1"),
        ("google", "", "google user-1"),
        ("google", "", "google\x00user-1"),
        ("google", "", "google@example.test"),
        ("microsoft", "tenant:a", "tenant:a:graph-user-1"),
        ("unknown", "", "opaque-user-1"),
    ),
)
def test_canonical_provider_identity_key_rejects_injected_or_inconsistent_identity(
    provider: str,
    tenant: str,
    account: str,
) -> None:
    """分隔符注入、tenant 不一致和错误供应商字段必须 fail closed。"""
    with pytest.raises(ValueError, match="provider identity key is invalid"):
        canonical_provider_identity_key(provider, tenant, account)


@pytest.mark.parametrize(
    "identity_key",
    (
        "microsoft:tenant-a:tenant-a:graph-user-1",
        "microsoft::graph-user-1",
        "microsoft:tenant-a:graph user-1",
        "google:tenant-a:google-user-1",
        "google::google@example.test",
        "unknown::opaque-user-1",
    ),
)
def test_parse_provider_identity_key_rejects_noncanonical_keys_without_echoing(
    identity_key: str,
) -> None:
    """配置 parser 对四段键和不安全段 fail closed，异常不得回显身份。"""
    with pytest.raises(ValueError, match="provider identity key is invalid") as exc_info:
        parse_provider_identity_key(identity_key)

    assert identity_key not in str(exc_info.value)


def test_canonical_provider_identity_key_error_does_not_echo_stored_identity() -> None:
    """数据库复合账户键非法时仅返回固定错误，避免身份进入日志或校验输出。"""
    stored_account_id = "tenant-a:private graph fragment"

    with pytest.raises(ValueError, match="provider identity key is invalid") as exc_info:
        canonical_provider_identity_key(
            "microsoft",
            "tenant-a",
            stored_account_id,
        )

    assert stored_account_id not in str(exc_info.value)
