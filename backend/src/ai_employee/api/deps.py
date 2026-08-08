"""组合认证应用端口、请求依赖与 RFC 9457 Problem Details 映射。"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Protocol, cast
from uuid import uuid4

from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ai_employee.application.ports.oauth import (
    OAuthAccount,
    OAuthAuthorizationRequest,
    OAuthProvider,
    OAuthProviderAdapter,
    OAuthRevocationResult,
    OAuthRevocationStatus,
    OAuthTokenSet,
)
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
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import (
    DomainError,
    InternalInvariantError,
    ModelOutputError,
    PermanentProviderError,
    StateConflictError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.domain.identity import AuthenticatedSession
from ai_employee.integrations.google.oauth import (
    GOOGLE_SCOPES,
    GoogleAccount,
    GoogleOAuthAdapter,
    GoogleTokenResponse,
    build_authorization_url,
)
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter

CSRF_COOKIE_NAME = "ai_employee_csrf"
_LOGGER = logging.getLogger(__name__)


class _LegacyGoogleOAuthClient(Protocol):
    """描述 Task 8 兼容包装器可调用的旧 Google OAuth 客户端形状。"""

    async def exchange_code(self, code: str, verifier: str) -> GoogleTokenResponse:
        """交换授权码。"""
        ...

    async def fetch_account(self, access_token: str) -> GoogleAccount:
        """读取 Google 账户。"""
        ...

    async def refresh_token(self, refresh_token: str) -> GoogleTokenResponse:
        """刷新 access token。"""
        ...

    async def revoke(self, token: str) -> None:
        """调用 Google 窄撤销端点。"""
        ...


class _GoogleOAuthAdapterCompat:
    """在 API 组合边界把 M1 Google 客户端适配到供应商中立端口。

    本兼容器只供测试模式和旧 fake 使用；它按请求传入的 scope 构造 URL，但交换结果仍
    固定声明 M1 已实际覆盖的只读 scope，因此测试不会联网，也不会误把写能力标为 enabled。
    生产组合根使用具备 token-info、nonce 和实际 scope 核验的 ``GoogleOAuthAdapter``。
    """

    provider = OAuthProvider.GOOGLE

    _CAPABILITY_SCOPES: Mapping[ConnectionCapability, frozenset[str]] = {
        ConnectionCapability.MAIL_READ: frozenset(
            {"https://www.googleapis.com/auth/gmail.readonly"}
        ),
        ConnectionCapability.MAIL_SEND: frozenset({"https://www.googleapis.com/auth/gmail.send"}),
        ConnectionCapability.CALENDAR_READ: frozenset(
            {"https://www.googleapis.com/auth/calendar.readonly"}
        ),
        ConnectionCapability.CALENDAR_WRITE: frozenset(
            {"https://www.googleapis.com/auth/calendar.events"}
        ),
    }

    def __init__(
        self,
        client: _LegacyGoogleOAuthClient,
        *,
        client_id: str,
        redirect_uri: str,
    ) -> None:
        """保存旧客户端和非敏感 OAuth 公共配置；client secret 仍只在客户端内部。"""
        self._client = client
        self._client_id = client_id
        self._redirect_uri = redirect_uri

    def scopes_for(
        self,
        capabilities: frozenset[ConnectionCapability],
    ) -> frozenset[str]:
        """返回身份 scope 与能力所需 scope 的不可变并集。"""
        scopes = {"openid", "email"}
        for capability in capabilities:
            scopes.update(self._CAPABILITY_SCOPES[capability])
        return frozenset(scopes)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """为测试模式保留旧客户端，同时使用请求中的精确 scope 与 nonce。"""
        return build_authorization_url(
            client_id=self._client_id,
            redirect_uri=self._redirect_uri,
            state=request.state,
            code_challenge=request.code_challenge,
            scopes=request.requested_scopes,
            oidc_nonce=request.oidc_nonce,
        )

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """把旧 token 值对象规范化，并只声明 M1 已实际请求的只读 scope。"""
        token = await self._client.exchange_code(code, verifier)
        return OAuthTokenSet(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_in=token.expires_in,
            granted_scopes=frozenset(GOOGLE_SCOPES),
        )

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """通过旧 userinfo 边界读取 Google subject，并规范化空 tenant 身份。"""
        del expected_nonce_hash
        account = await self._client.fetch_account(token.access_token)
        return OAuthAccount(
            provider_account_id=account.provider_account_id,
            account_email=account.email,
            provider_tenant_id="",
            account_type="google",
        )

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """规范化旧刷新结果；未轮换 refresh token 时保留 ``None``。"""
        token = await self._client.refresh_token(refresh_token)
        return OAuthTokenSet(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_in=token.expires_in,
            granted_scopes=frozenset(GOOGLE_SCOPES),
        )

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """仅在 Google 端点成功返回后报告 ``REVOKED``；网络异常原样传播。"""
        await self._client.revoke(token)
        return OAuthRevocationResult(OAuthRevocationStatus.REVOKED)


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
    trace_id = _request_trace_id(request)
    problem = ProblemDetails(
        type=(f"https://ai-employee.local/problems/{problem_error.error_code.replace('_', '-')}"),
        title=problem_error.title,
        status=problem_error.status_code,
        detail=problem_error.detail,
        instance=request.url.path,
        error_code=problem_error.error_code,
        trace_id=trace_id,
    )
    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None:
        # 标签只使用模板路由和稳定错误码，绝不把用户、query 或异常消息送入 Prometheus。
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        metrics.api_errors.labels(route=route_path, error_code=problem_error.error_code).inc()
    headers: dict[str, str] = {}
    if isinstance(problem_error, _TransientApiProblem) and problem_error.retry_after is not None:
        headers["Retry-After"] = str(int(problem_error.retry_after))
    return JSONResponse(
        status_code=problem_error.status_code,
        content=problem.model_dump(mode="json"),
        media_type="application/problem+json",
        headers=headers,
    )


def _request_trace_id(request: Request) -> str:
    """读取或创建仅用于客户端问题响应与内部日志关联的随机 trace ID。"""
    existing_trace_id = getattr(request.state, "trace_id", None)
    trace_id = existing_trace_id if isinstance(existing_trace_id, str) else uuid4().hex
    request.state.trace_id = trace_id
    return trace_id


class _TransientApiProblem(ApiProblem):
    """保存已规范化 Retry-After 的内部 Problem 表示，避免暴露供应商错误文本。"""

    def __init__(self, error: TransientProviderError) -> None:
        """把临时领域错误收敛为 503 及可选秒级重试提示。"""
        super().__init__(503, error.error_code, "Service temporarily unavailable", "Retry later.")
        self.retry_after = error.retry_after


def _domain_problem(error: DomainError) -> ApiProblem:
    """按公开领域类别创建 RFC 9457 Problem，永不回显领域 message 或 metadata。"""
    # Microsoft 管理员同意是组织侧冲突而非普通个人 re-auth；规格固定要求 409，
    # 因此必须在 UserActionRequiredError 的通用 403 分支之前做稳定错误码特判。
    if error.error_code == "microsoft_admin_consent_required":
        return ApiProblem(
            409,
            error.error_code,
            "Administrator consent required",
            "An organization administrator must approve the requested access.",
        )
    if isinstance(error, UserActionRequiredError):
        return ApiProblem(
            403, error.error_code, "User action required", "Complete the required action."
        )
    if isinstance(error, TransientProviderError):
        return _TransientApiProblem(error)
    if isinstance(error, (PermanentProviderError, ModelOutputError)):
        return ApiProblem(
            422, error.error_code, "Request cannot be completed", "The request cannot be completed."
        )
    if isinstance(error, StateConflictError):
        return ApiProblem(
            409, error.error_code, "State conflict", "The request conflicts with current state."
        )
    if isinstance(error, InternalInvariantError):
        return ApiProblem(
            500, error.error_code, "Internal server error", "An unexpected error occurred."
        )
    return ApiProblem(
        500, "internal_error", "Internal server error", "An unexpected error occurred."
    )


async def handle_domain_error(request: Request, exception: Exception) -> JSONResponse:
    """把稳定领域错误映射为不含原文的 RFC 9457 响应。

    未知 ``DomainError`` 被故意降级为 generic 500，防止新子类在未审查前意外公开语义。
    """
    if not isinstance(exception, DomainError):
        raise exception
    return _problem_response(request, _domain_problem(exception))


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


async def handle_unexpected_error(request: Request, exception: Exception) -> JSONResponse:
    """把未分类异常收敛为不含内部细节的 RFC 9457 500 响应。

    Args:
        request: 当前 HTTP 请求，仅用于生成路径与稳定 trace ID。
        exception: 未被业务层分类的异常；仅作为 logging 的 ``exc_info`` 来源，不能序列化消息、
            参数、请求头、Cookie 或正文。

    Returns:
        固定 ``internal_error`` 问题响应，客户端可使用 trace ID 向运维侧关联日志。
    """
    trace_id = _request_trace_id(request)
    _LOGGER.exception(
        "unexpected_api_error",
        exc_info=exception,
        extra={"trace_id": trace_id, "error_code": "internal_error"},
    )
    return _problem_response(
        request,
        ApiProblem(
            500,
            "internal_error",
            "Internal server error",
            "An unexpected error occurred. Use the trace_id when contacting support.",
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
    """在组合边界构造固定 adapter mapping 的供应商中立连接用例。

    非 ``APP_TEST_MODE`` 的契约/集成测试可在 ``app.state.oauth_adapters`` 一次性放入
    受 HTTP mock 保护的 mapping；普通运行时装配 Task 9 的 ``GoogleOAuthAdapter`` 与
    Task 10 的 ``MicrosoftOAuthAdapter``。``APP_TEST_MODE`` 的优先级最高，会完全忽略
    外部 mapping，并为两个供应商固定装配绝不联网的合成 fake。mapping 会由用例复制冻结，
    没有运行时注册或替换入口。真实 client secret 仍只在非测试模式从 Secret 文件读取。
    """
    from ai_employee.application.use_cases.connections import ConnectionsUseCase
    from ai_employee.infrastructure.security.encryption import AeadCipher

    settings = get_auth_settings(request)
    cipher = AeadCipher.from_file(settings.app_master_key_file)
    injected = getattr(request.app.state, "oauth_adapters", None)
    if settings.app_test_mode:
        # APP_TEST_MODE 是优先于 app.state 注入的不可绕过隔离边界。这里不尝试通过
        # 鸭子类型或调用方声明判断 adapter 是否安全，而是完全丢弃外部 mapping，避免
        # 真实 Google/Microsoft adapter 经 exchange、callback、refresh 接触网络。
        from ai_employee.integrations.google.fake import FakeGoogleOAuthClient
        from ai_employee.integrations.microsoft.fake import FakeMicrosoftOAuthAdapter

        oauth: _LegacyGoogleOAuthClient = FakeGoogleOAuthClient()
        adapters: dict[str, OAuthProviderAdapter] = {
            OAuthProvider.GOOGLE.value: _GoogleOAuthAdapterCompat(
                oauth,
                client_id=settings.google_client_id,
                redirect_uri=settings.google_redirect_uri,
            ),
            OAuthProvider.MICROSOFT.value: FakeMicrosoftOAuthAdapter(
                client_id=settings.microsoft_client_id or "test-mode-microsoft-client",
                redirect_uri=(
                    settings.microsoft_redirect_uri
                    or "https://app.example.test/api/v1/connections/microsoft/callback"
                ),
            ),
        }
    elif injected is not None:
        adapters = dict(cast(Mapping[str, OAuthProviderAdapter], injected))
    else:
        google_secret = settings.read_secret_file(
            settings.google_client_secret_file
        ).get_secret_value()
        # 生产 OAuth 必须经过具备 token-info 与 nonce 校验的真实 adapter；旧客户端仅
        # 由同步 Worker/隐私 Worker 使用，不能继续承担渐进授权组合根职责。
        adapters = {
            OAuthProvider.GOOGLE.value: GoogleOAuthAdapter(
                settings.google_client_id,
                google_secret,
                settings.google_redirect_uri,
            )
        }
        # 兼容现有只配置 Google 的开发/回归环境：只有 Microsoft 客户端配置完整时
        # 才读取其 Secret。生产部署应始终提供这三项配置，从而固定装配两个 provider。
        if settings.microsoft_client_id and settings.microsoft_redirect_uri:
            microsoft_secret = settings.read_secret_file(
                settings.microsoft_client_secret_file
            ).get_secret_value()
            adapters[OAuthProvider.MICROSOFT.value] = MicrosoftOAuthAdapter(
                settings.microsoft_client_id,
                microsoft_secret,
                settings.microsoft_redirect_uri,
            )
    return ConnectionsUseCase(
        request.app.state.connections_store_factory,
        cipher,
        adapters,
        get_auth_clock(request),
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


def get_daily_brief_alerts_use_case(request: Request):
    """从组合根取得用户范围的逾期简报告警用例。

    路由只通过该依赖获取应用层端口，不能直接构造 SQL 查询或接触其他用户的数据。

    Args:
        request: 当前 FastAPI 请求，用于读取组合根预先注入的用例实例。

    Returns:
        只接受认证用户 UUID 与显式时钟的逾期告警用例。
    """
    return request.app.state.daily_brief_alerts_use_case


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
