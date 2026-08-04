"""提供 CSRF 防护的异步隐私删除请求接口。"""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict

from ai_employee.api.deps import ApiProblem, CsrfProtectedSession
from ai_employee.application.use_cases.privacy import (
    RequestAllDataDeletionUseCase,
    RequestSourceCacheDeletionUseCase,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase

IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)]


class DeleteAllDataRequest(BaseModel):
    """限制全数据删除的确认输入，拒绝额外字段避免意外记录用户内容。"""

    model_config = ConfigDict(extra="forbid")
    confirmation: str


class PrivacyTaskResponse(BaseModel):
    """返回异步删除任务标识，不暴露删除进度或历史内容。"""

    task_id: str
    status: str


def build_privacy_router() -> APIRouter:
    """构建隐私路由，所有实际删除均转为经过 Outbox 的 Worker 任务。"""
    router = APIRouter(prefix="/api/v1/privacy", tags=["privacy"])

    def creator(request: Request) -> CreateTaskUseCase:
        """从受控组合根取得任务创建用例。"""
        return request.app.state.create_task_use_case

    def require_key(value: str | None) -> str:
        """统一验证客户端重放保护键。"""
        if value is None:
            raise ApiProblem(422, "idempotency_key_required", "Idempotency key required", "An Idempotency-Key header is required.")
        return value

    @router.post("/source-cache-deletions", status_code=status.HTTP_202_ACCEPTED, response_model=PrivacyTaskResponse)
    async def request_source_cache_deletion(
        authenticated: CsrfProtectedSession,
        task_creator: Annotated[CreateTaskUseCase, Depends(creator)],
        idempotency_key: IdempotencyKeyHeader = None,
    ) -> PrivacyTaskResponse:
        """请求删除可恢复来源缓存，保留登录凭据和对话工作区。"""
        result = await RequestSourceCacheDeletionUseCase(task_creator).execute(
            user_id=authenticated.user.id, idempotency_key=require_key(idempotency_key)
        )
        return PrivacyTaskResponse(task_id=str(result.task_id), status=result.status.value)

    @router.post("/all-data-deletions", status_code=status.HTTP_202_ACCEPTED, response_model=PrivacyTaskResponse)
    async def request_all_data_deletion(
        payload: DeleteAllDataRequest,
        authenticated: CsrfProtectedSession,
        task_creator: Annotated[CreateTaskUseCase, Depends(creator)],
        idempotency_key: IdempotencyKeyHeader = None,
    ) -> PrivacyTaskResponse:
        """验证精确确认短语后请求全数据删除，确认文本不会写入 TaskRun。"""
        if payload.confirmation != "DELETE ALL DATA":
            raise ApiProblem(422, "deletion_confirmation_invalid", "Invalid deletion confirmation", "The confirmation must exactly match DELETE ALL DATA.")
        result = await RequestAllDataDeletionUseCase(task_creator).execute(
            user_id=authenticated.user.id,
            idempotency_key=require_key(idempotency_key),
            request_id=uuid4().hex,
        )
        return PrivacyTaskResponse(task_id=str(result.task_id), status=result.status.value)

    return router
