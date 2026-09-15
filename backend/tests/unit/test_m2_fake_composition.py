"""证明离线写组合只在双测试开关下存在，且测试策略无法授予真实 registry 权限。"""

import base64
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from ai_employee.config import Settings
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.integrations import registry as composition


def _settings(*, environment: str = "test", test_mode: bool = True) -> Settings:
    """构造三层真实开关全部关闭的配置，不读取 Secret 或连接外部资源。"""
    return Settings(
        app_env=environment, app_test_mode=test_mode,
        external_writes_enabled=False, google_writes_enabled=False,
        microsoft_writes_enabled=False, write_test_account_allowlist=[],
    )


def test_double_test_switches_build_only_synthetic_action_slots() -> None:
    """实际共享组合根必须替换全部四动作；不能用生产 resolver 后再悄悄打开写开关。"""
    settings = _settings()
    registry = composition.build_trusted_action_registry(
        session_factory=cast(ManagedAsyncSessionMaker, object()), settings=settings,
    )
    assert type(registry).__name__ == "SyntheticTrustedActionRegistry"
    assert settings.external_writes_enabled is False
    assert settings.google_writes_enabled is False
    assert settings.microsoft_writes_enabled is False
    assert settings.write_test_account_allowlist == []


@pytest.mark.parametrize("environment,test_mode", [("test", False), ("development", False)])
def test_ordinary_composition_keeps_real_write_gates_closed(environment: str, test_mode: bool) -> None:
    """没有双开关时策略必须保持原 Settings，所有真实适配器仍受三层开关约束。"""
    settings = _settings(environment=environment, test_mode=test_mode)
    registry = composition.build_trusted_action_registry(
        session_factory=cast(ManagedAsyncSessionMaker, object()), settings=settings,
    )
    select_policy = getattr(composition, "trusted_action_write_policy", None)
    assert callable(select_policy), "shared composition must bind a policy to its exact registry"
    assert select_policy(settings=settings, registry=registry) is settings


def test_synthetic_policy_rejects_real_identities_and_real_registry_pairing() -> None:
    """仅接受测试连接 UUID 命名空间，不能把测试策略用于真实 adapter 或其他配置对象。"""
    settings = _settings()
    sessions = cast(ManagedAsyncSessionMaker, object())
    registry = composition.build_trusted_action_registry(session_factory=sessions, settings=settings)
    select_policy = getattr(composition, "trusted_action_write_policy", None)
    assert callable(select_policy), "shared composition must bind a policy to its exact registry"
    policy = select_policy(settings=settings, registry=registry)
    identifier = str(UUID(int=1))
    assert policy.provider_writes_enabled("google")
    assert policy.provider_writes_enabled("microsoft")
    assert not policy.provider_writes_enabled("unsupported")
    for identity in (f"google::synthetic-m2-{identifier}", f"microsoft:synthetic-tenant:synthetic-m2-{identifier}"):
        assert policy.write_account_allowed(identity)
    for identity in ("google::ordinary-account", "google::synthetic-m2-invalid", f"microsoft:other-tenant:synthetic-m2-{identifier}"):
        assert not policy.write_account_allowed(identity)
    production = composition.CredentialBoundProviderRegistry(session_factory=sessions, settings=settings)
    assert select_policy(settings=settings, registry=production) is settings
    assert select_policy(settings=_settings(), registry=registry).provider_writes_enabled("google") is False


def test_restore_preparation_uses_the_offline_reader_in_double_test_mode(tmp_path: Path) -> None:
    """恢复准备也必须由双开关选择离线端口，不能因只读 GET 而误用真实 OAuth adapter。"""
    from ai_employee.workers.prepare_calendar_restore import (
        build_prepare_calendar_restore_task_step,
    )

    key = tmp_path / "synthetic-master"
    key.write_bytes(base64.b64encode(b"m" * 32))
    settings = _settings().model_copy(update={"app_master_key_file": key})
    step = build_prepare_calendar_restore_task_step(
        session_factory=cast(ManagedAsyncSessionMaker, object()), settings=settings,
    )
    assert type(step._reader_resolver).__name__ == "SyntheticCalendarRestoreReaderResolver"
