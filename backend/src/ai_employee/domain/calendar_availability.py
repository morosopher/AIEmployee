"""基于用户本人同步日历确定性生成 M2 会议候选时间。"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ai_employee.domain.settings import WeeklyWorkingHours

type AvailabilityCompleteness = Literal["complete", "partial"]


@dataclass(frozen=True, slots=True)
class AvailabilityEvent:
    """表示候选算法所需的最小本人日历忙碌事实。

    标题、描述、地点、组织者和参会人都不进入该领域边界。全天事件仍使用同步层已规范化
    的两个 UTC 瞬间，并由 ``all_day`` 显式标记；取消或透明事实由算法安全忽略。
    """

    starts_at: datetime
    ends_at: datetime
    all_day: bool
    transparency: str
    status: str

    def __post_init__(self) -> None:
        """验证区间为严格正向 aware datetime，避免隐式宿主时区参与。"""
        if type(self.all_day) is not bool:
            raise TypeError("availability event all_day must be bool")
        if not isinstance(self.transparency, str) or not isinstance(self.status, str):
            raise TypeError("availability event status fields must be strings")
        start = _aware_utc(self.starts_at, field="availability event starts_at")
        end = _aware_utc(self.ends_at, field="availability event ends_at")
        if end <= start:
            raise ValueError("availability event ends_at must be after starts_at")


@dataclass(frozen=True, slots=True)
class CandidateTime:
    """表示一个精确 UTC 起止候选；展示层可按同一用户 IANA 时区转换。"""

    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        """强制候选为正向 UTC 瞬间，禁止携带模糊墙上时间。"""
        start = _aware_utc(self.starts_at, field="candidate starts_at")
        end = _aware_utc(self.ends_at, field="candidate ends_at")
        if self.starts_at.utcoffset() != timedelta() or self.ends_at.utcoffset() != timedelta():
            raise ValueError("candidate times must be represented in UTC")
        if end <= start:
            raise ValueError("candidate ends_at must be after starts_at")


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    """返回候选、本人日历完整性和固定的未查询参会人事实。"""

    candidates: tuple[CandidateTime, ...]
    completeness: AvailabilityCompleteness
    missing_connection_ids: tuple[UUID, ...]
    attendee_availability_checked: Literal[False] = False

    def __post_init__(self) -> None:
        """冻结集合并确保完整性不能与缺失连接事实矛盾。"""
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(candidate, CandidateTime) for candidate in self.candidates
        ):
            raise TypeError("availability candidates must be a CandidateTime tuple")
        if not isinstance(self.missing_connection_ids, tuple) or any(
            not isinstance(connection_id, UUID)
            for connection_id in self.missing_connection_ids
        ):
            raise TypeError("missing connection ids must be a UUID tuple")
        expected: AvailabilityCompleteness = (
            "partial" if self.missing_connection_ids else "complete"
        )
        if self.completeness != expected:
            raise ValueError("availability completeness contradicts missing connections")
        if self.attendee_availability_checked is not False:
            raise ValueError("M2 availability never checks attendee Free/Busy")


def suggest_meeting_times(
    *,
    requested_duration: timedelta,
    search_start: datetime,
    timezone: str,
    working_hours: WeeklyWorkingHours,
    meeting_buffer: timedelta,
    events: tuple[AvailabilityEvent, ...],
    missing_connection_ids: tuple[UUID, ...] = (),
    horizon_days: int = 14,
    grid_minutes: int = 15,
    limit: int = 3,
) -> AvailabilityResult:
    """在本人工作时间和同步事件上生成最多三个确定性候选。

    算法只消费调用方提供的本人日历事件；参数中刻意没有 attendee 或 Free/Busy 端口。
    春季跳时的不存在墙上时间会被 UTC 往返验证拒绝；秋季回拨固定选择 ``fold=0``。若
    请求区间两端 offset 不同，也会拒绝该候选，防止用户看到的墙上时长与批准时长不同。

    Args:
        requested_duration: 严格正向的原始会议时长。
        search_start: 搜索下界的带时区真实瞬间。
        timezone: 用户显式 IANA 时区。
        working_hours: 已在设置边界验证的不重叠周工作时间。
        meeting_buffer: 每个忙碌事件前后共同扩展的 0～120 分钟缓冲。
        events: 用户本人所有已同步日历事件。
        missing_connection_ids: 同步陈旧或失败的本人连接 ID。
        horizon_days: 从搜索本地日开始包含的天数，M2 最大十四天。
        grid_minutes: 墙上时间网格分钟数，M2 固定使用十五分钟。
        limit: 最大候选数，M2 最大三项。

    Returns:
        UTC 候选及 complete/partial 完整性事实。

    Raises:
        TypeError: 输入集合或数值不是声明的精确类型。
        ValueError: 时长、时区、缓冲、网格、范围或事件不满足领域约束。
    """
    if not isinstance(requested_duration, timedelta):
        raise TypeError("requested duration must be timedelta")
    if requested_duration <= timedelta():
        raise ValueError("requested duration must be positive")
    search_start_utc = _aware_utc(search_start, field="availability search_start")
    if not isinstance(timezone, str) or timezone == "":
        raise ValueError("availability timezone must be a nonempty IANA name")
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("availability timezone must be a valid IANA name") from error
    if not isinstance(working_hours, WeeklyWorkingHours):
        raise TypeError("working_hours must be WeeklyWorkingHours")
    buffer_minutes = _whole_minutes(meeting_buffer, field="meeting buffer")
    if not 0 <= buffer_minutes <= 120:
        raise ValueError("meeting buffer must be between 0 and 120 minutes")
    if type(horizon_days) is not int or not 1 <= horizon_days <= 14:
        raise ValueError("horizon_days must be between 1 and 14")
    if (
        type(grid_minutes) is not int
        or grid_minutes <= 0
        or grid_minutes > 60
        or 60 % grid_minutes != 0
    ):
        raise ValueError("grid_minutes must be a positive divisor of 60")
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("availability limit must be between 1 and 3")
    if not isinstance(events, tuple) or any(
        not isinstance(event, AvailabilityEvent) for event in events
    ):
        raise TypeError("events must be an AvailabilityEvent tuple")
    if not isinstance(missing_connection_ids, tuple) or any(
        not isinstance(connection_id, UUID) for connection_id in missing_connection_ids
    ):
        raise TypeError("missing_connection_ids must be a UUID tuple")

    normalized_missing = tuple(
        sorted(set(missing_connection_ids), key=str)
    )
    completeness: AvailabilityCompleteness = (
        "partial" if normalized_missing else "complete"
    )
    # 先把所有可用事件压缩成排序的半开忙碌区间；候选扫描只向前移动索引，避免每个
    # 候选重新遍历完整事件集合。事件不能在数据库层 LIMIT，合并必须看到全部事实。
    buffered_busy = _merge_buffered_intervals(events, meeting_buffer)
    local_start_date = search_start_utc.astimezone(zone).date()
    candidates: list[CandidateTime] = []
    seen: set[tuple[datetime, datetime]] = set()
    busy_index = 0

    for day_offset in range(horizon_days):
        local_day = local_start_date + timedelta(days=day_offset)
        for interval in working_hours.intervals_for(local_day.weekday()):
            for wall_start in _grid_points(
                local_day,
                interval.start,
                interval.end,
                grid_minutes=grid_minutes,
            ):
                wall_end = wall_start + requested_duration
                interval_end = datetime.combine(local_day, interval.end)
                if wall_end > interval_end:
                    continue
                aware_start = _resolve_wall_time(wall_start, zone)
                aware_end = _resolve_wall_time(wall_end, zone)
                if aware_start is None or aware_end is None:
                    continue
                if aware_start.utcoffset() != aware_end.utcoffset():
                    # DST 跳变发生在批准区间内时，真实时长会与墙上显示时长不同。
                    continue
                candidate_start = aware_start.astimezone(UTC)
                candidate_end = aware_end.astimezone(UTC)
                if candidate_end - candidate_start != requested_duration:
                    continue
                if candidate_start < search_start_utc:
                    continue
                identity = (candidate_start, candidate_end)
                while busy_index < len(buffered_busy) and buffered_busy[busy_index][1] <= candidate_start:
                    busy_index += 1
                overlaps_busy = (
                    busy_index < len(buffered_busy)
                    and buffered_busy[busy_index][0] < candidate_end
                )
                if identity in seen or overlaps_busy:
                    continue
                seen.add(identity)
                candidates.append(CandidateTime(candidate_start, candidate_end))
                if len(candidates) == limit:
                    return AvailabilityResult(
                        tuple(candidates),
                        completeness,
                        normalized_missing,
                    )

    return AvailabilityResult(tuple(candidates), completeness, normalized_missing)


def _whole_minutes(value: timedelta, *, field: str) -> int:
    """把非负整分钟 timedelta 收窄为整数，拒绝秒/微秒猜测。"""
    if not isinstance(value, timedelta):
        raise TypeError(f"{field} must be timedelta")
    seconds = value.total_seconds()
    if seconds < 0 or seconds % 60 != 0:
        raise ValueError(f"{field} must contain non-negative whole minutes")
    return int(seconds // 60)


def _aware_utc(value: datetime, *, field: str) -> datetime:
    """要求带时区 datetime 并规范到 UTC。"""
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _merge_buffered_intervals(
    events: tuple[AvailabilityEvent, ...],
    meeting_buffer: timedelta,
) -> tuple[tuple[datetime, datetime], ...]:
    """过滤、缓冲、排序并合并本人日历忙碌区间。

    使用半开区间 ``[start, end)``：重叠或刚好相邻的区间都可以合并，因为候选结束在
    ``busy_start`` 的瞬间不冲突，而候选开始在已占用区间结束的瞬间也不冲突。调用方必须
    在数据库读事务结束后传入完整 DTO 集合；本函数不执行任何 I/O 或截断。
    """
    intervals: list[tuple[datetime, datetime]] = []
    for event in events:
        if event.status.casefold() == "cancelled" or event.transparency.casefold() in {
            "transparent",
            "free",
        }:
            continue
        intervals.append(
            (
                event.starts_at.astimezone(UTC) - meeting_buffer,
                event.ends_at.astimezone(UTC) + meeting_buffer,
            )
        )
    intervals.sort(key=lambda item: (item[0], item[1]))
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        if end > previous_end:
            merged[-1] = (previous_start, end)
    return tuple(merged)


def _buffered_busy_events(
    events: tuple[AvailabilityEvent, ...],
    meeting_buffer: timedelta,
) -> tuple[tuple[datetime, datetime], ...]:
    """兼容旧内部调用名；新路径统一使用合并后的忙碌区间。"""
    return _merge_buffered_intervals(events, meeting_buffer)


def _grid_points(
    local_day: date,
    start: time,
    end: time,
    *,
    grid_minutes: int,
) -> tuple[datetime, ...]:
    """返回相对本地午夜对齐的网格点，结束边界不作为开始候选。"""
    if not isinstance(start, time) or not isinstance(end, time):
        raise TypeError("working interval endpoints must be time values")
    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    first = ((start_minutes + grid_minutes - 1) // grid_minutes) * grid_minutes
    return tuple(
        datetime.combine(local_day, datetime.min.time()) + timedelta(minutes=minute)
        for minute in range(first, end_minutes, grid_minutes)
    )


def _resolve_wall_time(value: datetime, zone: ZoneInfo) -> datetime | None:
    """按 fold=0 解析墙上时间，并用 UTC 往返拒绝 spring-forward 空洞。"""
    if value.tzinfo is not None:
        raise ValueError("wall time must be timezone-naive")
    candidate = value.replace(tzinfo=zone, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(zone)
    if round_trip.replace(tzinfo=None) != value or round_trip.fold != 0:
        return None
    return candidate


def _overlaps_any(
    start: datetime,
    end: datetime,
    busy_events: tuple[tuple[datetime, datetime], ...],
) -> bool:
    """按半开区间判断候选是否与任一缓冲后忙碌事实重叠。"""
    return any(start < busy_end and end > busy_start for busy_start, busy_end in busy_events)


__all__ = [
    "AvailabilityCompleteness",
    "AvailabilityEvent",
    "AvailabilityResult",
    "CandidateTime",
    "suggest_meeting_times",
]
