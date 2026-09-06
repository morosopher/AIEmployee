"""暴露统一可信动作快照与人工控制，不在请求进程执行供应商调用。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, field_validator

from ai_employee.api.deps import (
    ApiProblem,
    CsrfProtectedSession,
    CurrentSession,
    get_action_view_use_case,
    get_auth_clock,
    get_manual_resolution_use_case,
    get_request_action_reconciliation_use_case,
)
from ai_employee.application.use_cases.action_views import (
    ActionItemKind,
    ActionKind,
    ActionListPage,
    ActionProvider,
    ActionSnapshot,
    ActionViewUseCase,
    ManualResolutionUseCase,
    RequestActionReconciliationUseCase,
    canonical_cursor,
)
from ai_employee.application.use_cases.auth import Clock
from ai_employee.domain.errors import StateConflictError


class ManualResolutionRequest(BaseModel):
    """只接受已检查结果枚举与当前审计游标，禁止任何自由文本证据。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    resolution: Literal["confirmed_executed", "confirmed_not_executed"]
    task_version: str

    @field_validator("task_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        """复用数据库 CAS 的 BIGINT canonical decimal-string 规则。"""
        return canonical_cursor(value)


class ActionTaskResponse(BaseModel):
    """异步操作返回原权威 TaskRun 身份，不得冒用 ToolExecution UUID。"""

    task_id: UUID


class ManualResolutionResponse(ActionTaskResponse):
    """人工结论追加后返回新游标，供客户端重新读取统一快照。"""

    task_version: str


def _not_found() -> ApiProblem:
    """跨用户和不存在一律返回无内容 404。"""
    return ApiProblem(404, "action_not_found", "Action not found", "The action is unavailable.")


def _conflict() -> ApiProblem:
    """后到 mutation 不覆盖先提交事实，而是提示重读快照。"""
    return ApiProblem(
        409, "manual_resolution_conflict", "Action conflict", "Reload the current action."
    )


def build_actions_router() -> APIRouter:
    """组合当前 Cookie 会话、CSRF 与既有应用用例，所有响应禁用缓存。"""
    router = APIRouter(prefix="/api/v1/actions", tags=["actions"])

    @router.get("", response_model=ActionListPage)
    async def list_actions(
        authenticated: CurrentSession,
        response: Response,
        views: Annotated[ActionViewUseCase, Depends(get_action_view_use_case)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: Annotated[
            str | None,
            Query(
                pattern=r"^(editing|cancelled|stale|awaiting_approval|executing|sent|applied|created|queued|running|waiting_approval|retry_scheduled|reconciling|needs_attention|succeeded|failed|partially_succeeded)$"
            ),
        ] = None,
        item_kind: ActionItemKind | None = None,
        provider: ActionProvider | None = None,
        action: ActionKind | None = None,
    ) -> ActionListPage:
        """返回状态来自原本地对象/任务的无内容联合分页。"""
        response.headers["Cache-Control"] = "no-store"
        return await views.list_actions(
            user_id=authenticated.user.id,
            limit=limit,
            offset=offset,
            status=status,
            item_kind=item_kind,
            provider=provider,
            action=action,
        )

    @router.get("/{task_id}", response_model=ActionSnapshot)
    async def get_action(
        task_id: UUID,
        authenticated: CurrentSession,
        response: Response,
        views: Annotated[ActionViewUseCase, Depends(get_action_view_use_case)],
    ) -> ActionSnapshot:
        """读取同一 MVCC 视图的冻结预览、执行状态、时间线和游标。"""
        snapshot = await views.get(user_id=authenticated.user.id, task_id=task_id)
        if snapshot is None:
            raise _not_found()
        response.headers["Cache-Control"] = "no-store"
        return snapshot

    @router.post("/{task_id}/reconcile", status_code=202, response_model=ActionTaskResponse)
    async def reconcile_action(
        task_id: UUID,
        authenticated: CsrfProtectedSession,
        response: Response,
        views: Annotated[ActionViewUseCase, Depends(get_action_view_use_case)],
        use_case: Annotated[
            RequestActionReconciliationUseCase, Depends(get_request_action_reconciliation_use_case)
        ],
        clock: Annotated[Clock, Depends(get_auth_clock)],
    ) -> ActionTaskResponse:
        """重开原操作的只读核对；用例返回的执行 UUID 不参与 202 task_id 映射。"""
        if not await views.exists(user_id=authenticated.user.id, task_id=task_id):
            raise _not_found()
        try:
            await use_case.execute(user_id=authenticated.user.id, task_id=task_id, now=clock.now())
        except StateConflictError:
            raise _conflict() from None
        response.headers["Cache-Control"] = "no-store"
        return ActionTaskResponse(task_id=task_id)

    @router.post("/{task_id}/manual-resolution", response_model=ManualResolutionResponse)
    async def resolve_action(
        task_id: UUID,
        payload: ManualResolutionRequest,
        authenticated: CsrfProtectedSession,
        response: Response,
        views: Annotated[ActionViewUseCase, Depends(get_action_view_use_case)],
        use_case: Annotated[ManualResolutionUseCase, Depends(get_manual_resolution_use_case)],
        clock: Annotated[Clock, Depends(get_auth_clock)],
    ) -> ManualResolutionResponse:
        """用 canonical 游标 CAS 记录人工结论，不创建供应商写请求。"""
        if not await views.exists(user_id=authenticated.user.id, task_id=task_id):
            raise _not_found()
        try:
            version = await use_case.execute(
                user_id=authenticated.user.id,
                task_id=task_id,
                resolution=payload.resolution,
                task_version=payload.task_version,
                now=clock.now(),
            )
        except StateConflictError:
            raise _conflict() from None
        response.headers["Cache-Control"] = "no-store"
        return ManualResolutionResponse(task_id=task_id, task_version=version)

    return router
