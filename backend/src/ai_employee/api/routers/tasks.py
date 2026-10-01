"""暴露用户隔离的任务历史摘要、创建、快照、取消与重试接口。"""

import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import BaseModel, Field

from ai_employee.api.deps import ApiProblem, CsrfProtectedSession, CurrentSession
from ai_employee.application.task_history import (
    HistoryCategory,
    TaskHistoryCursorError,
    TaskHistoryFilterError,
    TaskHistoryPage,
    TaskHistoryQuery,
)
from ai_employee.application.task_history_dates import canonical_utc_timestamp
from ai_employee.application.use_cases.task_history import ListTaskHistoryUseCase
from ai_employee.application.use_cases.task_views import (
    CancelTaskUseCase,
    GetTaskUseCase,
    RetryTaskUseCase,
    TaskSnapshot,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import JsonValue

IdempotencyKeyHeader = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]
POSTGRESQL_BIGINT_MAX = 2**63 - 1
POSTGRESQL_BIGINT_MAX_TEXT = str(POSTGRESQL_BIGINT_MAX)
CANONICAL_EVENT_CURSOR = re.compile(r"^(0|[1-9][0-9]*)$")


class TaskHistoryItemResponse(BaseModel):
    """列表摘要白名单；时间使用独立规范文本，不改变旧详情的序列化契约。"""

    id: UUID
    kind: str
    category: HistoryCategory
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    error_code: str | None
    retry_of_task_id: UUID | None


class TaskHistoryPageResponse(BaseModel):
    """只读分页响应；仅提供有证据的双向入口，不公开载荷或总页数。"""

    items: list[TaskHistoryItemResponse]
    next_cursor: str | None
    previous_cursor: str | None
    server_time: str
    filter_timezone: str
    background_failed_count: int


def _history_problem(*, cursor: bool = False) -> ApiProblem:
    """统一无载荷422，禁止把过滤输入、游标或底层异常放入问题详情。"""
    return ApiProblem(
        422,
        "task_history_cursor_invalid" if cursor else "task_history_filter_invalid",
        "Invalid task history query",
        "The task history query is invalid.",
    )


def parse_task_history_query(
    *,
    scope: str = "business",
    kind: str | None = None,
    status: str | None = None,
    created_from_date: str | None = None,
    created_to_date: str | None = None,
    limit: str = "20",
    cursor: str | None = None,
) -> TaskHistoryQuery:
    """收窄HTTP文本，日期和类型组合继续由应用规范化函数负责。

    Args:
        scope: 精确用途范围；其他筛选同共享查询值类型。
        limit: 仅允许1至100的规范ASCII十进制文本。
        cursor: 不透明分页输入，进入密钥读取前拒绝空值或超长输入。
    Returns:
        尚未执行日期和组合校验的只读查询。
    Raises:
        ApiProblem: 格式非法的固定422，无原始输入回显。
    """
    if re.fullmatch(r"[1-9][0-9]?|100", limit) is None:
        raise _history_problem()
    if cursor is not None and (not cursor or len(cursor) > 2048):
        raise _history_problem(cursor=True)
    return TaskHistoryQuery(
        scope, kind, status, created_from_date, created_to_date, int(limit), cursor
    )


def task_history_response(page: TaskHistoryPage) -> TaskHistoryPageResponse:
    """显式投影九个安全字段，保留null并将所有时间转为六位微秒UTC Z。

    Args:
        page: 已通过用户隔离与只读查询的不可变应用结果。
    Returns:
        独立列表响应，不包含步骤、正文、执行租约或事件游标。
    """
    return TaskHistoryPageResponse(
        items=[
            TaskHistoryItemResponse(
                id=item.id,
                kind=item.kind,
                category=item.category,
                status=item.status.value,
                created_at=canonical_utc_timestamp(item.created_at),
                started_at=canonical_utc_timestamp(item.started_at)
                if item.started_at is not None
                else None,
                finished_at=canonical_utc_timestamp(item.finished_at)
                if item.finished_at is not None
                else None,
                error_code=item.error_code,
                retry_of_task_id=item.retry_of_task_id,
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
        previous_cursor=page.previous_cursor,
        server_time=canonical_utc_timestamp(page.server_time),
        filter_timezone=page.filter_timezone,
        background_failed_count=page.background_failed_count,
    )


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
    started_at: datetime | None
    finished_at: datetime | None


class TaskResponse(BaseModel):
    """公开任务快照，刻意不包含内部租约与执行器字段。"""

    id: UUID
    kind: str
    status: str
    retry_of_task_id: UUID | None
    error_code: str | None
    event_cursor: str
    steps: list[StepResponse]
    calendar_restore_proposal_id: UUID | None = None


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
        event_cursor=str(snapshot.event_cursor),
        calendar_restore_proposal_id=snapshot.calendar_restore_proposal_id,
        steps=[
            StepResponse(
                id=item.id,
                sequence=item.sequence,
                name=item.name,
                status=item.status,
                output_summary=item.output_summary,
                error_code=item.error_code,
                started_at=item.started_at,
                finished_at=item.finished_at,
            )
            for item in snapshot.steps
        ],
    )


def _missing_task() -> ApiProblem:
    """统一隐藏跨用户资源，返回与不存在相同的 404。"""
    return ApiProblem(404, "task_not_found", "Task not found", "The requested task was not found.")


def _event_cursor(*, header_value: str | None, query_value: str | None) -> int | None:
    """安全选择 SSE 重放游标，优先浏览器自动重连附加的标准 Header。

    主动创建的 ``EventSource`` 无法设置 ``Last-Event-ID`` Header，因此允许其把已知
    PostgreSQL 游标放在查询参数；一旦浏览器自动重连，Header 反映更近的已接收事件，必须
    优先以免静态查询参数倒退回放。两种输入只接受规范非负十进制字符串，即 ``0`` 或不以
    ``0`` 开头的数字；这保证浏览器、API 与 PostgreSQL BIGINT 之间不存在多个等价文本
    表示，并避免把不可信值交给持久事件查询。范围检查先比较固定的 BIGINT 十进制文本长度
    与字典序，防止超长 Header 在 Python ``int`` 转换前触发其内置的大整数长度限制。
    """
    value = header_value if header_value is not None else query_value
    if value is None:
        return None
    if CANONICAL_EVENT_CURSOR.fullmatch(value) is None:
        raise ApiProblem(
            422,
            "invalid_last_event_id",
            "Invalid event cursor",
            "Last-Event-ID must be a canonical non-negative decimal integer.",
        )
    if len(value) > len(POSTGRESQL_BIGINT_MAX_TEXT) or (
        len(value) == len(POSTGRESQL_BIGINT_MAX_TEXT) and value > POSTGRESQL_BIGINT_MAX_TEXT
    ):
        raise ApiProblem(
            422,
            "invalid_last_event_id",
            "Invalid event cursor",
            "Last-Event-ID exceeds the supported event ID range.",
        )
    return int(value)


def build_tasks_router() -> APIRouter:
    """构造路由；实际用例由组合根经依赖注入提供。"""
    from ai_employee.api.deps import (
        get_cancel_task_use_case,
        get_create_task_use_case,
        get_get_task_use_case,
        get_retry_task_use_case,
        get_task_history_use_case,
    )

    router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

    @router.get("", response_model=TaskHistoryPageResponse)
    async def list_task_history(
        authenticated: CurrentSession,
        response: Response,
        use_case: Annotated[ListTaskHistoryUseCase, Depends(get_task_history_use_case)],
        scope: str = "business",
        kind: str | None = None,
        status: str | None = None,
        created_from_date: str | None = None,
        created_to_date: str | None = None,
        limit: str = "20",
        cursor: str | None = None,
    ) -> TaskHistoryPageResponse:
        """认证后只读当前用户摘要；异常由统一Problem处理器保留no-store。"""
        response.headers["Cache-Control"] = "no-store"
        query = parse_task_history_query(
            scope=scope,
            kind=kind,
            status=status,
            created_from_date=created_from_date,
            created_to_date=created_to_date,
            limit=limit,
            cursor=cursor,
        )
        try:
            page = await use_case.execute(
                user_id=authenticated.user.id,
                timezone=authenticated.user.timezone,
                query=query,
            )
        except TaskHistoryFilterError:
            raise _history_problem() from None
        except TaskHistoryCursorError:
            raise _history_problem(cursor=True) from None
        return task_history_response(page)

    @router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=CreateTaskResponse)
    async def create_task(
        payload: CreateTaskRequest,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[CreateTaskUseCase, Depends(get_create_task_use_case)],
        idempotency_key: IdempotencyKeyHeader = None,
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
        query_last_event_id: Annotated[
            str | None, Query(alias="last_event_id", max_length=20)
        ] = None,
    ):
        """建立事件流；认证后先验证任务存在，避免跨用户订阅。"""
        if await use_case.execute(task_id=task_id, user_id=authenticated.user.id) is None:
            raise _missing_task()
        stream = request.app.state.task_event_stream
        last_event_id = _event_cursor(
            header_value=request.headers.get("Last-Event-ID"),
            query_value=query_last_event_id,
        )
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
        idempotency_key: IdempotencyKeyHeader = None,
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
