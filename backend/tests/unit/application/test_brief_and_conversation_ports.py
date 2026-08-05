"""验证简报与对话用例仅依赖应用层持久化端口。"""

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from ai_employee.application.use_cases.briefs import PersistDailyBriefUseCase
from ai_employee.application.use_cases.conversations import CreateConversationMessageUseCase
from ai_employee.application.use_cases.tasks import CreateTaskResult
from ai_employee.domain.briefs import DailyBriefContent


class BriefStoreFake:
    """记录简报写入请求，确保单元测试不需要 SQLAlchemy。"""

    def __init__(self, brief_id: UUID) -> None:
        """保存 Fake 返回的稳定简报标识。"""
        self.brief_id = brief_id
        self.calls: list[dict[str, object]] = []

    async def persist(self, **values: object) -> UUID:
        """记录用例交给端口的全部持久化意图。"""
        self.calls.append(values)
        return self.brief_id


class ConversationStoreFake:
    """记录对话消息与任务的一次性创建请求。"""

    def __init__(self, result: CreateTaskResult) -> None:
        """保存 Fake 返回的幂等任务结果。"""
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def create_message_and_task(self, **values: object) -> CreateTaskResult:
        """记录原子创建意图并返回预设结果。"""
        self.calls.append(values)
        return self.result


def _factory(store: object):
    """构造与真实 Repository factory 形状相同的异步事务上下文。"""

    @asynccontextmanager
    async def transaction():
        yield store

    return transaction


@pytest.mark.asyncio
async def test_persist_daily_brief_delegates_versioned_write_to_application_port() -> None:
    """简报用例把完整输入交给端口，不接触 ORM、查询或事务 SDK。"""
    brief_id = uuid4()
    store = BriefStoreFake(brief_id)
    content = DailyBriefContent(
        local_date=date(2026, 8, 4),
        source_cutoff=datetime(2026, 8, 4, tzinfo=UTC),
        completeness="complete",
        headline="Synthetic brief",
    )

    result = await PersistDailyBriefUseCase(_factory(store)).execute(
        user_id=uuid4(), task_id=uuid4(), content=content, markdown="# Synthetic"
    )

    assert result == brief_id
    assert len(store.calls) == 1
    assert store.calls[0]["content"] is content


@pytest.mark.asyncio
async def test_create_conversation_message_delegates_atomic_message_and_task_to_port() -> None:
    """对话用例把归属、幂等键及消息交给同一应用端口事务。"""
    task_id = uuid4()
    store = ConversationStoreFake(CreateTaskResult(task_id=task_id))
    user_id = uuid4()
    conversation_id = uuid4()

    result = await CreateConversationMessageUseCase(_factory(store)).execute(
        user_id=user_id,
        conversation_id=conversation_id,
        content_markdown="生成今日简报",
        client_request_id="request-1",
    )

    assert result.task_id == task_id
    assert store.calls == [
        {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "content_markdown": "生成今日简报",
            "idempotency_key": f"conversation:{user_id}:request-1",
        }
    ]
