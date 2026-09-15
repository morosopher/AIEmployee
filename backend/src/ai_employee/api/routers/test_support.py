"""仅供受控测试环境注入合成故障场景的 API 路由。"""

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict

from ai_employee.api.deps import ApiProblem, CsrfProtectedSession, CurrentSession
from ai_employee.config import Settings
from ai_employee.infrastructure.testing.scenarios import TestScenario

TEST_SCENARIO_TTL_SECONDS = 600


class RedisScenarioClient(Protocol):
    """收窄到测试场景所需的 Redis 原子命令，避免泄露通用客户端。"""

    async def set(self, key: str, value: str, *, ex: int) -> object:
        """以明确 TTL 写入用户隔离场景。"""

    async def getdel(self, key: str) -> bytes | str | None:
        """原子读取并删除一次性场景。"""


class TestScenarioRequest(BaseModel):
    """限制 test-only 注入值，禁止把任意供应商载荷塞进 Redis。"""

    model_config = ConfigDict(extra="forbid")

    scenario: TestScenario


class ExecuteTestTaskRequest(BaseModel):
    """限制 test-only 同步执行入口只能接收已经持久化的任务标识。"""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID


class SeedM2SourceRequest(BaseModel):
    """只允许选择两家固定 Fake，不接收账户、Token 或供应商原始载荷。"""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["google", "microsoft"]


class SeedM2SourceResponse(BaseModel):
    """合成来源的标识符响应，供生产编辑器绑定本例的精确连接和对象。"""

    model_config = ConfigDict(from_attributes=True)

    connection_id: UUID
    calendar_id: str
    thread_id: UUID
    message_id: UUID
    event_id: UUID
    starts_at: datetime
    ends_at: datetime


class M2EvidenceResponse(BaseModel):
    """无内容的数据库/外部 Fake 计数，独立验证一次写入与 Checkpoint。"""

    model_config = ConfigDict(from_attributes=True)

    execution_count: int
    write_calls: int
    reconcile_calls: int
    checkpoint_count: int
    real_writes_enabled: bool


class GenerateTestBriefRequest(BaseModel):
    """限制测试简报只能冻结当前测试刚创建的连接范围。"""

    connection_id: UUID


class GenerateTestBriefResponse(BaseModel):
    """返回可由测试执行入口消费的耐久任务标识。"""

    task_id: UUID


class SeedGoogleSourceResponse(BaseModel):
    """返回本次创建的合成连接，使 E2E 不会误用历史测试数据。"""

    connection_id: UUID


class TestScenarioStore:
    """以用户 ID 隔离、十分钟自动过期的一次性 fake adapter 场景存储。"""

    def __init__(self, redis: RedisScenarioClient) -> None:
        """注入仅能执行 SET/GETDEL 的 Redis 协议。"""
        self._redis = redis

    @staticmethod
    def key(*, user_id: UUID | str) -> str:
        """构造不含邮件、凭据或任务内容的每用户短期键。"""
        return f"ai_employee:test-scenario:{user_id}"

    async def set(self, *, user_id: UUID | str, scenario: str) -> None:
        """设置一次性合成场景并强制六百秒 TTL。"""
        await self._redis.set(self.key(user_id=user_id), scenario, ex=TEST_SCENARIO_TTL_SECONDS)

    async def consume(self, *, user_id: UUID | str) -> str | None:
        """原子消费场景，重复假请求不能重复施加同一故障。"""
        value = await self._redis.getdel(self.key(user_id=user_id))
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value if isinstance(value, str) else None

    async def aclose(self) -> None:
        """在测试 ASGI 生命周期结束时释放 Redis 客户端连接池。"""
        close = getattr(self._redis, "aclose", None)
        if close is not None:
            await close()


def should_register_test_support(settings: Settings) -> bool:
    """仅在 APP_ENV=test 且 APP_TEST_MODE=true 时允许路由进入 ASGI 表。"""
    return settings.app_env == "test" and settings.app_test_mode


def build_test_support_router() -> APIRouter:
    """构建 CSRF 保护的场景注入端点。

    Redis 连接由组合根在符合双开关时才创建；路由本身不读取 Secret、不连接真实
    Google，也不返回当前或历史测试场景。
    """
    router = APIRouter(prefix="/api/v1/test-support", tags=["test-support"], include_in_schema=False)

    @router.post("/scenario", status_code=status.HTTP_204_NO_CONTENT)
    async def set_scenario(
        payload: TestScenarioRequest,
        authenticated: CsrfProtectedSession,
        request: Request,
    ) -> None:
        """为当前登录用户设置一次性 fake adapter 故障，拒绝跨用户写入。"""
        store = request.app.state.test_scenario_store
        await store.set(user_id=authenticated.user.id, scenario=payload.scenario)

    @router.post(
        "/seed-google-source",
        status_code=status.HTTP_200_OK,
        response_model=SeedGoogleSourceResponse,
    )
    async def seed_google_source(
        authenticated: CsrfProtectedSession, request: Request
    ) -> SeedGoogleSourceResponse:
        """创建当前用户可消费的合成来源，并返回本次连接的稳定标识。"""
        connection_id = await request.app.state.test_support_fixture_service.seed_google_source(
            user_id=authenticated.user.id
        )
        return SeedGoogleSourceResponse(connection_id=connection_id)

    @router.post("/seed-m2-source", response_model=SeedM2SourceResponse)
    async def seed_m2_source(
        payload: SeedM2SourceRequest, authenticated: CsrfProtectedSession, request: Request,
    ) -> SeedM2SourceResponse:
        """创建当前用户的合成来源；不写入草稿、审批、ToolExecution 或任务结果。"""
        from ai_employee.infrastructure.testing.m2_sources import seed_m2_source as seed

        source = await seed(
            sessions=request.app.state.auth_session_factory,
            master_key_file=request.app.state.auth_settings.app_master_key_file,
            user_id=authenticated.user.id, provider=payload.provider,
        )
        return SeedM2SourceResponse.model_validate(source)

    @router.get("/m2-evidence/{task_id}", response_model=M2EvidenceResponse)
    async def m2_evidence(
        task_id: UUID, authenticated: CurrentSession, request: Request, response: Response,
    ) -> M2EvidenceResponse:
        """只读取同用户的计数；任务不属于当前用户时统一 404，响应不缓存。"""
        evidence = await request.app.state.m2_test_support.evidence(user_id=authenticated.user.id, task_id=task_id)
        if evidence is None:
            raise ApiProblem(404, "task_not_found", "Task not found", "The requested task is not available for this user.")
        response.headers["Cache-Control"] = "no-store"
        return M2EvidenceResponse.model_validate(evidence)

    @router.post("/expire-approval", status_code=status.HTTP_204_NO_CONTENT)
    async def expire_approval(
        payload: ExecuteTestTaskRequest, authenticated: CsrfProtectedSession, request: Request,
    ) -> None:
        """推进本用户精确待审批的测试截止时间，正式 store 负责到期收敛。"""
        if not await request.app.state.m2_test_support.expire_approval(user_id=authenticated.user.id, task_id=payload.task_id):
            raise ApiProblem(404, "approval_not_found", "Approval not found", "The requested approval is not available for this user.")

    @router.post("/reconcile-task", status_code=status.HTTP_204_NO_CONTENT)
    async def reconcile_task(
        payload: ExecuteTestTaskRequest, authenticated: CsrfProtectedSession, request: Request,
    ) -> None:
        """驱动本用户任务的专用只读 Worker；禁止接受写命令或人工结果。"""
        if not await request.app.state.m2_test_support.reconcile(user_id=authenticated.user.id, task_id=payload.task_id):
            raise ApiProblem(404, "task_not_found", "Task not found", "The requested task is not available for this user.")

    @router.post(
        "/generate-brief",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=GenerateTestBriefResponse,
    )
    async def generate_brief_for_test_source(
        payload: GenerateTestBriefRequest,
        authenticated: CsrfProtectedSession,
        request: Request,
    ) -> GenerateTestBriefResponse:
        """创建仅读取指定合成连接的每日简报任务。

        E2E 环境可能保留同一管理员以前运行留下的合成来源，因此测试必须把本例刚 seed
        的连接 ID 冻结到耐久任务载荷。生产简报 API 不接受该测试范围参数；此路由只在
        双测试开关下注册，并先以用户条件验证连接归属，不能借此读取另一用户来源。
        """
        task_id = await request.app.state.test_support_fixture_service.create_bound_brief_task(
            user_id=authenticated.user.id,
            connection_id=payload.connection_id,
        )
        if task_id is None:
            raise ApiProblem(
                404,
                "connection_not_found",
                "Connection not found",
                "The requested connection is not available for this user.",
            )
        return GenerateTestBriefResponse(task_id=task_id)

    @router.post("/execute-task", status_code=status.HTTP_204_NO_CONTENT)
    async def execute_task_for_test(
        payload: ExecuteTestTaskRequest,
        authenticated: CsrfProtectedSession,
        request: Request,
    ) -> None:
        """在双开关测试环境同步执行当前用户已排队的真实 Worker 路径。

        正常 API 永远只创建异步任务；此端点不在非测试路由表中，专门让浏览器 E2E 不依赖
        外部常驻 Taskiq 进程，同时仍复用 DurableTaskRunner、Fake adapter 与 PostgreSQL
        持久化边界。先按 ``user_id`` 验证任务归属，避免测试工具成为跨用户执行通道。
        """
        owned_task = await request.app.state.test_support_fixture_service.task_is_owned_by(
            user_id=authenticated.user.id,
            task_id=payload.task_id,
        )
        if not owned_task:
            raise ApiProblem(
                404,
                "task_not_found",
                "Task not found",
                "The requested task is not available for this user.",
            )
        if await request.app.state.m2_test_support.run_trusted_task(
            user_id=authenticated.user.id, task_id=payload.task_id,
        ):
            return
        await request.app.state.test_task_runner.run(
            payload.task_id,
            lease_owner=f"test-support:{uuid4()}",
        )

    return router
