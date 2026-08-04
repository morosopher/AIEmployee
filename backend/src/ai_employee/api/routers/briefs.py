"""暴露当前用户每日简报的只读与异步生成接口。"""

from datetime import date
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel

from ai_employee.api.deps import (
    ApiProblem,
    CsrfProtectedSession,
    CurrentSession,
    get_create_task_use_case,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.infrastructure.db.models.briefs import DailyBriefItemModel
from ai_employee.infrastructure.db.repositories.briefs import SqlAlchemyBriefRepository


class BriefResponse(BaseModel):
    """简报公开字段白名单，避免输出内部 ORM 数据。"""
    id: UUID
    local_date: date
    version: int
    task_id: UUID
    completeness: str
    headline: str
    markdown: str
    warnings: list[str]
    items: list[dict[str, object]] = []


def _brief_response(value: object, items: tuple[DailyBriefItemModel, ...] = ()) -> BriefResponse:
    """把 ORM 简报投影为稳定 API Schema。"""
    response = BriefResponse.model_validate(value, from_attributes=True)
    return response.model_copy(
        update={
            "items": [
                {
                    "position": item.position,
                    "section": item.section,
                    "priority": item.priority,
                    "title": item.title,
                    "body_markdown": item.body_markdown,
                    "source_refs": item.source_refs,
                    "suggested_action_kind": item.suggested_action_kind,
                }
                for item in items
            ]
        }
    )


def build_briefs_router() -> APIRouter:
    """构建所有简报资源路由，长任务仅创建 TaskRun。"""
    router = APIRouter(prefix="/api/v1/briefs", tags=["briefs"])

    @router.get("/today", response_model=BriefResponse)
    async def today(authenticated: CurrentSession, request: Request) -> BriefResponse:
        """按用户时区的当天获取最新成功或部分成功简报。"""
        from datetime import UTC, datetime
        from zoneinfo import ZoneInfo
        async with request.app.state.auth_session_factory() as session:
            repository = SqlAlchemyBriefRepository(session)
            value = await repository.latest(user_id=authenticated.user.id, local_date=datetime.now(UTC).astimezone(ZoneInfo(authenticated.user.timezone)).date())
            items = await repository.items(brief_id=value.id) if value else ()
        if value is None:
            raise ApiProblem(404, "brief_not_found", "Brief not found", "No brief is available for this date.")
        return _brief_response(value, items)

    @router.get("", response_model=list[BriefResponse])
    async def list_briefs(local_date: Annotated[date, Query()], authenticated: CurrentSession, request: Request) -> list[BriefResponse]:
        """列出当前用户指定本地日期的版本，按版本倒序。"""
        async with request.app.state.auth_session_factory() as session:
            repository = SqlAlchemyBriefRepository(session)
            values = await repository.list_for_date(user_id=authenticated.user.id, local_date=local_date)
            rendered = [(value, await repository.items(brief_id=value.id)) for value in values]
        return [_brief_response(value, items) for value, items in rendered]

    @router.get("/{brief_id}", response_model=BriefResponse)
    async def get_brief(brief_id: UUID, authenticated: CurrentSession, request: Request) -> BriefResponse:
        """读取本人拥有的一个简报，跨用户与不存在均返回 404。"""
        async with request.app.state.auth_session_factory() as session:
            repository = SqlAlchemyBriefRepository(session)
            value = await repository.get(user_id=authenticated.user.id, brief_id=brief_id)
            items = await repository.items(brief_id=value.id) if value else ()
        if value is None:
            raise ApiProblem(404, "brief_not_found", "Brief not found", "The requested brief was not found.")
        return _brief_response(value, items)

    @router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
    async def generate(authenticated: CsrfProtectedSession, tasks: Annotated[CreateTaskUseCase, Depends(get_create_task_use_case)]) -> dict[str, UUID]:
        """创建手动刷新任务；幂等键每次不同，因此会生成下一版本。"""
        result = await tasks.execute(user_id=authenticated.user.id, kind="daily_brief", input_payload={"schedule_kind": "manual"}, idempotency_key=f"daily_brief:{authenticated.user.id}:manual:{uuid4()}")
        return {"task_id": result.task_id}
    return router
