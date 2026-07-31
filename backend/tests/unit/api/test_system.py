"""验证 API 进程系统端点与高风险运行模式配置。"""

import pytest
from fastapi.testclient import TestClient

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


def test_production_rejects_test_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境不得启用测试适配器，避免合成数据被误作业务事实。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_TEST_MODE", "true")
    with pytest.raises(ValueError, match="APP_TEST_MODE"):
        Settings()
