"""暴露经 Cookie 会话与 CSRF 保护的连接及渐进能力 API。"""

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_employee.api.deps import ApiProblem, CsrfProtectedSession, CurrentSession, get_auth_settings
from ai_employee.application.ports.credential_rotation import RecoveryFailureCode
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
from ai_employee.config import Settings
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import PermanentProviderError
from ai_employee.integrations.microsoft.oauth import classify_microsoft_callback_error


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


_MICROSOFT_INITIAL_READ_CAPABILITIES = frozenset(
    {
        ConnectionCapability.MAIL_READ,
        ConnectionCapability.CALENDAR_READ,
    }
)


class MicrosoftStartConnectionRequest(BaseModel):
    """校验 Microsoft 首次授权可选择的读取数据源。

    ``None`` 兼容旧版无 body 调用并表示两项读取能力；一旦调用方显式提供列表，
    列表必须非空且只能包含 ``mail.read``/``calendar.read``。写能力只能通过连接已建立
    后的独立渐进授权入口取得，不能借此请求绕过依赖闭包。
    """

    model_config = ConfigDict(extra="forbid")

    capabilities: list[ConnectionCapability] | None = Field(default=None, min_length=1)

    @field_validator("capabilities")
    @classmethod
    def validate_read_capabilities(
        cls,
        value: list[ConnectionCapability] | None,
    ) -> list[ConnectionCapability] | None:
        """拒绝写能力和未知枚举，避免请求边界扩大 delegated scope。"""
        if value is None:
            return None
        selected = frozenset(value)
        if not selected or not selected.issubset(_MICROSOFT_INITIAL_READ_CAPABILITIES):
            raise ValueError("Microsoft initial capabilities must be a non-empty read subset")
        return value

    def capability_set(self) -> frozenset[ConnectionCapability]:
        """返回 provider-neutral use case 所需的不可变能力集合。"""
        if self.capabilities is None:
            return _MICROSOFT_INITIAL_READ_CAPABILITIES
        return frozenset(self.capabilities)


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


async def _complete_oauth_callback(
    *,
    provider: OAuthProvider,
    use_case: ConnectionsUseCase,
    state_value: str,
    code: str | None,
    error: str | None,
    error_description: str | None,
    error_codes: str | None,
) -> dict[str, str]:
    """两家 callback 共用互斥结果联合，shape 错误在消费 state 前失败。

    合法未知 error 也消费一次 state；raw 值只在本地 Microsoft 安全分类器短暂使用，
    应用端口/审计/Problem 只接收固定分类码。两家都不能通过错误分支到达凭据保存。

    Args:
        provider: 当前固定路由绑定的供应商。
        use_case: 组合根注入的连接用例，拥有消费/恢复/事务边界。
        state_value: 路由已检查非空和有界长度的一次性 state。
        code: 有界非空授权码，与 error 必须恰好提供一个。
        error: 有界非空供应商错误，只在本函数短暂分类。
        error_description: 有界原始说明，只提供给安全分类器，禁止透传。
        error_codes: 有界原始分类提示，只提供给安全分类器，禁止透传。

    Returns:
        成功 callback 的原连接 ID，不能携带 token 或供应商正文。

    Raises:
        ApiProblem: 输入联合不合法或 state 验证失败。
        DomainError: 已持久消费的安全失败分类或恢复结果异常。
    """
    if (code is None) == (error is None) or (error is not None and not error.strip()):
        raise ApiProblem(
            422,
            "request_validation_failed",
            "Request validation failed",
            "The request did not match the required schema.",
        )
    try:
        if error is not None:
            classified = (
                classify_microsoft_callback_error(
                    error=error,
                    error_description=error_description,
                    error_codes=error_codes,
                )
                if provider is OAuthProvider.MICROSOFT
                else None
            )
            if classified is None:
                classified = PermanentProviderError(
                    error_code="oauth_authorization_failed",
                    message="OAuth authorization failed",
                )
            await use_case.callback_error(
                provider=provider,
                state=state_value,
                error_code=cast(RecoveryFailureCode, classified.error_code),
            )
            raise classified
        if code is None:
            raise AssertionError("validated OAuth callback lacks a code")
        connection_id = await use_case.callback(code=code, state=state_value, provider=provider)
    except OAuthStateRejectedError:
        raise ApiProblem(
            400,
            "oauth_state_rejected",
            "OAuth state rejected",
            "The OAuth state is invalid or expired.",
        ) from None
    return {"connection_id": str(connection_id)}


def _oauth_success_response(
    request: Request, result: dict[str, str], settings: Settings
) -> Response:
    """把已完成的 OAuth 结果映射为浏览器返回或兼容 JSON，不改变授权事实。

    Args:
        request: 仅使用 Fetch Metadata 区分浏览器导航，不信任 Host 或返回地址参数。
        result: 用例已完成一次性消费和持久保存后返回的连接标识。
        settings: 管理员配置的应用地址；浏览器只能返回其固定连接页面。

    Returns:
        禁止缓存且不传递 Referer 的 303 或 200 响应。失败不会到达此函数。
    """
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if request.headers.get("sec-fetch-mode") == "navigate":
        # 清除整段 OAuth query，防止 code/state 经跳转地址或 Referer 进入前端。
        return RedirectResponse(
            f"{settings.app_base_url.rstrip('/')}/connections", status_code=303, headers=headers
        )
    return JSONResponse(result, headers=headers)


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

    @router.get(
        "/google/callback",
        response_model=dict[str, str],
        responses={303: {"description": "Return browser navigation to the connections page"}},
    )
    async def complete_google_connection(
        request: Request,
        state_value: Annotated[str, Query(alias="state", min_length=1, max_length=512)],
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
        settings: Annotated[Settings, Depends(get_auth_settings)],
        code: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
        error: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
        error_description: Annotated[str | None, Query(max_length=4096)] = None,
        error_codes: Annotated[str | None, Query(max_length=256)] = None,
    ) -> Response:
        """Google 先完成一次性授权，再按导航或 API 请求返回；错误保留脱敏 Problem。"""
        result = await _complete_oauth_callback(
            provider=OAuthProvider.GOOGLE,
            use_case=use_case,
            state_value=state_value,
            code=code,
            error=error,
            error_description=error_description,
            error_codes=error_codes,
        )
        return _oauth_success_response(request, result, settings)

    @router.post("/microsoft/start", response_model=StartConnectionResponse)
    async def start_microsoft_connection(
        authenticated: CsrfProtectedSession,
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
        request_body: Annotated[MicrosoftStartConnectionRequest | None, Body()] = None,
        query_capabilities: Annotated[
            list[ConnectionCapability] | None,
            Query(alias="capabilities", min_length=1),
        ] = None,
    ) -> StartConnectionResponse:
        """发起 Microsoft common v2 首次授权，仅请求非空读取能力子集。

        旧版无 body 调用仍默认两项读取能力；body 与 query 同时提供时拒绝歧义输入。
        写能力与未知值在 API Schema/枚举边界失败，不会传给 provider-neutral use case。
        """
        if request_body is not None and query_capabilities is not None:
            raise ApiProblem(
                422,
                "request_validation_failed",
                "Request validation failed",
                "The request did not match the required schema.",
            )
        if query_capabilities is not None:
            selected = frozenset(query_capabilities)
            if not selected or not selected.issubset(_MICROSOFT_INITIAL_READ_CAPABILITIES):
                raise ApiProblem(
                    422,
                    "request_validation_failed",
                    "Request validation failed",
                    "The request did not match the required schema.",
                )
            capabilities = selected
        else:
            capabilities = (
                request_body.capability_set()
                if request_body is not None
                else MicrosoftStartConnectionRequest().capability_set()
            )
        result = await use_case.start(
            user_id=authenticated.user.id,
            provider=OAuthProvider.MICROSOFT,
            capabilities=capabilities,
        )
        return StartConnectionResponse(authorization_url=result.authorization_url)

    @router.get(
        "/microsoft/callback",
        response_model=dict[str, str],
        responses={303: {"description": "Return browser navigation to the connections page"}},
    )
    async def complete_microsoft_connection(
        request: Request,
        state_value: Annotated[str, Query(alias="state", min_length=1, max_length=512)],
        use_case: Annotated[ConnectionsUseCase, Depends(get_connections_use_case)],
        settings: Annotated[Settings, Depends(get_auth_settings)],
        code: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
        error: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
        error_description: Annotated[str | None, Query(max_length=4096)] = None,
        error_codes: Annotated[str | None, Query(max_length=256)] = None,
    ) -> Response:
        """消费 Microsoft callback state，并将管理员同意错误映射为稳定 Problem。

        ``error_description`` 仅作为分类输入，绝不进入异常消息、审计或响应；未知错误
        统一收敛为不含供应商正文的永久失败。只有成功结果才按导航/API 契约返回。
        """
        result = await _complete_oauth_callback(
            provider=OAuthProvider.MICROSOFT,
            use_case=use_case,
            state_value=state_value,
            code=code,
            error=error,
            error_description=error_description,
            error_codes=error_codes,
        )
        return _oauth_success_response(request, result, settings)

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
