"""验证指标在 API 错误路径上实际采集，而不是仅声明 collector。"""

import asyncio
from contextlib import suppress

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_employee.api.deps import ApiProblem, handle_api_problem
from ai_employee.infrastructure.observability.metrics import (
    Metrics,
    create_metrics,
    run_periodic_heartbeat,
)


def test_api_problem_handler_records_route_and_error_code_metric() -> None:
    """Problem Details 边界应增加固定路由与错误码标签，不能携带用户或内容。"""
    app = FastAPI()
    app.state.metrics = create_metrics()
    app.add_exception_handler(ApiProblem, handle_api_problem)

    @app.get("/failure")
    def failure() -> None:
        """抛出合成安全错误以触发可观察性边界。"""
        raise ApiProblem(409, "state_conflict", "State conflict", "Safe public detail")

    response = TestClient(app, raise_server_exceptions=False).get("/failure")

    assert response.status_code == 409
    rendered = app.state.metrics.render().body.decode("utf-8")
    assert 'ai_employee_api_errors_total{error_code="state_conflict",route="/failure"} 1.0' in rendered


def test_metrics_records_task_model_and_process_observations_without_user_labels() -> None:
    """任务、模型和进程辅助方法必须写入计划规定的时序列。"""
    metrics = create_metrics()

    metrics.record_task_outcome(kind="daily_brief", status="succeeded", duration_seconds=2.5)
    metrics.record_task_retry(kind="daily_brief")
    metrics.record_queue_wait(kind="daily_brief", seconds=1.5)
    metrics.record_stuck_tasks(kind="daily_brief", count=2)
    metrics.record_sync_age(provider="google", resource="gmail", seconds=60)
    metrics.record_provider_error(provider="google", error_code="google_rate_limited")
    metrics.record_model_response(
        model="test-model",
        input_tokens=10,
        output_tokens=5,
        estimated_cost_usd=0.0125,
        latency_seconds=0.2,
    )
    metrics.record_model_schema_repair(model="test-model", outcome="succeeded")
    metrics.record_sse_connection(connected=True)
    metrics.record_sse_connection(connected=False)
    metrics.record_heartbeat(process="worker", age_seconds=0)
    metrics.record_dependency_health(dependency="postgres", healthy=True)

    rendered = metrics.render().body.decode("utf-8")

    assert 'ai_employee_tasks_total{kind="daily_brief",status="succeeded"} 1.0' in rendered
    assert 'ai_employee_task_retries_total{kind="daily_brief"} 1.0' in rendered
    assert 'ai_employee_model_tokens_total{direction="input",model="test-model"} 10.0' in rendered
    assert 'ai_employee_model_tokens_total{direction="output",model="test-model"} 5.0' in rendered
    assert 'ai_employee_sse_connections{state="active"} 0.0' in rendered
    assert 'ai_employee_process_heartbeat_age_seconds{process="worker"}' in rendered
    assert 'ai_employee_dependency_health{dependency="postgres"} 1.0' in rendered


def test_heartbeat_age_advances_at_scrape_time() -> None:
    """心跳 Gauge 必须反映距上次刷新经过的时间，而非永久为零。"""
    current = [10.0]
    metrics = Metrics(monotonic_clock=lambda: current[0])
    metrics.record_heartbeat(process="worker", age_seconds=0)
    current[0] = 17.5

    rendered = metrics.render().body.decode("utf-8")

    assert 'ai_employee_process_heartbeat_age_seconds{process="worker"} 7.5' in rendered


def test_sync_freshness_age_advances_from_last_success_at_scrape_time() -> None:
    """同步成功后 Gauge 必须随 scrape 反映实际经过时间，失败不能重置该时间。"""
    current = [10.0]
    metrics = Metrics(monotonic_clock=lambda: current[0])
    metrics.record_sync_success(provider="google", resource="gmail")
    current[0] = 17.5

    rendered = metrics.render().body.decode("utf-8")

    assert 'ai_employee_sync_age_seconds{provider="google",resource="gmail"} 7.5' in rendered


def test_clearing_stuck_metrics_resets_previously_seen_kind() -> None:
    """后续扫描没有过期任务时，旧 kind 必须归零而不能保留陈旧告警。"""
    metrics = create_metrics()
    metrics.record_stuck_tasks(kind="sync_mail", count=2)
    metrics.clear_stuck_tasks(active_kinds=set())

    assert 'ai_employee_stuck_tasks{kind="sync_mail"} 0.0' in metrics.render().body.decode("utf-8")


@pytest.mark.asyncio
async def test_periodic_heartbeat_runs_while_process_is_idle_and_cancels_cleanly() -> None:
    """后台心跳不依赖任务流量，并可由生命周期取消以避免测试或进程泄漏。"""
    metrics = create_metrics()
    task = asyncio.create_task(
        run_periodic_heartbeat(metrics=metrics, process="worker", interval_seconds=0.001)
    )
    try:
        await asyncio.sleep(0.003)
        rendered = metrics.render().body.decode("utf-8")
        assert 'ai_employee_process_heartbeat_age_seconds{process="worker"}' in rendered
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
