"""实现以 PostgreSQL 审计事件为事实来源的可重放任务 SSE。"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sse_starlette import EventSourceResponse, ServerSentEvent

from ai_employee.application.use_cases.task_views import TaskSnapshot
from ai_employee.domain.tasks import JsonValue
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

HEARTBEAT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class DurableTaskEvent:
    """从审计表读取并可直接装入 SSE 信封的持久事件。"""

    id: int
    task_id: UUID
    event: str
    occurred_at: datetime
    step_id: UUID | None
    payload: dict[str, JsonValue]


class TaskEventStore:
    """只从 PostgreSQL 读取用户隔离的任务事件与快照。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker, task_store: object) -> None:
        """保存会话工厂及只读任务快照端口。"""
        self._session_factory = session_factory
        self._task_store = task_store

    async def events_after(
        self, *, task_id: UUID, user_id: UUID, after_id: int
    ) -> tuple[DurableTaskEvent, ...]:
        """读取大于游标的有序审计行；用户条件是第二道隔离防线。"""
        async with self._session_factory() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(AuditEventModel)
                        .where(
                            AuditEventModel.task_id == task_id,
                            AuditEventModel.user_id == user_id,
                            AuditEventModel.id > after_id,
                        )
                        .order_by(AuditEventModel.id)
                    )
                ).all()
            )
        return tuple(
            DurableTaskEvent(
                id=row.id,
                task_id=task_id,
                event=row.event_type,
                occurred_at=row.created_at,
                step_id=None,
                payload=row.event_metadata,
            )
            for row in rows
        )

    async def oldest_event_id(self, *, task_id: UUID, user_id: UUID) -> int | None:
        """返回当前保留区间起点，用于判断 Last-Event-ID 回放间隙。"""
        async with self._session_factory() as session:
            return await session.scalar(
                select(func.min(AuditEventModel.id)).where(
                    AuditEventModel.task_id == task_id, AuditEventModel.user_id == user_id
                )
            )

    async def snapshot_and_max(
        self, *, task_id: UUID, user_id: UUID
    ) -> tuple[TaskSnapshot | None, int]:
        """在同一事务读快照与最大事件游标，避免 gap 快照之后重复旧事件。"""
        from ai_employee.infrastructure.db.repositories.task_views import SqlAlchemyTaskViewStore

        if not isinstance(self._task_store, SqlAlchemyTaskViewStore):
            raise TypeError("task event store requires SQL task store")
        async with self._session_factory.begin() as session:
            snapshot = await self._task_store._get_in_session(
                session, task_id=task_id, user_id=user_id
            )
            maximum = await session.scalar(
                select(func.max(AuditEventModel.id)).where(
                    AuditEventModel.task_id == task_id, AuditEventModel.user_id == user_id
                )
            )
        return snapshot, maximum or 0


def _event(event: DurableTaskEvent) -> ServerSentEvent:
    """把数据库行映射为协议规定的 SSE ID 和等值 sequence。"""
    return ServerSentEvent(
        id=str(event.id),
        event=event.event,
        data={
            "id": event.id,
            "task_id": str(event.task_id),
            "sequence": event.id,
            "event": event.event,
            "occurred_at": event.occurred_at.isoformat(),
            "step_id": str(event.step_id) if event.step_id else None,
            "payload": event.payload,
        },
    )


class TaskEventStream:
    """先回放再轮询持久事件的 SSE 生成器；通知丢失不影响恢复。"""

    def __init__(self, store: TaskEventStore) -> None:
        """注入 PostgreSQL 事实读取端口。"""
        self._store = store

    async def events(
        self, *, task_id: UUID, user_id: UUID, last_event_id: int | None
    ) -> AsyncIterator[ServerSentEvent]:
        """产出重放、gap 快照及 15 秒轮询后的 heartbeat，不持久化心跳。"""
        cursor = max(last_event_id or 0, 0)
        oldest = await self._store.oldest_event_id(task_id=task_id, user_id=user_id)
        if oldest is None:
            snapshot, _ = await self._store.snapshot_and_max(task_id=task_id, user_id=user_id)
            if snapshot is None:
                return
        elif last_event_id is not None and last_event_id < oldest - 1:
            snapshot, cursor = await self._store.snapshot_and_max(task_id=task_id, user_id=user_id)
            if snapshot is None:
                return
            yield ServerSentEvent(
                id=str(cursor),
                event="task.snapshot",
                data={
                    "id": cursor,
                    "task_id": str(task_id),
                    "sequence": cursor,
                    "event": "task.snapshot",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "step_id": None,
                    "payload": {
                        "id": str(snapshot.id),
                        "kind": snapshot.kind,
                        "status": snapshot.status.value,
                        "steps": [
                            {
                                "id": str(step.id),
                                "sequence": step.sequence,
                                "name": step.name,
                                "status": step.status,
                                "output_summary": step.output_summary,
                            }
                            for step in snapshot.steps
                        ],
                    },
                },
            )
        while True:
            rows = await self._store.events_after(task_id=task_id, user_id=user_id, after_id=cursor)
            for row in rows:
                if row.id > cursor:
                    cursor = row.id
                    yield _event(row)
            await asyncio.sleep(HEARTBEAT_SECONDS)
            yield ServerSentEvent(event="heartbeat", data={})

    def response(
        self, *, task_id: UUID, user_id: UUID, last_event_id: int | None
    ) -> EventSourceResponse:
        """按 SSE 标准禁用响应缓冲并暴露异步生成器。"""
        return EventSourceResponse(
            self.events(task_id=task_id, user_id=user_id, last_event_id=last_event_id),
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )
