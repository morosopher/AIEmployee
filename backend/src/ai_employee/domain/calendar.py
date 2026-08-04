"""定义每日简报使用的确定性日程冲突规则。"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """供冲突规则处理的最小日程值对象。

    全天事件允许没有精确时间，仍可由简报展示；定时事件则必须提供带时区的起止
    时间。适配器负责将供应商状态和透明度规范化为稳定的小写字符串。

    Attributes:
        event_id: 供应商无关且稳定的日程标识。
        start_at: 定时事件开始时刻。
        end_at: 定时事件结束时刻。
        status: 日程状态，例如 ``cancelled``。
        transparency: 忙闲透明度，``transparent`` 表示不占用时间。
        all_day: 是否为全天展示事件。
    """

    event_id: str
    start_at: datetime | None = None
    end_at: datetime | None = None
    status: str = "confirmed"
    transparency: str = "opaque"
    all_day: bool = False


def find_conflicts(events: Iterable[CalendarEvent]) -> tuple[tuple[CalendarEvent, CalendarEvent], ...]:
    """找出排序后忙碌定时事件之间唯一的重叠事件对。

    取消、free、透明、全天和缺少完整精确时间的事件都不会参与 M1 冲突计算。带时区
    ``datetime`` 的比较以绝对时刻进行，因此跨 UTC 午夜和不同时区输入无需依赖
    字符串日期即可正确比较。相邻边界 ``end == start`` 不属于重叠。

    Args:
        events: 可迭代的已规范化日程集合，输入顺序不影响结果。

    Returns:
        以开始时间顺序排列的唯一冲突事件对。

    Raises:
        ValueError: 参与比较的定时事件没有带时区时间或结束不晚于开始。
    """
    # 同步分页或重复投递可能带来同一供应商事件的多份副本；先按稳定标识去重，
    # 避免把事件自身误判为冲突，同时保留首次出现的规范化版本。
    unique_events: dict[str, CalendarEvent] = {}
    for event in events:
        unique_events.setdefault(event.event_id, event)
    busy_events = [event for event in unique_events.values() if _participates_in_conflicts(event)]
    normalized_events = sorted(busy_events, key=lambda event: _validated_start(event))
    conflicts: list[tuple[CalendarEvent, CalendarEvent]] = []

    for index, earlier in enumerate(normalized_events):
        earlier_start = _validated_start(earlier)
        earlier_end = _validated_end(earlier)
        for later in normalized_events[index + 1 :]:
            later_start = _validated_start(later)
            if later_start >= earlier_end:
                # 后续事件已按开始时间排序，不可能再与当前事件重叠。
                break
            later_end = _validated_end(later)
            if earlier_start < later_end:
                conflicts.append((earlier, later))
    return tuple(conflicts)


def _participates_in_conflicts(event: CalendarEvent) -> bool:
    """判断事件是否为可比较的忙碌定时事件。"""
    return (
        not event.all_day
        and event.status.casefold() != "cancelled"
        and event.transparency.casefold() not in {"transparent", "free"}
        and event.start_at is not None
        and event.end_at is not None
    )


def _validated_start(event: CalendarEvent) -> datetime:
    """返回并校验参与计算事件的带时区开始时间。"""
    start_at = event.start_at
    if start_at is None or start_at.tzinfo is None or start_at.utcoffset() is None:
        raise ValueError("timed calendar event start_at must be timezone-aware")
    return start_at


def _validated_end(event: CalendarEvent) -> datetime:
    """返回并校验参与计算事件的带时区结束时间及严格正向区间。"""
    end_at = event.end_at
    start_at = _validated_start(event)
    if end_at is None or end_at.tzinfo is None or end_at.utcoffset() is None:
        raise ValueError("timed calendar event end_at must be timezone-aware")
    if end_at <= start_at:
        raise ValueError("timed calendar event end_at must be after start_at")
    return end_at
