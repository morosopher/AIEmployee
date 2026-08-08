"""创建 FastAPI 应用并暴露供 ASGI 服务器加载的进程级实例。"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.api.deps import (
    ApiProblem,
    SystemClock,
    handle_api_problem,
    handle_domain_error,
    handle_request_validation_error,
    handle_unexpected_error,
)
from ai_employee.api.routers.approvals import build_approvals_router
from ai_employee.api.routers.auth import build_auth_router
from ai_employee.api.routers.briefs import build_briefs_router
from ai_employee.api.routers.connections import build_connections_router
from ai_employee.api.routers.conversations import build_conversations_router
from ai_employee.api.routers.privacy import build_privacy_router
from ai_employee.api.routers.settings import build_settings_router
from ai_employee.api.routers.system import build_system_router
from ai_employee.api.routers.tasks import build_tasks_router
from ai_employee.api.sse import TaskEventStore, TaskEventStream
from ai_employee.application.use_cases.approvals import ApprovalDecisionUseCase
from ai_employee.application.use_cases.diagnostics import GetDailyBriefOverdueAlertUseCase
from ai_employee.application.use_cases.task_views import (
    CancelTaskUseCase,
    GetTaskUseCase,
    RetryTaskUseCase,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.config import get_settings
from ai_employee.domain.errors import DomainError
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStoreFactory
from ai_employee.infrastructure.db.repositories.diagnostics import SqlAlchemyOverdueBriefReader
from ai_employee.infrastructure.db.repositories.identity import (
    SqlAlchemyIdentityRepositoryFactory,
)
from ai_employee.infrastructure.db.repositories.task_views import (
    PostgresQueuedTaskDispatcher,
    SqlAlchemyTaskViewStore,
)
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepositoryFactory
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.events.publisher import TaskEventPublisher
from ai_employee.infrastructure.observability.logging import configure_json_logging
from ai_employee.infrastructure.observability.metrics import create_metrics, run_periodic_heartbeat
from ai_employee.infrastructure.observability.sync import refresh_sync_age_metrics
from ai_employee.infrastructure.observability.tracing import initialize_tracing
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
    session_factory = build_session_factory(
        settings.database_url,
        task_event_publisher=TaskEventPublisher(settings.redis_url),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """在 API 进程退出时释放认证数据库连接池。"""
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            if app.state.metrics is not None:

                async def refresh_api_sync_age() -> None:
                    """以 API 所有的只读连接池恢复重启后的同步新鲜度。"""
                    await refresh_sync_age_metrics(
                        session_factory=session_factory,
                        metrics=app.state.metrics,
                        now=datetime.now(UTC),
                    )

                heartbeat_task = asyncio.create_task(
                    run_periodic_heartbeat(
                        metrics=app.state.metrics,
                        process="api",
                        on_tick=refresh_api_sync_age,
                    )
                )
            yield
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            scenario_store = getattr(app.state, "test_scenario_store", None)
            if scenario_store is not None:
                # 此 Redis 客户端只在 test 双开关下创建；与主 readiness 客户端分开关闭，
                # 防止 Playwright/pytest 多次建 app 时留下连接。
                await scenario_store.aclose()
            await session_factory.dispose()

    app = FastAPI(title="AI Employee API", version="0.1.0", lifespan=lifespan)
    app.state.metrics = create_metrics() if settings.metrics_enabled else None
    configure_json_logging(tuple(settings.model_redaction_patterns))
    initialize_tracing(
        app=app,
        async_engine=session_factory.engine,
        enabled=settings.otel_enabled,
        service_name=settings.otel_service_name,
        endpoint=settings.otel_exporter_otlp_endpoint,
    )
    app.state.auth_settings = settings
    app.state.auth_session_factory = session_factory
    app.state.auth_repository_factory = SqlAlchemyIdentityRepositoryFactory(session_factory)
    app.state.auth_clock = SystemClock()
    app.state.auth_token_factory = new_token
    app.state.auth_token_hasher = hash_token
    app.state.auth_password_verifier = PasswordHasher()
    app.state.connections_store_factory = SqlAlchemyConnectionStoreFactory(session_factory)
    # 正常运行由 deps 惰性构造固定的 Google/Microsoft provider mapping；关闭
    # APP_TEST_MODE 的契约测试可在请求前一次性注入受 HTTP mock 保护的 mapping。测试
    # 模式会忽略该 state 并固定使用内置 fake；用例构造后复制冻结，不暴露运行时注册入口。
    app.state.oauth_adapters = None
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
    app.state.daily_brief_alerts_use_case = GetDailyBriefOverdueAlertUseCase(
        reader=SqlAlchemyOverdueBriefReader(session_factory)
    )
    app.state.task_event_stream = TaskEventStream(
        TaskEventStore(session_factory, task_store),
        redis_url=settings.redis_url,
        metrics=app.state.metrics,
    )
    app.add_exception_handler(ApiProblem, handle_api_problem)
    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)

    async def real_readiness_probe() -> dict[str, bool]:
        """以最小 SQL 与 Redis PING 检查真实依赖，不返回连接或异常原文。"""
        postgres_healthy = False
        redis_healthy = False
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
            postgres_healthy = True
        except SQLAlchemyError:
            pass
        client = Redis.from_url(settings.redis_url)
        try:
            redis_healthy = bool(await client.ping())
        except (OSError, RedisError, TimeoutError):
            pass
        finally:
            await client.aclose()

        return {"postgres": postgres_healthy, "redis": redis_healthy}

    probe = readiness_probe or real_readiness_probe
    app.include_router(build_system_router(probe))
    if app.state.metrics is not None:

        @app.get("/metrics", include_in_schema=False)
        async def metrics():
            """暴露不含用户和内容标签的 Prometheus 指标。"""
            return app.state.metrics.render()

    app.include_router(build_auth_router())
    app.include_router(build_connections_router())
    app.include_router(build_privacy_router())
    app.include_router(build_briefs_router())
    app.include_router(build_conversations_router())
    app.include_router(build_settings_router())
    app.include_router(build_tasks_router())
    app.include_router(build_approvals_router())
    # 故障注入只能在两个显式测试开关同时打开后注册；生产路由表中完全不存在该入口。
    if settings.app_env == "test" and settings.app_test_mode:
        from ai_employee.api.routers.test_support import (
            TestScenarioStore,
            build_test_support_router,
        )
        from ai_employee.infrastructure.testing.test_support import TestSupportFixtureService
        from ai_employee.workers.execute_task import build_task_runner_for_session

        app.state.test_scenario_store = TestScenarioStore(Redis.from_url(settings.redis_url))
        # 测试同步执行复用 API 生命周期拥有的 session factory；lifespan 统一释放引擎，
        # 避免 E2E 每次请求经全局 Worker 缓存泄漏独立连接池。
        app.state.test_task_runner = build_task_runner_for_session(
            session_factory,
            settings=settings,
        )
        app.state.test_support_fixture_service = TestSupportFixtureService(
            session_factory,
            app.state.create_task_use_case,
            app_master_key_file=settings.app_master_key_file,
        )
        app.include_router(build_test_support_router())
    return app


app = create_app()
