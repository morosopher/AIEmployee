"""验证日程冲突仅比较有时间段的忙碌事件。"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ai_employee.domain.calendar import CalendarEvent, find_conflicts


def test_overlapping_busy_timed_events_conflict_once() -> None:
    """排序无关的重叠忙碌日程应返回唯一且按开始时间排列的事件对。"""
    later = CalendarEvent(
        event_id="later",
        start_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 4, 11, 0, tzinfo=UTC),
    )
    earlier = CalendarEvent(
        event_id="earlier",
        start_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
        end_at=datetime(2026, 8, 4, 10, 30, tzinfo=UTC),
    )

    assert find_conflicts((later, earlier)) == ((earlier, later),)


def test_adjacent_events_do_not_conflict() -> None:
    """前一事件结束恰等于后一事件开始不属于重叠。"""
    first = CalendarEvent(
        event_id="first",
        start_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
    )
    second = CalendarEvent(
        event_id="second",
        start_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 4, 11, 0, tzinfo=UTC),
    )

    assert find_conflicts((first, second)) == ()


def test_cancelled_transparent_and_all_day_events_are_ignored() -> None:
    """取消、透明与全天日程可展示但不得制造 M1 时间冲突。"""
    busy = CalendarEvent(
        event_id="busy",
        start_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
    )
    cancelled = CalendarEvent(
        event_id="cancelled",
        start_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
        end_at=datetime(2026, 8, 4, 10, 30, tzinfo=UTC),
        status="cancelled",
    )
    transparent = CalendarEvent(
        event_id="transparent",
        start_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
        end_at=datetime(2026, 8, 4, 10, 30, tzinfo=UTC),
        transparency="transparent",
    )
    all_day = CalendarEvent(event_id="all-day", all_day=True)

    assert find_conflicts((busy, cancelled, transparent, all_day)) == ()


def test_free_events_are_ignored() -> None:
    """明确标记为 free 的事件可展示但不应占用冲突时间。"""
    busy = CalendarEvent(
        event_id="busy",
        start_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
    )
    free = CalendarEvent(
        event_id="free",
        start_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
        end_at=datetime(2026, 8, 4, 10, 30, tzinfo=UTC),
        transparency="free",
    )

    assert find_conflicts((busy, free)) == ()


def test_cross_utc_midnight_events_compare_after_timezone_normalization() -> None:
    """跨 UTC 午夜的带时区事件必须按瞬时比较，而非按本地日期字符串比较。"""
    shanghai = ZoneInfo("Asia/Shanghai")
    first = CalendarEvent(
        event_id="first",
        start_at=datetime(2026, 8, 4, 23, 30, tzinfo=UTC),
        end_at=datetime(2026, 8, 5, 0, 30, tzinfo=UTC),
    )
    second = CalendarEvent(
        event_id="second",
        start_at=datetime(2026, 8, 5, 8, 15, tzinfo=shanghai),
        end_at=datetime(2026, 8, 5, 9, 0, tzinfo=shanghai),
    )

    assert find_conflicts((second, first)) == ((first, second),)


def test_duplicate_event_id_is_not_compared_with_itself() -> None:
    """重复同步同一稳定事件不得生成事件与自身的冲突对。"""
    event = CalendarEvent(
        event_id="duplicate",
        start_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
    )

    assert find_conflicts((event, event)) == ()
