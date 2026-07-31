"""创建 FastAPI 应用并暴露供 ASGI 服务器加载的进程级实例。"""

from collections.abc import Callable

from fastapi import FastAPI

from ai_employee.api.routers.system import build_system_router


def create_app(
    readiness_probe: Callable[[], dict[str, bool]] | None = None,
) -> FastAPI:
    """创建并装配 AI Employee API 应用。

    Args:
        readiness_probe: 可选依赖就绪探针。测试可注入确定性探针；未提供时使用
            当前脚手架的默认成功结果，后续基础设施任务会接入真实 PostgreSQL 与
            Redis 探测。

    Returns:
        已注册系统状态路由的 FastAPI 应用实例。
    """
    app = FastAPI(title="AI Employee API", version="0.1.0")
    probe = readiness_probe or (lambda: {"postgres": True, "redis": True})
    app.include_router(build_system_router(probe))
    return app


app = create_app()
