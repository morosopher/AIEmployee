"""定义 M1 对话意图、原子消息创建用例及其应用端口。"""

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from ai_employee.application.use_cases.tasks import CreateTaskResult

UNSUPPORTED_RESPONSE = "当前 M1 仅支持生成或查看每日简报，不会执行外部写操作或通用规划。"


def unsupported_response() -> str:
    """返回不调用模型、工具或外部提供商的固定边界回复。"""
    return UNSUPPORTED_RESPONSE


class ConversationNotFoundError(Exception):
    """表示用户范围内不存在所请求会话。"""


class ConversationMessageStore(Protocol):
    """定义消息、任务和 Outbox 必须共同提交的原子持久化能力。"""

    async def create_message_and_task(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        content_markdown: str,
        idempotency_key: str,
    ) -> CreateTaskResult:
        """按用户范围和稳定键原子创建消息及回复任务。"""


class ConversationMessageStoreFactory(Protocol):
    """为一次消息创建提供提交或回滚受控的事务上下文。"""

    def __call__(self) -> AbstractAsyncContextManager[ConversationMessageStore]:
        """创建仅覆盖当前对话写入的事务上下文。"""


class CreateConversationMessageUseCase:
    """协调同一事务中的用户消息和 ``conversation.respond`` 任务创建。"""

    def __init__(self, stores: ConversationMessageStoreFactory) -> None:
        """注入不暴露 SQLAlchemy 或 ORM 的对话事务端口。"""
        self._stores = stores

    async def execute(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        content_markdown: str,
        client_request_id: str,
    ) -> CreateTaskResult:
        """用稳定客户端键幂等创建消息与 TaskRun，绝不留下半条消息。"""
        idempotency_key = f"conversation:{user_id}:{client_request_id}"
        async with self._stores() as store:
            return await store.create_message_and_task(
                user_id=user_id,
                conversation_id=conversation_id,
                content_markdown=content_markdown,
                idempotency_key=idempotency_key,
            )
