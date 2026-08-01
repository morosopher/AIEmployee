"""验证 API 进程系统端点与高风险运行模式配置。"""

from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ai_employee.config import Settings
from ai_employee.main import create_app


def test_health_returns_process_status() -> None:
    """健康端点应仅报告 API 进程存活，不执行外部依赖探测。"""
    client = TestClient(create_app())
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}


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
