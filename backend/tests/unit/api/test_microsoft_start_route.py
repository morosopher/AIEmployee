"""使用最小 FastAPI 应用验证 Microsoft start 路由的 body/query 契约。"""

from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from ai_employee.api.deps import (
    ApiProblem,
    get_connections_use_case,
    handle_api_problem,
    handle_request_validation_error,
    require_csrf_authenticated_session,
)
from ai_employee.api.routers.connections import build_connections_router
from ai_employee.application.use_cases.connections import OAuthStartResult
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.identity import (
    AuthenticatedSession,
    SessionRecord,
    UserIdentity,
)


class _RecordingConnectionsUseCase:
    """记录路由传入的 provider-neutral 能力集合，不接触数据库或网络。"""

    def __init__(self) -> None:
        self.calls: list[frozenset[ConnectionCapability]] = []

    async def start(self, **kwargs: object) -> OAuthStartResult:
        capabilities = kwargs["capabilities"]
        assert type(capabilities) is frozenset
        self.calls.append(capabilities)
        return OAuthStartResult("https://provider.example.test/authorize")


def _test_session() -> AuthenticatedSession:
    """创建固定合成管理员会话，绕过认证实现但保留路由参数形状。"""
    user_id = UUID("00000000-0000-0000-0000-000000000101")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    return AuthenticatedSession(
        user=UserIdentity(user_id, "owner@example.test", "Owner", "UTC", "en-US", time(8, 0)),
        session=SessionRecord(
            id=uuid4(),
            user_id=user_id,
            csrf_hash=b"synthetic-csrf-hash",
            created_at=now,
            expires_at=now + timedelta(days=1),
            last_seen_at=now,
            revoked_at=None,
        ),
    )


@pytest.mark.asyncio
async def test_microsoft_start_route_parses_body_query_and_rejects_writes() -> None:
    """真实 FastAPI 参数解析必须保留单项选择，并对写能力返回脱敏 422。"""
    app = FastAPI()
    app.include_router(build_connections_router())
    app.add_exception_handler(ApiProblem, handle_api_problem)
    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    use_case = _RecordingConnectionsUseCase()
    app.dependency_overrides[require_csrf_authenticated_session] = _test_session
    app.dependency_overrides[get_connections_use_case] = lambda: use_case

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        default_response = await client.post("/api/v1/connections/microsoft/start")
        mail_response = await client.post(
            "/api/v1/connections/microsoft/start",
            json={"capabilities": ["mail.read"]},
        )
        calendar_response = await client.post(
            "/api/v1/connections/microsoft/start?capabilities=calendar.read"
        )
        write_response = await client.post(
            "/api/v1/connections/microsoft/start",
            json={"capabilities": ["mail.send"]},
        )
        duplicate_source_response = await client.post(
            "/api/v1/connections/microsoft/start?capabilities=calendar.read",
            json={"capabilities": ["mail.read"]},
        )

    assert default_response.status_code == 200
    assert mail_response.status_code == 200
    assert calendar_response.status_code == 200
    assert write_response.status_code == 422
    assert write_response.json()["error_code"] == "request_validation_failed"
    assert duplicate_source_response.status_code == 422
    assert duplicate_source_response.json()["error_code"] == "request_validation_failed"
    assert use_case.calls == [
        frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.CALENDAR_READ}),
        frozenset({ConnectionCapability.MAIL_READ}),
        frozenset({ConnectionCapability.CALENDAR_READ}),
    ]
