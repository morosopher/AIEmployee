"""提供 M1 范围内的用户对话资源。"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ai_employee.api.deps import (
    ApiProblem,
    CsrfProtectedSession,
    CurrentSession,
    get_create_task_use_case,
)
from ai_employee.application.use_cases.conversations import (
    ConversationNotFoundError,
    CreateConversationMessageUseCase,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.infrastructure.db.models.briefs import ConversationModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories.conversations import (
    SqlAlchemyConversationMessageStoreFactory,
    SqlAlchemyConversationRepository,
)


class ConversationResponse(BaseModel):
    """会话列表公开摘要。"""
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class MessageResponse(BaseModel):
    """会话消息公开内容。"""
    id: UUID
    role: str
    content_markdown: str
    task_id: UUID | None
    created_at: datetime


class MessageRequest(BaseModel):
    """用户发送的一条简短消息及客户端幂等键。"""
    content_markdown: str = Field(min_length=1, max_length=20_000)
    client_request_id: str = Field(min_length=1, max_length=255)


def build_conversations_router() -> APIRouter:
    """构建会话 CRUD 与异步回复任务入口。"""
    router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

    @router.get("", response_model=list[ConversationResponse])
    async def list_conversations(authenticated: CurrentSession, request: Request) -> list[ConversationResponse]:
        """列出当前用户自己的会话。"""
        async with request.app.state.auth_session_factory() as session:
            values = await SqlAlchemyConversationRepository(session).list(user_id=authenticated.user.id)
        return [ConversationResponse.model_validate(value, from_attributes=True) for value in values]

    @router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
    async def create_conversation(authenticated: CsrfProtectedSession, request: Request) -> ConversationResponse:
        """创建空会话，标题保持合成默认值且不推断隐私内容。"""
        async with request.app.state.auth_session_factory.begin() as session:
            # 认证事务早于路由事务；锁后复核使创建与隐私最终提交有唯一先后顺序。
            active = await session.scalar(
                select(UserModel.is_active)
                .where(
                    UserModel.id == authenticated.user.id,
                )
                .with_for_update()
            )
            if active is not True:
                raise ApiProblem(
                    401,
                    "authentication_required",
                    "Authentication required",
                    "Sign in to continue.",
                )
            value = ConversationModel(user_id=authenticated.user.id, title="New conversation")
            session.add(value)
            await session.flush()
            return ConversationResponse.model_validate(value, from_attributes=True)

    @router.get("/{conversation_id}")
    async def get_conversation(conversation_id: UUID, authenticated: CurrentSession, request: Request) -> dict[str, object]:
        """读取本人会话及按创建时间排序的消息。"""
        async with request.app.state.auth_session_factory() as session:
            repository = SqlAlchemyConversationRepository(session)
            value = await repository.get(user_id=authenticated.user.id, conversation_id=conversation_id)
            messages = await repository.messages(user_id=authenticated.user.id, conversation_id=conversation_id) if value else ()
        if value is None: raise ApiProblem(404, "conversation_not_found", "Conversation not found", "The requested conversation was not found.")
        return {"conversation": ConversationResponse.model_validate(value, from_attributes=True).model_dump(), "messages": [MessageResponse.model_validate(message, from_attributes=True).model_dump() for message in messages]}

    @router.post("/{conversation_id}/messages", status_code=status.HTTP_202_ACCEPTED)
    async def create_message(conversation_id: UUID, payload: MessageRequest, authenticated: CsrfProtectedSession, request: Request, tasks: Annotated[CreateTaskUseCase, Depends(get_create_task_use_case)]) -> dict[str, UUID]:
        """先持久化用户消息，再创建可恢复回复任务；重复 client id 复用任务。"""
        try:
            result = await CreateConversationMessageUseCase(
                SqlAlchemyConversationMessageStoreFactory(
                    request.app.state.auth_session_factory
                )
            ).execute(
                user_id=authenticated.user.id,
                conversation_id=conversation_id,
                content_markdown=payload.content_markdown,
                client_request_id=payload.client_request_id,
            )
        except ConversationNotFoundError:
            raise ApiProblem(404, "conversation_not_found", "Conversation not found", "The requested conversation was not found.") from None
        # TaskRun 已与消息一同提交；再次按相同键调用只会投递既有任务，不会制造第二份事实。
        await tasks.execute(user_id=authenticated.user.id, kind="conversation.respond", input_payload={"conversation_id": str(conversation_id), "content": payload.content_markdown}, idempotency_key=f"conversation:{authenticated.user.id}:{payload.client_request_id}")
        return {"task_id": result.task_id}

    @router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_conversation(conversation_id: UUID, authenticated: CsrfProtectedSession, request: Request) -> Response:
        """仅删除本人会话与级联消息，并追加不含内容的删除审计。"""
        async with request.app.state.auth_session_factory.begin() as session:
            # 同一锁覆盖 lookup/DELETE/audit，旧请求不能在匿名化之后追加删除审计。
            active = await session.scalar(
                select(UserModel.is_active)
                .where(
                    UserModel.id == authenticated.user.id,
                )
                .with_for_update()
            )
            if active is not True:
                raise ApiProblem(
                    401,
                    "authentication_required",
                    "Authentication required",
                    "Sign in to continue.",
                )
            value = await SqlAlchemyConversationRepository(session).get(user_id=authenticated.user.id, conversation_id=conversation_id)
            if value is None: raise ApiProblem(404, "conversation_not_found", "Conversation not found", "The requested conversation was not found.")
            await session.delete(value)
            session.add(AuditEventModel(user_id=authenticated.user.id, task_id=None, event_type="conversation.deleted", actor_type="user", actor_id=str(authenticated.user.id), event_metadata={"conversation_id": str(conversation_id)}))
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return router
