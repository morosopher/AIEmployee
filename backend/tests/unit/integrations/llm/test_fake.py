"""Fake 模型场景可区分且不需要网络。"""

import pytest

from ai_employee.config import Settings
from ai_employee.domain.briefs import EmailJudgement
from ai_employee.infrastructure.observability.metrics import create_metrics
from ai_employee.integrations.llm.fake import (
    FakeModelGateway,
    ModelGatewayError,
    build_model_gateway,
)


@pytest.mark.asyncio
async def test_partial_scenario_has_distinct_metadata() -> None:
    response = await FakeModelGateway(scenario="partial").complete(
        model_name="x",
        prompt_version="x",
        messages=[{"role": "user", "content": "thread"}],
        response_model=EmailJudgement,
    )
    assert response.value.reason_codes == ["fake_partial"]
    assert response.usage.output_tokens == 1


@pytest.mark.asyncio
async def test_fake_gateway_records_the_same_success_metrics_as_a_real_gateway() -> None:
    """测试模式模型也必须输出统一的 token、成本和延迟指标。"""
    metrics = create_metrics()

    await FakeModelGateway(metrics=metrics).complete(
        model_name="fake-model",
        prompt_version="test",
        messages=[{"role": "user", "content": "thread"}],
        response_model=EmailJudgement,
    )

    rendered = metrics.render().body.decode("utf-8")
    assert 'ai_employee_model_tokens_total{direction="input",model="fake-model"} 0.0' in rendered
    assert 'ai_employee_model_tokens_total{direction="output",model="fake-model"} 0.0' in rendered
    assert 'ai_employee_model_cost_usd_total{model="fake-model"} 0.0' in rendered
    assert 'ai_employee_model_latency_seconds_count{model="fake-model"} 1.0' in rendered


@pytest.mark.asyncio
async def test_fake_gateway_records_schema_repair_and_error_for_invalid_output() -> None:
    """测试模式非法输出必须和真实 Gateway 一样留下安全的失败计数。"""
    metrics = create_metrics()

    with pytest.raises(ModelGatewayError):
        await FakeModelGateway(scenario="invalid_once", metrics=metrics).complete(
            model_name="fake-model",
            prompt_version="test",
            messages=[{"role": "user", "content": "thread"}],
            response_model=EmailJudgement,
        )

    rendered = metrics.render().body.decode("utf-8")
    assert 'ai_employee_model_schema_repairs_total{model="fake-model",outcome="requested"} 1.0' in rendered
    assert 'ai_employee_provider_errors_total{error_code="model_invalid_output",provider="model"} 1.0' in rendered


def test_test_mode_factory_keeps_the_supplied_metrics_registry() -> None:
    """APP_TEST_MODE 工厂不能丢弃调用方提供的进程级 registry。"""
    metrics = create_metrics()

    gateway = build_model_gateway(Settings(app_test_mode=True), metrics=metrics)

    assert isinstance(gateway, FakeModelGateway)
    assert gateway._metrics is metrics
