"""任务 SSE 的 PostgreSQL 重放、恢复和通知边界集成测试。"""

import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, time
from uuid import UUID

import pytest
from sse_starlette import ServerSentEvent

from ai_employee.api.sse import TaskEventStore, TaskEventStream
from ai_employee.application.use_cases.task_views import RetryTaskUseCase
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.task_views import (
    PostgresQueuedTaskDispatcher,
    SqlAlchemyTaskViewStore,
)
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepositoryFactory
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.events.publisher import TaskEventPublisher


class _TimedOutSubscription:
    """模拟 Redis 通知丢失后的超时轮询，不提供任何事件正文。"""

    async def open(self) -> None:
        """模拟可用但本次未收到通知的订阅连接。"""

    async def wait(self, timeout_seconds: float) -> bool:
        """立即返回超时，驱动测试中的下一次 PostgreSQL 兜底查询。"""
        del timeout_seconds
        return False

    async def aclose(self) -> None:
        """保持与生产订阅相同的可关闭资源边界。"""


class _BlockingTimedOutSubscription:
    """在测试明确推进时才超时，模拟已丢失的通知后的下一次心跳。"""

    def __init__(self) -> None:
        """初始化订阅已进入等待状态和测试推进门闩。"""
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def open(self) -> None:
        """该假订阅不需要外部连接。"""

    async def wait(self, timeout_seconds: float) -> bool:
        """等待测试插入审计事实后触发超时返回。"""
        del timeout_seconds
        self.waiting.set()
        await self.release.wait()
        return False

    async def aclose(self) -> None:
        """该假订阅没有需要关闭的外部资源。"""


def _event_payload(event: ServerSentEvent) -> dict[str, object]:
    """解析 SSE 编码中的 JSON 信封，保留真实 SSE 序列化路径。"""
    encoded = event.encode().decode("utf-8")
    data_line = next(line for line in encoded.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


@pytest.fixture
async def sse_store(
    database_url: str,
) -> tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]]:
    """构造真实 PostgreSQL 任务事件库及当前用户范围的 SSE 流。"""
    session_factory = build_session_factory(database_url)
    async with session_factory.begin() as session:
        user = UserModel(
            email="sse-owner@example.com",
            display_name="SSE owner",
            password_hash="synthetic-password-hash",
            timezone="Asia/Shanghai",
            locale="zh-CN",
            brief_time=time(8, 0),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        user_id = user.id

    def build_stream() -> TaskEventStream:
        """为每个断言提供关闭状态独立的 stream 实例。"""
        task_store = SqlAlchemyTaskViewStore(session_factory)
        return TaskEventStream(
            TaskEventStore(session_factory, task_store),
            subscription_factory=lambda _: _TimedOutSubscription(),
        )

    yield session_factory, user_id, build_stream
    await session_factory.dispose()


async def _create_task(
    session_factory: ManagedAsyncSessionMaker, user_id: UUID, *, status: str = "queued"
) -> UUID:
    """写入不含业务副作用的合成任务，供审计重放测试引用。"""
    async with session_factory.begin() as session:
        task = TaskRunModel(
            user_id=user_id,
            kind="fake_write",
            status=status,
            idempotency_key=f"sse-task-{user_id}-{status}",
            input_payload={},
        )
        session.add(task)
        await session.flush()
        return task.id


async def _append_event(
    session_factory: ManagedAsyncSessionMaker,
    user_id: UUID,
    task_id: UUID,
    event_type: str,
) -> int:
    """追加可审计合成事件并返回数据库分配的事实 ID。"""
    async with session_factory.begin() as session:
        event = AuditEventModel(
            user_id=user_id,
            task_id=task_id,
            event_type=event_type,
            actor_type="system",
            actor_id=None,
            event_metadata={"status": "running"},
        )
        session.add(event)
        await session.flush()
        return event.id


@pytest.mark.asyncio
async def test_task_event_route_is_registered() -> None:
    """任务事件流必须使用规定的可重放 SSE 地址。"""
    from ai_employee.main import create_app

    paths = {
        route.path
        for included in create_app().routes
        for route in getattr(getattr(included, "original_router", included), "routes", (included,))
        if hasattr(route, "path")
    }

    assert "/api/v1/tasks/{task_id}/events" in paths


@pytest.mark.asyncio
async def test_last_event_id_replays_only_later_durable_events(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
) -> None:
    """重连游标只重放其后的审计事实，SSE ID 与 sequence 均等于审计 ID。"""
    session_factory, user_id, build_stream = sse_store
    task_id = await _create_task(session_factory, user_id)
    first_id = await _append_event(session_factory, user_id, task_id, "task.queued")
    second_id = await _append_event(session_factory, user_id, task_id, "task.running")

    events = build_stream().events(task_id=task_id, user_id=user_id, last_event_id=first_id)
    payload = _event_payload(await anext(events))
    await events.aclose()

    assert payload["id"] == second_id
    assert payload["sequence"] == second_id
    assert payload["event"] == "task.status_changed"


@pytest.mark.asyncio
async def test_create_and_retry_queue_events_replay_the_queued_snapshot(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
) -> None:
    """创建和重试都持久化 QUEUED 转换，重放末项必须与当前快照一致。"""
    session_factory, user_id, build_stream = sse_store
    task_store = SqlAlchemyTaskViewStore(session_factory)
    created = await CreateTaskUseCase(
        SqlAlchemyTaskRepositoryFactory(session_factory),
        PostgresQueuedTaskDispatcher(session_factory),
    ).execute(
        user_id=user_id,
        kind="fake_write",
        input_payload={},
        idempotency_key="sse-created-task",
    )
    async with session_factory.begin() as session:
        failed = TaskRunModel(
            user_id=user_id,
            kind="fake_write",
            status=TaskStatus.FAILED.value,
            idempotency_key="sse-failed-task",
            input_payload={},
        )
        session.add(failed)
        await session.flush()
        failed_id = failed.id
    retried = await RetryTaskUseCase(task_store).execute(
        task_id=failed_id,
        user_id=user_id,
        idempotency_key="sse-retry-task",
        now=datetime.now(UTC),
    )
    assert retried is not None

    for task_id in (created.task_id, retried.id):
        snapshot = await task_store.get(task_id=task_id, user_id=user_id)
        assert snapshot is not None
        events = build_stream().events(task_id=task_id, user_id=user_id, last_event_id=None)
        replayed = [_event_payload(await anext(events)), _event_payload(await anext(events))]
        await events.aclose()

        assert replayed[-1].get("event") == "task.status_changed"
        assert replayed[0].get("payload") == (
            {"kind": "fake_write", "status": "created"}
            if task_id == created.task_id
            else {"retry_of_task_id": str(failed_id), "status": "created"}
        )
        assert replayed[-1]["payload"] == {"status": snapshot.status.value}


@pytest.mark.asyncio
async def test_expired_approval_replays_as_canonical_approval_resolution(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
) -> None:
    """过期审批保留原始审计类型，但 SSE 必须公开稳定的解决事件语义。"""
    session_factory, user_id, build_stream = sse_store
    task_id = await _create_task(session_factory, user_id)
    event_id = await _append_event(session_factory, user_id, task_id, "approval.expired")

    events = build_stream().events(task_id=task_id, user_id=user_id, last_event_id=None)
    payload = _event_payload(await anext(events))
    await events.aclose()

    assert payload["id"] == event_id
    assert payload["event"] == "approval.resolved"
    assert payload["payload"] == {
        "status": "expired",
        "reason": "approval_expired",
    }


@pytest.mark.asyncio
async def test_retention_gap_emits_current_snapshot_at_current_audit_id(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
) -> None:
    """过期游标先接收同一数据库快照中的任务状态和最大审计游标。"""
    session_factory, user_id, build_stream = sse_store
    task_id = await _create_task(session_factory, user_id, status="running")
    event_id = await _append_event(session_factory, user_id, task_id, "task.running")

    events = build_stream().events(task_id=task_id, user_id=user_id, last_event_id=0)
    payload = _event_payload(await anext(events))
    await events.aclose()

    assert payload["event"] == "task.snapshot"
    assert payload["id"] == event_id
    assert payload["sequence"] == event_id
    assert payload["payload"] == {
        "id": str(task_id),
        "kind": "fake_write",
        "status": "running",
        "steps": [],
    }


@pytest.mark.asyncio
async def test_dropped_pubsub_notification_is_recovered_by_next_postgres_tick(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
) -> None:
    """Redis 未通知时，下一轮 PostgreSQL 查询仍发现持久审计事实。"""
    session_factory, user_id, _ = sse_store
    task_id = await _create_task(session_factory, user_id)
    subscription = _BlockingTimedOutSubscription()
    task_store = SqlAlchemyTaskViewStore(session_factory)
    stream = TaskEventStream(
        TaskEventStore(session_factory, task_store),
        subscription_factory=lambda _: subscription,
    )
    events = stream.events(task_id=task_id, user_id=user_id, last_event_id=None)
    pending_event = asyncio.create_task(anext(events))
    await asyncio.wait_for(subscription.waiting.wait(), timeout=1)
    event_id = await _append_event(session_factory, user_id, task_id, "task.running")
    subscription.release.set()
    payload = _event_payload(await asyncio.wait_for(pending_event, timeout=1))
    await events.aclose()

    assert payload["id"] == event_id
    assert payload["event"] == "task.status_changed"


@pytest.mark.asyncio
async def test_redis_pubsub_notification_wakes_task_stream(
    database_url: str,
) -> None:
    """事务提交后的审计事实必须自行唤醒在线 SSE，而非等待心跳轮询。"""
    redis_url = os.environ["TEST_REDIS_URL"]
    session_factory = build_session_factory(
        database_url,
        task_event_publisher=TaskEventPublisher(redis_url),
    )
    async with session_factory.begin() as session:
        user = UserModel(
            email="sse-notify-owner@example.com",
            display_name="SSE notify owner",
            password_hash="synthetic-password-hash",
            timezone="Asia/Shanghai",
            locale="zh-CN",
            brief_time=time(8, 0),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        user_id = user.id
    task_id = await _create_task(session_factory, user_id)
    task_store = SqlAlchemyTaskViewStore(session_factory)
    stream = TaskEventStream(TaskEventStore(session_factory, task_store), redis_url=redis_url)
    events = stream.events(task_id=task_id, user_id=user_id, last_event_id=None)
    pending_event = asyncio.create_task(anext(events))
    await asyncio.sleep(0.1)
    event_id = await _append_event(session_factory, user_id, task_id, "task.running")
    payload = _event_payload(await asyncio.wait_for(pending_event, timeout=2))
    await events.aclose()
    await session_factory.dispose()

    assert payload["id"] == event_id
    assert payload["event"] == "task.status_changed"


@pytest.mark.asyncio
async def test_heartbeat_is_ephemeral_and_not_persisted(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
) -> None:
    """无遗漏事件的轮询只发送临时 heartbeat，绝不追加审计行。"""
    session_factory, user_id, build_stream = sse_store
    task_id = await _create_task(session_factory, user_id)
    stream = build_stream()
    before = await stream._store.events_after(task_id=task_id, user_id=user_id, after_id=0)
    events = stream.events(task_id=task_id, user_id=user_id, last_event_id=None)
    heartbeat = await anext(events)
    await events.aclose()
    after = await stream._store.events_after(task_id=task_id, user_id=user_id, after_id=0)

    assert heartbeat.event == "heartbeat"
    assert before == after == ()
