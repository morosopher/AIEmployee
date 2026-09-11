"""任务 SSE 的 PostgreSQL 重放、恢复和通知边界集成测试。"""

import asyncio
import json
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import ServerSentEvent

from ai_employee.api.sse import (
    RedisTaskEventSubscription,
    TaskEventStore,
    TaskEventStream,
    TaskEventSubscription,
)
from ai_employee.application.use_cases.task_views import RetryTaskUseCase
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel, TaskStepModel
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
async def test_task_snapshot_uses_one_repeatable_read_view_during_concurrent_update(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照读取中并发提交的状态事件不得让旧任务状态携带新游标。

    第一次任务读取后，另一会话提交状态更新及其审计事实。PostgreSQL 默认
    ``READ COMMITTED`` 会让后续最大审计 ID 查询看见该事件，导致客户端以一个并未包含在
    快照内的游标重连并永久跳过状态变化。读取事务必须固定为 ``REPEATABLE READ``，使整个
    快照要么看见提交前事实，要么在下一次 GET 中整体看见提交后事实。
    """
    session_factory, user_id, _ = sse_store
    task_id = await _create_task(session_factory, user_id)
    original_scalar = AsyncSession.scalar

    async def scalar_after_concurrent_commit(
        session: AsyncSession, *args: Any, **kwargs: Any
    ) -> Any:
        """在读取任务行后精确插入一次并发提交，复现三次读取的时间窗口。"""
        result = await original_scalar(session, *args, **kwargs)
        if session.info.pop("commit_after_task_read", False):
            async with session_factory.begin() as writer:
                await writer.execute(
                    update(TaskRunModel)
                    .where(TaskRunModel.id == task_id)
                    .values(status="running")
                )
                writer.add(
                    AuditEventModel(
                        user_id=user_id,
                        task_id=task_id,
                        event_type="task.running",
                        actor_type="system",
                        actor_id=None,
                        event_metadata={"status": "running"},
                    )
                )
        return result

    class InterleavingSessionFactory:
        """只为本回归用例在读取事务开始前标记一次受控并发提交。"""

        @asynccontextmanager
        async def begin(self) -> Any:
            """沿用真实事务上下文，并让首次任务查询触发另一个会话提交。"""
            async with session_factory.begin() as session:
                session.info["commit_after_task_read"] = True
                yield session

    monkeypatch.setattr(AsyncSession, "scalar", scalar_after_concurrent_commit)
    store = SqlAlchemyTaskViewStore(cast(ManagedAsyncSessionMaker, InterleavingSessionFactory()))

    snapshot = await store.get(task_id=task_id, user_id=user_id)

    assert snapshot is not None
    assert snapshot.status is TaskStatus.QUEUED
    assert snapshot.event_cursor == 0


class _ObservedRedisTaskEventSubscription:
    """包装真实 Redis 订阅，在流完成初始回放并开始等待通知时打开测试门闩。"""

    def __init__(self, delegate: TaskEventSubscription) -> None:
        """保存生产订阅实现及仅用于观察其等待边界的事件。"""
        self._delegate = delegate
        self.waiting_for_notification = asyncio.Event()

    async def open(self) -> None:
        """按生产路径建立真实 Redis 订阅。"""
        await self._delegate.open()

    async def wait(self, timeout_seconds: float) -> bool:
        """标记已进入等待，再委托生产订阅读取实际 Redis 通知。"""
        self.waiting_for_notification.set()
        return await self._delegate.wait(timeout_seconds)

    async def aclose(self) -> None:
        """沿用生产订阅的资源释放语义。"""
        await self._delegate.aclose()


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

    assert payload["id"] == str(second_id)
    assert payload["sequence"] == str(second_id)
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

    assert payload["id"] == str(event_id)
    assert payload["event"] == "approval.resolved"
    assert payload["payload"] == {
        "status": "expired",
        "reason": "approval_expired",
    }


@pytest.mark.asyncio
async def test_retention_gap_emits_current_snapshot_at_current_audit_id(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
) -> None:
    """过期游标接收同一快照的任务状态与最大审计游标；普通任务的恢复结果必须为空。"""
    session_factory, user_id, build_stream = sse_store
    original_task_id = await _create_task(session_factory, user_id, status="failed")
    task_id = await _create_task(session_factory, user_id, status="running")
    event_id = await _append_event(session_factory, user_id, task_id, "task.running")
    async with session_factory.begin() as session:
        task = await session.get(TaskRunModel, task_id)
        assert task is not None
        task.retry_of_task_id = original_task_id
        task.error_code = "provider_temporarily_unavailable"
        task.input_payload = {"internal_only": "must-not-leak"}
        session.add(
            TaskStepModel(
                task_id=task_id,
                sequence=1,
                name="recoverable-step",
                kind="test",
                status="completed",
                input_summary={},
                started_at=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
                finished_at=datetime(2026, 8, 3, 0, 0, 2, tzinfo=UTC),
            )
        )

    events = build_stream().events(task_id=task_id, user_id=user_id, last_event_id=0)
    payload = _event_payload(await anext(events))
    await events.aclose()

    assert payload["event"] == "task.snapshot"
    assert payload["id"] == str(event_id)
    assert payload["sequence"] == str(event_id)
    assert payload["payload"] == {
        "id": str(task_id),
        "kind": "fake_write",
        "status": "running",
        "retry_of_task_id": str(original_task_id),
        "error_code": "provider_temporarily_unavailable",
        "event_cursor": str(event_id),
        "calendar_restore_proposal_id": None,
        "steps": [
            {
                "id": payload["payload"]["steps"][0]["id"],
                "sequence": 1,
                "name": "recoverable-step",
                "status": "completed",
                "output_summary": None,
                "error_code": None,
                "started_at": "2026-08-03T00:00:00+00:00",
                "finished_at": "2026-08-03T00:00:02+00:00",
            }
        ],
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

    assert payload["id"] == str(event_id)
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
    subscription = _ObservedRedisTaskEventSubscription(
        RedisTaskEventSubscription(redis_url, task_id)
    )
    stream = TaskEventStream(
        TaskEventStore(session_factory, task_store),
        subscription_factory=lambda _: subscription,
    )
    events = stream.events(task_id=task_id, user_id=user_id, last_event_id=None)
    pending_event = asyncio.create_task(anext(events))
    await asyncio.wait_for(subscription.waiting_for_notification.wait(), timeout=1)
    event_id = await _append_event(session_factory, user_id, task_id, "task.running")
    payload = _event_payload(await asyncio.wait_for(pending_event, timeout=2))
    await events.aclose()
    await session_factory.dispose()

    assert payload["id"] == str(event_id)
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


@pytest.mark.asyncio
async def test_m1_step_events_preserve_legacy_step_fields(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
) -> None:
    """真实持久步骤经线上 SSE 保留旧 reducer 所需字段，同时移除内容与任意嵌套元数据。

    与前端共用合成线上契约；仅将数据库实际分配的 task/audit ID 代入期望，步骤自己的
    sequence 固定为 7，证明不能用信封中的审计游标替代步骤排序。
    """
    fixture = Path(__file__).parents[2] / "contract/fixtures/task_step_events.json"
    expected = json.loads(fixture.read_text(encoding="utf-8"))
    session_factory, user_id, build_stream = sse_store
    task_id = await _create_task(session_factory, user_id, status="running")
    async with session_factory.begin() as session:
        for item in expected:
            metadata = {
                **item["payload"],
                "subject": "synthetic-private-subject",
                "unexpected": {"body_text": "synthetic-private-body"},
            }
            summary = metadata["output_summary"]
            if summary is not None:
                metadata["output_summary"] = {
                    **summary,
                    "body_text": "synthetic-private-body",
                    "attendees": ["person@example.test"],
                    "unexpected": {"subject": "synthetic-private-subject"},
                }
            row = AuditEventModel(
                user_id=user_id,
                task_id=task_id,
                event_type=item["event"],
                actor_type="system",
                actor_id=None,
                created_at=datetime.fromisoformat(item["occurred_at"]),
                event_metadata=metadata,
            )
            session.add(row)
            await session.flush()
            item.update(id=str(row.id), sequence=str(row.id), task_id=str(task_id))
    events = build_stream().events(task_id=task_id, user_id=user_id, last_event_id=None)
    try:
        actual = [_event_payload(await anext(events)) for _ in expected]
    finally:
        await events.aclose()
    assert actual == expected


@pytest.mark.asyncio
async def test_m2_events_replay_ordered_content_free_and_ignore_duplicate_notifications(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
) -> None:
    """M2 与 OAuth 恢复事件保留顺序；未知扩展也只能携带封闭的无内容字段。"""
    session_factory, user_id, build_stream = sse_store
    task_id = await _create_task(session_factory, user_id)
    names = (
        "action.submitted",
        "approval.invalidated",
        "tool.claimed",
        "tool.oauth_refresh_required",
        "tool.oauth_refresh_confirmed",
        "tool.reconciling",
        "tool.needs_attention",
        "tool.manually_resolved",
        "future.extension",
    )
    ids = []
    async with session_factory.begin() as session:
        for name in names:
            row = AuditEventModel(
                user_id=user_id,
                task_id=task_id,
                event_type=name,
                actor_type="system",
                actor_id=None,
                event_metadata={
                    "provider": "google",
                    "action": "mail.send",
                    "version": 1,
                    "status": "needs_attention",
                    "reconciliation_attempt_count": 4,
                    "name": "load_sources",
                    "sequence": 7,
                    "output_summary": {"status": "succeeded", "attempt_count": 1},
                    "subject": "synthetic private subject",
                    "body": "synthetic private body",
                    "attendees": ["person@example.test"],
                    "unexpected": {"nested": "synthetic private body"},
                    "provider_url": "https://provider.example.test/private",
                    "prompt": "private",
                },
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
    events = build_stream().events(task_id=task_id, user_id=user_id, last_event_id=None)
    replayed = [_event_payload(await anext(events)) for _ in names]
    assert (await anext(events)).event == "heartbeat"
    await events.aclose()
    assert [item["event"] for item in replayed] == list(names)
    assert [item["sequence"] for item in replayed] == [str(value) for value in ids]
    for item in replayed:
        assert item["payload"] == {
            "provider": "google",
            "action": "mail.send",
            "version": 1,
            "status": "needs_attention",
            "reconciliation_attempt_count": 4,
        }
    resumed = build_stream().events(task_id=task_id, user_id=user_id, last_event_id=ids[-2])
    assert _event_payload(await anext(resumed))["sequence"] == str(ids[-1])
    assert (await anext(resumed)).event == "heartbeat"
    await resumed.aclose()


@pytest.mark.asyncio
async def test_m2_snapshot_fallback_filters_step_summary_content(
    sse_store: tuple[ManagedAsyncSessionMaker, UUID, Callable[[], TaskEventStream]],
) -> None:
    """游标缺口恢复的可信任务快照也必须过滤历史 step summary，不能绕过事件白名单。"""
    factory, user_id, build_stream = sse_store
    earlier_task = await _create_task(factory, user_id, status="failed")
    await _append_event(factory, user_id, earlier_task, "task.queued")
    task_id = await _create_task(factory, user_id)
    event_id = await _append_event(factory, user_id, task_id, "tool.needs_attention")
    async with factory.begin() as session:
        await session.execute(
            update(TaskRunModel).where(TaskRunModel.id == task_id).values(kind="trusted_action")
        )
        session.add(
            TaskStepModel(
                task_id=task_id,
                sequence=1,
                name="trusted_action_graph",
                kind="trusted_action",
                status="running",
                input_summary={},
                output_summary={
                    "status": "needs_attention",
                    "subject": "synthetic-private-subject",
                    "body_text": "synthetic-private-body",
                },
            )
        )
    events = build_stream().events(task_id=task_id, user_id=user_id, last_event_id=0)
    payload = _event_payload(await anext(events))
    await events.aclose()
    assert payload["event"] == "task.snapshot" and payload["id"] == str(event_id)
    assert payload["payload"]["steps"][0]["output_summary"] == {"status": "needs_attention"}
