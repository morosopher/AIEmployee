"""验证真实就绪探针在数据库连接失败时仍能独立报告 Redis 状态。

只替换驱动的网络边界，保留应用装配、SQLAlchemy 会话和正式 HTTP 响应映射；
合成异常不得通过响应泄漏，程序错误也不能被误判为普通依赖不可用。
"""

from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy.exc import SQLAlchemyError

from ai_employee import main
from ai_employee.config import Settings


@pytest.mark.parametrize(
    "failure",
    [
        OSError("Synthetic database connection refused"),
        TimeoutError("Synthetic database connection timeout"),
        SQLAlchemyError("Synthetic database driver failure"),
    ],
    ids=["connection-refused", "connection-timeout", "sqlalchemy-error"],
)
@pytest.mark.parametrize("redis_available", [True, False])
def test_database_connection_failure_reports_each_dependency(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    redis_available: bool,
) -> None:
    """数据库驱动失败应返回 503，继续探测 Redis，并且不公开异常内容。"""
    settings = Settings(_env_file=None, metrics_enabled=False)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(side_effect=failure))
    monkeypatch.setattr(Redis, "ping", AsyncMock(return_value=redis_available))

    with TestClient(main.create_app(), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/system/readiness")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "dependencies": {"postgres": False, "redis": redis_available},
    }
    assert "Synthetic database" not in response.text


def test_unexpected_probe_error_remains_an_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅降级已知连接错误，防止宽泛捕获隐藏程序错误或伪造正常探针结果。"""
    settings = Settings(_env_file=None, metrics_enabled=False)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        asyncpg, "connect", AsyncMock(side_effect=ValueError("Synthetic programming error"))
    )
    monkeypatch.setattr(Redis, "ping", AsyncMock(return_value=True))

    with TestClient(main.create_app(), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/system/readiness")

    assert response.status_code == 500
    assert response.json()["error_code"] == "internal_error"
    assert "Synthetic programming error" not in response.text
