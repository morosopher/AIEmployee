"""暴露经 Cookie 会话与 CSRF 保护的连接及渐进能力 API。"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel

from ai_employee.api.deps import ApiProblem, CsrfProtectedSession, CurrentSession
from ai_employee.application.ports.oauth import OAuthProvider
from ai_employee.application.use_cases.connections import (
    CapabilityEnableResult,
    ConnectionCapabilitySnapshot,
    ConnectionNotFoundError,
    ConnectionSummary,
    ConnectionsUseCase,
    OAuthStateRejectedError,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability


class ConnectionResponse(BaseModel):
    """返回连接状态的公开 schema，绝不序列化 credential 字段。"""

    id: UUID
    provider: str
    account_email: str
    scopes: list[str]
    status: str
    last_error_code: str | None


class StartConnectionResponse(BaseModel):
    """返回前端可安全跳转的授权地址。"""

    authorization_url: str


class CapabilityEnableResponse(BaseModel):
    """返回渐进授权地址与依赖闭包后的完整能力并集。"""

    authorization_url: str
    requested_capabilities: list[ConnectionCapability]


class CapabilityDisableResponse(BaseModel):
    """返回本地关闭的精确能力及稳定状态。"""

    capability: ConnectionCapability
    status: CapabilityStatus


class ConnectionCapabilityResponse(BaseModel):
    """公开单项能力状态与供应商实际 scope，不包含 token 或响应原文。"""

    capability: ConnectionCapability
    status: CapabilityStatus
    actual_scopes: list[str]
    last_verified_at: datetime | None
    last_error_code: str | None


class ProviderCalendarResponse(BaseModel):
    """公开日历选择器所需的规范字段，刻意排除 cursor 与 raw JSON。"""

    id: str
    name: str
    timezone: str
    is_primary: bool
    access_role: str
    can_write: bool
    provider_url: str | None


class ConnectionCapabilitiesResponse(BaseModel):
    """返回一个连接的四项能力与已同步日历目录。"""

    connection_id: UUID
    provider: str
    capabilities: list[ConnectionCapabilityResponse]
    provider_calendars: list[ProviderCalendarResponse]


class SyncResponse(BaseModel):
    """手动同步接受后的两个异步任务标识。"""

    gmail_task_id: UUID
    calendar_task_id: UUID


def _connection_response(value: ConnectionSummary) -> ConnectionResponse:
    """显式映射应用摘要，防止持久模型新增字段意外暴露给前端。"""
    return ConnectionResponse(
        id=value.id,
        provider=value.provider,
        account_email=value.account_email,
        scopes=list(value.scopes),
        status=value.status,
        last_error_code=value.last_error_code,
    )


def _capabilities_response(
    value: ConnectionCapabilitySnapshot,
) -> ConnectionCapabilitiesResponse:
    """显式白名单映射能力与日历字段，禁止 ORM/cursor/raw JSON 自动序列化。"""
    return ConnectionCapabilitiesResponse(
        connection_id=value.connection_id,
        provider=value.provider,
        capabilities=[
            ConnectionCapabilityResponse(
                capability=item.capability,
                status=item.status,
                actual_scopes=list(item.actual_scopes),
                last_verified_at=item.last_verified_at,
                last_error_code=item.last_error_code,
            )
            for item in value.capabilities
        ],
        provider_calendars=[
            ProviderCalendarResponse(
                id=item.id,
                name=item.name,
                timezone=item.timezone,
                is_primary=item.is_primary,
                access_role=item.access_role,
                can_write=item.can_write,
                provider_url=item.provider_url,
            )
            for item in value.provider_calendars
        ],
    )


def _enable_response(value: CapabilityEnableResult) -> CapabilityEnableResponse:
    """把不可变能力结果映射为 JSON 列表并保持确定性顺序。"""
    return CapabilityEnableResponse(
        authorization_url=value.authorization_url,
        requested_capabilities=list(value.requested_capabilities),
    )


def _not_found_problem() -> ApiProblem:
    """创建不区分缺失与跨用户资源的稳定 404 Problem。"""
    return ApiProblem(
        404,
        "connection_not_found",
        "Connection not found",
        "The requested connection was not found.",
    )


def build_connections_router() -> APIRouter:
    """构建连接路由；固定 ``/google/*`` 必须先于动态连接路径注册。"""
    from ai_employee.api.deps import get_connections_use_case, get_create_task_use_case

    router = APIRouter(prefix="/api/v1/connections", tags=["connections"])

    @router.get("", response_model=list[ConnectionResponse])
    async def list_connections(
        authenticated: CurrentSession,
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
    ) -> list[ConnectionResponse]:
        """仅列出当前认证用户拥有的连接及脱敏状态。"""
        return [
            _connection_response(item)
            for item in await use_case.list(user_id=authenticated.user.id)
        ]

    @router.post("/google/start", response_model=StartConnectionResponse)
    async def start_google_connection(
        authenticated: CsrfProtectedSession,
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
    ) -> StartConnectionResponse:
        """保持 M1 固定入口，显式选择 Google 的两项只读数据源。"""
        result = await use_case.start(
            user_id=authenticated.user.id,
            provider=OAuthProvider.GOOGLE,
            capabilities=frozenset(
                {
                    ConnectionCapability.MAIL_READ,
                    ConnectionCapability.CALENDAR_READ,
                }
            ),
        )
        return StartConnectionResponse(authorization_url=result.authorization_url)

    @router.get("/google/callback")
    async def complete_google_connection(
        code: Annotated[str, Query(min_length=1, max_length=4096)],
        state_value: Annotated[str, Query(alias="state", min_length=1, max_length=512)],
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
    ) -> dict[str, str]:
        """消费一次性 state 并完成 callback；state 本身提供 OAuth CSRF 绑定。"""
        try:
            connection_id = await use_case.callback(code=code, state=state_value)
        except OAuthStateRejectedError:
            raise ApiProblem(
                400,
                "oauth_state_rejected",
                "OAuth state rejected",
                "The OAuth state is invalid or expired.",
            ) from None
        return {"connection_id": str(connection_id)}

    @router.get(
        "/{connection_id}/capabilities",
        response_model=ConnectionCapabilitiesResponse,
    )
    async def get_connection_capabilities(
        connection_id: UUID,
        response: Response,
        authenticated: CurrentSession,
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
    ) -> ConnectionCapabilitiesResponse:
        """返回当前用户的能力与日历目录，并禁止缓存账户/日历名称。"""
        try:
            snapshot = await use_case.get_capabilities(
                user_id=authenticated.user.id,
                connection_id=connection_id,
            )
        except ConnectionNotFoundError:
            raise _not_found_problem() from None
        response.headers["Cache-Control"] = "no-store"
        return _capabilities_response(snapshot)

    @router.post(
        "/{connection_id}/capabilities/{capability}/enable",
        response_model=CapabilityEnableResponse,
    )
    async def enable_connection_capability(
        connection_id: UUID,
        capability: ConnectionCapability,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
    ) -> CapabilityEnableResponse:
        """在 CSRF 与用户归属校验后发起单项能力渐进授权。"""
        try:
            result = await use_case.start_capability_enable(
                user_id=authenticated.user.id,
                connection_id=connection_id,
                capability=capability,
            )
        except ConnectionNotFoundError:
            raise _not_found_problem() from None
        return _enable_response(result)

    @router.post(
        "/{connection_id}/capabilities/{capability}/disable",
        response_model=CapabilityDisableResponse,
    )
    async def disable_connection_capability(
        connection_id: UUID,
        capability: ConnectionCapability,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
    ) -> CapabilityDisableResponse:
        """验证依赖后本地关闭能力；未认领动作取消由 Task 25 实现。"""
        try:
            result = await use_case.disable_capability(
                user_id=authenticated.user.id,
                connection_id=connection_id,
                capability=capability,
            )
        except ConnectionNotFoundError:
            raise _not_found_problem() from None
        return CapabilityDisableResponse(
            capability=result.capability,
            status=result.status,
        )

    @router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def disconnect_connection(
        connection_id: UUID,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
    ) -> None:
        """验证拥有权后删除本地 token 密文并调用固定供应商撤销边界。"""
        try:
            await use_case.disconnect(
                user_id=authenticated.user.id,
                connection_id=connection_id,
            )
        except ConnectionNotFoundError:
            raise _not_found_problem() from None

    @router.post(
        "/{connection_id}/sync",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=SyncResponse,
    )
    async def sync_connection(
        connection_id: UUID,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
        tasks: Annotated[CreateTaskUseCase, Depends(get_create_task_use_case)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ] = None,
    ) -> SyncResponse:
        """为拥有的已连接帐号幂等接受邮件、Calendar 两项同步任务。"""
        if idempotency_key is None:
            raise ApiProblem(
                422,
                "idempotency_key_required",
                "Idempotency key required",
                "An Idempotency-Key header is required.",
            )
        try:
            result = await use_case.start_manual_sync(
                user_id=authenticated.user.id,
                connection_id=connection_id,
                idempotency_key=idempotency_key,
                tasks=tasks,
            )
        except ConnectionNotFoundError:
            raise _not_found_problem() from None
        return SyncResponse(
            gmail_task_id=result.gmail_task_id,
            calendar_task_id=result.calendar_task_id,
        )

    return router
