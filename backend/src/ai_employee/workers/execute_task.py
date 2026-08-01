"""实现持久执行租约、节点超时预算与安全错误终止。"""

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, update

from ai_employee.config import get_settings
from ai_employee.domain.errors import (
    DomainError,
    InternalInvariantError,
    TransientProviderError,
)
from ai_employee.domain.tasks import JsonValue, TaskStatus
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.queue.broker import broker


@dataclass(frozen=True, slots=True)
class LeasedTask:
    """保存 Worker 已通过 PostgreSQL CAS 获得的最小任务快照。

    ``started_at`` 是跨 Taskiq 重投持久保留的总预算起点；Runner 不使用当前消息接收
    时间重置它。输入只含内部 JSON 值，不携带 ORM 或队列 SDK 类型。
    """

    task_id: UUID
    kind: str
    input_payload: dict[str, JsonValue]
    started_at: datetime


class TaskExecutionStep(Protocol):
    """定义一个可重入、具名且受独立超时保护的执行节点。"""

    name: str

    async def execute(self, task: LeasedTask) -> None:
        """执行当前节点；副作用必须由后续具体 Graph 保证幂等。"""


class TaskExecutionStore(Protocol):
    """定义 Runner 所需的窄持久化端口，所有写入均由 owner 做 CAS。"""

    async def prepare_retry(self, *, task_id: UUID, now: datetime) -> None:
        """把 RETRY_SCHEDULED 原子归队，其他状态保持不变。"""

    async def acquire(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> LeasedTask | None:
        """尝试以单条条件 UPDATE 获取任务租约。"""

    async def renew(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> bool:
        """仅当前 owner 能在节点边界续租。"""

    async def finish(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        status: TaskStatus,
        finished_at: datetime,
        error_code: str | None,
    ) -> bool:
        """仅当前 owner 能清理租约并写入终止或重试状态。"""


class _StepDeadlineExceeded(Exception):
    """标记由单节点截止时间触发的内部控制流，不跨 Worker 边界传播。"""


class _LeaseLost(Exception):
    """标记续租 CAS 未命中；旧 Worker 必须立即停止且不写终态。"""


def _utc_instant(value: datetime, *, field: str) -> datetime:
    """验证并规范化带时区时间，阻止宿主机本地时区参与租约计算。

    Args:
        value: 待验证的时间瞬间。
        field: 用于稳定错误消息的字段名。

    Returns:
        与输入表示同一瞬间的 UTC 时间。

    Raises:
        ValueError: 输入是不带时区的 datetime。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


class DurableTaskRunner:
    """用持久租约执行节点序列，并把所有非临时错误安全收敛到 PostgreSQL。

    Runner 只重新抛出 :class:`TransientProviderError`；Taskiq 的 SmartRetry 因而不会
    把未知程序异常或用户操作错误误判为可重试。每个节点拥有独立预算，整个执行另受
    从持久 ``started_at`` 计算的剩余总预算约束。节点之间必须续租，CAS 失败即放弃提交。
    """

    def __init__(
        self,
        *,
        store: TaskExecutionStore,
        clock: Callable[[], datetime],
        lease_duration: timedelta,
        task_timeout_seconds: float,
        task_step_timeout_seconds: float,
        resolve_steps: Callable[[LeasedTask], Sequence[TaskExecutionStep]],
    ) -> None:
        """注入持久化、确定性时钟、预算与任务节点解析器。

        Raises:
            ValueError: 租约或超时参数不是正数，或单步预算超过任务总预算。
        """
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if task_timeout_seconds <= 0 or task_step_timeout_seconds <= 0:
            raise ValueError("task timeout budgets must be positive")
        if task_step_timeout_seconds > task_timeout_seconds:
            raise ValueError("task step timeout cannot exceed task timeout")
        if lease_duration.total_seconds() < task_step_timeout_seconds:
            raise ValueError("lease_duration cannot be shorter than task step timeout")
        self._store = store
        self._clock = clock
        self._lease_duration = lease_duration
        self._task_timeout_seconds = task_timeout_seconds
        self._task_step_timeout_seconds = task_step_timeout_seconds
        self._resolve_steps = resolve_steps

    async def run(self, task_id: UUID, lease_owner: str | None = None) -> bool:
        """获取租约并执行一次持久任务尝试。

        Args:
            task_id: PostgreSQL 中的稳定任务 UUID。
            lease_owner: 当前进程与投递尝试唯一的租约 owner；省略时生成不含主机资料的
                进程与随机标识。

        Returns:
            当前 Worker 最终仍拥有并处理了任务时为 ``True``；未获租约或执行期间丢失
            owner 时为 ``False``。

        Raises:
            TransientProviderError: 临时供应商错误已经持久化为 RETRY_SCHEDULED，调用方
                应让 Taskiq SmartRetry 重新投递。
        """
        owner = lease_owner or f"worker:{os.getpid()}:{uuid4().hex}"
        now = _utc_instant(self._clock(), field="clock")
        await self._store.prepare_retry(task_id=task_id, now=now)
        leased = await self._store.acquire(
            task_id=task_id,
            lease_owner=owner,
            now=now,
            lease_expires_at=now + self._lease_duration,
        )
        if leased is None:
            return False

        started_at = _utc_instant(leased.started_at, field="started_at")
        # acquisition 自首次写入 started_at 起也消耗总预算；读取新鲜时钟而非复用查询前 now。
        budget_now = _utc_instant(self._clock(), field="clock")
        remaining = self._task_timeout_seconds - (budget_now - started_at).total_seconds()
        if remaining <= 0:
            return await self._finish(
                leased,
                lease_owner=owner,
                status=TaskStatus.FAILED,
                error_code="task_timeout",
            )

        try:
            async with asyncio.timeout(remaining):
                await self._run_steps(leased, lease_owner=owner)
        except _LeaseLost:
            return False
        except _StepDeadlineExceeded:
            return await self._finish(
                leased,
                lease_owner=owner,
                status=TaskStatus.FAILED,
                error_code="task_step_timeout",
            )
        except TimeoutError:
            return await self._finish(
                leased,
                lease_owner=owner,
                status=TaskStatus.FAILED,
                error_code="task_timeout",
            )
        except TransientProviderError as error:
            persisted = await self._finish(
                leased,
                lease_owner=owner,
                status=TaskStatus.RETRY_SCHEDULED,
                error_code=error.error_code,
            )
            if persisted:
                raise
            return False
        except DomainError as error:
            return await self._finish(
                leased,
                lease_owner=owner,
                status=TaskStatus.FAILED,
                error_code=error.error_code,
            )
        except Exception:  # noqa: BLE001 - 未知节点异常必须在持久边界安全终止。
            # 未知异常的原始文本可能含第三方载荷；只持久化稳定安全码且不再抛给 SmartRetry。
            return await self._finish(
                leased,
                lease_owner=owner,
                status=TaskStatus.FAILED,
                error_code="internal_worker_error",
            )

        return await self._finish(
            leased,
            lease_owner=owner,
            status=TaskStatus.SUCCEEDED,
            error_code=None,
        )

    async def _run_steps(self, task: LeasedTask, *, lease_owner: str) -> None:
        """按顺序执行节点，并只在仍有下一节点时续租。

        Raises:
            _StepDeadlineExceeded: 当前节点耗尽独立预算。
            _LeaseLost: 节点边界续租 CAS 未命中。
        """
        steps = tuple(self._resolve_steps(task))
        for index, step in enumerate(steps):
            try:
                async with asyncio.timeout(self._task_step_timeout_seconds):
                    await step.execute(task)
            except TimeoutError as error:
                raise _StepDeadlineExceeded from error

            if index + 1 < len(steps):
                now = _utc_instant(self._clock(), field="clock")
                renewed = await self._store.renew(
                    task_id=task.task_id,
                    lease_owner=lease_owner,
                    lease_expires_at=now + self._lease_duration,
                )
                if not renewed:
                    raise _LeaseLost

    async def _finish(
        self,
        task: LeasedTask,
        *,
        lease_owner: str,
        status: TaskStatus,
        error_code: str | None,
    ) -> bool:
        """以当前 owner CAS 写状态并清租约，返回是否仍拥有提交权。"""
        return await self._store.finish(
            task_id=task.task_id,
            lease_owner=lease_owner,
            status=status,
            finished_at=_utc_instant(self._clock(), field="clock"),
            error_code=error_code,
        )


class SqlAlchemyTaskExecutionStore:
    """用 PostgreSQL 条件 UPDATE 实现租约、续租与 owner 保护的状态写入。

    每个方法使用独立短事务。租约 acquisition 在一条 ``UPDATE ... RETURNING`` 中同时
    判断状态、过期时间并写 owner，避免先查后写竞态；完成、失败和 RETRY_SCHEDULED
    同样以当前 ``lease_owner`` 做 CAS，丢失租约的 Worker 无法提交陈旧终态。
    """

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级 Session factory，不在构造时占用连接。"""
        self._session_factory = session_factory

    async def prepare_retry(self, *, task_id: UUID, now: datetime) -> None:
        """在 Taskiq 重投入口把 RETRY_SCHEDULED 原子转换为 QUEUED。

        只有 RETRY_SCHEDULED 会命中；首次投递的 CREATED/QUEUED、正在运行及终态保持
        原样。归队事务先于下一次租约 acquisition 提交，保证重试状态变化可恢复、可审计。
        """
        now = _utc_instant(now, field="now")
        async with self._session_factory.begin() as session:
            row = (
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == task_id,
                        TaskRunModel.status == TaskStatus.RETRY_SCHEDULED.value,
                    )
                    .values(
                        status=TaskStatus.QUEUED.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                    .returning(TaskRunModel.user_id)
                )
            ).one_or_none()
            if row is not None:
                session.add(
                    AuditEventModel(
                        user_id=row.user_id,
                        task_id=task_id,
                        event_type="task.queued",
                        actor_type="system",
                        actor_id=None,
                        event_metadata={"reason": "taskiq_retry"},
                    )
                )

    async def acquire(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> LeasedTask | None:
        """以一条条件 UPDATE 获取 QUEUED 或租约已过期 RUNNING 任务。

        ``started_at = coalesce(started_at, now)`` 只在首次尝试写入；接管过期租约和 Taskiq
        重投都沿用同一总预算起点。未命中表示任务不存在、处于终态、已有未过期 owner，
        或 QUEUED 行仍带有效 claim，调用方必须无副作用退出。
        """
        now = _utc_instant(now, field="now")
        lease_expires_at = _utc_instant(lease_expires_at, field="lease_expires_at")
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be later than now")

        eligible_status = or_(
            TaskRunModel.status == TaskStatus.QUEUED.value,
            and_(
                TaskRunModel.status == TaskStatus.RUNNING.value,
                TaskRunModel.lease_expires_at.is_not(None),
                TaskRunModel.lease_expires_at <= now,
            ),
        )
        lease_available = or_(
            TaskRunModel.lease_expires_at.is_(None),
            TaskRunModel.lease_expires_at <= now,
        )
        async with self._session_factory.begin() as session:
            row = (
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == task_id,
                        eligible_status,
                        lease_available,
                    )
                    .values(
                        status=TaskStatus.RUNNING.value,
                        lease_owner=lease_owner,
                        lease_expires_at=lease_expires_at,
                        started_at=func.coalesce(TaskRunModel.started_at, now),
                        attempt_count=TaskRunModel.attempt_count + 1,
                        updated_at=now,
                    )
                    .returning(
                        TaskRunModel.id,
                        TaskRunModel.user_id,
                        TaskRunModel.kind,
                        TaskRunModel.input_payload,
                        TaskRunModel.started_at,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            session.add(
                AuditEventModel(
                    user_id=row.user_id,
                    task_id=row.id,
                    event_type="task.running",
                    actor_type="worker",
                    actor_id=lease_owner,
                    event_metadata={"attempt_started": True},
                )
            )
            return LeasedTask(
                task_id=row.id,
                kind=row.kind,
                input_payload=row.input_payload,
                started_at=row.started_at,
            )

    async def renew(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> bool:
        """仅当前 RUNNING owner 能把租约延长到未来时刻。"""
        lease_expires_at = _utc_instant(lease_expires_at, field="lease_expires_at")
        async with self._session_factory.begin() as session:
            task_id_result = await session.scalar(
                update(TaskRunModel)
                .where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                )
                .values(lease_expires_at=lease_expires_at)
                .returning(TaskRunModel.id)
            )
        return task_id_result is not None

    async def finish(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        status: TaskStatus,
        finished_at: datetime,
        error_code: str | None,
    ) -> bool:
        """以 owner CAS 写入批准状态并清理租约。

        ``RETRY_SCHEDULED`` 释放租约但不写 ``finished_at``；SUCCEEDED、FAILED 与
        CANCELLED 是真正终态并冻结完成时间。其他目标状态不属于 Runner 完成边界，直接
        拒绝以防绕过领域状态机。
        """
        allowed = {
            TaskStatus.RETRY_SCHEDULED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
        if status not in allowed:
            raise ValueError("finish status is not allowed")
        finished_at = _utc_instant(finished_at, field="finished_at")
        terminal = status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
        async with self._session_factory.begin() as session:
            row = (
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == task_id,
                        TaskRunModel.status == TaskStatus.RUNNING.value,
                        TaskRunModel.lease_owner == lease_owner,
                    )
                    .values(
                        status=status.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        finished_at=finished_at if terminal else None,
                        error_code=error_code,
                        updated_at=finished_at,
                    )
                    .returning(TaskRunModel.user_id)
                )
            ).one_or_none()
            if row is None:
                return False
            event_metadata: dict[str, JsonValue] = {"status": status.value}
            if error_code is not None:
                event_metadata["error_code"] = error_code
            session.add(
                AuditEventModel(
                    user_id=row.user_id,
                    task_id=task_id,
                    event_type=f"task.{status.value}",
                    actor_type="worker",
                    actor_id=lease_owner,
                    event_metadata=event_metadata,
                )
            )
            return True


class _MissingTaskHandlerStep:
    """在真实 Graph 尚未接入时以稳定内部错误终止，绝不伪造任务成功。"""

    name = "resolve_task_handler"

    async def execute(self, task: LeasedTask) -> None:
        """拒绝执行尚无 M1 handler 的任务种类。

        Args:
            task: 已获取租约的任务快照。

        Raises:
            InternalInvariantError: 当前 Task 7 尚未注册后续 LangGraph handler。
        """
        raise InternalInvariantError(
            error_code="task_handler_not_registered",
            message="task handler is not registered",
            metadata={"task_kind": task.kind},
        )


@lru_cache
def build_task_runner() -> DurableTaskRunner:
    """按进程配置构造并缓存共享数据库连接池与持久 Runner。

    Task 7 只建立可靠执行底座；Task 8/15 会把 resolver 替换为审批和简报 Graph 节点。
    在此之前使用显式失败节点，避免收到队列消息后把未实现业务错误标记为成功。
    """
    settings = get_settings()
    session_factory = build_session_factory(settings.database_url)
    return DurableTaskRunner(
        store=SqlAlchemyTaskExecutionStore(session_factory),
        clock=lambda: datetime.now(UTC),
        lease_duration=timedelta(seconds=settings.task_lease_seconds),
        task_timeout_seconds=settings.task_timeout_seconds,
        task_step_timeout_seconds=settings.task_step_timeout_seconds,
        resolve_steps=lambda task: (_MissingTaskHandlerStep(),),
    )


@broker.task(retry_on_error=True)
async def execute_task(task_id: str) -> None:
    """Taskiq 入口：解析 task_id，并只允许已持久化的临时供应商错误触发重试。

    Args:
        task_id: Outbox 发送的规范 UUID 字符串；消息不包含任务正文或结果。

    Raises:
        TransientProviderError: Runner 已把状态写为 RETRY_SCHEDULED 后原样重新抛出，交由
            SmartRetryMiddleware 按固定策略重新投递。
    """
    try:
        parsed_task_id = UUID(task_id)
    except ValueError:
        # 队列载荷由内部 enqueue adapter 生成；非法值不能关联持久任务，也不得反复重试。
        return

    try:
        await build_task_runner().run(parsed_task_id)
    except TransientProviderError:
        raise
    except Exception:  # noqa: BLE001 - Taskiq 入口不得让未知异常进入 SmartRetry。
        # Runner 已负责分类和安全持久化。构建/边界未知异常也不能泄漏给 SmartRetry，避免
        # 将程序不变量错误放大为重复投递风暴；这里只安全终止，不记录原始异常文本。
        return
