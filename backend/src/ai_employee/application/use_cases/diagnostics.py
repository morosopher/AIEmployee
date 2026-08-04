"""定义逾期每日简报的派生告警与耐久诊断任务创建规则。"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from ai_employee.application.use_cases.schedules import (
    ScheduledTaskCreator,
    scheduled_daily_brief_instant,
)

OVERDUE_BRIEF_GRACE: timedelta = timedelta(minutes=15)
_SUCCESSFUL_COMPLETENESS: frozenset[str] = frozenset({"complete", "partial"})


@dataclass(frozen=True, slots=True)
class ActiveUserOverdueBriefSchedule:
    """描述计算一名活动用户当前本地日期逾期状态所需的最小偏好。"""

    user_id: UUID
    timezone: str
    brief_time: time


@dataclass(frozen=True, slots=True)
class DailyBriefOverdueAlert:
    """表示可由 API 返回的逾期简报派生告警，不持久化来源内容。"""

    code: str
    severity: str
    local_date: date
    diagnostic_task_id: UUID | None


class OverdueBriefReader(Protocol):
    """定义逾期判断需要的 PostgreSQL 派生视图读取能力。"""

    async def list_active(self) -> tuple[ActiveUserOverdueBriefSchedule, ...]:
        """返回所有活动用户的时区与每日简报时间快照。"""

    async def list_brief_completeness(
        self, *, user_id: UUID, local_date: date
    ) -> tuple[str, ...]:
        """返回指定用户及本地日期的简报完整度，查询必须强制用户条件。"""


class UserOverdueBriefReader(Protocol):
    """定义已认证用户读取单条逾期告警所需的最小用户隔离端口。"""

    async def get_active_schedule(
        self, *, user_id: UUID
    ) -> ActiveUserOverdueBriefSchedule | None:
        """返回指定活动用户的计划；不存在或停用时返回空。"""

    async def list_brief_completeness(
        self, *, user_id: UUID, local_date: date
    ) -> tuple[str, ...]:
        """返回指定用户本地日期的简报完整度，禁止跨用户聚合。"""

    async def get_diagnostic_task_id(
        self, *, user_id: UUID, local_date: date
    ) -> UUID | None:
        """返回同一用户、本地日期的耐久诊断任务标识，不存在时返回空。"""


def overdue_diagnostic_idempotency_key(*, user_id: UUID, local_date: date) -> str:
    """生成按用户本地日期去重的逾期诊断任务键。

    Args:
        user_id: 任务拥有者，保证两个用户的相同本地日期互不复用。
        local_date: 在用户 IANA 时区中得到的业务日期。

    Returns:
        可传给 ``CreateTaskUseCase`` 的稳定英文幂等键。
    """
    return f"diagnostic:daily-brief:{user_id}:{local_date.isoformat()}"


def is_daily_brief_overdue(
    *,
    now: datetime,
    local_date: date,
    brief_time: time,
    timezone_name: str,
    completeness: tuple[str, ...],
) -> bool:
    """判断当前业务日期是否已超过宽限期且没有可用简报。

    ``scheduled_daily_brief_instant`` 通过 UTC 往返验证 DST 缺失和重复分钟；本函数只在
    其确定的真实瞬间上增加 15 分钟，因此不会把宿主机时区或不存在的墙上时间混入告警。

    Args:
        now: 带时区的当前真实瞬间。
        local_date: 用户时区中的当前日期。
        brief_time: 用户配置的不带时区墙上时间。
        timezone_name: 用户通过设置校验后的 IANA 时区。
        completeness: 当日已持久化简报的完整度列表。

    Returns:
        当超过 grace 且没有 ``complete`` 或 ``partial`` 简报时为 ``True``。

    Raises:
        ValueError: ``now`` 为 naive datetime 时拒绝猜测其时区。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if any(value in _SUCCESSFUL_COMPLETENESS for value in completeness):
        return False
    due_at = scheduled_daily_brief_instant(
        local_date=local_date,
        brief_time=brief_time,
        timezone_name=timezone_name,
    ) + OVERDUE_BRIEF_GRACE
    return now.astimezone(UTC) >= due_at


def daily_brief_overdue_alert(
    *,
    now: datetime,
    schedule: ActiveUserOverdueBriefSchedule,
    completeness: tuple[str, ...],
    diagnostic_task_id: UUID | None,
) -> DailyBriefOverdueAlert | None:
    """按一名已认证用户的本地日期构造逾期告警。

    Args:
        now: 带时区的当前真实瞬间。
        schedule: 已按认证用户过滤的简报偏好快照。
        completeness: 同一用户当日的简报完整度。
        diagnostic_task_id: 同一本地日期已有诊断任务的 UUID；尚未被 Scheduler 创建时为空。

    Returns:
        已超过宽限期且没有 complete/partial 简报时返回 critical 告警，否则返回 ``None``。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local_date = now.astimezone(ZoneInfo(schedule.timezone)).date()
    if not is_daily_brief_overdue(
        now=now,
        local_date=local_date,
        brief_time=schedule.brief_time,
        timezone_name=schedule.timezone,
        completeness=completeness,
    ):
        return None
    return DailyBriefOverdueAlert(
        code="daily_brief_overdue",
        severity="critical",
        local_date=local_date,
        diagnostic_task_id=diagnostic_task_id,
    )


class DispatchOverdueBriefDiagnosticsUseCase:
    """扫描活动用户，为真正逾期的每日简报创建可恢复诊断任务。"""

    def __init__(self, *, reader: OverdueBriefReader, task_creator: ScheduledTaskCreator) -> None:
        """注入用户隔离读取端口及已承诺 Outbox 的任务创建用例。"""
        self._reader = reader
        self._task_creator = task_creator

    async def execute(self, *, now: datetime) -> int:
        """创建当前已逾期用户的诊断任务，并返回尝试创建数量。

        至少一次调度可重复调用本方法；数据库中按用户范围唯一的任务幂等键会复用同一
        TaskRun，故两个 Scheduler 实例重叠不会产生第二个诊断事实。

        Args:
            now: 带时区的扫描时刻，由 Worker 注入 UTC 时钟。

        Returns:
            本轮提交给任务创建用例的逾期用户数量。

        Raises:
            ValueError: 输入时刻没有时区时抛出。
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(UTC)
        created = 0
        for schedule in await self._reader.list_active():
            local_date = now.astimezone(ZoneInfo(schedule.timezone)).date()
            completeness = await self._reader.list_brief_completeness(
                user_id=schedule.user_id,
                local_date=local_date,
            )
            if not is_daily_brief_overdue(
                now=now,
                local_date=local_date,
                brief_time=schedule.brief_time,
                timezone_name=schedule.timezone,
                completeness=completeness,
            ):
                continue
            await self._task_creator.execute(
                user_id=schedule.user_id,
                kind="brief.overdue_diagnostic",
                input_payload={"local_date": local_date.isoformat()},
                idempotency_key=overdue_diagnostic_idempotency_key(
                    user_id=schedule.user_id,
                    local_date=local_date,
                ),
            )
            created += 1
        return created


class GetDailyBriefOverdueAlertUseCase:
    """为已认证用户构造不含来源内容的单条逾期简报告警。"""

    def __init__(self, *, reader: UserOverdueBriefReader) -> None:
        """注入严格按用户过滤的读取端口。

        Args:
            reader: 读取偏好、简报完整度和诊断任务的基础设施适配器；所有方法必须显式
                接受 ``user_id``，避免 API 路由把跨用户可见性藏进查询实现。
        """
        self._reader = reader

    async def execute(
        self, *, user_id: UUID, now: datetime
    ) -> DailyBriefOverdueAlert | None:
        """读取并计算当前用户本地日期的派生告警。

        Args:
            user_id: 由认证会话提供的用户标识，绝不来自请求参数。
            now: 带时区的当前真实瞬间；调用方应注入 UTC 时钟以保证可测试性。

        Returns:
            超过宽限且无 complete/partial 简报时的 critical 告警，否则为 ``None``。

        Raises:
            ValueError: ``now`` 没有时区时拒绝推断宿主机本地时区。
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        schedule = await self._reader.get_active_schedule(user_id=user_id)
        if schedule is None:
            return None
        normalized_now = now.astimezone(UTC)
        local_date = normalized_now.astimezone(ZoneInfo(schedule.timezone)).date()
        completeness = await self._reader.list_brief_completeness(
            user_id=user_id, local_date=local_date
        )
        diagnostic_task_id = await self._reader.get_diagnostic_task_id(
            user_id=user_id, local_date=local_date
        )
        return daily_brief_overdue_alert(
            now=normalized_now,
            schedule=schedule,
            completeness=completeness,
            diagnostic_task_id=diagnostic_task_id,
        )
