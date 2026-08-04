"""组合认证应用端口、请求依赖与 RFC 9457 Problem Details 映射。"""

from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import uuid4

from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ai_employee.application.use_cases.auth import (
    AuthenticateSessionUseCase,
    AuthenticationRequiredError,
    Clock,
    CsrfRejectedError,
    IdentityRepositoryFactory,
    ListSessionsUseCase,
    LoginUseCase,
    LogoutUseCase,
    PasswordVerifier,
    RevokeSessionUseCase,
    TokenFactory,
    TokenHasher,
    ValidateCsrfUseCase,
)
from ai_employee.config import Settings
from ai_employee.domain.identity import AuthenticatedSession

CSRF_COOKIE_NAME = "ai_employee_csrf"


class ProblemDetails(BaseModel):
    """定义带稳定业务错误码和 Trace ID 的 RFC 9457 错误响应。"""

    type: str
    title: str
    status: int
    detail: str
    instance: str
    error_code: str
    trace_id: str


class ApiProblem(Exception):
    """把已分类 API 失败传递给统一 Problem Details 处理器。"""

    def __init__(self, status_code: int, error_code: str, title: str, detail: str) -> None:
        """保存不含凭据、Cookie 或内部堆栈的公共错误字段。"""
        super().__init__(title)
        self.status_code = status_code
        self.error_code = error_code
        self.title = title
        self.detail = detail


def _problem_response(request: Request, problem_error: ApiProblem) -> JSONResponse:
    """生成统一 Problem Details 响应并为当前请求分配稳定 Trace ID。"""
    existing_trace_id = getattr(request.state, "trace_id", None)
    trace_id = existing_trace_id if isinstance(existing_trace_id, str) else uuid4().hex
    request.state.trace_id = trace_id
    problem = ProblemDetails(
        type=(f"https://ai-employee.local/problems/{problem_error.error_code.replace('_', '-')}"),
        title=problem_error.title,
        status=problem_error.status_code,
        detail=problem_error.detail,
        instance=request.url.path,
        error_code=problem_error.error_code,
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=problem_error.status_code,
        content=problem.model_dump(mode="json"),
        media_type="application/problem+json",
    )


async def handle_api_problem(request: Request, exception: Exception) -> JSONResponse:
    """把类型化业务 API 错误渲染为 ``application/problem+json``。

    Args:
        request: 发生错误的 FastAPI 请求，仅使用不含 query 的路径作为 instance。
        exception: FastAPI 传入的异常；注册边界保证实际类型为 ``ApiProblem``。

    Returns:
        不泄露敏感输入的 RFC 9457 JSON 响应。

    Raises:
        Exception: 若处理器被错误注册到其他异常类型，保留原异常而非误分类。
    """
    if not isinstance(exception, ApiProblem):
        raise exception
    return _problem_response(request, exception)


async def handle_request_validation_error(request: Request, exception: Exception) -> JSONResponse:
    """把请求 Schema 错误映射为脱敏 Problem，避免回显密码等原始输入。

    Args:
        request: 发生请求校验错误的 FastAPI 请求。
        exception: FastAPI 传入的异常；注册边界保证为 ``RequestValidationError``。

    Returns:
        不包含 Pydantic ``input`` 字段的稳定 422 Problem Details。

    Raises:
        Exception: 处理器误注册到其他异常类型时保留原异常。
    """
    if not isinstance(exception, RequestValidationError):
        raise exception
    return _problem_response(
        request,
        ApiProblem(
            422,
            "request_validation_failed",
            "Request validation failed",
            "The request did not match the required schema.",
        ),
    )


class SystemClock:
    """生产进程使用的显式 UTC 时钟适配器。"""

    def now(self) -> datetime:
        """返回带 UTC 时区信息的当前时间。"""
        return datetime.now(UTC)


def get_auth_settings(request: Request) -> Settings:
    """从应用组合根读取已验证配置并收窄 Starlette state 类型。"""
    return cast(Settings, request.app.state.auth_settings)


def get_identity_repositories(request: Request) -> IdentityRepositoryFactory:
    """读取认证用例共享的窄 Repository transaction factory。"""
    return cast(IdentityRepositoryFactory, request.app.state.auth_repository_factory)


def get_auth_clock(request: Request) -> Clock:
    """读取可在测试中替换的 UTC 时钟。"""
    return cast(Clock, request.app.state.auth_clock)


def get_auth_token_factory(request: Request) -> TokenFactory:
    """读取仅在登录时生成原始 Cookie 令牌的工厂。"""
    return cast(TokenFactory, request.app.state.auth_token_factory)


def get_auth_token_hasher(request: Request) -> TokenHasher:
    """读取会话与 CSRF 共用的 SHA-256 摘要适配器。"""
    return cast(TokenHasher, request.app.state.auth_token_hasher)


def get_password_verifier(request: Request) -> PasswordVerifier:
    """读取 Argon2id 密码验证适配器。"""
    return cast(PasswordVerifier, request.app.state.auth_password_verifier)


def get_create_task_use_case(request: Request):
    """从组合根取得创建任务用例，路由不接触 ORM 或队列。"""
    return request.app.state.create_task_use_case


def get_connections_use_case(request: Request):
    """按当前受控 Secret 和数据库工厂装配 Google 连接用例。

    加密主密钥与 Google client secret 只在请求装配时从挂载文件读取，不存入应用 state，
    因而异常页面、调试工具和测试替身都无法通过 state 意外取得敏感原文。
    """
    from ai_employee.application.use_cases.connections import GoogleConnectionsUseCase
    from ai_employee.infrastructure.security.encryption import AeadCipher
    from ai_employee.integrations.google.oauth import GoogleOAuthClient

    settings = get_auth_settings(request)
    cipher = AeadCipher.from_file(settings.app_master_key_file)
    secret = settings.read_secret_file(settings.google_client_secret_file).get_secret_value()
    return GoogleConnectionsUseCase(
        request.app.state.connections_store_factory,
        cipher,
        GoogleOAuthClient(settings.google_client_id, secret, settings.google_redirect_uri),
        get_auth_clock(request),
        settings.google_client_id,
        settings.google_redirect_uri,
    )


def get_get_task_use_case(request: Request):
    """从组合根取得用户范围任务读取用例。"""
    return request.app.state.get_task_use_case


def get_cancel_task_use_case(request: Request):
    """从组合根取得任务取消用例。"""
    return request.app.state.cancel_task_use_case


def get_retry_task_use_case(request: Request):
    """从组合根取得任务重试用例。"""
    return request.app.state.retry_task_use_case


def get_approval_decision_use_case(request: Request):
    """从组合根取得冻结审批决定用例。"""
    return request.app.state.approval_decision_use_case


def get_login_use_case(
    repositories: Annotated[IdentityRepositoryFactory, Depends(get_identity_repositories)],
    password_verifier: Annotated[PasswordVerifier, Depends(get_password_verifier)],
    token_factory: Annotated[TokenFactory, Depends(get_auth_token_factory)],
    token_hasher: Annotated[TokenHasher, Depends(get_auth_token_hasher)],
    clock: Annotated[Clock, Depends(get_auth_clock)],
    settings: Annotated[Settings, Depends(get_auth_settings)],
) -> LoginUseCase:
    """按当前应用配置组合一次登录用例。"""
    return LoginUseCase(
        repositories,
        password_verifier,
        token_factory,
        token_hasher,
        clock,
        settings.session_ttl_seconds,
    )


async def get_authenticated_session(
    request: Request,
    repositories: Annotated[IdentityRepositoryFactory, Depends(get_identity_repositories)],
    token_hasher: Annotated[TokenHasher, Depends(get_auth_token_hasher)],
    clock: Annotated[Clock, Depends(get_auth_clock)],
    settings: Annotated[Settings, Depends(get_auth_settings)],
) -> AuthenticatedSession:
    """认证请求 Cookie，并把无效会话统一映射为 401 Problem Details。"""
    raw_session_token = request.cookies.get(settings.session_cookie_name)
    try:
        return await AuthenticateSessionUseCase(repositories, token_hasher, clock).execute(
            raw_session_token
        )
    except AuthenticationRequiredError:
        raise ApiProblem(
            401,
            "authentication_required",
            "Authentication required",
            "An active session is required for this request.",
        ) from None


async def require_csrf_authenticated_session(
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    token_hasher: Annotated[TokenHasher, Depends(get_auth_token_hasher)],
) -> AuthenticatedSession:
    """为修改类请求追加双提交 Cookie 与数据库摘要 CSRF 校验。"""
    try:
        ValidateCsrfUseCase(token_hasher).execute(
            authenticated,
            cookie_token=request.cookies.get(CSRF_COOKIE_NAME),
            header_token=request.headers.get("X-CSRF-Token"),
        )
    except CsrfRejectedError:
        raise ApiProblem(
            403,
            "csrf_rejected",
            "CSRF validation failed",
            "The CSRF cookie and request token did not match the active session.",
        ) from None
    return authenticated


def get_logout_use_case(
    repositories: Annotated[IdentityRepositoryFactory, Depends(get_identity_repositories)],
    clock: Annotated[Clock, Depends(get_auth_clock)],
) -> LogoutUseCase:
    """组合当前会话撤销用例。"""
    return LogoutUseCase(repositories, clock)


def get_list_sessions_use_case(
    repositories: Annotated[IdentityRepositoryFactory, Depends(get_identity_repositories)],
    clock: Annotated[Clock, Depends(get_auth_clock)],
) -> ListSessionsUseCase:
    """组合当前用户活动会话列表用例。"""
    return ListSessionsUseCase(repositories, clock)


def get_revoke_session_use_case(
    repositories: Annotated[IdentityRepositoryFactory, Depends(get_identity_repositories)],
    clock: Annotated[Clock, Depends(get_auth_clock)],
) -> RevokeSessionUseCase:
    """组合用户隔离的指定会话撤销用例。"""
    return RevokeSessionUseCase(repositories, clock)


CurrentSession = Annotated[AuthenticatedSession, Depends(get_authenticated_session)]
CsrfProtectedSession = Annotated[AuthenticatedSession, Depends(require_csrf_authenticated_session)]
