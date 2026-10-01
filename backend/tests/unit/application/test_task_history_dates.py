"""用真实 IANA 规则验证民用日边界与分页微秒编码。"""

from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID

import pytest

from ai_employee.application.task_history import (
    TaskHistoryFilterError,
    TaskHistoryQuery,
    normalize_history_filters,
)
from ai_employee.application.task_history_dates import (
    canonical_utc_timestamp,
    local_day_start,
    parse_history_timestamp,
)


@pytest.mark.parametrize(
    "day,zone,start,end",
    [
        (
            "2026-03-08",
            "America/New_York",
            "2026-03-08T05:00:00+00:00",
            "2026-03-09T04:00:00+00:00",
        ),
        (
            "2026-11-01",
            "America/New_York",
            "2026-11-01T04:00:00+00:00",
            "2026-11-02T05:00:00+00:00",
        ),
        ("2020-11-01", "America/Havana", "2020-11-01T04:00:00+00:00", "2020-11-02T05:00:00+00:00"),
        (
            "2018-11-04",
            "America/Sao_Paulo",
            "2018-11-04T03:00:00+00:00",
            "2018-11-05T02:00:00+00:00",
        ),
        ("2011-12-30", "Pacific/Apia", "2011-12-30T10:00:00+00:00", "2011-12-30T10:00:00+00:00"),
        ("2024-02-29", "Asia/Shanghai", "2024-02-28T16:00:00+00:00", "2024-02-29T16:00:00+00:00"),
        (
            "1986-01-01",
            "Asia/Kathmandu",
            "1985-12-31T18:30:00+00:00",
            "1986-01-01T18:15:00+00:00",
        ),
    ],
)
def test_civil_range_boundaries(day: str, zone: str, start: str, end: str) -> None:
    """23/25 小时、午夜重叠、非整小时跳跃及整日跳过均取真实边界。"""
    result = normalize_history_filters(
        user_id=UUID(int=1),
        timezone=zone,
        query=TaskHistoryQuery(created_from_date=day, created_to_date=day),
    )
    assert result.created_from == datetime.fromisoformat(start)
    assert result.created_before == datetime.fromisoformat(end)


def test_extreme_conversion_is_fixed_error() -> None:
    """最小日期转换溢出不泄漏底层异常或悄悄截断。"""
    with pytest.raises(TaskHistoryFilterError, match="^task_history_filter_invalid$"):
        normalize_history_filters(
            user_id=UUID(int=1),
            timezone="Asia/Tokyo",
            query=TaskHistoryQuery(created_from_date="0001-01-01"),
        )
    with pytest.raises(ValueError, match="^task_history_date_invalid$"):
        local_day_start(date.min, "Asia/Tokyo")


def test_timestamp_round_trip_preserves_microseconds_and_utc() -> None:
    """编码统一 UTC 六位微秒，偏移不影响表示同一瞬间的结果。"""
    instant = datetime(2026, 1, 2, 3, 4, 5, 123456, timezone(timedelta(hours=8)))
    encoded = canonical_utc_timestamp(instant)
    assert encoded == "2026-01-01T19:04:05.123456Z"
    assert parse_history_timestamp(encoded) == instant
    assert parse_history_timestamp(encoded).tzinfo is UTC
    assert canonical_utc_timestamp(datetime(1, 1, 1, tzinfo=UTC)) == "0001-01-01T00:00:00.000000Z"


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00.1Z",
        "2026-01-01T00:00:00.000000+00:00",
        "2026-02-30T00:00:00.000000Z",
        "2026-01-01 00:00:00.000000Z",
        "2026-01-01T00:00:00.000000Z\n",
    ],
)
def test_timestamp_decoder_rejects_noncanonical(value: str) -> None:
    """严格解码拒绝宽松 ISO 变体，避免多种签名表示。"""
    with pytest.raises(ValueError, match="^task_history_date_invalid$"):
        parse_history_timestamp(value)


def test_naive_timestamp_rejected() -> None:
    """未标注时区的时间不得隐式使用主机本地时区。"""
    with pytest.raises(ValueError, match="^task_history_date_invalid$"):
        canonical_utc_timestamp(datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None))
