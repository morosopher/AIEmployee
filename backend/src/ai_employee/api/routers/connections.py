"""暴露经认证与 CSRF 保护的 Google OAuth 连接管理接口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel

from ai_employee.api.deps import ApiProblem, CsrfProtectedSession, CurrentSession
from ai_employee.application.use_cases.connections import (
    ConnectionNotFoundError,
    ConnectionSummary,
    GoogleConnectionsUseCase,
    OAuthStateRejectedError,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase


class ConnectionResponse(BaseModel):
    """返回连接状态的公开 schema，绝不序列化 credential 字段。"""

    id: UUID
    provider: str
    account_email: str
    scopes: list[str]
    status: str
    last_error_code: str | None


class StartConnectionResponse(BaseModel):
    """返回前端可安全跳转的 Google 授权地址。"""

    authorization_url: str


class SyncResponse(BaseModel):
    """手动同步接受后的两个异步任务标识。"""

    gmail_task_id: UUID
    calendar_task_id: UUID


def _connection_response(value: ConnectionSummary) -> ConnectionResponse:
    """显式映射应用层摘要，防止 ORM 新增字段意外暴露给前端。"""
    return ConnectionResponse(
        id=value.id,
        provider=value.provider,
        account_email=value.account_email,
        scopes=list(value.scopes),
        status=value.status,
        last_error_code=value.last_error_code,
    )


def build_connections_router() -> APIRouter:
    """构建连接路由；运行时依赖由组合根提供以便集成测试替换。"""
    from ai_employee.api.deps import get_connections_use_case, get_create_task_use_case

    router = APIRouter(prefix="/api/v1/connections", tags=["connections"])

    @router.get("", response_model=list[ConnectionResponse])
    async def list_connections(
        authenticated: CurrentSession,
        use_case: Annotated[GoogleConnectionsUseCase, Depends(get_connections_use_case)],
    ) -> list[ConnectionResponse]:
        """仅列出当前认证用户拥有的连接及脱敏状态。"""
        return [
            _connection_response(item)
            for item in await use_case.list(user_id=authenticated.user.id)
        ]

    @router.post("/google/start", response_model=StartConnectionResponse)
    async def start_google_connection(
        authenticated: CsrfProtectedSession,
        use_case: Annotated[GoogleConnectionsUseCase, Depends(get_connections_use_case)],
    ) -> StartConnectionResponse:
        """在 CSRF 校验后创建短期 state，并返回 Google 授权 URL。"""
        result = await use_case.start(user_id=authenticated.user.id)
        return StartConnectionResponse(authorization_url=result.authorization_url)

    @router.get("/google/callback")
    async def complete_google_connection(
        code: Annotated[str, Query(min_length=1, max_length=4096)],
        state_value: Annotated[str, Query(alias="state", min_length=1, max_length=512)],
        use_case: Annotated[GoogleConnectionsUseCase, Depends(get_connections_use_case)],
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

    @router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def disconnect_connection(
        connection_id: UUID,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[GoogleConnectionsUseCase, Depends(get_connections_use_case)],
    ) -> None:
        """验证拥有权后删除本地 token 密文并尽力撤销远端授权。"""
        try:
            await use_case.disconnect(user_id=authenticated.user.id, connection_id=connection_id)
        except ConnectionNotFoundError:
            raise ApiProblem(
                404,
                "connection_not_found",
                "Connection not found",
                "The requested connection was not found.",
            ) from None

    @router.post(
        "/{connection_id}/sync", status_code=status.HTTP_202_ACCEPTED, response_model=SyncResponse
    )
    async def sync_connection(
        connection_id: UUID,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[GoogleConnectionsUseCase, Depends(get_connections_use_case)],
        tasks: Annotated[CreateTaskUseCase, Depends(get_create_task_use_case)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ] = None,
    ) -> SyncResponse:
        """为拥有的已连接帐号幂等接受 Gmail、Calendar 的两项同步任务。"""
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
            raise ApiProblem(
                404,
                "connection_not_found",
                "Connection not found",
                "The requested connection was not found.",
            ) from None
        return SyncResponse(
            gmail_task_id=result.gmail_task_id, calendar_task_id=result.calendar_task_id
        )

    return router
