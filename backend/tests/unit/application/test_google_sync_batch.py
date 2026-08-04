"""验证手动 Google 同步的两个任务创建共享同一事务边界。"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from ai_employee.application.use_cases.tasks import (
    CreateTaskBatchItem,
    CreateTaskResult,
    CreateTaskUseCase,
)
from ai_employee.domain.tasks import TaskStatus


@dataclass
class RecordingRepository:
    """模拟可回滚事务存储，用于证明第二次持久化失败不会提交第一项。"""

    fail_on_second: bool = False
    pending: list[str] = field(default_factory=list)
    committed: list[str] = field(default_factory=list)

    async def create_with_outbox(
        self, *, user_id: UUID, kind: str, input_payload: dict, idempotency_key: str
    ) -> CreateTaskResult:
        """记录一项待提交任务，并按测试开关在 Calendar 项模拟持久化失败。"""
        del user_id, input_payload
        self.pending.append(kind)
        if self.fail_on_second and kind == "sync_calendar":
            raise RuntimeError("simulated second persistence failure")
        return CreateTaskResult(uuid4())

    async def create_many_with_outbox(
        self, *, user_id: UUID, items: tuple[CreateTaskBatchItem, ...]
    ) -> tuple[CreateTaskResult, ...]:
        """在同一待提交列表中顺序写入，异常由上下文管理器整体回滚。"""
        return tuple(
            [
                await self.create_with_outbox(
                    user_id=user_id,
                    kind=item.kind,
                    input_payload=item.input_payload,
                    idempotency_key=item.idempotency_key,
                )
                for item in items
            ]
        )


class RecordingFactory:
    """以显式 commit/rollback 行为模拟 Repository factory。"""

    def __init__(self, repository: RecordingRepository) -> None:
        """保存单测可观测的事务对象。"""
        self.repository = repository

    @asynccontextmanager
    async def __call__(self):
        """异常时清空 pending，正常返回才复制为 committed。"""
        try:
            yield self.repository
        except Exception:
            self.repository.pending.clear()
            raise
        else:
            self.repository.committed.extend(self.repository.pending)
            self.repository.pending.clear()


class RecordingDispatcher:
    """记录提交后投递，防止回滚路径仍产生外部副作用。"""

    def __init__(self) -> None:
        """初始化空的已投递任务列表。"""
        self.dispatched: list[UUID] = []

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """记录投递并返回稳定创建状态。"""
        self.dispatched.append(task_id)
        return TaskStatus.CREATED


@pytest.mark.asyncio
async def test_batch_creation_rolls_back_both_tasks_when_second_persistence_fails() -> None:
    """Calendar 写入失败时 Gmail 任务、审计和 Outbox 也不能提交或投递。"""
    repository = RecordingRepository(fail_on_second=True)
    dispatcher = RecordingDispatcher()
    use_case = CreateTaskUseCase(RecordingFactory(repository), dispatcher)

    with pytest.raises(RuntimeError, match="second persistence failure"):
        await use_case.execute_many(
            user_id=uuid4(),
            items=(
                CreateTaskBatchItem("sync_gmail", {"connection_id": "one"}, "sync:gmail"),
                CreateTaskBatchItem("sync_calendar", {"connection_id": "one"}, "sync:calendar"),
            ),
        )

    assert repository.committed == []
    assert dispatcher.dispatched == []
