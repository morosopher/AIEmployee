"""实现以 PostgreSQL 审计事件为事实来源的可重放任务 SSE。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sse_starlette import EventSourceResponse, ServerSentEvent

from ai_employee.application.use_cases.task_views import TaskSnapshot
from ai_employee.domain.tasks import JsonValue
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.events.publisher import TaskEventPublisher
from ai_employee.infrastructure.observability.metrics import Metrics

HEARTBEAT_SECONDS = 15
REDIS_OPERATION_TIMEOUT_SECONDS = 1.0
REDIS_PUBSUB_POLL_SECONDS = 0.25

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DurableTaskEvent:
    """从审计表读取并可直接装入 SSE 信封的持久事件。"""

    id: int
    task_id: UUID
    event: str
    occurred_at: datetime
    step_id: UUID | None
    payload: dict[str, JsonValue]


class TaskEventSubscription(Protocol):
    """定义 Redis 唤醒订阅的可替换资源边界。"""

    async def open(self) -> None:
        """建立订阅，使其从首次 PostgreSQL 回放前开始接收后续通知。"""

    async def wait(self, timeout_seconds: float) -> bool:
        """等待通知或超时；返回值只表示是否有唤醒信号。"""

    async def aclose(self) -> None:
        """释放订阅与连接，不遗留 Redis Pub/Sub 资源。"""


class RedisTaskEventSubscription:
    """订阅单个任务频道，只把 Redis 消息当作重新读取 PostgreSQL 的提示。"""

    def __init__(self, redis_url: str, task_id: UUID) -> None:
        """保存惰性连接配置，避免构造 SSE 对象时占用 Redis 连接。"""
        self._redis_url = redis_url
        self._task_id = task_id
        self._client: Redis | None = None
        self._pubsub: PubSub | None = None

    async def open(self) -> None:
        """在固定预算内连接并订阅 Redis，避免初始回放与实时通知之间丢信号。"""
        client = Redis.from_url(self._redis_url, decode_responses=True)
        pubsub = client.pubsub()
        try:
            async with asyncio.timeout(REDIS_OPERATION_TIMEOUT_SECONDS):
                await pubsub.subscribe(TaskEventPublisher.channel(self._task_id))
        except BaseException:
            await self._close_resources(pubsub=pubsub, client=client)
            raise
        self._client = client
        self._pubsub = pubsub

    async def wait(self, timeout_seconds: float) -> bool:
        """等待频道消息；忽略订阅确认且仍完整等待一个心跳周期。"""
        pubsub = self._pubsub
        if pubsub is None:
            raise RuntimeError("task event subscription is not open")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            read_timeout = min(remaining, REDIS_PUBSUB_POLL_SECONDS)
            async with asyncio.timeout(REDIS_OPERATION_TIMEOUT_SECONDS):
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=read_timeout,
                )
            if message is not None:
                return True

    async def aclose(self) -> None:
        """关闭 Pub/Sub 后再关闭客户端，确保断线不泄漏专用连接。"""
        pubsub = self._pubsub
        client = self._client
        self._pubsub = None
        self._client = None
        await self._close_resources(pubsub=pubsub, client=client)

    async def _close_resources(self, *, pubsub: PubSub | None, client: Redis | None) -> None:
        """在有限时间内释放 Redis 资源，清理失败不破坏 PostgreSQL 轮询降级。

        Args:
            pubsub: 可能已建立的 Pub/Sub 专用连接。
            client: 创建 Pub/Sub 的 Redis 客户端。
        """
        if pubsub is not None:
            try:
                async with asyncio.timeout(REDIS_OPERATION_TIMEOUT_SECONDS):
                    await pubsub.aclose()
            except (OSError, RedisError, TimeoutError):
                logger.warning("task event subscription pubsub cleanup unavailable")
        if client is not None:
            try:
                async with asyncio.timeout(REDIS_OPERATION_TIMEOUT_SECONDS):
                    await client.aclose()
            except (OSError, RedisError, TimeoutError):
                logger.warning("task event subscription client cleanup unavailable")


class PollingTaskEventSubscription:
    """Redis 不可用时的空订阅，保留固定 PostgreSQL 心跳轮询恢复能力。"""

    async def open(self) -> None:
        """轮询不依赖外部资源，因此无需建立连接。"""

    async def wait(self, timeout_seconds: float) -> bool:
        """等待一个完整心跳周期后返回超时，触发 PostgreSQL 兜底查询。"""
        await asyncio.sleep(timeout_seconds)
        return False

    async def aclose(self) -> None:
        """轮询订阅没有外部资源需要关闭。"""


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
                step_id=_step_id(row.event_metadata),
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
        """在同一可重复读快照读取任务与最大审计游标，避免并发跳过事件。"""
        from ai_employee.infrastructure.db.repositories.task_views import SqlAlchemyTaskViewStore

        if not isinstance(self._task_store, SqlAlchemyTaskViewStore):
            raise TypeError("task event store requires SQL task store")
        async with self._session_factory.begin() as session:
            # 首条 SQL 在任何快照读取前设置隔离级别，确保任务、步骤和最大事件来自同一视图。
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            snapshot = await self._task_store._get_in_session(
                session, task_id=task_id, user_id=user_id
            )
            maximum = await session.scalar(
                select(func.max(AuditEventModel.id)).where(
                    AuditEventModel.task_id == task_id, AuditEventModel.user_id == user_id
                )
            )
        return snapshot, maximum or 0


def _step_id(metadata: dict[str, JsonValue]) -> UUID | None:
    """从兼容的审计元数据提取步骤标识，非法值绝不破坏整个 SSE 流。"""
    candidate = metadata.get("step_id")
    if not isinstance(candidate, str):
        return None
    try:
        return UUID(candidate)
    except ValueError:
        return None


def _public_event_type(audit_event_type: str) -> str:
    """把旧版持久审计命名明确映射为前端稳定 SSE 合同事件类型。"""
    if audit_event_type.startswith("task."):
        return "task.status_changed"
    if audit_event_type == "approval.requested":
        return "approval.required"
    if audit_event_type == "approval.expired":
        return "approval.resolved"
    return audit_event_type


def _public_event_payload(event: DurableTaskEvent) -> dict[str, JsonValue]:
    """复制审计元数据并补齐旧事件映射需要的公开语义，不改写历史审计记录。"""
    if event.event == "approval.expired":
        return {
            **event.payload,
            "status": "expired",
            "reason": "approval_expired",
        }
    return event.payload


def _event(event: DurableTaskEvent) -> ServerSentEvent:
    """把数据库行映射为协议规定的 SSE ID、sequence 和标准化事件类型。"""
    event_type = _public_event_type(event.event)
    return ServerSentEvent(
        id=str(event.id),
        event=event_type,
        data=json.dumps(
            {
                "id": str(event.id),
                "task_id": str(event.task_id),
                "sequence": str(event.id),
                "event": event_type,
                "occurred_at": event.occurred_at.isoformat(),
                "step_id": str(event.step_id) if event.step_id else None,
                "payload": _public_event_payload(event),
            }
        ),
    )


class TaskEventStream:
    """先订阅、后回放的 SSE 生成器；Redis 丢失时仍定期轮询持久事实。"""

    def __init__(
        self,
        store: TaskEventStore,
        *,
        redis_url: str | None = None,
        subscription_factory: Callable[[UUID], TaskEventSubscription] | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        """注入 PostgreSQL 事实库和可替换通知源。

        ``subscription_factory`` 仅决定何时唤醒读取，不能提供业务事件；测试以无通知订阅
        验证轮询恢复，生产默认绑定任务专属 Redis Pub/Sub 频道。
        """
        self._store = store
        if subscription_factory is not None:
            self._subscription_factory = subscription_factory
        elif redis_url is not None:
            self._subscription_factory = lambda task_id: RedisTaskEventSubscription(redis_url, task_id)
        else:
            self._subscription_factory = lambda _: PollingTaskEventSubscription()
        self._metrics = metrics

    async def events(
        self, *, task_id: UUID, user_id: UUID, last_event_id: int | None
    ) -> AsyncIterator[ServerSentEvent]:
        """产出重放、缺口快照、通知唤醒和 15 秒 PostgreSQL 心跳恢复事件。"""
        cursor = max(last_event_id or 0, 0)
        subscription = self._subscription_factory(task_id)
        if self._metrics is not None:
            self._metrics.record_sse_connection(connected=True)
        try:
            try:
                await subscription.open()
            except (OSError, RedisError, TimeoutError):
                await subscription.aclose()
                subscription = PollingTaskEventSubscription()
                await subscription.open()

            oldest = await self._store.oldest_event_id(task_id=task_id, user_id=user_id)
            if oldest is None:
                snapshot, _ = await self._store.snapshot_and_max(task_id=task_id, user_id=user_id)
                if snapshot is None:
                    return
            elif last_event_id is not None and last_event_id < oldest - 1:
                snapshot, cursor = await self._store.snapshot_and_max(task_id=task_id, user_id=user_id)
                if snapshot is None:
                    return
                yield _snapshot_event(task_id=task_id, snapshot=snapshot, event_id=cursor)

            initial_rows = await self._store.events_after(
                task_id=task_id, user_id=user_id, after_id=cursor
            )
            for row in initial_rows:
                if row.id > cursor:
                    cursor = row.id
                    yield _event(row)
            while True:
                timed_out = False
                try:
                    notified = await subscription.wait(HEARTBEAT_SECONDS)
                    timed_out = not notified
                except (OSError, RedisError):
                    await subscription.aclose()
                    subscription = PollingTaskEventSubscription()
                    await subscription.open()
                    timed_out = True

                # 每个通知或心跳 tick 都先从 PostgreSQL 重读遗漏事实，再决定是否发送 heartbeat。
                rows = await self._store.events_after(
                    task_id=task_id, user_id=user_id, after_id=cursor
                )
                for row in rows:
                    if row.id > cursor:
                        cursor = row.id
                        yield _event(row)
                if timed_out:
                    yield ServerSentEvent(event="heartbeat", data={})
        finally:
            await subscription.aclose()
            if self._metrics is not None:
                self._metrics.record_sse_connection(connected=False)

    def response(
        self, *, task_id: UUID, user_id: UUID, last_event_id: int | None
    ) -> EventSourceResponse:
        """按 SSE 标准禁用响应缓冲并暴露异步生成器。"""
        return EventSourceResponse(
            self.events(task_id=task_id, user_id=user_id, last_event_id=last_event_id),
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )


def _snapshot_event(*, task_id: UUID, snapshot: TaskSnapshot, event_id: int) -> ServerSentEvent:
    """构造缺口恢复快照，使用同一数据库一致视图中的最大审计 ID 作为游标。"""
    return ServerSentEvent(
        id=str(event_id),
        event="task.snapshot",
        data=json.dumps(
            {
                "id": str(event_id),
                "task_id": str(task_id),
                "sequence": str(event_id),
                "event": "task.snapshot",
                "occurred_at": datetime.now(UTC).isoformat(),
                "step_id": None,
                "payload": {
                    "id": str(snapshot.id),
                    "kind": snapshot.kind,
                    "status": snapshot.status.value,
                    "retry_of_task_id": (
                        str(snapshot.retry_of_task_id) if snapshot.retry_of_task_id else None
                    ),
                    "error_code": snapshot.error_code,
                    "event_cursor": str(snapshot.event_cursor),
                    "steps": [
                        {
                            "id": str(step.id),
                            "sequence": step.sequence,
                            "name": step.name,
                            "status": step.status,
                            "output_summary": step.output_summary,
                            "error_code": step.error_code,
                            "started_at": step.started_at.isoformat() if step.started_at else None,
                            "finished_at": step.finished_at.isoformat() if step.finished_at else None,
                        }
                        for step in snapshot.steps
                    ],
                },
            }
        ),
    )
