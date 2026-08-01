"""实现 PostgreSQL Outbox 的短 claim、提交后投递与安全失败退避。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, update

from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.tasks import OutboxEventModel, TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class TaskEnqueuer(Protocol):
    """定义只把稳定 task_id 发送到外部队列的窄端口。"""

    async def enqueue(self, task_id: UUID) -> None:
        """把任务标识投递到 Taskiq；实现不得携带业务正文或结果。"""


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


def _utc_instant(value: datetime, *, field: str) -> datetime:
    """验证时间带时区并规范为 UTC，避免数据库比较依赖宿主机时区。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


class SqlAlchemyOutboxStore:
    """用多个边界清晰的短事务实现 Outbox claim、确认与失败恢复。

    claim 事务只锁到状态和 ``available_at`` 更新提交为止，绝不在持锁期间调用 Redis。
    成功和失败分别使用新事务，并以 claim 的 ``available_at`` 充当 CAS token，防止已经
    超时并被另一 relay 重新 claim 的旧调用覆盖新结果。
    """

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级 Session factory，不提前占用连接。"""
        self._session_factory = session_factory

    async def claim_due(
        self,
        *,
        now: datetime,
        claim_until: datetime,
        limit: int,
        task_id: UUID | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        """按稳定顺序 claim 到期的未发布任务事件并先把 CREATED 归队。

        Args:
            now: 本轮扫描的 UTC 瞬间。
            claim_until: claim 提交后再次允许扫描的 UTC 瞬间。
            limit: 本事务最多锁定的行数，必须为正。
            task_id: 提交后立即投递时限定的任务；minute relay 省略。

        Returns:
            提交后可安全进行外部 I/O 的不可变事件快照。

        Raises:
            ValueError: 时间不带时区、claim 未向未来移动或 limit 非正。
        """
        now = _utc_instant(now, field="now")
        claim_until = _utc_instant(claim_until, field="claim_until")
        if claim_until <= now:
            raise ValueError("claim_until must be later than now")
        if limit <= 0:
            raise ValueError("limit must be positive")

        async with self._session_factory.begin() as session:
            query = (
                select(OutboxEventModel)
                .where(
                    OutboxEventModel.topic == "task.execute",
                    OutboxEventModel.published_at.is_(None),
                    OutboxEventModel.available_at <= now,
                )
                .order_by(OutboxEventModel.available_at, OutboxEventModel.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            if task_id is not None:
                query = query.where(OutboxEventModel.aggregate_id == task_id)
            events = tuple((await session.scalars(query)).all())

            claimed: list[ClaimedOutboxEvent] = []
            for event in events:
                # 状态归队与 claim 位于同一事务；enqueue 回调从新连接观察时二者都已提交。
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == event.aggregate_id,
                        TaskRunModel.status == TaskStatus.CREATED.value,
                    )
                    .values(status=TaskStatus.QUEUED.value, updated_at=now)
                )
                event.available_at = claim_until
                claimed.append(
                    ClaimedOutboxEvent(
                        event_id=event.id,
                        task_id=event.aggregate_id,
                        claim_until=claim_until,
                        attempt_count=event.attempt_count,
                    )
                )
            return tuple(claimed)

    async def mark_published(self, claim: ClaimedOutboxEvent, *, published_at: datetime) -> bool:
        """在新短事务中以 claim token CAS 写入发布时间。"""
        published_at = _utc_instant(published_at, field="published_at")
        async with self._session_factory.begin() as session:
            event_id = await session.scalar(
                update(OutboxEventModel)
                .where(
                    OutboxEventModel.id == claim.event_id,
                    OutboxEventModel.published_at.is_(None),
                    OutboxEventModel.available_at == claim.claim_until,
                )
                .values(published_at=published_at, last_error=None)
                .returning(OutboxEventModel.id)
            )
        return event_id is not None

    async def mark_failed(
        self,
        claim: ClaimedOutboxEvent,
        *,
        available_at: datetime,
    ) -> bool:
        """在新短事务中记录内容无关错误码、增加次数并安排下一次扫描。

        原始异常文本可能包含 Redis URL、网络地址或第三方载荷，因此数据库只保存固定
        ``queue_enqueue_failed``，详细工程异常由上层结构化日志的脱敏策略另行处理。
        """
        available_at = _utc_instant(available_at, field="available_at")
        async with self._session_factory.begin() as session:
            event_id = await session.scalar(
                update(OutboxEventModel)
                .where(
                    OutboxEventModel.id == claim.event_id,
                    OutboxEventModel.published_at.is_(None),
                    OutboxEventModel.available_at == claim.claim_until,
                )
                .values(
                    attempt_count=OutboxEventModel.attempt_count + 1,
                    last_error="queue_enqueue_failed",
                    available_at=available_at,
                )
                .returning(OutboxEventModel.id)
            )
        return event_id is not None

    async def get_task_status(self, task_id: UUID) -> TaskStatus:
        """读取提交后投递完成时的任务状态，不返回 ORM 对象。"""
        async with self._session_factory() as session:
            status = await session.scalar(
                select(TaskRunModel.status).where(TaskRunModel.id == task_id)
            )
        if status is None:
            raise RuntimeError("dispatched task no longer exists")
        return TaskStatus(status)


class OutboxRelay:
    """协调 claim、事务外 enqueue 以及成功/失败的独立持久确认。"""

    def __init__(
        self,
        *,
        store: SqlAlchemyOutboxStore,
        enqueuer: TaskEnqueuer,
        clock: OutboxClock,
        claim_ttl: timedelta,
        retry_base: timedelta,
        retry_max: timedelta,
    ) -> None:
        """注入 Outbox store、队列端口、时钟及正数 claim/退避配置。

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
        return _utc_instant(self._clock(), field="clock")

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
                await self._enqueuer.enqueue(claim.task_id)
            except Exception:  # noqa: BLE001 - 外部队列边界需统一转为安全持久失败。
                # 只捕获普通外部调用错误；取消信号等 BaseException 继续传播以便进程退出。
                retry_at = self._now() + self._retry_delay(claim.attempt_count)
                await self._store.mark_failed(claim, available_at=retry_at)
            else:
                if await self._store.mark_published(claim, published_at=self._now()):
                    published += 1
        return published
