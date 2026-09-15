"""验证 OAuth 成功回调的浏览器返回路径与原有 JSON/失败契约，不访问供应商。"""

from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from ai_employee.api.deps import (
    ApiProblem,
    get_auth_settings,
    get_connections_use_case,
    handle_api_problem,
    handle_request_validation_error,
)
from ai_employee.api.routers.connections import build_connections_router
from ai_employee.application.ports.oauth import OAuthProvider
from ai_employee.application.use_cases.connections import OAuthStateRejectedError
from ai_employee.config import Settings

CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000201")


class _CallbackUseCase:
    """替代数据库和供应商边界；路由映射、输入校验与 Problem 处理仍执行真实代码。"""

    async def callback(
        self, *, code: str, state: str, provider: str | OAuthProvider | None = None
    ) -> UUID:
        """只接受合成输入，显式过期分支用于证明失败不会被成功跳转掩盖。"""
        assert code == "synthetic-code"
        assert provider in (OAuthProvider.GOOGLE, OAuthProvider.MICROSOFT)
        if state == "synthetic-expired-state":
            raise OAuthStateRejectedError
        assert state == "synthetic-state"
        return CONNECTION_ID


def _callback_app() -> FastAPI:
    """创建无真实配置、无网络与无会话凭据的最小 HTTP 应用。"""
    app = FastAPI()
    app.include_router(build_connections_router())
    app.add_exception_handler(ApiProblem, handle_api_problem)
    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    settings = Settings(_env_file=None, app_base_url="http://localhost:5173/")
    app.dependency_overrides[get_auth_settings] = lambda: settings
    app.dependency_overrides[get_connections_use_case] = _CallbackUseCase
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "microsoft"])
async def test_browser_callback_returns_to_trusted_app_without_oauth_query(provider: str) -> None:
    """浏览器只返回配置的连接页；不信任 Host、Referer 或客户端指定返回地址。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_callback_app()), base_url="http://callback.example.test"
    ) as client:
        response = await client.get(
            f"/api/v1/connections/{provider}/callback",
            headers={
                "Sec-Fetch-Mode": "navigate",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Host": "untrusted.example.test",
                "Referer": "https://untrusted.example.test/",
            },
            params={
                "code": "synthetic-code",
                "state": "synthetic-state",
                "next": "https://untrusted.example.test/",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"] == "http://localhost:5173/connections"
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("referrer-policy") == "no-referrer"
    assert response.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "microsoft"])
async def test_programmatic_callback_preserves_json_without_caching(provider: str) -> None:
    """现有非导航客户端仍收到连接标识，不能因浏览器修复改为跟随跳转。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_callback_app()), base_url="http://callback.example.test"
    ) as client:
        response = await client.get(
            f"/api/v1/connections/{provider}/callback",
            params={"code": "synthetic-code", "state": "synthetic-state"},
        )

    assert response.status_code == 200
    assert response.json() == {"connection_id": str(CONNECTION_ID)}
    assert "location" not in response.headers
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("referrer-policy") == "no-referrer"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "microsoft"])
async def test_failed_browser_callback_preserves_sanitized_problem(provider: str) -> None:
    """状态过期必须保留明确失败，响应不得包含 code/state 或成功返回地址。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_callback_app()), base_url="http://callback.example.test"
    ) as client:
        response = await client.get(
            f"/api/v1/connections/{provider}/callback",
            headers={"Sec-Fetch-Mode": "navigate"},
            params={"code": "synthetic-code", "state": "synthetic-expired-state"},
        )

    assert response.status_code == 400
    assert response.json()["error_code"] == "oauth_state_rejected"
    assert "location" not in response.headers
    assert "synthetic-code" not in response.text
    assert "synthetic-expired-state" not in response.text
