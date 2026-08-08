"""验证 M2 真实写入配置默认关闭，并以供应商身份键限制测试账户。"""

import os

import pytest
from pydantic_settings import SettingsError

from ai_employee.config import Settings


def _clear_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除 Settings 字段对应环境变量，避免开发机或 CI 宿主配置污染单元测试。

    Pydantic Settings 默认按大小写不敏感规则查找环境键，因此先以 ``casefold``
    归一化字段名、环境前缀及字符串别名，再从环境键快照中收集所有匹配项并删除。
    需要验证环境字符串解析的测试会在本 helper 完成后显式设置合成值。

    Args:
        monkeypatch: Pytest 提供的进程环境隔离工具，测试结束后自动恢复宿主值。
    """
    environment_prefix = str(Settings.model_config.get("env_prefix", ""))
    settings_environment_names: set[str] = set()
    for field_name, field_info in Settings.model_fields.items():
        settings_environment_names.add(f"{environment_prefix}{field_name}".casefold())
        for field_alias in (field_info.alias, field_info.validation_alias):
            if isinstance(field_alias, str):
                settings_environment_names.add(field_alias.casefold())

    environment_keys_to_remove = tuple(
        environment_key
        for environment_key in os.environ
        if environment_key.casefold() in settings_environment_names
    )
    for environment_key in environment_keys_to_remove:
        monkeypatch.delenv(environment_key, raising=False)


@pytest.fixture(autouse=True)
def clear_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """在每个配置测试前应用统一的宿主环境隔离。"""
    _clear_settings_environment(monkeypatch)


def test_external_writes_are_fail_closed_by_default() -> None:
    """未提供环境配置时，全局、供应商和账户三层写入门禁都必须拒绝。"""
    settings = Settings(_env_file=None)

    assert settings.external_writes_enabled is False
    assert settings.google_writes_enabled is False
    assert settings.microsoft_writes_enabled is False
    assert settings.write_test_account_allowlist == []
    assert settings.provider_writes_enabled("google") is False
    assert settings.provider_writes_enabled("microsoft") is False
    assert settings.provider_writes_enabled("unknown") is False
    assert settings.write_account_allowed("google::synthetic-account") is False


def test_clear_settings_environment_removes_lowercase_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """隔离 helper 必须清除 Pydantic 大小写不敏感匹配到的小写宿主变量。"""
    monkeypatch.setenv("external_writes_enabled", "true")
    monkeypatch.setenv("google_writes_enabled", "true")
    monkeypatch.setenv(
        "write_test_account_allowlist",
        '["google::synthetic-lowercase-account"]',
    )

    _clear_settings_environment(monkeypatch)
    settings = Settings(_env_file=None)

    assert settings.external_writes_enabled is False
    assert settings.google_writes_enabled is False
    assert settings.write_test_account_allowlist == []


def test_provider_switches_cannot_bypass_the_global_write_switch() -> None:
    """仅打开供应商开关时仍不得启用真实写入，避免单层误配置突破总闸。"""
    settings = Settings(
        _env_file=None,
        google_writes_enabled=True,
        microsoft_writes_enabled=True,
    )

    assert settings.provider_writes_enabled("google") is False
    assert settings.provider_writes_enabled("microsoft") is False


@pytest.mark.parametrize("provider_switch", ["google_writes_enabled", "microsoft_writes_enabled"])
def test_non_production_writes_require_a_test_account_allowlist(
    provider_switch: str,
) -> None:
    """非生产环境同时打开全局与任一供应商写入时必须配置专用账户白名单。"""
    with pytest.raises(ValueError, match="WRITE_TEST_ACCOUNT_ALLOWLIST"):
        Settings(
            _env_file=None,
            app_env="staging",
            external_writes_enabled=True,
            **{provider_switch: True},
        )


def test_write_allowlist_matches_only_the_exact_provider_identity_key() -> None:
    """白名单按规范化连接身份精确匹配，不接受裸邮箱或同供应商的其他账户。"""
    settings = Settings(
        _env_file=None,
        app_env="staging",
        external_writes_enabled=True,
        google_writes_enabled=True,
        write_test_account_allowlist=["google::synthetic-account"],
    )

    assert settings.provider_writes_enabled("google") is True
    assert settings.provider_writes_enabled("microsoft") is False
    assert settings.write_account_allowed("google::synthetic-account") is True
    assert settings.write_account_allowed("google::another-account") is False
    assert settings.write_account_allowed("synthetic-account@example.test") is False


def test_write_allowlist_accepts_canonical_microsoft_identity_and_rejects_four_segments() -> None:
    """Microsoft 白名单只接受去重 tenant 后的三段键，并继续执行精确匹配。"""
    identity_key = "microsoft:synthetic-tenant:synthetic-graph-account"
    settings = Settings(
        _env_file=None,
        app_env="staging",
        external_writes_enabled=True,
        microsoft_writes_enabled=True,
        write_test_account_allowlist=[identity_key],
    )

    assert settings.write_account_allowed(identity_key) is True
    other_identity_key = "microsoft:synthetic-tenant:another-synthetic-graph-account"
    assert settings.write_account_allowed(other_identity_key) is False
    with pytest.raises(ValueError, match="WRITE_TEST_ACCOUNT_ALLOWLIST"):
        Settings(
            _env_file=None,
            app_env="staging",
            external_writes_enabled=True,
            microsoft_writes_enabled=True,
            write_test_account_allowlist=[
                "microsoft:synthetic-tenant:synthetic-tenant:synthetic-graph-account"
            ],
        )


@pytest.mark.parametrize(
    "identity_key",
    (
        "microsoft:synthetic-tenant:synthetic graph account",
        "google::synthetic\taccount",
        "google::synthetic\x00account",
    ),
)
def test_write_allowlist_rejects_identity_whitespace_and_controls_without_echoing(
    identity_key: str,
) -> None:
    """配置复用领域 parser，拒绝旧宽松校验会接受的内部空白和控制字符。"""
    with pytest.raises(ValueError, match="WRITE_TEST_ACCOUNT_ALLOWLIST") as exc_info:
        Settings(
            _env_file=None,
            write_test_account_allowlist=[identity_key],
        )

    assert identity_key not in str(exc_info.value)


def test_write_allowlist_rejects_bare_account_email_entries() -> None:
    """裸邮箱不是 canonical provider key，配置拒绝且不回显原始身份。"""
    rejected_identity = "synthetic-account@example.test"

    with pytest.raises(ValueError, match="WRITE_TEST_ACCOUNT_ALLOWLIST") as exc_info:
        Settings(
            _env_file=None,
            write_test_account_allowlist=[rejected_identity],
        )

    assert rejected_identity not in str(exc_info.value)


def test_write_allowlist_accepts_canonical_encoded_opaque_identity() -> None:
    """opaque ``@``、``:``、``%`` 经 canonical 编码后可配置，原始非规范形式仍拒绝。"""
    identity_key = "google::synthetic%40opaque%3Asubject%251"
    settings = Settings(
        _env_file=None,
        app_env="staging",
        external_writes_enabled=True,
        google_writes_enabled=True,
        write_test_account_allowlist=[identity_key],
    )

    assert settings.write_account_allowed(identity_key) is True
    assert settings.write_account_allowed("google::synthetic@opaque%3Asubject%251") is False


def test_write_allowlist_parses_normalized_identities_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON 环境字符串应解析 Google 空 tenant 与 Microsoft 非空 tenant 身份键。"""
    google_identity = "google::synthetic-google-account"
    microsoft_identity = "microsoft:synthetic-tenant:synthetic-microsoft-account"
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("EXTERNAL_WRITES_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_WRITES_ENABLED", "true")
    monkeypatch.setenv("MICROSOFT_WRITES_ENABLED", "true")
    monkeypatch.setenv(
        "WRITE_TEST_ACCOUNT_ALLOWLIST",
        '["google::synthetic-google-account",'
        '"microsoft:synthetic-tenant:synthetic-microsoft-account"]',
    )

    settings = Settings(_env_file=None)

    assert settings.write_test_account_allowlist == [google_identity, microsoft_identity]
    assert settings.provider_writes_enabled("google") is True
    assert settings.provider_writes_enabled("microsoft") is True
    assert settings.write_account_allowed(google_identity) is True
    assert settings.write_account_allowed(microsoft_identity) is True
    assert settings.write_account_allowed("google::another-account") is False


def test_write_allowlist_rejects_invalid_json_environment_without_echoing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法 JSON 环境值必须拒绝，且解析异常不得回显其中的合成身份内容。"""
    sensitive_fragment = "synthetic-json-account"
    monkeypatch.setenv(
        "WRITE_TEST_ACCOUNT_ALLOWLIST",
        '["google::synthetic-json-account"',
    )

    with pytest.raises(SettingsError) as exc_info:
        Settings(_env_file=None)

    assert sensitive_fragment not in str(exc_info.value)


def test_write_allowlist_rejects_invalid_environment_identity_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合法 JSON 中的非法身份键必须拒绝，且校验异常不得回显原始条目。"""
    rejected_identity = "google::synthetic-env-account@example.test"
    monkeypatch.setenv(
        "WRITE_TEST_ACCOUNT_ALLOWLIST",
        '["google::synthetic-env-account@example.test"]',
    )

    with pytest.raises(ValueError, match="WRITE_TEST_ACCOUNT_ALLOWLIST") as exc_info:
        Settings(_env_file=None)

    assert rejected_identity not in str(exc_info.value)


def test_production_empty_allowlist_allows_accounts_but_non_empty_list_still_restricts() -> None:
    """生产可依赖其他写入门禁而省略白名单；一旦配置则继续按精确身份收窄。"""
    unrestricted = Settings(_env_file=None, app_env="production")
    restricted = Settings(
        _env_file=None,
        app_env="production",
        write_test_account_allowlist=["google::synthetic-account"],
    )

    assert unrestricted.write_account_allowed("google::synthetic-account") is True
    assert restricted.write_account_allowed("google::synthetic-account") is True
    assert restricted.write_account_allowed("google::another-account") is False


@pytest.mark.parametrize(
    "identity_key",
    (
        "unknown::synthetic-account",
        "google::synthetic-account@example.test",
        "microsoft:tenant:tenant:graph-account",
        "microsoft:tenant:graph account",
    ),
)
def test_production_empty_allowlist_rejects_noncanonical_identity_candidates(
    identity_key: str,
) -> None:
    """生产空白名单也只放行可由同一 parser 验证的规范身份候选。"""
    settings = Settings(_env_file=None, app_env="production")

    assert settings.write_account_allowed(identity_key) is False
