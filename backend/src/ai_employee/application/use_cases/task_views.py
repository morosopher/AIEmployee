"""定义任务读取、取消与重试的应用用例边界。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_employee.domain.tasks import JsonValue, TaskStatus


@dataclass(frozen=True, slots=True)
class TaskStepSnapshot:
    """供 REST 与 SSE 快照共享的步骤视图，仅含可公开恢复时间线的字段。"""

    id: UUID
    sequence: int
    name: str
    status: str
    output_summary: dict[str, JsonValue] | None
    error_code: str | None
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """任务当前状态、步骤及同一 PostgreSQL 读取视图的事件游标。"""

    id: UUID
    kind: str
    status: TaskStatus
    retry_of_task_id: UUID | None
    input_payload: dict[str, JsonValue]
    error_code: str | None
    event_cursor: int
    steps: tuple[TaskStepSnapshot, ...]
    # 仅成功恢复准备任务可公开已验证的本人提案UUID，绝不传播任意result_payload。
    calendar_restore_proposal_id: UUID | None = None


class TaskViewStore(Protocol):
    """定义用户范围内任务视图与命令所需端口。"""

    async def get(self, *, task_id: UUID, user_id: UUID) -> TaskSnapshot | None:
        """返回用户拥有的任务快照，跨用户与不存在均返回空。"""

    async def cancel(self, *, task_id: UUID, user_id: UUID, now: datetime) -> TaskSnapshot | None:
        """取消可取消任务并返回新快照。"""

    async def retry(
        self, *, task_id: UUID, user_id: UUID, idempotency_key: str, now: datetime
    ) -> TaskSnapshot | None:
        """从失败任务创建幂等 replacement；非失败状态抛领域冲突。"""


class GetTaskUseCase:
    """读取用户隔离的任务快照。"""

    def __init__(self, store: TaskViewStore) -> None:
        """注入任务视图端口。"""
        self._store = store

    async def execute(self, *, task_id: UUID, user_id: UUID) -> TaskSnapshot | None:
        """按用户范围读取任务。"""
        return await self._store.get(task_id=task_id, user_id=user_id)


class CancelTaskUseCase:
    """取消仍可协作停止的任务。"""

    def __init__(self, store: TaskViewStore) -> None:
        """注入任务命令端口。"""
        self._store = store

    async def execute(self, *, task_id: UUID, user_id: UUID, now: datetime) -> TaskSnapshot | None:
        """执行用户范围的取消命令。"""
        return await self._store.cancel(task_id=task_id, user_id=user_id, now=now)


class RetryTaskUseCase:
    """从失败任务创建不可变的新执行尝试。"""

    def __init__(self, store: TaskViewStore) -> None:
        """注入任务重试端口。"""
        self._store = store

    async def execute(
        self, *, task_id: UUID, user_id: UUID, idempotency_key: str, now: datetime
    ) -> TaskSnapshot | None:
        """创建或复用同键 replacement，绝不重开原终态任务。"""
        return await self._store.retry(
            task_id=task_id, user_id=user_id, idempotency_key=idempotency_key, now=now
        )
