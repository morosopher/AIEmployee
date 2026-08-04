"""验证非 ASGI 进程启动时独立暴露安全指标。"""

from ai_employee.infrastructure.observability.metrics import create_metrics
from ai_employee.workers.observability import initialize_process_metrics


def test_process_metrics_start_listener_and_publish_initial_heartbeat() -> None:
    """Worker/Scheduler 启动仅暴露内部指标并立即写入健康心跳。"""
    metrics = create_metrics()
    ports: list[int] = []
    metrics.start_internal_listener = lambda *, port: ports.append(port)  # type: ignore[method-assign]

    initialize_process_metrics(metrics=metrics, process="worker", port=9101)

    assert ports == [9101]
    rendered = metrics.render().body.decode("utf-8")
    sample = next(
        line for line in rendered.splitlines() if line.startswith("ai_employee_process_heartbeat_age_seconds{")
    )
    assert 0 <= float(sample.rsplit(" ", maxsplit=1)[1]) < 1
