"""创建 FastAPI 应用并暴露供 ASGI 服务器加载的进程级实例。"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from ai_employee.api.deps import (
    ApiProblem,
    SystemClock,
    handle_api_problem,
    handle_request_validation_error,
)
from ai_employee.api.routers.approvals import build_approvals_router
from ai_employee.api.routers.auth import build_auth_router
from ai_employee.api.routers.system import build_system_router
from ai_employee.api.routers.tasks import build_tasks_router
from ai_employee.api.sse import TaskEventStore, TaskEventStream
from ai_employee.application.use_cases.approvals import ApprovalDecisionUseCase
from ai_employee.application.use_cases.task_views import (
    CancelTaskUseCase,
    GetTaskUseCase,
    RetryTaskUseCase,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.config import get_settings
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.identity import (
    SqlAlchemyIdentityRepositoryFactory,
)
from ai_employee.infrastructure.db.repositories.task_views import (
    PostgresQueuedTaskDispatcher,
    SqlAlchemyTaskViewStore,
)
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepositoryFactory
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.infrastructure.security.tokens import hash_token, new_token


def create_app(
    readiness_probe: Callable[[], dict[str, bool]] | None = None,
) -> FastAPI:
    """创建并装配 AI Employee API 应用。

    Args:
        readiness_probe: 可选依赖就绪探针。测试可注入确定性探针；未提供时使用
            当前脚手架的默认成功结果，后续基础设施任务会接入真实 PostgreSQL 与
            Redis 探测。

    Returns:
        已注册系统状态与认证路由，并持有可释放数据库引擎的 FastAPI 应用实例。
    """
    settings = get_settings()
    session_factory = build_session_factory(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """在 API 进程退出时释放认证数据库连接池。"""
        try:
            yield
        finally:
            await session_factory.dispose()

    app = FastAPI(title="AI Employee API", version="0.1.0", lifespan=lifespan)
    app.state.auth_settings = settings
    app.state.auth_session_factory = session_factory
    app.state.auth_repository_factory = SqlAlchemyIdentityRepositoryFactory(session_factory)
    app.state.auth_clock = SystemClock()
    app.state.auth_token_factory = new_token
    app.state.auth_token_hasher = hash_token
    app.state.auth_password_verifier = PasswordHasher()
    task_store = SqlAlchemyTaskViewStore(session_factory)
    app.state.create_task_use_case = CreateTaskUseCase(
        SqlAlchemyTaskRepositoryFactory(session_factory),
        PostgresQueuedTaskDispatcher(session_factory),
    )
    app.state.get_task_use_case = GetTaskUseCase(task_store)
    app.state.cancel_task_use_case = CancelTaskUseCase(task_store)
    app.state.retry_task_use_case = RetryTaskUseCase(task_store)
    app.state.approval_decision_use_case = ApprovalDecisionUseCase(
        SqlAlchemyApprovalStore(session_factory)
    )
    app.state.task_event_stream = TaskEventStream(
        TaskEventStore(session_factory, task_store), redis_url=settings.redis_url
    )
    app.add_exception_handler(ApiProblem, handle_api_problem)
    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    probe = readiness_probe or (lambda: {"postgres": True, "redis": True})
    app.include_router(build_system_router(probe))
    app.include_router(build_auth_router())
    app.include_router(build_tasks_router())
    app.include_router(build_approvals_router())
    return app


app = create_app()
