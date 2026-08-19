"""验证 M2 日历候选时间只使用确定性本人日历事实。"""

from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import pytest

from ai_employee.domain.calendar_availability import (
    AvailabilityEvent,
    _merge_buffered_intervals,
    suggest_meeting_times,
)
from ai_employee.domain.settings import (
    WeeklyWorkingHours,
    validate_meeting_buffer,
)

MISSING_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000901")
_DAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def weekday_hours(start: str, end: str) -> WeeklyWorkingHours:
    """构造周一至周五相同、周末为空的严格工作时间。"""
    return WeeklyWorkingHours.from_mapping(
        {
            name: [[start, end]] if index < 5 else []
            for index, name in enumerate(_DAY_NAMES)
        }
    )


def hours_for(
    weekday: int, *intervals: tuple[str, str]
) -> WeeklyWorkingHours:
    """构造只有指定星期存在工作区间的测试设置。"""
    return WeeklyWorkingHours.from_mapping(
        {
            name: [list(interval) for interval in intervals] if index == weekday else []
            for index, name in enumerate(_DAY_NAMES)
        }
    )


def busy(
    start: str,
    end: str,
    *,
    all_day: bool = False,
    transparency: str = "opaque",
    status: str = "confirmed",
) -> AvailabilityEvent:
    """构造已规范化且不含标题、地点或参会人的忙碌事实。"""
    return AvailabilityEvent(
        starts_at=datetime.fromisoformat(start),
        ends_at=datetime.fromisoformat(end),
        all_day=all_day,
        transparency=transparency,
        status=status,
    )


def test_candidates_apply_working_hours_buffer_grid_and_limit() -> None:
    """候选必须遵守工作时间、缓冲、十五分钟网格与最多三项。"""
    result = suggest_meeting_times(
        requested_duration=timedelta(minutes=30),
        search_start=datetime(2030, 3, 11, 0, tzinfo=UTC),
        timezone="America/Los_Angeles",
        working_hours=weekday_hours("09:00", "18:00"),
        meeting_buffer=timedelta(minutes=10),
        events=(busy("2030-03-11T17:00:00Z", "2030-03-11T18:00:00Z"),),
        horizon_days=14,
        grid_minutes=15,
        limit=3,
    )

    assert len(result.candidates) == 3
    assert all(item.starts_at.minute in {0, 15, 30, 45} for item in result.candidates)
    assert result.completeness == "complete"


def test_multiple_intervals_and_weekend_configuration_are_respected() -> None:
    """每日多个区间与周末显式工作时间都不能被默认工作周覆盖。"""
    result = suggest_meeting_times(
        requested_duration=timedelta(minutes=30),
        search_start=datetime(2030, 3, 9, 16, tzinfo=UTC),
        timezone="America/Los_Angeles",
        working_hours=hours_for(5, ("09:00", "10:00"), ("13:00", "14:00")),
        meeting_buffer=timedelta(),
        events=(busy("2030-03-09T17:00:00Z", "2030-03-09T20:00:00Z"),),
        limit=2,
    )

    assert tuple(candidate.starts_at for candidate in result.candidates) == (
        datetime(2030, 3, 9, 21, 0, tzinfo=UTC),
        datetime(2030, 3, 9, 21, 15, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("buffer_minutes", "expected_start"),
    (
        (0, datetime(2030, 3, 11, 9, 0, tzinfo=UTC)),
        (120, datetime(2030, 3, 11, 13, 0, tzinfo=UTC)),
    ),
)
def test_zero_and_maximum_buffer_are_applied(
    buffer_minutes: int, expected_start: datetime
) -> None:
    """合法缓冲边界 0 与 120 分钟均精确参与忙碌区间扩展。"""
    result = suggest_meeting_times(
        requested_duration=timedelta(minutes=30),
        search_start=datetime(2030, 3, 11, 9, tzinfo=UTC),
        timezone="UTC",
        working_hours=weekday_hours("09:00", "14:00"),
        meeting_buffer=timedelta(minutes=buffer_minutes),
        events=(busy("2030-03-11T10:00:00Z", "2030-03-11T11:00:00Z"),),
        limit=1,
    )

    assert result.candidates[0].starts_at == expected_start


def test_all_day_busy_event_blocks_its_local_day() -> None:
    """忙碌全天事件阻塞整个本地日，而不是被当作缺少精确时间忽略。"""
    result = suggest_meeting_times(
        requested_duration=timedelta(minutes=30),
        search_start=datetime(2030, 3, 11, 0, tzinfo=UTC),
        timezone="UTC",
        working_hours=weekday_hours("09:00", "10:00"),
        meeting_buffer=timedelta(),
        events=(
            busy(
                "2030-03-11T00:00:00Z",
                "2030-03-12T00:00:00Z",
                all_day=True,
            ),
        ),
        limit=1,
    )

    assert result.candidates[0].starts_at == datetime(2030, 3, 12, 9, tzinfo=UTC)


def test_transparent_and_cancelled_events_do_not_block_candidates() -> None:
    """透明/free 与取消事件不应制造虚假忙碌区间。"""
    result = suggest_meeting_times(
        requested_duration=timedelta(minutes=30),
        search_start=datetime(2030, 3, 11, 9, tzinfo=UTC),
        timezone="UTC",
        working_hours=weekday_hours("09:00", "10:00"),
        meeting_buffer=timedelta(minutes=10),
        events=(
            busy(
                "2030-03-11T09:00:00Z",
                "2030-03-11T10:00:00Z",
                transparency="transparent",
            ),
            busy(
                "2030-03-11T09:00:00Z",
                "2030-03-11T10:00:00Z",
                status="cancelled",
            ),
        ),
        limit=1,
    )

    assert result.candidates[0].starts_at == datetime(2030, 3, 11, 9, tzinfo=UTC)


def test_buffered_busy_intervals_are_sorted_and_merge_adjacent_ranges() -> None:
    """先过滤非忙碌事实，再把嵌套/相邻半开区间合并为最小集合。"""
    merged = _merge_buffered_intervals(
        (
            busy("2030-03-11T10:00:00Z", "2030-03-11T11:00:00Z"),
            busy("2030-03-11T09:00:00Z", "2030-03-11T10:00:00Z"),
            busy(
                "2030-03-11T09:30:00Z",
                "2030-03-11T09:45:00Z",
                transparency="transparent",
            ),
            busy(
                "2030-03-11T11:00:00Z",
                "2030-03-11T11:30:00Z",
                status="cancelled",
            ),
        ),
        timedelta(minutes=15),
    )

    assert merged == (
        (
            datetime(2030, 3, 11, 8, 45, tzinfo=UTC),
            datetime(2030, 3, 11, 11, 15, tzinfo=UTC),
        ),
    )


def test_large_event_set_matches_independent_brute_force_oracle() -> None:
    """超过一万条事件时仍与逐候选扫描原始事实的独立 oracle 完全一致。

    大部分事件位于搜索窗口之外；真正影响结果的取消、透明、嵌套、相邻和后置忙碌事实
    刻意放在第 10,000 条之后。如果实现先截断事件集合，候选会与 oracle 产生差异。本测试
    只比较确定性结果，不测墙钟耗时，也不复用 production 的排序或区间合并 helper。
    """
    search_start = datetime(2030, 3, 11, 9, tzinfo=UTC)
    duration = timedelta(minutes=30)
    meeting_buffer = timedelta()
    distant_start = datetime(2030, 4, 1, 0, tzinfo=UTC)
    distant_busy = tuple(
        AvailabilityEvent(
            starts_at=distant_start + timedelta(minutes=index),
            ends_at=distant_start + timedelta(minutes=index + 1),
            all_day=False,
            transparency="opaque",
            status="confirmed",
        )
        for index in range(10_000)
    )
    events = distant_busy + (
        # 这两项覆盖整个工作窗口，但按领域规则必须被过滤。
        busy(
            "2030-03-11T09:00:00Z",
            "2030-03-11T12:00:00Z",
            status="cancelled",
        ),
        busy(
            "2030-03-11T09:00:00Z",
            "2030-03-11T12:00:00Z",
            transparency="transparent",
        ),
        # 三项形成嵌套与相邻的 [09:30, 10:15) 忙碌范围。
        busy("2030-03-11T09:30:00Z", "2030-03-11T10:00:00Z"),
        busy("2030-03-11T09:40:00Z", "2030-03-11T09:50:00Z"),
        busy("2030-03-11T10:00:00Z", "2030-03-11T10:15:00Z"),
        # 最后一项在截断边界之后影响前三个候选，防止“看似处理大集合”的假阳性。
        busy("2030-03-11T10:30:00Z", "2030-03-11T11:00:00Z"),
    )

    result = suggest_meeting_times(
        requested_duration=duration,
        search_start=search_start,
        timezone="UTC",
        working_hours=hours_for(0, ("09:00", "12:00")),
        meeting_buffer=meeting_buffer,
        events=events,
        horizon_days=1,
        grid_minutes=15,
        limit=3,
    )

    # Oracle 直接逐候选扫描所有原始事件；它不排序、不合并，也不调用被测私有 helper。
    oracle: list[tuple[datetime, datetime]] = []
    for minute_offset in range(0, 180, 15):
        candidate_start = search_start + timedelta(minutes=minute_offset)
        candidate_end = candidate_start + duration
        if candidate_end > datetime(2030, 3, 11, 12, tzinfo=UTC):
            continue
        overlaps = any(
            candidate_start < event.ends_at.astimezone(UTC) + meeting_buffer
            and candidate_end > event.starts_at.astimezone(UTC) - meeting_buffer
            for event in events
            if event.status.casefold() != "cancelled"
            and event.transparency.casefold() not in {"transparent", "free"}
        )
        if overlaps:
            continue
        oracle.append((candidate_start, candidate_end))
        if len(oracle) == 3:
            break

    assert len(events) == 10_006
    assert tuple((item.starts_at, item.ends_at) for item in result.candidates) == tuple(oracle)


def test_missing_connections_make_result_partial_without_attendee_claim() -> None:
    """缺失本人连接必须显式 partial，且类型事实固定声明未查询参会人。"""
    result = suggest_meeting_times(
        requested_duration=timedelta(minutes=30),
        search_start=datetime(2030, 3, 11, 9, tzinfo=UTC),
        timezone="UTC",
        working_hours=weekday_hours("09:00", "10:00"),
        meeting_buffer=timedelta(),
        events=(),
        missing_connection_ids=(MISSING_CONNECTION_ID,),
        limit=1,
    )

    assert result.completeness == "partial"
    assert result.missing_connection_ids == (MISSING_CONNECTION_ID,)
    assert result.attendee_availability_checked is False


def test_spring_forward_skips_nonexistent_grid_points() -> None:
    """春季跳时的不存在墙上时间端点不能被映射为候选。"""
    result = suggest_meeting_times(
        requested_duration=timedelta(minutes=30),
        search_start=datetime(2030, 3, 10, 8, tzinfo=UTC),
        timezone="America/Los_Angeles",
        working_hours=hours_for(6, ("01:30", "03:30")),
        meeting_buffer=timedelta(),
        events=(),
        limit=3,
    )

    assert result.candidates[0].starts_at == datetime(2030, 3, 10, 10, 0, tzinfo=UTC)
    assert tuple(
        candidate.starts_at
        for candidate in result.candidates
        if candidate.starts_at.date() == datetime(2030, 3, 10, tzinfo=UTC).date()
    ) == (datetime(2030, 3, 10, 10, 0, tzinfo=UTC),)


def test_fall_back_uses_fold_zero_and_skips_offset_change_inside_duration() -> None:
    """秋季回拨取 fold=0，并拒绝批准时长与墙上显示时长不一致的候选。"""
    result = suggest_meeting_times(
        requested_duration=timedelta(minutes=30),
        search_start=datetime(2030, 11, 3, 8, tzinfo=UTC),
        timezone="America/Los_Angeles",
        working_hours=hours_for(6, ("01:00", "02:00")),
        meeting_buffer=timedelta(),
        events=(),
        limit=3,
    )

    assert tuple(candidate.starts_at for candidate in result.candidates[:2]) == (
        datetime(2030, 11, 3, 8, 0, tzinfo=UTC),
        datetime(2030, 11, 3, 8, 15, tzinfo=UTC),
    )
    assert datetime(2030, 11, 3, 8, 30, tzinfo=UTC) not in {
        candidate.starts_at for candidate in result.candidates
    }
    assert all(
        candidate.ends_at - candidate.starts_at == timedelta(minutes=30)
        for candidate in result.candidates
    )


@pytest.mark.parametrize(
    "value",
    (
        {"monday": [["9:00", "18:00"]]},
        {
            **{name: [] for name in _DAY_NAMES},
            "monday": [["09:00", "12:00"], ["11:45", "13:00"]],
        },
        {
            **{name: [] for name in _DAY_NAMES},
            "monday": [["18:00", "09:00"]],
        },
    ),
)
def test_working_hours_boundary_rejects_malformed_or_overlapping_values(
    value: object,
) -> None:
    """设置边界拒绝缺日、非 HH:MM、反向或重叠区间，绝不静默修复。"""
    with pytest.raises(ValueError):
        WeeklyWorkingHours.from_mapping(value)


@pytest.mark.parametrize("value", (-1, 121, True, 1.5))
def test_meeting_buffer_boundary_rejects_invalid_values(value: object) -> None:
    """会议缓冲只接受普通整数 0～120。"""
    with pytest.raises((TypeError, ValueError)):
        validate_meeting_buffer(value)


def test_working_interval_is_immutable() -> None:
    """工作时间解析结果必须冻结，避免设置验证后被调用方篡改。"""
    interval = weekday_hours("09:00", "18:00").intervals_for(0)[0]

    with pytest.raises((AttributeError, TypeError)):
        interval.start = time(8, 0)  # type: ignore[misc]
