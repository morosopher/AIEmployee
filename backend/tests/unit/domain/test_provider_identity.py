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
        ("google", "tenant-a", "google-user-1"),
        ("google", "", "google user-1"),
        ("google", "", "google\x00user-1"),
        ("microsoft", "tenant:a", "tenant:a:graph-user-1"),
        ("unknown", "", "opaque-user-1"),
    ),
)
def test_canonical_provider_identity_key_rejects_injected_or_inconsistent_identity(
    provider: str,
    tenant: str,
    account: str,
) -> None:
    """tenant 不一致、控制字符、结构性多冒号和错误供应商字段必须 fail closed。"""
    with pytest.raises(ValueError, match="provider identity key is invalid"):
        canonical_provider_identity_key(provider, tenant, account)


@pytest.mark.parametrize(
    "identity_key",
    (
        "microsoft:tenant-a:tenant-a:graph-user-1",
        "microsoft::graph-user-1",
        "microsoft:tenant-a:graph user-1",
        "google:tenant-a:google-user-1",
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


@pytest.mark.parametrize(
    ("provider", "tenant", "account", "expected"),
    (
        (
            "google",
            "",
            "google@opaque:subject%1",
            "google::google%40opaque%3Asubject%251",
        ),
        (
            "microsoft",
            "tenant-a",
            "tenant-a:graph@opaque%1",
            "microsoft:tenant-a:graph%40opaque%251",
        ),
    ),
)
def test_canonical_provider_identity_key_percent_encodes_opaque_reserved_characters(
    provider: str,
    tenant: str,
    account: str,
    expected: str,
) -> None:
    """opaque 账户中的 ``@``、``:``、``%`` 必须编码且可逆，普通 ID 保持原样。"""
    canonical = canonical_provider_identity_key(provider, tenant, account)

    assert canonical == expected
    assert parse_provider_identity_key(canonical) == (provider, tenant, account)


@pytest.mark.parametrize(
    "identity_key",
    (
        "google::google@opaque",
        "google::google%opaque",
        "google::google%4aopaque",
        "google::google%41opaque",
        "google::google%ZZopaque",
        "google::google%",
        "google::google%00opaque",
        "google::google%20opaque",
        "google::google%FFopaque",
        "microsoft:tenant-a:graph%3Aid",
    ),
)
def test_parse_provider_identity_key_rejects_noncanonical_percent_encoding(
    identity_key: str,
) -> None:
    """parser 必须拒绝未编码、大小写错误、过度编码和 malformed 百分号。"""
    with pytest.raises(ValueError, match="provider identity key is invalid") as exc_info:
        parse_provider_identity_key(identity_key)

    assert str(exc_info.value) == "provider identity key is invalid"


def test_percent_encoding_keeps_literal_escapes_distinct_from_reserved_characters() -> None:
    """实际保留字符与形似 escape 的 literal 百分号必须生成不同且可逆的 key。"""
    accounts = ("opaque:subject", "opaque%3Asubject", "opaque@subject", "opaque%40subject")

    identity_keys = {
        canonical_provider_identity_key("google", "", account): account for account in accounts
    }

    assert len(identity_keys) == len(accounts)
    for identity_key, account in identity_keys.items():
        assert parse_provider_identity_key(identity_key) == ("google", "", account)


@pytest.mark.parametrize(
    ("account", "should_pass"),
    (
        ("a" * 255, True),
        ("a" * 256, False),
        ("@" * 255, True),
        ("@" * 256, False),
    ),
)
def test_google_identity_enforces_raw_length_without_rejecting_percent_expansion(
    account: str,
    should_pass: bool,
) -> None:
    """Google raw account 以 255 字符为界，percent 扩展使用独立有界容量。"""
    if should_pass:
        canonical = canonical_provider_identity_key("google", "", account)
        assert parse_provider_identity_key(canonical) == ("google", "", account)
    else:
        with pytest.raises(ValueError, match="provider identity key is invalid"):
            canonical_provider_identity_key("google", "", account)


def test_parser_rejects_google_decoded_component_longer_than_255() -> None:
    """allowlist segment 即使语法简单，解码后的 raw account 也不能超过 OAuth 上限。"""
    identity_key = "google::" + ("a" * 256)

    with pytest.raises(ValueError, match="provider identity key is invalid"):
        parse_provider_identity_key(identity_key)


def test_google_identity_keeps_255_four_byte_opaque_characters_round_trippable() -> None:
    """raw 255 字符上限按 OAuth 端口的字符语义执行，不误伤 UTF-8 percent 扩展。"""
    account = "😀" * 255

    canonical = canonical_provider_identity_key("google", "", account)

    assert parse_provider_identity_key(canonical) == ("google", "", account)


@pytest.mark.parametrize(
    ("tenant", "account", "should_pass"),
    (
        ("t" * 253, ("t" * 253) + ":g", True),
        ("t" * 254, ("t" * 254) + ":g", False),
        ("tenant-a", "tenant-a:" + ("g" * 246), True),
        ("tenant-a", "tenant-a:" + ("g" * 247), False),
        ("tenant-a", "tenant-a:" + ("@" * 246), True),
        ("tenant-a", "tenant-a:" + ("@" * 247), False),
        ("tenant-a", "tenant-a:graph:id", False),
    ),
)
def test_microsoft_identity_enforces_component_storage_and_encoding_lengths(
    tenant: str,
    account: str,
    should_pass: bool,
) -> None:
    """Microsoft 同时限制 tenant、Graph segment、encoded segment 与存储复合键。"""
    if should_pass:
        canonical = canonical_provider_identity_key("microsoft", tenant, account)
        assert parse_provider_identity_key(canonical) == ("microsoft", tenant, account)
    else:
        with pytest.raises(ValueError, match="provider identity key is invalid"):
            canonical_provider_identity_key("microsoft", tenant, account)
