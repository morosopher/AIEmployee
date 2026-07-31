"""提供 API 进程存活状态与外部依赖就绪状态端点。"""

from collections.abc import Callable

from fastapi import APIRouter, Response, status


def build_system_router(readiness_probe: Callable[[], dict[str, bool]]) -> APIRouter:
    """构建可注入依赖探针的系统状态路由。

    Args:
        readiness_probe: 返回依赖名称及可用状态的同步探针。探针结果仅报告状态，
            不应包含连接串、凭据或供应商响应等敏感信息。

    Returns:
        挂载在 ``/api/v1/system`` 下的 FastAPI 路由实例。
    """
    router = APIRouter(prefix="/api/v1/system", tags=["system"])

    @router.get("/health")
    def health() -> dict[str, str]:
        """报告 API 进程已存活，不触发外部依赖 I/O。

        Returns:
            包含固定服务标识与存活状态的响应字典。
        """
        return {"status": "ok", "service": "api"}

    @router.get("/readiness")
    def readiness(response: Response) -> dict[str, object]:
        """执行注入的依赖探针并映射为就绪状态响应。

        Args:
            response: FastAPI 当前响应对象，用于在依赖失败时设置 HTTP 503。

        Returns:
            总体就绪状态及每项依赖的布尔探测结果。
        """
        dependencies = readiness_probe()
        ready = all(dependencies.values())
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if ready else "not_ready",
            "dependencies": dependencies,
        }

    return router
