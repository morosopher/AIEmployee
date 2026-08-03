"""暴露用户隔离的任务创建、快照、取消与重试接口。"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, Field

from ai_employee.api.deps import ApiProblem, CsrfProtectedSession, CurrentSession
from ai_employee.application.use_cases.task_views import (
    CancelTaskUseCase,
    GetTaskUseCase,
    RetryTaskUseCase,
    TaskSnapshot,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import JsonValue


class CreateTaskRequest(BaseModel):
    """创建可恢复任务时接收的内部种类与规范 JSON 输入。"""

    kind: str = Field(min_length=1, max_length=100)
    input_payload: dict[str, JsonValue] = Field(default_factory=dict)


class StepResponse(BaseModel):
    """前端时间线可安全显示的步骤摘要。"""

    id: UUID
    sequence: int
    name: str
    status: str
    output_summary: dict[str, JsonValue] | None
    error_code: str | None


class TaskResponse(BaseModel):
    """公开任务快照，刻意不包含内部租约与执行器字段。"""

    id: UUID
    kind: str
    status: str
    retry_of_task_id: UUID | None
    error_code: str | None
    steps: list[StepResponse]


class CreateTaskResponse(BaseModel):
    """异步创建操作的稳定 202 响应。"""

    task_id: UUID
    status: str


def _task_response(snapshot: TaskSnapshot) -> TaskResponse:
    """显式映射应用快照，避免把输入载荷或未来字段意外公开。"""
    return TaskResponse(
        id=snapshot.id,
        kind=snapshot.kind,
        status=snapshot.status.value,
        retry_of_task_id=snapshot.retry_of_task_id,
        error_code=snapshot.error_code,
        steps=[
            StepResponse(
                id=item.id,
                sequence=item.sequence,
                name=item.name,
                status=item.status,
                output_summary=item.output_summary,
                error_code=item.error_code,
            )
            for item in snapshot.steps
        ],
    )


def _missing_task() -> ApiProblem:
    """统一隐藏跨用户资源，返回与不存在相同的 404。"""
    return ApiProblem(404, "task_not_found", "Task not found", "The requested task was not found.")


def build_tasks_router() -> APIRouter:
    """构造路由；实际用例由组合根经依赖注入提供。"""
    from ai_employee.api.deps import (
        get_cancel_task_use_case,
        get_create_task_use_case,
        get_get_task_use_case,
        get_retry_task_use_case,
    )

    router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

    @router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=CreateTaskResponse)
    async def create_task(
        payload: CreateTaskRequest,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[CreateTaskUseCase, Depends(get_create_task_use_case)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CreateTaskResponse:
        """事务创建任务，并要求客户端提供用户范围的幂等键。"""
        if not idempotency_key:
            raise ApiProblem(
                422,
                "idempotency_key_required",
                "Idempotency key required",
                "An Idempotency-Key header is required.",
            )
        result = await use_case.execute(
            user_id=authenticated.user.id,
            kind=payload.kind,
            input_payload=payload.input_payload,
            idempotency_key=idempotency_key,
        )
        return CreateTaskResponse(task_id=result.task_id, status=result.status.value)

    @router.get("/{task_id}", response_model=TaskResponse)
    async def get_task(
        task_id: UUID,
        authenticated: CurrentSession,
        use_case: Annotated[GetTaskUseCase, Depends(get_get_task_use_case)],
    ) -> TaskResponse:
        """读取当前用户拥有的任务与按序步骤。"""
        snapshot = await use_case.execute(task_id=task_id, user_id=authenticated.user.id)
        if snapshot is None:
            raise _missing_task()
        return _task_response(snapshot)

    @router.get("/{task_id}/events")
    async def task_events(
        task_id: UUID,
        request: Request,
        authenticated: CurrentSession,
        use_case: Annotated[GetTaskUseCase, Depends(get_get_task_use_case)],
    ):
        """建立事件流；认证后先验证任务存在，避免跨用户订阅。"""
        if await use_case.execute(task_id=task_id, user_id=authenticated.user.id) is None:
            raise _missing_task()
        stream = request.app.state.task_event_stream
        last_value = request.headers.get("Last-Event-ID")
        try:
            last_event_id = int(last_value) if last_value is not None else None
        except ValueError:
            raise ApiProblem(
                422,
                "invalid_last_event_id",
                "Invalid event cursor",
                "Last-Event-ID must be an integer.",
            ) from None
        return stream.response(
            task_id=task_id, user_id=authenticated.user.id, last_event_id=last_event_id
        )

    @router.post("/{task_id}/cancel", response_model=TaskResponse)
    async def cancel_task(
        task_id: UUID,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[CancelTaskUseCase, Depends(get_cancel_task_use_case)],
    ) -> TaskResponse:
        """取消可取消任务；终态或运行外状态冲突映射为 409。"""
        try:
            snapshot = await use_case.execute(
                task_id=task_id, user_id=authenticated.user.id, now=datetime.now(UTC)
            )
        except StateConflictError:
            raise ApiProblem(
                409,
                "task_state_conflict",
                "Task state conflict",
                "The task cannot be changed from its current state.",
            ) from None
        if snapshot is None:
            raise _missing_task()
        return _task_response(snapshot)

    @router.post("/{task_id}/retry", response_model=TaskResponse)
    async def retry_task(
        task_id: UUID,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[RetryTaskUseCase, Depends(get_retry_task_use_case)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> TaskResponse:
        """从失败任务创建新运行记录，不修改原终态事实。"""
        if not idempotency_key:
            raise ApiProblem(
                422,
                "idempotency_key_required",
                "Idempotency key required",
                "An Idempotency-Key header is required.",
            )
        try:
            snapshot = await use_case.execute(
                task_id=task_id,
                user_id=authenticated.user.id,
                idempotency_key=idempotency_key,
                now=datetime.now(UTC),
            )
        except StateConflictError:
            raise ApiProblem(
                409,
                "task_state_conflict",
                "Task state conflict",
                "The task cannot be changed from its current state.",
            ) from None
        if snapshot is None:
            raise _missing_task()
        return _task_response(snapshot)

    return router
