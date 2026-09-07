"""在真实 PostgreSQL 上验证 Outbox claim、投递确认与失败恢复。"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from ai_employee.application.use_cases.maintenance import ExpireSessionsUseCase
from ai_employee.application.use_cases.outbox import OutboxRelay
from ai_employee.application.use_cases.schedules import DispatchDueDailyBriefsUseCase
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl as ValidatedTestDatabaseUrl,
)
from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.repositories.identity import (
    SqlAlchemyActiveUserScheduleReader,
    SqlAlchemySessionMaintenanceRepositoryFactory,
)
from ai_employee.infrastructure.db.repositories.outbox import SqlAlchemyOutboxStore
from ai_employee.infrastructure.db.repositories.task_execution import (
    SqlAlchemyTaskExecutionStore,
)
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepositoryFactory
from ai_employee.infrastructure.db.session import build_session_factory

# 本模块创建多个独立异步 pool；先使用 Cycle 5 disposable regular head，再由专用
# registry 在每个测试结束时释放这些 pool，确保临时库 cleanup 与 anchor 复核前无遗留连接。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """把 Outbox 模块绑定到已完成 typed lifecycle 的 disposable regular head。

    该覆盖禁止本模块把 Step 33 传入的 roles-absent 0018 anchor 当作普通应用库；
    fixture 仅返回共享 helper 已验证的 URL，不自行迁移、授权或修改 anchor。
    """
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """覆盖会话级迁移入口，并复用 disposable regular head 的既有迁移事实。

    依赖参数保证数据库已经由 Cycle 5 helper 完成 role-bootstrap、迁移和 catalog
    验证；本 fixture 本身保持零写，避免放宽 typed migration guard 或触碰共享 anchor。
    """
    del cycle5_regular_database_url
    yield


class RecordingEnqueuer:
    """记录 Redis 边界收到的任务标识，并可注入一个安全失败。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.task_ids: list[UUID] = []

    async def enqueue(self, task_id: UUID) -> None:
        """记录投递；失败消息不包含 payload、凭据或用户数据。"""
        self.task_ids.append(task_id)
        if self.fail:
            raise RuntimeError("synthetic queue unavailable")


class RecordingTaskEventPublisher:
    """记录生命周期审计唤醒，并可注入不包含业务内容的发布故障。"""

    def __init__(self, *, fail: bool = False) -> None:
        """初始化发布记录与一次固定故障开关。"""
        self.fail = fail
        self.events: list[tuple[UUID, int]] = []

    async def publish(self, *, task_id: UUID, event_id: int) -> None:
        """只记录任务与已提交审计主键，禁止接收生命周期正文。"""
        self.events.append((task_id, event_id))
        if self.fail:
            raise RuntimeError("synthetic task event publisher unavailable")


class InspectingTaskEventPublisher:
    """在 publish 回调中证明 claim 已提交而 published_at 尚未伪造。"""

    def __init__(self, database_url: str, *, outbox_id: UUID) -> None:
        """保存隔离数据库与待观察 Outbox 行。"""
        self._database_url = database_url
        self._outbox_id = outbox_id
        self.events: list[tuple[UUID, int]] = []
        self.observed: list[tuple[datetime | None, datetime, UUID | None, str]] = []

    async def publish(self, *, task_id: UUID, event_id: int) -> None:
        """从新事务读取 claim、审计归属和发布确认前状态。"""
        session_factory = build_session_factory(self._database_url)
        try:
            async with session_factory() as session:
                outbox = await session.get(OutboxEventModel, self._outbox_id)
                audit = await session.get(AuditEventModel, event_id)
            assert outbox is not None
            assert audit is not None
            self.events.append((task_id, event_id))
            self.observed.append(
                (
                    outbox.published_at,
                    outbox.available_at,
                    audit.task_id,
                    audit.event_type,
                )
            )
        finally:
            await session_factory.dispose()


class InspectingEnqueuer:
    """在 enqueue 调用中从新事务读取 claim 已提交而 publish 尚未发生的状态。"""

    def __init__(self, database_url: str, *, expected_available_at: datetime) -> None:
        self._database_url = database_url
        self._expected_available_at = expected_available_at
        self.observed: list[tuple[str, datetime | None, datetime]] = []

    async def enqueue(self, task_id: UUID) -> None:
        """确认外部 I/O 之前 PostgreSQL 已经保存 QUEUED 与 60 秒 claim。"""
        session_factory = build_session_factory(self._database_url)
        try:
            async with session_factory() as session:
                task = await session.get(TaskRunModel, task_id)
                event = await session.scalar(
                    select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
                )
            assert task is not None
            assert event is not None
            assert event.available_at == self._expected_available_at
            self.observed.append((task.status, event.published_at, event.available_at))
        finally:
            await session_factory.dispose()


class CommitObservingDispatcher:
    """为 relay 测试保留初始 Outbox，同时证明创建事务已在新 Session 可见。"""

    def __init__(self, database_url: str) -> None:
        """保存隔离测试数据库地址，并初始化调用记录。"""
        self._database_url = database_url
        self.task_ids: list[UUID] = []

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """观察已提交 Task/Outbox 后返回当前状态，不执行队列投递。"""
        session_factory = build_session_factory(self._database_url)
        try:
            async with session_factory() as session:
                task = await session.get(TaskRunModel, task_id)
                event = await session.scalar(
                    select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
                )
            assert task is not None
            assert event is not None
            self.task_ids.append(task_id)
            return TaskStatus(task.status)
        finally:
            await session_factory.dispose()


class SyntheticProcessCrash(BaseException):
    """模拟 enqueue 开始后、结果持久化前进程被终止的不可捕获控制流。"""


class CrashOnceEnqueuer:
    """第一次投递模拟进程崩溃，claim 到期后的重复投递正常返回。"""

    def __init__(self) -> None:
        """初始化投递记录与一次性崩溃开关。"""
        self.task_ids: list[UUID] = []
        self._must_crash = True

    async def enqueue(self, task_id: UUID) -> None:
        """记录每次至少一次投递，并在首次调用抛出 BaseException。"""
        self.task_ids.append(task_id)
        if self._must_crash:
            self._must_crash = False
            raise SyntheticProcessCrash


def _user() -> UserModel:
    """构造仅含合成资料且显式使用 UTC 的活动用户。"""
    return UserModel(
        email=f"outbox-worker-{uuid4().hex}@example.com",
        display_name="Outbox Worker",
        password_hash=None,
        timezone="UTC",
        locale="zh-CN",
        brief_time=time(8, 0),
        is_active=True,
    )


async def _seed_lifecycle_outbox(
    database_url: str,
    *,
    topic: str,
    now: datetime,
) -> tuple[UUID, int, UUID]:
    """创建一条已提交审计及只含其主键的生命周期 Outbox 事实。

    Args:
        database_url: disposable regular head 的异步测试 DSN。
        topic: 待验证的固定生命周期 topic。
        now: Outbox 首次可投递时间。

    Returns:
        任务 ID、审计事件 ID 与 Outbox ID。
    """
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _user()
            session.add(user)
            await session.flush()
            task = TaskRunModel(
                user_id=user.id,
                kind="trusted_action",
                status=TaskStatus.CANCELLED.value,
                idempotency_key=f"lifecycle:{topic}:{user.id}",
                input_payload={},
            )
            session.add(task)
            await session.flush()
            audit = AuditEventModel(
                user_id=user.id,
                task_id=task.id,
                event_type=topic,
                actor_type="system",
                actor_id=None,
                event_metadata={},
            )
            session.add(audit)
            await session.flush()
            assert audit.id is not None
            event = OutboxEventModel(
                topic=topic,
                aggregate_id=task.id,
                deduplication_key=f"{topic}:{task.id}:synthetic",
                payload={"task_id": str(task.id), "audit_event_id": audit.id},
                available_at=now,
            )
            session.add(event)
            await session.flush()
            return task.id, audit.id, event.id
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "topic",
    (
        "approval.invalidated",
        "approval.expired",
        "task.cancelled",
        "tool.oauth_refresh_required",
        "tool.oauth_refresh_confirmed",
    ),
)
async def test_minute_relay_publishes_fixed_lifecycle_audit_event_then_marks_outbox(
    database_url: str,
    topic: str,
) -> None:
    """分钟 relay 只向事件端口发送 task_id 与已提交审计 ID，成功后才确认发布。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    task_id, audit_event_id, outbox_id = await _seed_lifecycle_outbox(
        database_url,
        topic=topic,
        now=now,
    )
    session_factory = build_session_factory(database_url)
    enqueuer = RecordingEnqueuer()
    publisher = InspectingTaskEventPublisher(database_url, outbox_id=outbox_id)
    try:
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=enqueuer,
            event_publisher=publisher,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        published = await relay.relay_once(limit=1)

        assert published == 1
        assert enqueuer.task_ids == []
        assert publisher.events == [(task_id, audit_event_id)]
        assert publisher.observed == [
            (
                None,
                now + timedelta(seconds=60),
                task_id,
                topic,
            )
        ]
        async with session_factory() as session:
            event = await session.get(OutboxEventModel, outbox_id)
        assert event is not None
        assert event.published_at == now
        assert event.attempt_count == 0
        assert event.last_error is None
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_lifecycle_publish_failure_remains_unpublished_with_safe_backoff(
    database_url: str,
) -> None:
    """事件发布故障不得伪造 published_at，并用固定错误码安排安全重试。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    task_id, audit_event_id, outbox_id = await _seed_lifecycle_outbox(
        database_url,
        topic="approval.expired",
        now=now,
    )
    session_factory = build_session_factory(database_url)
    enqueuer = RecordingEnqueuer()
    publisher = RecordingTaskEventPublisher(fail=True)
    try:
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=enqueuer,
            event_publisher=publisher,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        published = await relay.relay_once(limit=1)

        assert published == 0
        assert enqueuer.task_ids == []
        assert publisher.events == [(task_id, audit_event_id)]
        async with session_factory() as session:
            event = await session.get(OutboxEventModel, outbox_id)
        assert event is not None
        assert event.published_at is None
        assert event.attempt_count == 1
        assert event.last_error == "task_event_publish_failed"
        assert event.available_at == now + timedelta(seconds=5)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_immediate_dispatch_does_not_claim_lifecycle_event(database_url: str) -> None:
    """请求提交后的即时 dispatch 只处理 task.execute，不能抢走生命周期通知。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    task_id, _audit_event_id, outbox_id = await _seed_lifecycle_outbox(
        database_url,
        topic="task.cancelled",
        now=now,
    )
    session_factory = build_session_factory(database_url)
    enqueuer = RecordingEnqueuer()
    publisher = RecordingTaskEventPublisher()
    try:
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=enqueuer,
            event_publisher=publisher,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        status = await relay.dispatch(task_id)

        assert status is TaskStatus.CANCELLED
        assert enqueuer.task_ids == []
        assert publisher.events == []
        async with session_factory() as session:
            event = await session.get(OutboxEventModel, outbox_id)
        assert event is not None
        assert event.available_at == now
        assert event.published_at is None
        assert event.attempt_count == 0
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_minute_relay_never_claims_unsupported_topic(database_url: str) -> None:
    """固定 allowlist 之外的 topic 即使到期也保持原样且不触达任何外部端口。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    task_id, _audit_event_id, outbox_id = await _seed_lifecycle_outbox(
        database_url,
        topic="synthetic.unsupported",
        now=now,
    )
    session_factory = build_session_factory(database_url)
    enqueuer = RecordingEnqueuer()
    publisher = RecordingTaskEventPublisher()
    try:
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=enqueuer,
            event_publisher=publisher,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        assert await relay.relay_once(limit=1) == 0

        assert enqueuer.task_ids == []
        assert publisher.events == []
        async with session_factory() as session:
            event = await session.get(OutboxEventModel, outbox_id)
        assert event is not None
        assert event.aggregate_id == task_id
        assert event.available_at == now
        assert event.published_at is None
        assert event.attempt_count == 0
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_kind",
    ("json-array", "json-null", "audit-id-overflow"),
)
async def test_malformed_lifecycle_payload_does_not_rollback_batch_and_is_recoverable(
    database_url: str,
    malformed_kind: str,
) -> None:
    """非法 JSON 形状隔离自身且不阻塞同批正常事件，人工修复后可发布。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    current = now + timedelta(seconds=1)
    malformed_task_id, malformed_audit_id, malformed_outbox_id = (
        await _seed_lifecycle_outbox(
            database_url,
            topic="approval.invalidated",
            now=now,
        )
    )
    valid_task_id, valid_audit_id, valid_outbox_id = await _seed_lifecycle_outbox(
        database_url,
        topic="task.cancelled",
        now=now + timedelta(microseconds=1),
    )
    session_factory = build_session_factory(database_url)
    enqueuer = RecordingEnqueuer()
    publisher = RecordingTaskEventPublisher()
    try:
        async with session_factory.begin() as session:
            event = await session.get(
                OutboxEventModel,
                malformed_outbox_id,
                with_for_update=True,
            )
            assert event is not None
            if malformed_kind == "json-array":
                event.payload = ["synthetic-invalid-array"]  # type: ignore[assignment]
            elif malformed_kind == "json-null":
                event.payload = None  # type: ignore[assignment]
            else:
                event.payload = {
                    "task_id": str(malformed_task_id),
                    "audit_event_id": 2**100,
                }

        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=enqueuer,
            event_publisher=publisher,
            clock=lambda: current,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        assert await relay.relay_once(limit=2) == 1
        assert enqueuer.task_ids == []
        assert publisher.events == [(valid_task_id, valid_audit_id)]
        async with session_factory() as session:
            quarantined = await session.get(OutboxEventModel, malformed_outbox_id)
            published_valid = await session.get(OutboxEventModel, valid_outbox_id)
        assert quarantined is not None
        assert quarantined.published_at is None
        assert quarantined.attempt_count == 1
        assert quarantined.last_error == "invalid_outbox_payload"
        assert quarantined.available_at == current + timedelta(seconds=60)
        assert published_valid is not None
        assert published_valid.published_at == current
        assert published_valid.attempt_count == 0
        assert published_valid.last_error is None

        current += timedelta(seconds=59)
        assert await relay.relay_once(limit=2) == 0
        async with session_factory() as session:
            still_quarantined = await session.get(OutboxEventModel, malformed_outbox_id)
        assert still_quarantined is not None
        assert still_quarantined.attempt_count == 1
        assert still_quarantined.available_at == now + timedelta(seconds=61)
        assert publisher.events == [(valid_task_id, valid_audit_id)]

        async with session_factory.begin() as session:
            repairable = await session.get(
                OutboxEventModel,
                malformed_outbox_id,
                with_for_update=True,
            )
            assert repairable is not None
            repairable.payload = {
                "task_id": str(malformed_task_id),
                "audit_event_id": malformed_audit_id,
            }

        current += timedelta(seconds=1)
        assert await relay.relay_once(limit=2) == 1
        assert enqueuer.task_ids == []
        assert publisher.events == [
            (valid_task_id, valid_audit_id),
            (malformed_task_id, malformed_audit_id),
        ]
        async with session_factory() as session:
            recovered = await session.get(OutboxEventModel, malformed_outbox_id)
        assert recovered is not None
        assert recovered.published_at == now + timedelta(seconds=61)
        assert recovered.attempt_count == 1
        assert recovered.last_error is None
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_relay_claims_created_task_then_marks_event_published(database_url: str) -> None:
    """claim 先提交 CREATED→QUEUED，外部投递成功后再用短事务发布确认。"""
    session_factory = build_session_factory(database_url)
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    enqueuer = InspectingEnqueuer(
        database_url,
        expected_available_at=now + timedelta(seconds=60),
    )
    try:
        async with session_factory.begin() as session:
            user = _user()
            session.add(user)
            await session.flush()
            user_id = user.id

        setup_dispatcher = CommitObservingDispatcher(database_url)
        created = await CreateTaskUseCase(
            SqlAlchemyTaskRepositoryFactory(session_factory),
            dispatcher=setup_dispatcher,
        ).execute(
            user_id=user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-08-01"},
            idempotency_key="daily_brief:relay:2026-08-01:scheduled",
        )
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=enqueuer,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        published = await relay.relay_once(limit=100)

        assert published == 1
        assert setup_dispatcher.task_ids == [created.task_id]
        assert enqueuer.observed == [
            (
                TaskStatus.QUEUED.value,
                None,
                now + timedelta(seconds=60),
            )
        ]
        async with session_factory() as session:
            task = await session.get(TaskRunModel, created.task_id)
            event = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == created.task_id)
            )
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
        assert event is not None
        assert event.published_at == now
        assert event.attempt_count == 0
        assert event.last_error is None
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_failed_enqueue_leaves_claimed_task_queued_for_later_relay(
    database_url: str,
) -> None:
    """队列故障不回滚已提交任务，未发布事件只记录安全错误与指数退避。"""
    session_factory = build_session_factory(database_url)
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    enqueuer = RecordingEnqueuer(fail=True)
    try:
        async with session_factory.begin() as session:
            user = _user()
            session.add(user)
            await session.flush()
            user_id = user.id

        setup_dispatcher = CommitObservingDispatcher(database_url)
        created = await CreateTaskUseCase(
            SqlAlchemyTaskRepositoryFactory(session_factory),
            dispatcher=setup_dispatcher,
        ).execute(
            user_id=user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-08-01"},
            idempotency_key="daily_brief:failed-relay:2026-08-01:scheduled",
        )
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=enqueuer,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        published = await relay.relay_once(limit=100)

        assert published == 0
        assert setup_dispatcher.task_ids == [created.task_id]
        async with session_factory() as session:
            task = await session.get(TaskRunModel, created.task_id)
            event = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == created.task_id)
            )
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
        assert event is not None
        assert event.published_at is None
        assert event.attempt_count == 1
        assert event.last_error == "queue_enqueue_failed"
        assert event.available_at == now + timedelta(seconds=5)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_create_task_immediately_uses_same_claim_and_enqueue_path(
    database_url: str,
) -> None:
    """创建事务提交后立即 claim 并投递；队列失败仍返回 QUEUED 且保留未发布事实。"""
    session_factory = build_session_factory(database_url)
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    enqueuer = RecordingEnqueuer(fail=True)
    relay = OutboxRelay(
        store=SqlAlchemyOutboxStore(session_factory),
        enqueuer=enqueuer,
        clock=lambda: now,
        claim_ttl=timedelta(seconds=60),
        retry_base=timedelta(seconds=5),
        retry_max=timedelta(seconds=300),
    )
    try:
        async with session_factory.begin() as session:
            user = _user()
            session.add(user)
            await session.flush()
            user_id = user.id

        created = await CreateTaskUseCase(
            SqlAlchemyTaskRepositoryFactory(session_factory),
            dispatcher=relay,
        ).execute(
            user_id=user_id,
            kind="daily_brief",
            input_payload={"local_date": "2030-08-01"},
            idempotency_key="daily_brief:immediate:2030-08-01:scheduled",
        )

        assert created.status is TaskStatus.QUEUED
        assert enqueuer.task_ids == [created.task_id]
        async with session_factory() as session:
            task = await session.get(TaskRunModel, created.task_id)
            event = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == created.task_id)
            )
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
        assert event is not None
        assert event.published_at is None
        assert event.attempt_count == 1
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_concurrent_relays_skip_rows_already_claimed_by_other_transaction(
    database_url: str,
) -> None:
    """SKIP LOCKED 与 claim 时间窗保证两个 relay 不在同轮投递同一 Outbox 行。"""
    session_factory = build_session_factory(database_url)
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    first_enqueuer = RecordingEnqueuer()
    second_enqueuer = RecordingEnqueuer()
    try:
        async with session_factory.begin() as session:
            user = _user()
            session.add(user)
            await session.flush()
            user_id = user.id

        setup_dispatcher = CommitObservingDispatcher(database_url)
        created = await CreateTaskUseCase(
            SqlAlchemyTaskRepositoryFactory(session_factory),
            dispatcher=setup_dispatcher,
        ).execute(
            user_id=user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-08-01"},
            idempotency_key="daily_brief:concurrent-relay:2026-08-01:scheduled",
        )
        first = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=first_enqueuer,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )
        second = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=second_enqueuer,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        counts = await asyncio.gather(
            first.relay_once(limit=1),
            second.relay_once(limit=1),
        )

        assert sum(counts) == 1
        assert setup_dispatcher.task_ids == [created.task_id]
        assert first_enqueuer.task_ids + second_enqueuer.task_ids == [created.task_id]
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_claim_survives_crash_and_becomes_due_after_exact_claim_window(
    database_url: str,
) -> None:
    """claim 提交后崩溃不伪造结果，60 秒内跳过，到期后允许至少一次重复投递。"""
    session_factory = build_session_factory(database_url)
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    current = now
    enqueuer = CrashOnceEnqueuer()
    try:
        async with session_factory.begin() as session:
            user = _user()
            session.add(user)
            await session.flush()
            user_id = user.id

        setup_dispatcher = CommitObservingDispatcher(database_url)
        created = await CreateTaskUseCase(
            SqlAlchemyTaskRepositoryFactory(session_factory),
            dispatcher=setup_dispatcher,
        ).execute(
            user_id=user_id,
            kind="daily_brief",
            input_payload={"local_date": "2030-08-01"},
            idempotency_key="daily_brief:crash-recovery:2030-08-01:scheduled",
        )
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=enqueuer,
            clock=lambda: current,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        with pytest.raises(SyntheticProcessCrash):
            await relay.relay_once(limit=1)

        async with session_factory() as session:
            crashed_task = await session.get(TaskRunModel, created.task_id)
            crashed_event = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == created.task_id)
            )
        assert crashed_task is not None
        assert crashed_task.status == TaskStatus.QUEUED.value
        assert crashed_event is not None
        assert crashed_event.available_at == now + timedelta(seconds=60)
        assert crashed_event.published_at is None
        assert crashed_event.attempt_count == 0
        assert crashed_event.last_error is None

        current = now + timedelta(seconds=59)
        assert await relay.relay_once(limit=1) == 0
        assert enqueuer.task_ids == [created.task_id]

        current = now + timedelta(seconds=60)
        assert await relay.relay_once(limit=1) == 1
        assert enqueuer.task_ids == [created.task_id, created.task_id]
        async with session_factory() as session:
            recovered_event = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == created.task_id)
            )
        assert recovered_event is not None
        assert recovered_event.published_at == now + timedelta(seconds=60)
        assert recovered_event.last_error is None
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_execution_lease_is_single_owner_replaceable_after_expiry_and_cas_finished(
    database_url: str,
) -> None:
    """单条条件 UPDATE 排除第二 owner，允许过期接管，并拒绝旧 owner 提交终态。"""
    session_factory = build_session_factory(database_url)
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            user = _user()
            session.add(user)
            await session.flush()
            task = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.QUEUED.value,
                idempotency_key="lease:single-owner",
                input_payload={"local_date": "2030-08-01"},
            )
            session.add(task)
            await session.flush()
            task_id = task.id

        store = SqlAlchemyTaskExecutionStore(session_factory)
        first = await store.acquire(
            task_id=task_id,
            lease_owner="worker-a",
            now=now,
            lease_expires_at=now + timedelta(seconds=30),
        )
        blocked = await store.acquire(
            task_id=task_id,
            lease_owner="worker-b",
            now=now + timedelta(seconds=1),
            lease_expires_at=now + timedelta(seconds=31),
        )
        replacement = await store.acquire(
            task_id=task_id,
            lease_owner="worker-b",
            now=now + timedelta(seconds=31),
            lease_expires_at=now + timedelta(seconds=61),
        )

        assert first is not None
        assert blocked is None
        assert replacement is not None
        assert replacement.started_at == now

        stale_finish = await store.finish(
            task_id=task_id,
            lease_owner="worker-a",
            status=TaskStatus.SUCCEEDED,
            finished_at=now + timedelta(seconds=32),
            error_code=None,
        )
        owner_finish = await store.finish(
            task_id=task_id,
            lease_owner="worker-b",
            status=TaskStatus.SUCCEEDED,
            finished_at=now + timedelta(seconds=32),
            error_code=None,
        )

        assert stale_finish is False
        assert owner_finish is True
        async with session_factory() as session:
            persisted = await session.get(TaskRunModel, task_id)
        assert persisted is not None
        assert persisted.status == TaskStatus.SUCCEEDED.value
        assert persisted.attempt_count == 2
        assert persisted.started_at == now
        assert persisted.finished_at == now + timedelta(seconds=32)
        assert persisted.lease_owner is None
        assert persisted.lease_expires_at is None

        terminal_claim = await store.acquire(
            task_id=task_id,
            lease_owner="worker-c",
            now=now + timedelta(seconds=90),
            lease_expires_at=now + timedelta(seconds=120),
        )
        assert terminal_claim is None
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_execution_lease_writes_are_strictly_before_deadline_and_renew_is_time_valid(
    database_url: str,
) -> None:
    """等于租约截止时间即失效，未来期限不能复活过期或被接管的 owner。"""
    session_factory = build_session_factory(database_url)
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    deadline = now + timedelta(seconds=30)
    try:
        async with session_factory.begin() as session:
            user = _user()
            session.add(user)
            await session.flush()
            finish_task = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.RUNNING.value,
                lease_owner="worker-a",
                lease_expires_at=deadline,
                started_at=now,
                idempotency_key="lease:strict:finish",
                input_payload={"local_date": "2030-08-01"},
            )
            retry_task = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.RUNNING.value,
                lease_owner="worker-a",
                lease_expires_at=deadline,
                started_at=now,
                idempotency_key="lease:strict:retry",
                input_payload={"local_date": "2030-08-01"},
            )
            failure_task = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.RUNNING.value,
                lease_owner="worker-a",
                lease_expires_at=deadline,
                started_at=now,
                idempotency_key="lease:strict:failure",
                input_payload={"local_date": "2030-08-01"},
            )
            renew_task = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.RUNNING.value,
                lease_owner="worker-a",
                lease_expires_at=deadline,
                started_at=now,
                idempotency_key="lease:strict:renew",
                input_payload={"local_date": "2030-08-01"},
            )
            valid_renew_task = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.RUNNING.value,
                lease_owner="worker-a",
                lease_expires_at=deadline,
                started_at=now,
                idempotency_key="lease:strict:valid-renew",
                input_payload={"local_date": "2030-08-01"},
            )
            takeover_task = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.QUEUED.value,
                idempotency_key="lease:strict:takeover",
                input_payload={"local_date": "2030-08-01"},
            )
            session.add_all(
                (
                    finish_task,
                    retry_task,
                    failure_task,
                    renew_task,
                    valid_renew_task,
                    takeover_task,
                )
            )
            await session.flush()
            finish_id = finish_task.id
            retry_id = retry_task.id
            failure_id = failure_task.id
            renew_id = renew_task.id
            valid_renew_id = valid_renew_task.id
            takeover_id = takeover_task.id

        store = SqlAlchemyTaskExecutionStore(session_factory)
        assert (
            await store.finish(
                task_id=finish_id,
                lease_owner="worker-a",
                status=TaskStatus.SUCCEEDED,
                finished_at=deadline,
                error_code=None,
            )
            is False
        )
        assert (
            await store.schedule_retry(
                task_id=retry_id,
                lease_owner="worker-a",
                scheduled_at=deadline,
                retry_available_at=deadline + timedelta(seconds=5),
                error_code="synthetic_retry",
                attempt_count=1,
            )
            is False
        )
        assert (
            await store.fail_internal(
                task_id=failure_id,
                lease_owner="worker-a",
                failed_at=deadline,
                error_code="task_execution_internal_error",
            )
            is False
        )
        assert (
            await store.renew(
                task_id=renew_id,
                lease_owner="worker-a",
                renewed_at=deadline,
                lease_expires_at=deadline + timedelta(seconds=30),
            )
            is False
        )
        assert (
            await store.renew(
                task_id=renew_id,
                lease_owner="worker-a",
                renewed_at=deadline + timedelta(seconds=1),
                lease_expires_at=deadline + timedelta(seconds=31),
            )
            is False
        )
        assert (
            await store.renew(
                task_id=valid_renew_id,
                lease_owner="worker-a",
                renewed_at=now + timedelta(seconds=1),
                lease_expires_at=now + timedelta(seconds=31),
            )
            is True
        )

        first = await store.acquire(
            task_id=takeover_id,
            lease_owner="worker-a",
            now=now,
            lease_expires_at=deadline,
        )
        replacement = await store.acquire(
            task_id=takeover_id,
            lease_owner="worker-b",
            now=deadline,
            lease_expires_at=deadline + timedelta(seconds=30),
        )
        assert first is not None
        assert replacement is not None
        assert (
            await store.renew(
                task_id=takeover_id,
                lease_owner="worker-a",
                renewed_at=deadline + timedelta(seconds=1),
                lease_expires_at=deadline + timedelta(seconds=31),
            )
            is False
        )
        assert (
            await store.finish(
                task_id=takeover_id,
                lease_owner="worker-b",
                status=TaskStatus.SUCCEEDED,
                finished_at=deadline + timedelta(seconds=29),
                error_code=None,
            )
            is True
        )

        async with session_factory() as session:
            retry_outbox = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_id == retry_id,
                    OutboxEventModel.topic == "task.execute",
                )
            )
            persisted = {
                task.id: task
                for task in (
                    await session.scalars(
                        select(TaskRunModel).where(
                            TaskRunModel.id.in_((finish_id, retry_id, failure_id, renew_id))
                        )
                    )
                ).all()
            }
        assert retry_outbox == 0
        assert persisted[finish_id].status == TaskStatus.RUNNING.value
        assert persisted[retry_id].status == TaskStatus.RUNNING.value
        assert persisted[failure_id].status == TaskStatus.RUNNING.value
        assert persisted[renew_id].lease_expires_at == deadline
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_internal_execution_failure_cas_preserves_foreign_owner_and_terminal_state(
    database_url: str,
) -> None:
    """前置未知异常只失败无 owner 或本 owner 的非终态，不得覆盖他人和既有终态。"""
    session_factory = build_session_factory(database_url)
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            user = _user()
            session.add(user)
            await session.flush()
            queued = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.QUEUED.value,
                idempotency_key="internal-failure:queued",
                input_payload={"local_date": "2030-08-01"},
            )
            retry_scheduled = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.RETRY_SCHEDULED.value,
                idempotency_key="internal-failure:retry",
                input_payload={"local_date": "2030-08-01"},
            )
            same_owner = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.RUNNING.value,
                idempotency_key="internal-failure:same-owner",
                input_payload={"local_date": "2030-08-01"},
                lease_owner="worker-a",
                lease_expires_at=now + timedelta(seconds=30),
                started_at=now,
            )
            foreign_owner = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.RUNNING.value,
                idempotency_key="internal-failure:foreign-owner",
                input_payload={"local_date": "2030-08-01"},
                lease_owner="worker-b",
                lease_expires_at=now + timedelta(seconds=30),
                started_at=now,
            )
            terminal = TaskRunModel(
                user_id=user.id,
                kind="daily_brief",
                status=TaskStatus.SUCCEEDED.value,
                idempotency_key="internal-failure:terminal",
                input_payload={"local_date": "2030-08-01"},
                started_at=now,
                finished_at=now,
            )
            session.add_all((queued, retry_scheduled, same_owner, foreign_owner, terminal))
            await session.flush()
            task_ids = {
                "queued": queued.id,
                "retry": retry_scheduled.id,
                "same": same_owner.id,
                "foreign": foreign_owner.id,
                "terminal": terminal.id,
            }

        store = SqlAlchemyTaskExecutionStore(session_factory)
        results = {
            name: await store.fail_internal(
                task_id=task_id,
                lease_owner="worker-a",
                failed_at=now + timedelta(seconds=1),
                error_code="task_execution_internal_error",
            )
            for name, task_id in task_ids.items()
        }

        assert results == {
            "queued": True,
            "retry": True,
            "same": True,
            "foreign": False,
            "terminal": False,
        }
        async with session_factory() as session:
            tasks = {
                task.id: task
                for task in (
                    await session.scalars(
                        select(TaskRunModel).where(TaskRunModel.id.in_(task_ids.values()))
                    )
                ).all()
            }
            failure_audits = (
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.task_id.in_(task_ids.values()),
                        AuditEventModel.event_type == "task.failed",
                    )
                )
            ).all()

        for key in ("queued", "retry", "same"):
            task = tasks[task_ids[key]]
            assert task.status == TaskStatus.FAILED.value
            assert task.error_code == "task_execution_internal_error"
            assert task.finished_at == now + timedelta(seconds=1)
            assert task.lease_owner is None
            assert task.lease_expires_at is None

        assert tasks[task_ids["foreign"]].status == TaskStatus.RUNNING.value
        assert tasks[task_ids["foreign"]].lease_owner == "worker-b"
        assert tasks[task_ids["terminal"]].status == TaskStatus.SUCCEEDED.value
        assert tasks[task_ids["terminal"]].error_code is None
        assert {audit.task_id for audit in failure_audits} == {
            task_ids["queued"],
            task_ids["retry"],
            task_ids["same"],
        }
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_fall_back_due_scan_creates_and_enqueues_one_task_for_active_user(
    database_url: str,
) -> None:
    """重复小时两次扫描共享用户本地日期幂等键，且停用用户从 PostgreSQL reader 排除。"""
    session_factory = build_session_factory(database_url)
    first_fold = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    second_fold = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    enqueuer = RecordingEnqueuer()
    try:
        async with session_factory.begin() as session:
            active = UserModel(
                email="dst-active@example.com",
                display_name="DST Active",
                password_hash=None,
                timezone="America/New_York",
                locale="zh-CN",
                brief_time=time(1, 30),
                is_active=True,
            )
            inactive = UserModel(
                email="dst-inactive@example.com",
                display_name="DST Inactive",
                password_hash=None,
                timezone="America/New_York",
                locale="zh-CN",
                brief_time=time(1, 30),
                is_active=False,
            )
            session.add_all((active, inactive))
            await session.flush()
            active_id = active.id

        current = first_fold
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=enqueuer,
            clock=lambda: current,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )
        use_case = DispatchDueDailyBriefsUseCase(
            reader=SqlAlchemyActiveUserScheduleReader(session_factory),
            task_creator=CreateTaskUseCase(
                SqlAlchemyTaskRepositoryFactory(session_factory),
                dispatcher=relay,
            ),
        )

        first_count = await use_case.execute(now=first_fold)
        current = second_fold
        second_count = await use_case.execute(now=second_fold)

        assert first_count == 1
        assert second_count == 1
        assert len(enqueuer.task_ids) == 1
        async with session_factory() as session:
            tasks = (await session.scalars(select(TaskRunModel))).all()
            events = (await session.scalars(select(OutboxEventModel))).all()
        assert len(tasks) == 1
        assert tasks[0].user_id == active_id
        assert tasks[0].idempotency_key == (f"daily_brief:{active_id}:2026-11-01:scheduled")
        assert len(events) == 1
        assert events[0].published_at == first_fold
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_expire_sessions_use_case_deletes_only_due_session(database_url: str) -> None:
    """小时维护 job 的 SQL 适配器真实删除到期摘要，同时保留未来会话。"""
    session_factory = build_session_factory(database_url)
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            user = _user()
            session.add(user)
            await session.flush()
            expired = UserSessionModel(
                user_id=user.id,
                token_hash=b"e" * 32,
                csrf_hash=b"x" * 32,
                created_at=now - timedelta(days=1),
                expires_at=now,
                last_seen_at=now - timedelta(hours=1),
            )
            active = UserSessionModel(
                user_id=user.id,
                token_hash=b"a" * 32,
                csrf_hash=b"y" * 32,
                created_at=now - timedelta(hours=1),
                expires_at=now + timedelta(hours=1),
                last_seen_at=now - timedelta(minutes=1),
            )
            session.add_all((expired, active))
            await session.flush()
            active_id = active.id

        deleted = await ExpireSessionsUseCase(
            SqlAlchemySessionMaintenanceRepositoryFactory(session_factory)
        ).execute(now=now)

        assert deleted == 1
        async with session_factory() as session:
            sessions = (await session.scalars(select(UserSessionModel))).all()
        assert [session.id for session in sessions] == [active_id]
    finally:
        await session_factory.dispose()
