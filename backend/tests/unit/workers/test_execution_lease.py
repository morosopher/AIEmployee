"""验证 Worker 在至少一次投递下的租约、超时与错误分类语义。"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import ai_employee.workers.execute_task as execute_task_module
from ai_employee.application.use_cases.task_execution import (
    DurableTaskRunner,
    LeasedTask,
    TaskExecutionStep,
)
from ai_employee.config import Settings
from ai_employee.domain.errors import TransientProviderError
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.queue.broker import broker
from ai_employee.infrastructure.queue.enqueue import TaskiqTaskEnqueuer


@pytest.mark.parametrize(
    "field",
    [
        "task_lease_seconds",
        "outbox_claim_seconds",
        "outbox_retry_base_seconds",
        "outbox_retry_max_seconds",
        "outbox_relay_batch_size",
    ],
)
def test_task7_duration_and_batch_settings_must_be_positive(field: str) -> None:
    """租约、退避与批量参数拒绝零值，避免无期限 claim 或空转 relay。"""
    with pytest.raises(ValidationError):
        Settings(**{field: 0})


def test_outbox_retry_base_cannot_exceed_retry_max() -> None:
    """初始退避不得高于上限，否则首次故障就违背配置所表达的最大延迟。"""
    with pytest.raises(ValueError, match="OUTBOX_RETRY_BASE_SECONDS"):
        Settings(outbox_retry_base_seconds=301, outbox_retry_max_seconds=300)


def test_task_lease_must_cover_one_complete_step_budget() -> None:
    """只在节点边界续租时，租约必须至少覆盖单节点截止时间以免中途并发接管。"""
    with pytest.raises(ValueError, match="TASK_LEASE_SECONDS"):
        Settings(
            task_timeout_seconds=600,
            task_step_timeout_seconds=300,
            task_lease_seconds=299,
        )


def test_runner_rejects_lease_shorter_than_step_timeout() -> None:
    """直接构造 Runner 也必须保持租约覆盖节点预算，不能只依赖 Settings 组合。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    task = _leased_task(started_at=now)
    with pytest.raises(ValueError, match="lease_duration"):
        DurableTaskRunner(
            store=RecordingLeaseStore(task),
            clock=MutableClock(now),
            lease_duration=timedelta(seconds=9),
            task_timeout_seconds=60,
            task_step_timeout_seconds=10,
            max_transient_retries=3,
            resolve_steps=lambda leased: (),
        )


def test_redis_stream_broker_uses_fixed_queue_group_without_redis_retry_scheduler() -> None:
    """延迟重试只由 PostgreSQL Outbox 表达，Taskiq 不安装 Redis 重试中间件。"""
    assert broker.queue_name == "ai_employee_tasks"
    assert broker.consumer_group_name == "ai_employee_workers"
    assert broker.consumer_id == "0-0"
    assert broker.middlewares == []
    assert execute_task_module.execute_task.labels["retry_on_error"] is False


def test_retry_recovery_delay_covers_taskiq_max_delay_and_scheduler_margin() -> None:
    """耐久重试以固定短延迟投递，恢复期限由 relay 确认后另行处理。"""
    settings = Settings()

    assert settings.task_retry_recovery_seconds == 360
    assert execute_task_module.RETRY_DELAY_SECONDS == 5


@pytest.mark.asyncio
async def test_taskiq_entrypoint_uses_durable_retry_without_reading_message_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """消息标签不再控制重试，Runner 使用 PostgreSQL attempt_count 保持上限。"""
    task_id = uuid4()
    received: list[tuple[UUID, bool]] = []

    class RecordingRunner:
        """记录 Worker 传递的基础设施无关重试预算。"""

        async def run(
            self,
            received_task_id: UUID,
            *,
            may_retry_transient: bool,
            retry_delay: timedelta,
            recover_waiting_approval: bool = False,
        ) -> bool:
            """记录当前任务与保守预算决定，不连接任何持久化基础设施。"""
            del retry_delay, recover_waiting_approval
            received.append((received_task_id, may_retry_transient))
            return True

    class NonFakeStore:
        """让入口按非 fake 任务走缓存 Runner，不读取真实数据库。"""

        async def get_fake_write_task(self, *, task_id: UUID) -> None:
            """返回空快照，代表任务不是 fake-write。"""
            del task_id

    monkeypatch.setattr(execute_task_module, "build_task_runner", lambda: RecordingRunner())
    monkeypatch.setattr(
        execute_task_module, "SqlAlchemyApprovalStore", lambda _factory: NonFakeStore()
    )
    context = object()

    await execute_task_module.execute_task.original_func(str(task_id), context)

    assert received == [(task_id, True)]


@pytest.mark.asyncio
async def test_fake_write_worker_disposes_every_message_scoped_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fake-write 消息临时创建的每个数据库工厂必须在退出路径释放连接池。"""
    task_id = uuid4()
    factories: list[object] = []

    class Factory:
        """记录 Worker 是否释放此消息创建的资源。"""

        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            """模拟 SQLAlchemy engine 的异步关闭。"""
            self.disposed = True

    class Store:
        """只返回 fake-write 判别快照，避免该单测连接数据库。"""

        def __init__(self, _factory: Factory) -> None:
            pass

        async def get_fake_write_task(self, *, task_id: UUID) -> object:
            """返回最小任务分类对象。"""
            del task_id
            return type("FakeTask", (), {"kind": "fake_write", "input_payload": {}})()

    class Runner:
        """替代执行用例，仅确保组合完成后可检查资源释放。"""

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> bool:
            """不执行图，模拟正常 Worker 返回。"""
            return True

    def build_factory(_database_url: str, *, task_event_publisher: object) -> Factory:
        """模拟 Worker 的通知型会话工厂，并确认生产组合根已提供发布适配器。"""
        assert task_event_publisher is not None
        factory = Factory()
        factories.append(factory)
        return factory

    monkeypatch.setattr(execute_task_module, "build_session_factory", build_factory)
    monkeypatch.setattr(execute_task_module, "SqlAlchemyApprovalStore", Store)
    monkeypatch.setattr(execute_task_module, "DurableTaskRunner", Runner)

    await execute_task_module.execute_task.original_func(str(task_id))

    assert len(factories) == 1
    assert all(factory.disposed for factory in factories)


@pytest.mark.asyncio
async def test_fake_write_and_kind_lookup_runners_receive_worker_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fake-write 和分类读取失败路径也必须注入同一安全指标端口。

    两个 Runner 都绕过 ``build_task_runner`` 的缓存组合根；遗漏该参数会让高风险审批任务
    与数据库异常路径丢失终态、重试和排队时间指标。
    """
    task_id = uuid4()
    captured_metrics: list[object | None] = []
    class WorkerMetrics:
        """满足入口心跳与 Runner 注入所需的最小安全指标端口。"""

        def record_heartbeat(self, *, process: str, age_seconds: float) -> None:
            """忽略本测试不关注的进程心跳写入。"""
            del process, age_seconds

    worker_metrics = WorkerMetrics()

    class Factory:
        """提供入口 finally 所需的最小释放协议。"""

        async def dispose(self) -> None:
            """模拟消息生命周期结束。"""

    class FakeWriteStore:
        """令入口选择 fake-write 专用 Runner。"""

        def __init__(self, _factory: Factory) -> None:
            """接受组合根注入的工厂。"""

        async def get_fake_write_task(self, *, task_id: UUID) -> object:
            """返回只含受控 kind 的快照。"""
            del task_id
            return type("FakeTask", (), {"kind": "fake_write"})()

    class FailingLookupStore:
        """令入口构造分类读取异常的安全失败 Runner。"""

        def __init__(self, _factory: Factory) -> None:
            """接受组合根注入的工厂。"""

        async def get_fake_write_task(self, *, task_id: UUID) -> None:
            """模拟数据库分类读取失败。"""
            del task_id
            raise OSError("synthetic database failure")

    class Runner:
        """捕获构造参数而不触发真实耐久执行。"""

        def __init__(self, **kwargs: object) -> None:
            """记录受控的 metrics 端口引用。"""
            captured_metrics.append(kwargs.get("metrics"))

        async def run(self, *_args: object, **_kwargs: object) -> bool:
            """模拟已处理的持久任务。"""
            return True

    monkeypatch.setattr(execute_task_module, "build_session_factory", lambda *_args, **_kwargs: Factory())
    monkeypatch.setattr(execute_task_module, "DurableTaskRunner", Runner)
    monkeypatch.setattr(execute_task_module, "_worker_metrics", worker_metrics)
    async def skip_stuck_probe(**_kwargs: object) -> None:
        """组合测试不创建数据库时替代只读指标探针。"""

    monkeypatch.setattr(execute_task_module, "refresh_stuck_task_metrics", skip_stuck_probe)
    monkeypatch.setattr(execute_task_module, "SqlAlchemyApprovalStore", FakeWriteStore)
    await execute_task_module.execute_task.original_func(str(task_id))
    monkeypatch.setattr(execute_task_module, "SqlAlchemyApprovalStore", FailingLookupStore)
    await execute_task_module.execute_task.original_func(str(task_id))

    assert captured_metrics == [worker_metrics, worker_metrics]


@pytest.mark.asyncio
async def test_fake_write_graph_reuses_worker_message_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph 审批存储必须复用 Worker 已管理的工厂，不能额外创建连接池。"""
    task_id = uuid4()
    stores: list[object] = []
    captured_steps: list[object] = []

    class Store:
        """提供 fake-write 分类，并记录由组合层创建的审批存储。"""

        def __init__(self, received_factory: object) -> None:
            assert received_factory is factory
            stores.append(self)

        async def get_fake_write_task(self, *, task_id: UUID) -> object:
            """返回最小 fake-write 分类快照。"""
            del task_id
            return type("FakeTask", (), {"kind": "fake_write", "input_payload": {}})()

    class Runner:
        """捕获步骤解析器输出而不执行外部 I/O。"""

        def __init__(self, **kwargs: object) -> None:
            self._resolve_steps = kwargs["resolve_steps"]

        async def run(self, received_task_id: UUID, **_kwargs: object) -> bool:
            """解析一次步骤，验证 Graph 持有同一审批存储实例。"""
            captured_steps.extend(self._resolve_steps(_leased_task(started_at=datetime.now(UTC))))
            return True

    class Factory:
        """模拟带释放入口的单条消息数据库工厂。"""

        async def dispose(self) -> None:
            """满足 Worker 的 finally 释放协议。"""

    factory = Factory()
    def build_factory(_database_url: str, *, task_event_publisher: object) -> Factory:
        """返回同一合成工厂，并验证 Worker 没有绕开提交后通知组合边界。"""
        assert task_event_publisher is not None
        return factory

    monkeypatch.setattr(execute_task_module, "build_session_factory", build_factory)
    monkeypatch.setattr(execute_task_module, "SqlAlchemyApprovalStore", Store)
    monkeypatch.setattr(execute_task_module, "DurableTaskRunner", Runner)

    await execute_task_module.execute_task.original_func(str(task_id))

    assert len(stores) == 1
    assert len(captured_steps) == 1
    assert captured_steps[0]._approval_store is stores[0]


@pytest.mark.asyncio
async def test_enqueue_adapter_sends_only_canonical_task_id() -> None:
    """enqueue 适配器调用 Taskiq 入口的 ``kiq``，且 Redis 消息只携带 UUID 字符串。"""
    task_id = uuid4()
    received: list[str] = []

    async def fake_kiq(value: str) -> None:
        received.append(value)

    await TaskiqTaskEnqueuer(fake_kiq).enqueue(task_id)

    assert received == [str(task_id)]


class MutableClock:
    """提供完全由测试控制的 UTC 当前时间，避免依赖真实时钟。"""

    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class RecordingLeaseStore:
    """记录 Runner 发出的持久化意图，并允许模拟丢失租约。"""

    def __init__(self, task: LeasedTask | None) -> None:
        self.task = task
        self.prepared: list[UUID] = []
        self.acquired: list[tuple[UUID, str, datetime, datetime]] = []
        self.renewed: list[tuple[UUID, str, datetime]] = []
        self.finished: list[tuple[TaskStatus, str | None]] = []
        self.retry_recovery_deadlines: list[datetime | None] = []
        self.scheduled_retries: list[tuple[UUID, str, datetime, datetime, str, int]] = []
        self.internal_failures: list[tuple[UUID, str, datetime, str]] = []
        self.allow_renew = True
        self.allow_finish = True
        self.allow_internal_failure = True

    async def prepare_retry(self, *, task_id: UUID, now: datetime) -> None:
        """记录重试消息在重新租约前执行了持久状态归队。"""
        self.prepared.append(task_id)

    async def acquire(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> LeasedTask | None:
        """返回预置租约快照，并记录精确租约期限。"""
        self.acquired.append((task_id, lease_owner, now, lease_expires_at))
        return self.task

    async def renew(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> bool:
        """按测试开关模拟 CAS 续租成功或租约已经丢失。"""
        self.renewed.append((task_id, lease_owner, lease_expires_at))
        return self.allow_renew

    async def finish(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        status: TaskStatus,
        finished_at: datetime,
        error_code: str | None,
        retry_recovery_at: datetime | None = None,
    ) -> bool:
        """记录带 owner 的终态 CAS；结果由测试开关控制。"""
        self.finished.append((status, error_code))
        self.retry_recovery_deadlines.append(retry_recovery_at)
        return self.allow_finish

    async def schedule_retry(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        scheduled_at: datetime,
        retry_available_at: datetime,
        error_code: str,
        attempt_count: int,
    ) -> bool:
        """记录耐久重试 Outbox 的全部输入，避免测试依赖 Redis 或 Taskiq。"""
        self.scheduled_retries.append(
            (
                task_id,
                lease_owner,
                scheduled_at,
                retry_available_at,
                error_code,
                attempt_count,
            )
        )
        return self.allow_finish

    async def fail_internal(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        failed_at: datetime,
        error_code: str,
    ) -> bool:
        """记录前置持久化异常后的 owner/status 安全失败 CAS 意图。"""
        self.internal_failures.append((task_id, lease_owner, failed_at, error_code))
        return self.allow_internal_failure


class FailingPrepareStore(RecordingLeaseStore):
    """在重试准备边界注入未知数据库异常。"""

    async def prepare_retry(self, *, task_id: UUID, now: datetime) -> None:
        """模拟 prepare transaction 在应用用例可分类前失败。"""
        del task_id, now
        raise RuntimeError("synthetic prepare failure")


class FailingAcquireStore(RecordingLeaseStore):
    """在租约获取边界注入未知数据库异常。"""

    async def acquire(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> LeasedTask | None:
        """模拟 acquisition transaction 的未知失败或提交结果丢失。"""
        del task_id, lease_owner, now, lease_expires_at
        raise RuntimeError("synthetic acquire failure")


class AdvancingAcquireStore(RecordingLeaseStore):
    """在租约 acquisition 往返期间推进时钟，验证该耗时计入总预算。"""

    def __init__(
        self,
        task: LeasedTask,
        *,
        clock: MutableClock,
        elapsed: timedelta,
    ) -> None:
        super().__init__(task)
        self._clock = clock
        self._elapsed = elapsed

    async def acquire(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> LeasedTask | None:
        """记录 acquisition 后模拟数据库等待占用的真实墙钟预算。"""
        task = await super().acquire(
            task_id=task_id,
            lease_owner=lease_owner,
            now=now,
            lease_expires_at=lease_expires_at,
        )
        self._clock.current += self._elapsed
        return task


class CallableStep:
    """把异步函数包装成具名执行节点，便于断言节点边界续租。"""

    def __init__(self, name: str, callback: Callable[[], Awaitable[None]]) -> None:
        self.name = name
        self._callback = callback

    async def execute(self, task: LeasedTask) -> None:
        """执行测试回调；任务快照由 Runner 传入但不做修改。"""
        del task
        await self._callback()


class PersistenceUnavailableRunner:
    """模拟主失败后数据库也无法写入安全终态的最后防线场景。"""

    async def run(self, task_id: UUID, **_kwargs: object) -> bool:
        """对可关联任务抛出未知持久化异常。"""
        del task_id
        raise RuntimeError("synthetic persistence unavailable")


def _leased_task(*, started_at: datetime) -> LeasedTask:
    """构造不含真实用户数据的已持有租约快照。"""
    return LeasedTask(
        task_id=uuid4(),
        kind="daily_brief",
        input_payload={"local_date": "2026-08-01"},
        started_at=started_at,
    )


def _runner(
    *,
    store: RecordingLeaseStore,
    clock: MutableClock,
    steps: Sequence[TaskExecutionStep],
    task_timeout_seconds: float = 60,
    task_step_timeout_seconds: float = 10,
) -> DurableTaskRunner:
    """用确定性端口构造 Runner，避免单元测试连接数据库或 Redis。"""
    return DurableTaskRunner(
        store=store,
        clock=clock,
        lease_duration=timedelta(seconds=30),
        task_timeout_seconds=task_timeout_seconds,
        task_step_timeout_seconds=task_step_timeout_seconds,
        max_transient_retries=3,
        resolve_steps=lambda task: steps,
    )


class RecordingTaskMetrics:
    """记录 application 层安全指标端口调用，避免单元测试依赖 Prometheus。"""

    def __init__(self) -> None:
        """初始化所有聚合记录列表。"""
        self.outcomes: list[tuple[str, str, float]] = []
        self.retries: list[str] = []
        self.queue_waits: list[tuple[str, float]] = []

    def record_task_outcome(self, *, kind: str, status: str, duration_seconds: float) -> None:
        """记录终态指标调用。"""
        self.outcomes.append((kind, status, duration_seconds))

    def record_task_retry(self, *, kind: str) -> None:
        """记录重试指标调用。"""
        self.retries.append(kind)

    def record_queue_wait(self, *, kind: str, seconds: float) -> None:
        """记录首次排队等待调用。"""
        self.queue_waits.append((kind, seconds))


@pytest.mark.asyncio
async def test_runner_records_terminal_outcome_through_safe_metrics_port() -> None:
    """Runner 成功 CAS 后才记录终态，且端口不接受任务正文。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    clock = MutableClock(now)
    task = _leased_task(started_at=now)
    store = RecordingLeaseStore(task)
    metrics = RecordingTaskMetrics()

    async def complete() -> None:
        """模拟不产生外部副作用的成功任务节点。"""

    runner = DurableTaskRunner(
        store=store,
        clock=clock,
        lease_duration=timedelta(seconds=30),
        task_timeout_seconds=60,
        task_step_timeout_seconds=10,
        max_transient_retries=3,
        resolve_steps=lambda _task: (CallableStep("complete", complete),),
        metrics=metrics,
    )

    assert await runner.run(task.task_id)
    assert metrics.outcomes == [("daily_brief", "succeeded", 0.0)]


@pytest.mark.asyncio
async def test_runner_prepares_retry_then_renews_between_nodes_and_succeeds() -> None:
    """重投先归队，成功获取租约后只在节点边界续租并以 owner 提交成功。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    clock = MutableClock(now)
    task = _leased_task(started_at=now)
    store = RecordingLeaseStore(task)
    executed: list[str] = []

    async def first() -> None:
        executed.append("first")

    async def second() -> None:
        executed.append("second")

    runner = _runner(
        store=store,
        clock=clock,
        steps=(CallableStep("first", first), CallableStep("second", second)),
    )

    owned = await runner.run(task_id=task.task_id, lease_owner="worker-a")

    assert owned is True
    assert store.prepared == [task.task_id]
    assert executed == ["first", "second"]
    assert len(store.renewed) == 1
    assert store.finished == [(TaskStatus.SUCCEEDED, None)]


@pytest.mark.asyncio
async def test_runner_does_not_commit_terminal_state_after_renew_loses_lease() -> None:
    """节点间 CAS 续租失败表示所有权已丢失，旧 Worker 不得覆盖新 owner 的结果。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    clock = MutableClock(now)
    task = _leased_task(started_at=now)
    store = RecordingLeaseStore(task)
    store.allow_renew = False

    async def completed() -> None:
        return None

    runner = _runner(
        store=store,
        clock=clock,
        steps=(CallableStep("first", completed), CallableStep("second", completed)),
    )

    owned = await runner.run(task_id=task.task_id, lease_owner="stale-worker")

    assert owned is False
    assert store.finished == []


@pytest.mark.asyncio
async def test_step_timeout_is_persisted_as_stable_non_retryable_failure() -> None:
    """单节点超过独立截止时间时写稳定错误码，且异常不会创建重试 Outbox。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    clock = MutableClock(now)
    task = _leased_task(started_at=now)
    store = RecordingLeaseStore(task)

    async def never_completes() -> None:
        await __import__("asyncio").Event().wait()

    runner = _runner(
        store=store,
        clock=clock,
        steps=(CallableStep("blocked", never_completes),),
        task_step_timeout_seconds=0.01,
    )

    owned = await runner.run(task_id=task.task_id, lease_owner="worker-a")

    assert owned is True
    assert store.finished == [(TaskStatus.FAILED, "task_step_timeout")]


@pytest.mark.asyncio
async def test_persisted_started_at_prevents_retry_from_resetting_total_budget() -> None:
    """已耗尽的持久总预算在重投时立即失败，节点完全不会再次运行。"""
    now = datetime(2026, 8, 1, 0, 1, tzinfo=UTC)
    clock = MutableClock(now)
    task = _leased_task(started_at=now - timedelta(seconds=61))
    store = RecordingLeaseStore(task)
    executed = False

    async def should_not_run() -> None:
        nonlocal executed
        executed = True

    runner = _runner(
        store=store,
        clock=clock,
        steps=(CallableStep("late", should_not_run),),
        task_timeout_seconds=60,
    )

    await runner.run(task_id=task.task_id, lease_owner="worker-retry")

    assert executed is False
    assert store.finished == [(TaskStatus.FAILED, "task_timeout")]


@pytest.mark.asyncio
async def test_acquisition_latency_is_included_in_whole_task_budget() -> None:
    """租约数据库往返后的新鲜时钟决定剩余预算，不能只用 acquisition 前的旧 now。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    clock = MutableClock(now)
    task = _leased_task(started_at=now)
    store = AdvancingAcquireStore(task, clock=clock, elapsed=timedelta(seconds=61))
    executed = False

    async def should_not_run() -> None:
        nonlocal executed
        executed = True

    runner = _runner(
        store=store,
        clock=clock,
        steps=(CallableStep("late", should_not_run),),
        task_timeout_seconds=60,
    )

    await runner.run(task_id=task.task_id, lease_owner="worker-a")

    assert executed is False
    assert store.finished == [(TaskStatus.FAILED, "task_timeout")]


@pytest.mark.asyncio
async def test_transient_provider_error_creates_durable_retry_outbox_without_taskiq_rethrow() -> (
    None
):
    """临时错误只创建耐久延迟 Outbox，不能绕过 relay 交给 Taskiq。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    clock = MutableClock(now)
    task = _leased_task(started_at=now)
    store = RecordingLeaseStore(task)
    transient = TransientProviderError(
        error_code="provider_temporarily_unavailable",
        message="provider temporarily unavailable",
    )

    async def fail_transiently() -> None:
        # 队列消息可能在长节点运行后才失败；恢复期限不能使用 Worker 收到消息的旧时刻。
        clock.current += timedelta(seconds=302)
        raise transient

    runner = _runner(
        store=store,
        clock=clock,
        steps=(CallableStep("provider", fail_transiently),),
    )

    owned = await runner.run(
        task_id=task.task_id,
        lease_owner="worker-a",
        may_retry_transient=True,
        retry_delay=timedelta(seconds=5),
    )

    assert owned is True
    assert store.finished == []
    assert store.retry_recovery_deadlines == []
    assert store.scheduled_retries == [
        (
            task.task_id,
            "worker-a",
            now + timedelta(seconds=302),
            now + timedelta(seconds=307),
            "provider_temporarily_unavailable",
            1,
        )
    ]


@pytest.mark.asyncio
async def test_transient_provider_retry_after_overrides_worker_default_delay() -> None:
    """供应商 Retry-After 必须覆盖 Worker 的固定回退，避免过早重试再次触发限流。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    task = _leased_task(started_at=now)
    store = RecordingLeaseStore(task)
    transient = TransientProviderError(
        error_code="google_rate_limited",
        message="Google Gmail is temporarily unavailable",
        retry_after=37,
    )

    async def fail_rate_limited() -> None:
        """模拟 Gmail 429，供 DurableTaskRunner 读取领域重试提示。"""
        raise transient

    runner = _runner(
        store=store,
        clock=MutableClock(now),
        steps=(CallableStep("gmail", fail_rate_limited),),
    )

    await runner.run(
        task_id=task.task_id,
        lease_owner="worker-a",
        may_retry_transient=True,
        retry_delay=timedelta(seconds=5),
    )

    assert store.scheduled_retries[0][3] == now + timedelta(seconds=37)


@pytest.mark.asyncio
async def test_transient_provider_error_without_retry_budget_is_persisted_as_terminal_failure() -> (
    None
):
    """最后一次允许尝试遇到临时错误必须终止，避免 ACK 后永久停在 RETRY_SCHEDULED。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    clock = MutableClock(now)
    task = _leased_task(started_at=now)
    store = RecordingLeaseStore(task)
    transient = TransientProviderError(
        error_code="provider_temporarily_unavailable",
        message="provider temporarily unavailable",
    )

    async def fail_transiently() -> None:
        raise transient

    runner = _runner(
        store=store,
        clock=clock,
        steps=(CallableStep("provider", fail_transiently),),
    )

    owned = await runner.run(
        task_id=task.task_id,
        lease_owner="worker-a",
        may_retry_transient=False,
    )

    assert owned is True
    assert store.finished == [(TaskStatus.FAILED, "task_retries_exhausted")]


@pytest.mark.asyncio
async def test_transient_retry_without_durable_delay_safely_fails() -> None:
    """遗漏延迟 Outbox 的可投递时刻不能留下 RETRY_SCHEDULED 悬挂任务。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    task = _leased_task(started_at=now)
    store = RecordingLeaseStore(task)
    transient = TransientProviderError(
        error_code="provider_temporarily_unavailable",
        message="provider temporarily unavailable",
    )

    async def fail_transiently() -> None:
        raise transient

    runner = _runner(
        store=store,
        clock=MutableClock(now),
        steps=(CallableStep("provider", fail_transiently),),
    )

    owned = await runner.run(
        task_id=task.task_id,
        lease_owner="worker-a",
        may_retry_transient=True,
    )

    assert owned is True
    assert store.finished == [(TaskStatus.FAILED, "task_retry_delay_missing")]


@pytest.mark.asyncio
async def test_unknown_exception_is_safely_failed_without_reaching_retry() -> None:
    """未知程序错误只写稳定安全码并被吞掉，绝不伪装成供应商临时错误。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    clock = MutableClock(now)
    task = _leased_task(started_at=now)
    store = RecordingLeaseStore(task)

    async def fail_unknown() -> None:
        raise RuntimeError("sensitive implementation detail")

    runner = _runner(
        store=store,
        clock=clock,
        steps=(CallableStep("unknown", fail_unknown),),
    )

    owned = await runner.run(task_id=task.task_id, lease_owner="worker-a")

    assert owned is True
    assert store.finished == [(TaskStatus.FAILED, "internal_worker_error")]


@pytest.mark.asyncio
async def test_prepare_retry_unknown_error_requests_safe_non_retry_failure() -> None:
    """重试准备未知异常必须尝试写安全失败码，且不能绕过持久化边界。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    task = _leased_task(started_at=now)
    store = FailingPrepareStore(task)
    runner = _runner(store=store, clock=MutableClock(now), steps=())

    handled = await runner.run(task_id=task.task_id, lease_owner="worker-prepare")

    assert handled is True
    assert store.acquired == []
    assert store.internal_failures == [
        (
            task.task_id,
            "worker-prepare",
            now,
            "task_execution_internal_error",
        )
    ]


@pytest.mark.asyncio
async def test_acquire_unknown_error_requests_safe_non_retry_failure() -> None:
    """租约获取未知异常必须覆盖可能已提交的本 owner，而不重新抛出。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    task = _leased_task(started_at=now)
    store = FailingAcquireStore(task)
    runner = _runner(store=store, clock=MutableClock(now), steps=())

    handled = await runner.run(task_id=task.task_id, lease_owner="worker-acquire")

    assert handled is True
    assert store.prepared == [task.task_id]
    assert store.internal_failures == [
        (
            task.task_id,
            "worker-acquire",
            now,
            "task_execution_internal_error",
        )
    ]


@pytest.mark.asyncio
async def test_internal_failure_cas_miss_does_not_fallback_to_overwrite_owner() -> None:
    """安全失败 CAS 未命中时必须无副作用退出，不能再用宽松终态写覆盖他人。"""
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    task = _leased_task(started_at=now)
    store = FailingAcquireStore(task)
    store.allow_internal_failure = False
    runner = _runner(store=store, clock=MutableClock(now), steps=())

    handled = await runner.run(task_id=task.task_id, lease_owner="stale-worker")

    assert handled is False
    assert len(store.internal_failures) == 1
    assert store.finished == []


@pytest.mark.asyncio
async def test_taskiq_entrypoint_propagates_unknown_when_failure_cannot_be_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库也不可写时入口不得静默确认未持久化的执行失败。"""

    class NonFakeStore:
        """绕开分类读取异常，确保测试抵达持久 Runner 的最终失败边界。"""

        async def get_fake_write_task(self, *, task_id: UUID) -> None:
            """返回空快照，表示当前任务不使用 fake-write Graph。"""
            del task_id

    monkeypatch.setattr(
        execute_task_module,
        "build_task_runner",
        lambda: PersistenceUnavailableRunner(),
    )
    monkeypatch.setattr(
        execute_task_module,
        "SqlAlchemyApprovalStore",
        lambda _factory: NonFakeStore(),
    )

    with pytest.raises(RuntimeError, match="task execution persistence boundary unavailable"):
        await execute_task_module.execute_task.original_func(str(uuid4()))
