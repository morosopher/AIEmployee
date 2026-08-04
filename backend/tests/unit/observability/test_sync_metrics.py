"""验证 Google 同步观测边界只记录无内容的聚合指标。"""

import pytest

from ai_employee.domain.errors import DomainError, TransientProviderError, UserActionRequiredError
from ai_employee.infrastructure.observability.metrics import create_metrics
from ai_employee.infrastructure.observability.sync import observe_google_sync


@pytest.mark.asyncio
async def test_successful_google_sync_resets_only_resource_freshness() -> None:
    """成功同步应把对应资源的新鲜度写为零，且没有用户或连接维度。"""
    metrics = create_metrics()

    async def succeed() -> None:
        """以 Fake 协程替代真实 Google 调用。"""

    await observe_google_sync(metrics=metrics, resource="gmail", operation=succeed)

    rendered = metrics.render().body.decode("utf-8")
    sync_samples = [line for line in rendered.splitlines() if line.startswith("ai_employee_sync_age_seconds{")]
    assert len(sync_samples) == 1
    assert 'provider="google",resource="gmail"' in sync_samples[0]
    assert 0 <= float(sync_samples[0].rsplit(" ", maxsplit=1)[1]) < 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        TransientProviderError(error_code="google_rate_limited", message="safe"),
        UserActionRequiredError(error_code="google_reauthorization_required", message="safe"),
    ),
)
async def test_failed_google_sync_records_only_normalized_error_code(error: DomainError) -> None:
    """可分类 Google 失败必须递增错误指标，并保持原异常控制流。"""
    metrics = create_metrics()

    async def fail() -> None:
        """以合成领域错误模拟已规范化的供应商失败。"""
        raise error

    with pytest.raises(type(error)):
        await observe_google_sync(metrics=metrics, resource="calendar", operation=fail)

    rendered = metrics.render().body.decode("utf-8")
    assert (
        'ai_employee_provider_errors_total{error_code="'
        f"{error.error_code}\",provider=\"google\"}} 1.0"
    ) in rendered
    assert "safe" not in rendered
