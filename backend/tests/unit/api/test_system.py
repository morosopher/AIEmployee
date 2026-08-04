"""验证 API 进程系统端点与高风险运行模式配置。"""

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_args, get_type_hints
from uuid import UUID, uuid4
from zoneinfo import ZoneInfoNotFoundError

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ai_employee.api import deps
from ai_employee.api.deps import get_authenticated_session
from ai_employee.api.routers.system import build_system_router
from ai_employee.application.use_cases.auth import Clock
from ai_employee.application.use_cases.diagnostics import DailyBriefOverdueAlert
from ai_employee.config import Settings
from ai_employee.infrastructure.observability.logging import _JsonFormatter
from ai_employee.main import create_app


class _RecordingAlertsUseCase:
    """记录告警路由传入的认证用户与 UTC 时刻，不依赖真实数据库。"""

    def __init__(self) -> None:
        """初始化空调用记录与一条合成的最小告警。"""
        self.calls: list[tuple[UUID, datetime]] = []

    async def execute(self, *, user_id: UUID, now: datetime) -> DailyBriefOverdueAlert | None:
        """返回用户范围告警，供路由契约测试观察授权输入。

        Args:
            user_id: 应由认证会话而非请求参数提供的用户标识。
            now: 路由从应用时钟读取的 UTC 真实时刻。

        Returns:
            不含来源内容的稳定严重告警。
        """
        self.calls.append((user_id, now))
        return DailyBriefOverdueAlert(
            code="daily_brief_overdue",
            severity="critical",
            local_date=date(2026, 8, 1),
            diagnostic_task_id=None,
        )


def test_health_returns_process_status() -> None:
    """健康端点应仅报告 API 进程存活，不执行外部依赖探测。"""
    client = TestClient(create_app())
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}


def test_alerts_requires_authentication() -> None:
    """逾期告警是用户范围派生视图，匿名请求必须得到稳定的认证失败。"""
    client = TestClient(create_app())

    response = client.get("/api/v1/system/alerts")

    assert response.status_code == 401
    assert response.json()["error_code"] == "authentication_required"


def test_alerts_uses_authenticated_user_without_accepting_user_id_from_client() -> None:
    """路由只把认证会话中的用户传给用例，响应不泄露跨用户来源信息。"""
    user_id = uuid4()
    use_case = _RecordingAlertsUseCase()
    app = create_app()
    app.state.daily_brief_alerts_use_case = use_case
    app.state.auth_clock = SimpleNamespace(now=lambda: datetime(2026, 8, 1, 0, 16, tzinfo=UTC))
    app.dependency_overrides[get_authenticated_session] = lambda: SimpleNamespace(
        user=SimpleNamespace(id=user_id)
    )
    client = TestClient(app)

    response = client.get(f"/api/v1/system/alerts?user_id={uuid4()}")

    assert response.status_code == 200
    assert response.json() == {
        "alerts": [
            {
                "code": "daily_brief_overdue",
                "severity": "critical",
                "local_date": "2026-08-01",
                "diagnostic_task_id": None,
            }
        ]
    }
    assert use_case.calls == [(user_id, datetime(2026, 8, 1, 0, 16, tzinfo=UTC))]


def test_alerts_route_declares_the_clock_protocol_dependency() -> None:
    """告警路由必须以 Clock 协议注入时间，禁止把时钟退化成无约束 object。"""
    router = build_system_router(dict)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == "/api/v1/system/alerts"
    )

    clock_annotation = get_args(get_type_hints(endpoint, include_extras=True)["clock"])[0]

    assert clock_annotation is Clock


def test_unknown_api_error_is_redacted_problem_with_trace_id() -> None:
    """未分类异常必须映射成泛化 500，不能将内部异常文本或堆栈返回给客户端。"""
    app = create_app()

    @app.get("/_test-unexpected-error")
    def unexpected_error() -> None:
        """仅用于验证统一异常边界，模拟不应向外泄露的内部失败。"""
        raise RuntimeError("synthetic internal detail")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/_test-unexpected-error")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error_code"] == "internal_error"
    assert response.json()["trace_id"]
    assert "synthetic internal detail" not in response.text


def test_unknown_api_error_logs_redacted_structured_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知异常日志保留 trace 关联和调用栈，但不得记录请求或异常中的敏感值。"""
    captured: list[str] = []

    class CapturingHandler(logging.Handler):
        """把结构化日志序列化结果留在内存，避免测试向标准错误写入。"""

        def emit(self, record: logging.LogRecord) -> None:
            """保存 formatter 输出，供断言检查公共日志边界。"""
            captured.append(self.format(record))

    handler = CapturingHandler()
    handler.setFormatter(_JsonFormatter(()))
    monkeypatch.setattr(deps._LOGGER, "propagate", False)
    deps._LOGGER.addHandler(handler)
    try:
        app = create_app()

        @app.post("/_test-unexpected-log")
        def unexpected_error() -> None:
            """模拟异常消息意外含有授权信息的内部失败。"""
            raise RuntimeError("Authorization: Bearer exception-secret")

        response = TestClient(app, raise_server_exceptions=False).post(
            "/_test-unexpected-log?query=not-loggable",
            content="request-body-secret",
            headers={"Authorization": "Bearer header-secret", "Cookie": "session=cookie-secret"},
        )
    finally:
        deps._LOGGER.removeHandler(handler)

    assert response.status_code == 500
    assert len(captured) == 1
    payload = json.loads(captured[0])
    assert payload["event"] == "ai_employee.api.deps"
    assert payload["error_code"] == "internal_error"
    assert payload["trace_id"] == response.json()["trace_id"]
    assert payload["stack"]
    assert "exception-secret" not in captured[0]
    assert "header-secret" not in captured[0]
    assert "cookie-secret" not in captured[0]
    assert "request-body-secret" not in captured[0]


def test_readiness_reports_dependency_probe_result() -> None:
    """依赖探测部分失败时，应以 503 和逐项结果阻止流量进入。"""
    app = create_app(readiness_probe=lambda: {"postgres": True, "redis": False})
    client = TestClient(app)
    response = client.get("/api/v1/system/readiness")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "dependencies": {"postgres": True, "redis": False},
    }


def test_system_responses_expose_stable_openapi_schemas() -> None:
    """健康与就绪端点应以命名且字段受约束的 Schema 暴露公共响应契约。"""
    openapi_schema = create_app().openapi()
    paths = openapi_schema["paths"]
    components = openapi_schema["components"]["schemas"]

    health_response = paths["/api/v1/system/health"]["get"]["responses"]["200"]
    assert health_response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HealthResponse"
    }
    health_schema = components["HealthResponse"]
    assert health_schema["properties"]["status"]["const"] == "ok"
    assert health_schema["properties"]["service"]["const"] == "api"
    assert set(health_schema["required"]) == {"status", "service"}

    readiness_responses = paths["/api/v1/system/readiness"]["get"]["responses"]
    readiness_response = readiness_responses["200"]
    assert readiness_response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadinessResponse"
    }
    assert readiness_responses["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadinessResponse"
    }
    readiness_schema = components["ReadinessResponse"]
    assert readiness_schema["properties"]["status"]["enum"] == ["ready", "not_ready"]
    assert readiness_schema["properties"]["dependencies"]["additionalProperties"] == {
        "type": "boolean"
    }
    assert set(readiness_schema["required"]) == {"status", "dependencies"}


def test_production_rejects_test_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境不得启用测试适配器，避免合成数据被误作业务事实。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_TEST_MODE", "true")
    with pytest.raises(ValueError, match="APP_TEST_MODE"):
        Settings()


def test_settings_reject_invalid_iana_timezone() -> None:
    """默认时区必须是时区数据库中存在的 IANA 名称。"""
    with pytest.raises(ZoneInfoNotFoundError):
        Settings(default_timezone="Invalid/Timezone")


def test_settings_reject_step_timeout_above_task_timeout() -> None:
    """单步超时超过任务总超时时应拒绝启动配置。"""
    with pytest.raises(ValueError, match="TASK_STEP_TIMEOUT_SECONDS"):
        Settings(task_timeout_seconds=60, task_step_timeout_seconds=61)


@pytest.mark.parametrize("session_ttl_seconds", [0, -1, 31_536_001])
def test_settings_rejects_session_ttl_outside_security_bounds(
    session_ttl_seconds: int,
) -> None:
    """启动配置必须拒绝立即失效、负数或超过一年的会话 TTL。"""
    with pytest.raises(ValueError, match="session_ttl_seconds"):
        Settings(session_ttl_seconds=session_ttl_seconds)


@pytest.mark.parametrize("session_ttl_seconds", [1, 31_536_000])
def test_settings_accepts_session_ttl_security_boundaries(
    session_ttl_seconds: int,
) -> None:
    """启动配置必须接受批准范围的首尾两个精确 TTL 值。"""
    assert Settings(session_ttl_seconds=session_ttl_seconds).session_ttl_seconds == (
        session_ttl_seconds
    )


def test_read_secret_file_reads_utf8_strips_whitespace_and_returns_secret(
    tmp_path: Path,
) -> None:
    """Secret 文件应按 UTF-8 读取、去除边界空白并返回遮蔽类型。"""
    secret_path = tmp_path / "secret"
    secret_path.write_text("  密钥内容\n", encoding="utf-8")

    secret = Settings().read_secret_file(secret_path)

    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == "密钥内容"


def test_read_secret_file_does_not_swallow_missing_file_error(tmp_path: Path) -> None:
    """Secret 文件缺失时应保留文件系统异常，避免以空凭据继续运行。"""
    missing_path = tmp_path / "missing-secret"

    with pytest.raises(FileNotFoundError):
        Settings().read_secret_file(missing_path)
