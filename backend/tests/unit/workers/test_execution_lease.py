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
            resolve_steps=lambda leased: (),
        )


def test_redis_stream_broker_uses_fixed_queue_group_and_smart_retry_defaults() -> None:
    """队列只使用固定 Stream/group，SmartRetry 为三次、五秒、jitter 与封顶指数退避。"""
    assert broker.queue_name == "ai_employee_tasks"
    assert broker.consumer_group_name == "ai_employee_workers"
    assert len(broker.middlewares) == 1
    retry = broker.middlewares[0]
    assert retry.default_retry_count == 3
    assert retry.default_delay == 5
    assert retry.use_jitter is True
    assert retry.use_delay_exponent is True
    assert retry.max_delay_exponent == 300
    assert execute_task_module.execute_task.labels["retry_on_error"] is True


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
    ) -> bool:
        """记录带 owner 的终态 CAS；结果由测试开关控制。"""
        self.finished.append((status, error_code))
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

    async def run(self, task_id: UUID) -> bool:
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
        resolve_steps=lambda task: steps,
    )


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
    """单节点超过独立截止时间时写稳定错误码，且异常不会进入 Taskiq SmartRetry。"""
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
async def test_only_transient_provider_error_is_rethrown_after_retry_state_is_durable() -> None:
    """临时供应商错误先持久化 RETRY_SCHEDULED，再原样抛给 SmartRetry。"""
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

    with pytest.raises(TransientProviderError) as raised:
        await runner.run(task_id=task.task_id, lease_owner="worker-a")

    assert raised.value is transient
    assert store.finished == [(TaskStatus.RETRY_SCHEDULED, "provider_temporarily_unavailable")]


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
    """重试准备未知异常必须尝试写安全失败码，且不能逃逸到 SmartRetry。"""
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
async def test_taskiq_entrypoint_swallows_unknown_when_failure_cannot_be_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库也不可写时入口作为最后防线终止未知异常，不让 SmartRetry 接收它。"""
    monkeypatch.setattr(
        execute_task_module,
        "build_task_runner",
        lambda: PersistenceUnavailableRunner(),
    )

    await execute_task_module.execute_task.original_func(str(uuid4()))
