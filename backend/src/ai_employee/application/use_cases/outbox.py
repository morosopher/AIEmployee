"""定义 PostgreSQL Outbox 的 claim 后投递编排、端口与不可变快照。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from ai_employee.domain.tasks import TaskStatus


class TaskEnqueuer(Protocol):
    """定义只把稳定 task_id 发送到外部队列的窄端口。"""

    async def enqueue(self, task_id: UUID, *, resume: str | None = None) -> None:
        """把任务标识投递到队列；实现不得携带业务正文或结果。"""


class OutboxClock(Protocol):
    """提供可替换的带时区当前时间，便于退避与 claim 测试保持确定。"""

    def __call__(self) -> datetime:
        """返回当前时间瞬间；Relay 会进一步验证并规范为 UTC。"""


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    """表示 claim 事务已经提交、可在事务外投递的一条安全事件快照。"""

    event_id: UUID
    task_id: UUID
    claim_until: datetime
    attempt_count: int
    resume: str | None = None


class OutboxStore(Protocol):
    """定义 Outbox relay 所需的 claim、确认、失败与状态查询端口。"""

    async def claim_due(
        self,
        *,
        now: datetime,
        claim_until: datetime,
        limit: int,
        task_id: UUID | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        """claim 到期未发布事件，并返回提交后的不可变快照。"""

    async def mark_published(
        self,
        claim: ClaimedOutboxEvent,
        *,
        published_at: datetime,
    ) -> bool:
        """以 claim token CAS 确认一条事件已经投递。"""

    async def mark_failed(
        self,
        claim: ClaimedOutboxEvent,
        *,
        available_at: datetime,
    ) -> bool:
        """以 claim token CAS 记录安全错误并安排下一次投递。"""

    async def get_task_status(self, task_id: UUID) -> TaskStatus:
        """返回指定持久任务的当前状态。"""


def utc_instant(value: datetime, *, field: str) -> datetime:
    """验证时间带时区并规范为 UTC，避免业务比较依赖宿主机时区。

    Args:
        value: 待验证的时间瞬间。
        field: 用于稳定错误消息的字段名。

    Returns:
        与输入表示同一瞬间的 UTC 时间。

    Raises:
        ValueError: 输入不带时区。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


class OutboxRelay:
    """协调 claim、事务外 enqueue 以及成功/失败的独立持久确认。"""

    def __init__(
        self,
        *,
        store: OutboxStore,
        enqueuer: TaskEnqueuer,
        clock: OutboxClock,
        claim_ttl: timedelta,
        retry_base: timedelta,
        retry_max: timedelta,
    ) -> None:
        """注入 Outbox store、队列端口、时钟及正数 claim/退避配置。

        Args:
            store: 持久 claim 与结果确认端口。
            enqueuer: 只发送 task_id 的队列适配器。
            clock: 可替换的带时区时钟。
            claim_ttl: enqueue 结果未知时的恢复窗口。
            retry_base: 首次投递失败的退避时长。
            retry_max: 指数退避上限。

        Raises:
            ValueError: claim 或退避参数非正，或初始退避高于上限。
        """
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        if retry_base <= timedelta(0) or retry_max <= timedelta(0):
            raise ValueError("retry delays must be positive")
        if retry_base > retry_max:
            raise ValueError("retry_base cannot exceed retry_max")
        self._store = store
        self._enqueuer = enqueuer
        self._clock = clock
        self._claim_ttl = claim_ttl
        self._retry_base = retry_base
        self._retry_max = retry_max

    def _now(self) -> datetime:
        """调用并验证注入时钟，统一返回 UTC 瞬间。"""
        return utc_instant(self._clock(), field="clock")

    def _retry_delay(self, attempt_count: int) -> timedelta:
        """计算封顶指数退避，避免巨大整数指数消耗无意义资源。"""
        if attempt_count < 0:
            raise ValueError("attempt_count cannot be negative")
        delay = self._retry_base
        for _ in range(min(attempt_count, 63)):
            delay = min(delay * 2, self._retry_max)
            if delay == self._retry_max:
                break
        return delay

    async def relay_once(self, *, limit: int = 100) -> int:
        """claim 一批到期事件并逐条投递，返回成功发布数量。"""
        now = self._now()
        claims = await self._store.claim_due(
            now=now,
            claim_until=now + self._claim_ttl,
            limit=limit,
        )
        return await self._deliver(claims)

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """在创建事务提交后沿同一 claim/transition/enqueue 路径立即投递一项任务。"""
        now = self._now()
        claims = await self._store.claim_due(
            now=now,
            claim_until=now + self._claim_ttl,
            limit=1,
            task_id=task_id,
        )
        await self._deliver(claims)
        return await self._store.get_task_status(task_id)

    async def _deliver(self, claims: tuple[ClaimedOutboxEvent, ...]) -> int:
        """在 claim 事务之外执行队列 I/O，并为每条结果开启独立短确认事务。"""
        published = 0
        for claim in claims:
            try:
                if claim.resume is None:
                    await self._enqueuer.enqueue(claim.task_id)
                else:
                    await self._enqueuer.enqueue(claim.task_id, resume=claim.resume)
            except Exception:  # noqa: BLE001 - 外部队列边界需统一转为安全持久失败。
                # 只捕获普通外部调用错误；取消信号等 BaseException 继续传播以便进程退出。
                retry_at = self._now() + self._retry_delay(claim.attempt_count)
                await self._store.mark_failed(claim, available_at=retry_at)
            else:
                if await self._store.mark_published(claim, published_at=self._now()):
                    published += 1
        return published
