"""定义用户本地每日简报调度规则与到期扫描应用用例。"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from ai_employee.domain.tasks import JsonValue


@dataclass(frozen=True, slots=True)
class ActiveUserBriefSchedule:
    """表示从 PostgreSQL 读取的一名活动用户的最小简报计划。"""

    user_id: UUID
    timezone: str
    brief_time: time


class ActiveUserScheduleReader(Protocol):
    """读取活动用户持久化时区与简报时间的应用端口。"""

    async def list_active(self) -> tuple[ActiveUserBriefSchedule, ...]:
        """返回当前全部活动用户的计划快照。"""


class ScheduledTaskCreator(Protocol):
    """定义到期扫描创建可信任务所需的最小用例端口。"""

    async def execute(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> object:
        """幂等创建任务；具体返回值不影响本轮扫描继续处理其他用户。"""


def _valid_utc_instants(local_wall_time: datetime, timezone: ZoneInfo) -> tuple[datetime, ...]:
    """返回一个本地墙上时间真实对应的全部 UTC 瞬间。

    ``zoneinfo`` 允许直接构造不存在的本地时间，因此必须往返 UTC 验证；秋季重复小时
    可能有两个有效 fold，普通分钟的两个 fold 会折叠为同一 UTC 瞬间。
    """
    instants: set[datetime] = set()
    for fold in (0, 1):
        candidate = local_wall_time.replace(tzinfo=timezone, fold=fold)
        instant = candidate.astimezone(UTC)
        round_tripped = instant.astimezone(timezone)
        if round_tripped.replace(tzinfo=None) == local_wall_time:
            instants.add(instant)
    return tuple(sorted(instants))


def scheduled_daily_brief_instant(
    *,
    local_date: date,
    brief_time: time,
    timezone_name: str,
) -> datetime:
    """计算用户某本地日期的唯一简报到期瞬间。

    秋季重复分钟选择较早 UTC 瞬间，使第二个 fold 只重放同一幂等键；春季缺失分钟逐分
    向后查找 gap 后首个有效本地分钟。

    Raises:
        ValueError: ``brief_time`` 自带时区，或后续一整天内找不到有效分钟。
        ZoneInfoNotFoundError: 时区名称无效。
    """
    if brief_time.tzinfo is not None:
        raise ValueError("brief_time must not carry timezone information")

    timezone = ZoneInfo(timezone_name)
    local_wall_time = datetime.combine(local_date, brief_time)
    for minute_offset in range(24 * 60 + 1):
        candidate = local_wall_time + timedelta(minutes=minute_offset)
        instants = _valid_utc_instants(candidate, timezone)
        if instants:
            return instants[0]
    raise ValueError("no valid local minute found within one day after brief_time")


def is_daily_brief_due(
    *,
    now: datetime,
    local_date: date,
    brief_time: time,
    timezone_name: str,
) -> bool:
    """使用 timezone-aware instant 判断指定用户本地日期的简报是否已到期。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC) >= scheduled_daily_brief_instant(
        local_date=local_date,
        brief_time=brief_time,
        timezone_name=timezone_name,
    )


def daily_brief_idempotency_key(*, user_id: UUID, local_date: date) -> str:
    """生成用户、本地日期与固定 schedule kind 共同组成的幂等键。"""
    return f"daily_brief:{user_id}:{local_date.isoformat()}:scheduled"


class DispatchDueDailyBriefsUseCase:
    """扫描活动用户计划，并为当前真实瞬间已经到期的本地日期幂等创建简报任务。"""

    def __init__(
        self,
        *,
        reader: ActiveUserScheduleReader,
        task_creator: ScheduledTaskCreator,
    ) -> None:
        """注入 PostgreSQL 计划 reader 与带提交后 Outbox 投递的任务创建端口。"""
        self._reader = reader
        self._task_creator = task_creator

    async def execute(self, *, now: datetime) -> int:
        """为所有到期活动用户调用任务创建用例，并返回调用数量。

        对每名用户先把同一真实 UTC 瞬间转换为其 IANA 时区，再使用用户本地日期构造
        唯一键。fall-back 第二个 fold 重放同一键，spring-forward gap 则由调度瞬间函数
        推进到首个有效分钟。

        Raises:
            ValueError: ``now`` 不带时区。
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(UTC)
        created_count = 0
        for schedule in await self._reader.list_active():
            timezone = ZoneInfo(schedule.timezone)
            local_date = now.astimezone(timezone).date()
            if not is_daily_brief_due(
                now=now,
                local_date=local_date,
                brief_time=schedule.brief_time,
                timezone_name=schedule.timezone,
            ):
                continue
            await self._task_creator.execute(
                user_id=schedule.user_id,
                kind="daily_brief",
                input_payload={
                    "local_date": local_date.isoformat(),
                    "schedule_kind": "scheduled",
                },
                idempotency_key=daily_brief_idempotency_key(
                    user_id=schedule.user_id,
                    local_date=local_date,
                ),
            )
            created_count += 1
        return created_count
