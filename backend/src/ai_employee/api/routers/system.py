"""提供 API 进程存活状态与外部依赖就绪状态端点。"""

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel


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


def build_system_router(readiness_probe: Callable[[], dict[str, bool]]) -> APIRouter:
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

    @router.get("/readiness", response_model=ReadinessResponse)
    def readiness(response: Response) -> ReadinessResponse:
        """执行注入的依赖探针并映射为就绪状态响应。

        Args:
            response: FastAPI 当前响应对象，用于在依赖失败时设置 HTTP 503。

        Returns:
            总体就绪状态及每项依赖的布尔探测结果组成的类型化响应。
        """
        dependencies = readiness_probe()
        ready = all(dependencies.values())
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        readiness_status: Literal["ready", "not_ready"] = "ready" if ready else "not_ready"
        return ReadinessResponse(status=readiness_status, dependencies=dependencies)

    return router
