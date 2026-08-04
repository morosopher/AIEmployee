"""提供 API 进程存活状态与外部依赖就绪状态端点。"""

from collections.abc import Awaitable, Callable
from datetime import date
from inspect import isawaitable
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from ai_employee.api.deps import CurrentSession, get_auth_clock, get_daily_brief_alerts_use_case
from ai_employee.application.use_cases.auth import Clock
from ai_employee.application.use_cases.diagnostics import GetDailyBriefOverdueAlertUseCase


class HealthResponse(BaseModel):
    """定义 API 进程存活检查的稳定响应契约。

    ``status`` 与 ``service`` 均为固定字面量，避免客户端把系统端点误解为
    可返回任意字符串的松散字典，并使 OpenAPI 明确暴露当前服务身份。
    """

    status: Literal["ok"]
    service: Literal["api"]


class ReadinessResponse(BaseModel):
    """定义外部依赖就绪检查的稳定响应契约。

    总体状态只允许就绪或未就绪；依赖名称由已注入探针决定，但每项结果必须
    是布尔值。响应不得携带连接信息、凭据或供应商原始数据。
    """

    status: Literal["ready", "not_ready"]
    dependencies: dict[str, bool]


class SystemAlertResponse(BaseModel):
    """定义前端轮询使用的最小逾期告警，刻意不含邮件或日程来源内容。"""

    code: Literal["daily_brief_overdue"]
    severity: Literal["critical"]
    local_date: date
    diagnostic_task_id: UUID | None


class SystemAlertsResponse(BaseModel):
    """定义用户范围系统告警列表的稳定 API 容器。"""

    alerts: list[SystemAlertResponse]


def build_system_router(
    readiness_probe: Callable[[], dict[str, bool] | Awaitable[dict[str, bool]]]
) -> APIRouter:
    """构建可注入依赖探针的系统状态路由。

    Args:
        readiness_probe: 返回依赖名称及可用状态的同步探针。探针结果仅报告状态，
            不应包含连接串、凭据或供应商响应等敏感信息。

    Returns:
        挂载在 ``/api/v1/system`` 下的 FastAPI 路由实例。
    """
    router = APIRouter(prefix="/api/v1/system", tags=["system"])

    @router.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """报告 API 进程已存活，不触发外部依赖 I/O。

        Returns:
            包含固定服务标识与存活状态的类型化响应。
        """
        return HealthResponse(status="ok", service="api")

    @router.get(
        "/readiness",
        response_model=ReadinessResponse,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
    )
    async def readiness(request: Request, response: Response) -> ReadinessResponse:
        """执行注入的依赖探针并映射为就绪状态响应。

        Args:
            response: FastAPI 当前响应对象，用于在依赖失败时设置 HTTP 503。

        Returns:
            总体就绪状态及每项依赖的布尔探测结果组成的类型化响应。
        """
        probe_result = readiness_probe()
        dependencies = await probe_result if isawaitable(probe_result) else probe_result
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is not None:
            for dependency, healthy in dependencies.items():
                metrics.record_dependency_health(dependency=dependency, healthy=healthy)
        ready = all(dependencies.values())
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        readiness_status: Literal["ready", "not_ready"] = "ready" if ready else "not_ready"
        return ReadinessResponse(status=readiness_status, dependencies=dependencies)

    @router.get("/alerts", response_model=SystemAlertsResponse)
    async def alerts(
        authenticated: CurrentSession,
        use_case: Annotated[
            GetDailyBriefOverdueAlertUseCase, Depends(get_daily_brief_alerts_use_case)
        ],
        clock: Annotated[Clock, Depends(get_auth_clock)],
    ) -> SystemAlertsResponse:
        """返回当前认证用户的派生逾期简报告警。

        Args:
            authenticated: 已验证 Cookie 会话，只从其中读取用户 UUID。
            use_case: 已装配的用户隔离告警用例，不由路由访问数据库。
            clock: 可替换的 UTC 时钟，避免请求进程依赖宿主机本地日期。

        Returns:
            空列表或一条 critical ``daily_brief_overdue`` 告警；不会返回来源正文。
        """
        alert = await use_case.execute(user_id=authenticated.user.id, now=clock.now())
        if alert is None:
            return SystemAlertsResponse(alerts=[])
        return SystemAlertsResponse(
            alerts=[
                SystemAlertResponse(
                    code="daily_brief_overdue",
                    severity="critical",
                    local_date=alert.local_date,
                    diagnostic_task_id=alert.diagnostic_task_id,
                )
            ]
        )

    return router
