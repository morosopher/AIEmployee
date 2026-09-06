"""验证 M2 指标的封闭标签、实际抓取年龄及供应商 URL 防泄露边界。"""

from urllib.parse import quote

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from ai_employee.application.use_cases.trusted_actions import validate_provider_url
from ai_employee.infrastructure.observability.metrics import Metrics


def test_m2_metrics_have_exact_bounded_labels() -> None:
    """九个指标族只能使用规格批准的低基数维度，不保留任何任务或内容信息。"""
    registry = CollectorRegistry()
    metrics = Metrics(registry)
    metrics.record_provider_write(provider="google", action="mail.send", outcome="unknown")
    metrics.record_approval_decision(action="mail.send", decision="approved")
    metrics.record_approval_expired(action="calendar.update")
    metrics.record_reconciliation(
        provider="microsoft", action="calendar.update", outcome="confirmed_applied"
    )
    metrics.record_action_state(
        provider="google", action="mail.send", needs_attention=2, age_seconds=950
    )
    metrics.record_capability_state(
        provider="google", capability="mail.send", state="enabled", count=1
    )
    metrics.record_write_kill_switch(provider="google", enabled=False)
    metrics.record_calendar_version_conflict(provider="microsoft")
    expected = {
        "provider_write_requests": {"provider", "action", "outcome"},
        "approval_decisions": {"action", "decision"},
        "approval_expired": {"action"},
        "tool_reconciliation": {"provider", "action", "outcome"},
        "tool_reconciliation_age_seconds": {"provider", "action"},
        "needs_attention_tasks": {"provider", "action"},
        "calendar_version_conflicts": {"provider"},
        "connection_capability_state": {"provider", "capability", "state"},
        "write_kill_switch_state": {"provider"},
    }
    families = {family.name.removeprefix("ai_employee_"): family for family in registry.collect()}
    assert expected.keys() <= families.keys()
    for name, labelnames in expected.items():
        for sample in families[name].samples:
            assert set(sample.labels) == labelnames
    output = generate_latest(registry).decode()
    assert "task_id" not in output and "example.test" not in output
    with pytest.raises(ValueError):
        metrics.record_provider_write(
            provider="recipient@example.test", action="mail.send", outcome="unknown"
        )
    with pytest.raises(ValueError):
        metrics.record_approval_decision(action="arbitrary", decision="approved")


@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_adapter_observation_reports_enabled_but_missing_registry(enabled: bool) -> None:
    """真实 registry 缺少适配器仅在有效写开关开启时告警，观测本身绝不发出请求。"""
    from ai_employee.config import Settings
    from ai_employee.integrations.registry import ProviderAdapterRegistry
    from ai_employee.workers.observability import observe_write_runtime

    registry = CollectorRegistry()
    settings = Settings(_env_file=None).model_copy(
        update={"external_writes_enabled": enabled, "google_writes_enabled": enabled}
    )
    observe_write_runtime(
        metrics=Metrics(registry), settings=settings, adapters=ProviderAdapterRegistry()
    )
    assert registry.get_sample_value(
        "ai_employee_write_kill_switch_state", {"provider": "google"}
    ) == int(enabled)
    assert registry.get_sample_value(
        "ai_employee_dependency_health", {"dependency": "google_write_adapter_policy"}
    ) == int(not enabled)


def test_direct_worker_registry_scrape_advances_freshness_without_render() -> None:
    """Worker/Scheduler 直曝 registry 时，心跳和核对年龄仍随注入时钟推进。"""
    current = [100.0]
    registry = CollectorRegistry()
    metrics = Metrics(registry, monotonic_clock=lambda: current[0])
    metrics.record_heartbeat(process="reconciliation_scanner", age_seconds=0)
    metrics.record_sync_success(provider="google", resource="mail")
    metrics.record_action_state(
        provider="google", action="mail.send", needs_attention=1, age_seconds=20
    )
    current[0] = 140.0
    output = generate_latest(registry).decode()
    assert 'process="reconciliation_scanner"} 40.0' in output
    assert 'ai_employee_sync_age_seconds{provider="google",resource="mail"} 40.0' in output
    assert (
        'ai_employee_tool_reconciliation_age_seconds{action="mail.send",provider="google"} 60.0'
        in output
    )


def test_persisted_sync_age_supersedes_local_success_on_direct_scrape() -> None:
    """数据库的最旧来源年龄必须覆盖单个连接的本地成功，API/Worker 抓取保持一致。"""
    current = [100.0]
    registry = CollectorRegistry()
    metrics = Metrics(registry, monotonic_clock=lambda: current[0])
    metrics.record_sync_success(provider="google", resource="mail")
    current[0] = 120.0
    metrics.record_sync_age(provider="google", resource="mail", seconds=900)
    current[0] = 150.0
    labels = {"provider": "google", "resource": "mail"}
    assert registry.get_sample_value("ai_employee_sync_age_seconds", labels) == 930
    assert 'resource="mail"} 930.0' in metrics.render().body.decode()


@pytest.mark.parametrize("sensitive", ["recipient@example.test", "?token=synthetic-value"])
@pytest.mark.parametrize("depth", [1, 3, 4, 8, 20])
def test_provider_url_rejects_deeply_encoded_sensitive_content(sensitive: str, depth: int) -> None:
    """嵌套 percent 编码不得把地址或凭据参数传入供应商检查入口。"""
    encoded = sensitive
    for _ in range(depth):
        encoded = quote(encoded, safe="")
    assert validate_provider_url(f"https://provider.example.test/{encoded}") is None


def test_provider_url_preserves_legitimate_opaque_resource_encoding() -> None:
    """普通 opaque 资源 ID 的编码仍可保留，避免用全面禁用链接掩盖泄漏。"""
    url = "https://provider.example.test/items/synthetic%2Fresource?view=compact"
    assert validate_provider_url(url) == url


@pytest.mark.asyncio
async def test_maintenance_heartbeats_require_completed_scanner_and_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """维护任务真正完成才刷新专属心跳；进程仍存活不能掩盖扫描器或 relay 挂起。"""
    from datetime import UTC, datetime

    from ai_employee.workers import schedules

    class Recovery:
        """代替数据库恢复存储，保留真实维护函数的完成/失败边界。"""

        def __init__(self, _factory: object) -> None:
            pass

        async def recover_due_reconciliations(self, **_kwargs: object) -> int:
            return 0

    class Relay:
        """代替网络投递；只有真实 wrapper 可以记录完成后的心跳。"""

        async def relay_once(self, **_kwargs: object) -> int:
            return 0

    current = [10.0]
    registry = CollectorRegistry()
    metrics = Metrics(registry, monotonic_clock=lambda: current[0])
    monkeypatch.setattr(schedules, "_scheduler_metrics", metrics)
    monkeypatch.setattr(schedules, "SqlAlchemyTrustedActionReconciliationRecoveryStore", Recovery)
    monkeypatch.setattr(schedules, "_build_outbox_relay", lambda: Relay())
    await schedules.recover_due_reconciliations(now=datetime(2030, 1, 1, tzinfo=UTC), limit=1)
    await schedules.relay_outbox()
    current[0] = 40.0
    assert (
        registry.get_sample_value(
            "ai_employee_process_heartbeat_age_seconds", {"process": "reconciliation_scanner"}
        )
        == 30
    )
    assert (
        registry.get_sample_value(
            "ai_employee_process_heartbeat_age_seconds", {"process": "outbox_relay"}
        )
        == 30
    )

    async def failed_scan(self: object, **_kwargs: object) -> int:
        raise TimeoutError("synthetic scan failure")

    monkeypatch.setattr(Recovery, "recover_due_reconciliations", failed_scan)
    with pytest.raises(TimeoutError):
        await schedules.recover_due_reconciliations(now=datetime(2030, 1, 1, tzinfo=UTC), limit=1)
    assert (
        registry.get_sample_value(
            "ai_employee_process_heartbeat_age_seconds", {"process": "reconciliation_scanner"}
        )
        == 30
    )
