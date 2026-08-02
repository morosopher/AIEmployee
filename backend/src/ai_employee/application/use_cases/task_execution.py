"""定义持久任务执行用例、租约端口与基础设施无关任务快照。"""

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from ai_employee.domain.errors import DomainError, TransientProviderError
from ai_employee.domain.tasks import JsonValue, TaskStatus


@dataclass(frozen=True, slots=True)
class LeasedTask:
    """保存 Worker 已通过 PostgreSQL CAS 获得的最小任务快照。

    ``started_at`` 是跨 Taskiq 重投持久保留的总预算起点；执行用例不使用当前消息
    接收时间重置它。输入只含内部 JSON 值，不携带 ORM 或队列 SDK 类型。
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
    """定义执行用例所需的窄持久化端口，所有写入均由状态或 owner 做 CAS。"""

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
        """尝试获取任务租约，未命中时返回 ``None``。"""

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

    async def fail_internal(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        failed_at: datetime,
        error_code: str,
    ) -> bool:
        """以前置状态与 owner CAS 把可安全改写的非终态收敛为失败。"""


class _StepDeadlineExceeded(Exception):
    """标记由单节点截止时间触发的内部控制流，不跨 Worker 边界传播。"""


class _LeaseLost(Exception):
    """标记续租 CAS 未命中；旧 Worker 必须立即停止且不写终态。"""


def utc_instant(value: datetime, *, field: str) -> datetime:
    """验证并规范化带时区时间，阻止宿主机本地时区参与执行预算。

    Args:
        value: 待验证的时间瞬间。
        field: 用于稳定错误消息的字段名。

    Returns:
        与输入表示同一瞬间的 UTC 时间。

    Raises:
        ValueError: 输入是不带时区的 ``datetime``。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


class DurableTaskRunner:
    """用持久租约执行节点序列，并把所有非临时错误安全收敛到事实库。

    用例只重新抛出 :class:`TransientProviderError`；Taskiq 的 SmartRetry 因而不会把
    未知程序异常或用户操作错误误判为可重试。每个节点拥有独立预算，整个执行另受
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

        Args:
            store: 实现租约与 owner-safe 状态写入的持久化端口。
            clock: 返回带时区当前瞬间的可替换时钟。
            lease_duration: 单次持有任务的租约时长。
            task_timeout_seconds: 从持久 ``started_at`` 起计算的总预算。
            task_step_timeout_seconds: 每个节点独立预算。
            resolve_steps: 按任务快照解析可重入节点序列的函数。

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
            当前执行者最终仍拥有并处理了任务时为 ``True``；未获租约、丢失 owner 或
            安全失败 CAS 未命中时为 ``False``。

        Raises:
            TransientProviderError: 临时供应商错误已经持久化为 RETRY_SCHEDULED，调用方
                应让队列重试中间件重新投递。
            Exception: 主失败后持久化端口也不可用时保留原异常，由 Worker 入口作为最后
                防线吞掉，避免未知错误进入 SmartRetry。
        """
        owner = lease_owner or f"worker:{os.getpid()}:{uuid4().hex}"
        now = utc_instant(self._clock(), field="clock")
        try:
            await self._store.prepare_retry(task_id=task_id, now=now)
            leased = await self._store.acquire(
                task_id=task_id,
                lease_owner=owner,
                now=now,
                lease_expires_at=now + self._lease_duration,
            )
        except Exception:  # noqa: BLE001 - 前置数据库未知异常必须转成稳定非重试失败。
            # acquisition 可能已在数据库提交但响应丢失；安全失败端口同时接受本 owner，
            # 又以状态/owner CAS 拒绝覆盖另一个有效 Worker 或任何终态。
            return await self._fail_internal(task_id=task_id, lease_owner=owner)
        if leased is None:
            return False

        started_at = utc_instant(leased.started_at, field="started_at")
        # acquisition 自首次写入 started_at 起也消耗总预算；读取新鲜时钟而非复用查询前 now。
        budget_now = utc_instant(self._clock(), field="clock")
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
            # 未知异常的原始文本可能含第三方载荷；只持久化稳定安全码且不交给队列重试。
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

        Args:
            task: 当前 owner 已租用的任务快照。
            lease_owner: 当前执行尝试的稳定 owner。

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
                now = utc_instant(self._clock(), field="clock")
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
            finished_at=utc_instant(self._clock(), field="clock"),
            error_code=error_code,
        )

    async def _fail_internal(self, *, task_id: UUID, lease_owner: str) -> bool:
        """尝试持久化前置未知异常，数据库不可写时允许异常上浮到 Worker 入口。

        正常可写路径必须写入 ``FAILED/task_execution_internal_error``；入口吞错只用于
        主失败后数据库也无法持久化的最后防线，不能替代这里的安全失败意图。
        """
        return await self._store.fail_internal(
            task_id=task_id,
            lease_owner=lease_owner,
            failed_at=utc_instant(self._clock(), field="clock"),
            error_code="task_execution_internal_error",
        )
