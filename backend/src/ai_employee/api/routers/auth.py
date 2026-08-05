"""暴露单管理员登录、当前身份、登出和会话管理 REST 接口。"""

from datetime import datetime, time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from ai_employee.api.deps import (
    CSRF_COOKIE_NAME,
    ApiProblem,
    CsrfProtectedSession,
    CurrentSession,
    ProblemDetails,
    get_auth_settings,
    get_list_sessions_use_case,
    get_login_use_case,
    get_logout_use_case,
    get_revoke_session_use_case,
)
from ai_employee.application.use_cases.auth import (
    InvalidCredentialsError,
    ListSessionsUseCase,
    LoginResult,
    LoginUseCase,
    LogoutUseCase,
    RevokeSessionUseCase,
    SessionNotFoundError,
)
from ai_employee.config import Settings
from ai_employee.domain.identity import ListedSession, UserIdentity


class LoginRequest(BaseModel):
    """定义不记录、不回显的管理员登录凭据输入。"""

    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class UserResponse(BaseModel):
    """定义不含密码哈希和会话令牌的当前管理员响应。"""

    id: UUID
    email: str
    display_name: str
    timezone: str
    locale: str
    brief_time: time


class SessionResponse(BaseModel):
    """定义会话管理页面可见的非敏感生命周期字段。"""

    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_current: bool


def _user_response(user: UserIdentity) -> UserResponse:
    """显式白名单映射公开用户字段，避免未来新增敏感字段被自动序列化。"""
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        timezone=user.timezone,
        locale=user.locale,
        brief_time=user.brief_time,
    )


def _session_response(session: ListedSession) -> SessionResponse:
    """把领域会话摘要映射为 API Schema，永不接触 Token 摘要。"""
    return SessionResponse(
        id=session.id,
        created_at=session.created_at,
        last_seen_at=session.last_seen_at,
        expires_at=session.expires_at,
        is_current=session.is_current,
    )


def _secure_cookies(settings: Settings) -> bool:
    """仅本地开发与双开关测试 harness 允许非 Secure Cookie。

    测试进程必须同时由 ``APP_ENV=test`` 与 ``APP_TEST_MODE=true`` 约束，且仅监听
    本机 HTTP 端口；生产、预发布及误设为 test 的普通进程仍维持 Secure Cookie。
    """
    return not (
        settings.app_env == "development"
        or (settings.app_env == "test" and settings.app_test_mode)
    )


def _set_auth_cookies(response: Response, result: LoginResult, settings: Settings) -> None:
    """按不同 SameSite/HttpOnly 策略写入会话与 CSRF Cookie。"""
    secure = _secure_cookies(settings)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=result.raw_session_token,
        max_age=settings.session_ttl_seconds,
        expires=result.session.expires_at,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=result.raw_csrf_token,
        max_age=settings.session_ttl_seconds,
        expires=result.session.expires_at,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    """使用与创建时完全一致的安全属性和 Path 清除两个认证 Cookie。"""
    secure = _secure_cookies(settings)
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


def build_auth_router() -> APIRouter:
    """构建 ``/api/v1/auth`` 路由并保持 HTTP 映射与应用规则分离。"""
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

    @router.post(
        "/login",
        response_model=UserResponse,
        responses={
            status.HTTP_401_UNAUTHORIZED: {"model": ProblemDetails},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ProblemDetails},
        },
    )
    async def login(
        payload: LoginRequest,
        response: Response,
        use_case: Annotated[LoginUseCase, Depends(get_login_use_case)],
        settings: Annotated[Settings, Depends(get_auth_settings)],
    ) -> UserResponse:
        """验证管理员凭据、创建数据库会话并设置两个安全 Cookie。"""
        try:
            result = await use_case.execute(email=payload.email, password=payload.password)
        except InvalidCredentialsError:
            raise ApiProblem(
                401,
                "invalid_credentials",
                "Invalid credentials",
                "The email or password was not accepted.",
            ) from None
        _set_auth_cookies(response, result, settings)
        return _user_response(result.user)

    @router.get(
        "/me",
        response_model=UserResponse,
        responses={status.HTTP_401_UNAUTHORIZED: {"model": ProblemDetails}},
    )
    async def me(authenticated: CurrentSession) -> UserResponse:
        """返回当前活动会话所属的公开管理员资料。"""
        return _user_response(authenticated.user)

    @router.post(
        "/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={
            status.HTTP_401_UNAUTHORIZED: {"model": ProblemDetails},
            status.HTTP_403_FORBIDDEN: {"model": ProblemDetails},
        },
    )
    async def logout(
        authenticated: CsrfProtectedSession,
        use_case: Annotated[LogoutUseCase, Depends(get_logout_use_case)],
        settings: Annotated[Settings, Depends(get_auth_settings)],
    ) -> Response:
        """在通过 CSRF 后撤销当前会话并清除浏览器 Cookie。"""
        await use_case.execute(authenticated)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        _clear_auth_cookies(response, settings)
        return response

    @router.get(
        "/sessions",
        response_model=list[SessionResponse],
        responses={status.HTTP_401_UNAUTHORIZED: {"model": ProblemDetails}},
    )
    async def list_sessions(
        authenticated: CurrentSession,
        use_case: Annotated[ListSessionsUseCase, Depends(get_list_sessions_use_case)],
    ) -> list[SessionResponse]:
        """列出当前用户所有活动会话，且只标识当前请求会话。"""
        sessions = await use_case.execute(authenticated)
        return [_session_response(session) for session in sessions]

    @router.delete(
        "/sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={
            status.HTTP_401_UNAUTHORIZED: {"model": ProblemDetails},
            status.HTTP_403_FORBIDDEN: {"model": ProblemDetails},
            status.HTTP_404_NOT_FOUND: {"model": ProblemDetails},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ProblemDetails},
        },
    )
    async def revoke_session(
        session_id: UUID,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[RevokeSessionUseCase, Depends(get_revoke_session_use_case)],
        settings: Annotated[Settings, Depends(get_auth_settings)],
    ) -> Response:
        """撤销本人指定活动会话；跨用户与不存在返回不可区分的 404。"""
        try:
            revoked_current = await use_case.execute(authenticated, session_id)
        except SessionNotFoundError:
            raise ApiProblem(
                404,
                "session_not_found",
                "Session not found",
                "The requested active session was not found.",
            ) from None
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        if revoked_current:
            _clear_auth_cookies(response, settings)
        return response

    return router
