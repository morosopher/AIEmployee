"""验证日历真实写命令的时间表示、稳定标识与不可变领域契约。"""

import base64
import re
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from ai_employee.domain.calendar_actions import (
    CalendarCreateCommand,
    CalendarRestoreCommand,
    CalendarUpdateCommand,
    NotificationPolicy,
    calendar_client_event_id,
)

OPERATION_ID = UUID("00000000-0000-0000-0000-000000000011")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000012")
SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000013")
STARTS_AT = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
ENDS_AT = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)


def _common_fields(**overrides: object) -> dict[str, object]:
    """返回三种日历命令共享的有效合成字段。"""
    values: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "connection_id": CONNECTION_ID,
        "calendar_id": "primary-synthetic",
        "title": "Synthetic meeting",
        "description": "Synthetic description",
        "location": "Synthetic room",
        "starts_at": STARTS_AT,
        "ends_at": ENDS_AT,
        "timezone": "Asia/Shanghai",
        "all_day": False,
        "attendees": ("owner@example.test",),
        "notification_policy": NotificationPolicy.ALL,
    }
    values.update(overrides)
    return values


def _create_command(**overrides: object) -> CalendarCreateCommand:
    """创建有效的 calendar.create 领域命令。"""
    values = _common_fields()
    values.update(
        {
            "schema_version": "calendar_create.v1",
            "action": "calendar.create",
            "client_event_id": calendar_client_event_id(OPERATION_ID),
        }
    )
    values.update(overrides)
    return CalendarCreateCommand(**values)  # type: ignore[arg-type]


def _update_command(**overrides: object) -> CalendarUpdateCommand:
    """创建有效的 calendar.update 领域命令。"""
    values = _common_fields()
    values.update(
        {
            "schema_version": "calendar_update.v1",
            "action": "calendar.update",
            "provider_event_id": "provider-event-synthetic",
            "base_etag": '"etag-synthetic"',
            "before_snapshot_id": SNAPSHOT_ID,
            "changed_fields": ("description", "title"),
        }
    )
    values.update(overrides)
    return CalendarUpdateCommand(**values)  # type: ignore[arg-type]


def _restore_command(**overrides: object) -> CalendarRestoreCommand:
    """创建有效的 calendar.restore 领域命令。"""
    values = _common_fields()
    values.update(
        {
            "schema_version": "calendar_restore.v1",
            "action": "calendar.restore",
            "provider_event_id": "provider-event-synthetic",
            "base_etag": '"etag-current"',
            "before_snapshot_id": SNAPSHOT_ID,
            "changed_fields": ("description", "title"),
        }
    )
    values.update(overrides)
    return CalendarRestoreCommand(**values)  # type: ignore[arg-type]


def test_calendar_client_event_id_uses_lowercase_unpadded_base32hex() -> None:
    """创建标识必须由 UUID 16 字节稳定编码，满足 Google 的小写字符集约束。"""
    expected = "a" + base64.b32hexencode(OPERATION_ID.bytes).decode("ascii").lower().rstrip("=")

    first = calendar_client_event_id(OPERATION_ID)
    second = calendar_client_event_id(OPERATION_ID)

    assert first == expected == second
    assert re.fullmatch(r"a[0-9a-v]+", first)
    assert "=" not in first
    assert "-" not in first


def test_all_three_calendar_command_types_are_explicit_and_frozen() -> None:
    """创建、修改、恢复应分别构造为显式类型，不能退化为松散字典。"""
    create = _create_command()
    update = _update_command()
    restore = _restore_command()

    assert create.action == "calendar.create"
    assert update.action == "calendar.update"
    assert restore.action == "calendar.restore"
    with pytest.raises(FrozenInstanceError):
        create.title = "Changed"  # type: ignore[misc]


def test_create_requires_client_event_id_derived_from_operation_id() -> None:
    """冻结创建标识必须与 operation_id 绑定，避免重试生成另一供应商事件。"""
    with pytest.raises(ValueError):
        _create_command(client_event_id="a0123456789")


@pytest.mark.parametrize(
    "overrides",
    (
        {"starts_at": STARTS_AT.replace(tzinfo=None), "ends_at": ENDS_AT},
        {"starts_at": STARTS_AT, "ends_at": ENDS_AT.replace(tzinfo=None)},
        {"starts_at": STARTS_AT, "ends_at": STARTS_AT},
        {"starts_at": STARTS_AT, "ends_at": STARTS_AT.replace(hour=8)},
        {"starts_at": date(2026, 8, 6), "ends_at": date(2026, 8, 7)},
    ),
)
def test_timed_calendar_commands_require_aware_increasing_datetimes(
    overrides: dict[str, object],
) -> None:
    """定时事件不得使用 naive datetime、日期或非正向区间。"""
    with pytest.raises(ValueError):
        _create_command(**overrides)


def test_timed_calendar_interval_accepts_positive_absolute_duration_across_dst_fold() -> None:
    """秋季回拨中墙上时间倒退时，只要 UTC 绝对时刻正向就应允许冻结。"""
    timezone = ZoneInfo("America/New_York")
    command = _create_command(
        timezone=timezone.key,
        starts_at=datetime(2026, 11, 1, 1, 30, tzinfo=timezone, fold=0),
        ends_at=datetime(2026, 11, 1, 1, 15, tzinfo=timezone, fold=1),
    )

    assert command.ends_at.astimezone(UTC) > command.starts_at.astimezone(UTC)


def test_timed_calendar_interval_rejects_reversed_absolute_time_across_dst_fold() -> None:
    """秋季回拨的重复小时不能用墙上时间顺序掩盖绝对时刻倒序。"""
    timezone = ZoneInfo("America/New_York")

    with pytest.raises(ValueError, match="ends_at must be after starts_at"):
        _create_command(
            timezone=timezone.key,
            starts_at=datetime(2026, 11, 1, 1, 15, tzinfo=timezone, fold=1),
            ends_at=datetime(2026, 11, 1, 1, 30, tzinfo=timezone, fold=0),
        )


@pytest.mark.parametrize(
    ("starts_at", "ends_at"),
    (
        (
            datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=14))),
            datetime(1, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=14))),
        ),
        (
            datetime(9999, 12, 31, 22, 0, tzinfo=timezone(-timedelta(hours=14))),
            datetime(9999, 12, 31, 23, 0, tzinfo=timezone(-timedelta(hours=14))),
        ),
    ),
    ids=("below-utc-minimum", "above-utc-maximum"),
)
def test_timed_calendar_interval_rejects_instants_not_representable_in_utc(
    starts_at: datetime,
    ends_at: datetime,
) -> None:
    """带 offset 的 RFC3339 极值若越过 UTC 年份边界，必须转为固定领域错误。"""
    with pytest.raises(ValueError, match="representable in UTC"):
        _create_command(starts_at=starts_at, ends_at=ends_at)


def test_all_day_calendar_commands_use_exclusive_date_end() -> None:
    """全天事件只能使用纯 date，且 exclusive end 必须严格晚于 start。"""
    command = _create_command(
        all_day=True,
        starts_at=date(2026, 8, 6),
        ends_at=date(2026, 8, 7),
    )

    assert type(command.starts_at) is date
    assert type(command.ends_at) is date

    with pytest.raises(ValueError):
        _create_command(
            all_day=True,
            starts_at=date(2026, 8, 6),
            ends_at=date(2026, 8, 6),
        )
    with pytest.raises(ValueError):
        _create_command(all_day=True, starts_at=STARTS_AT, ends_at=ENDS_AT)
    with pytest.raises(ValueError):
        _create_command(
            all_day=True,
            starts_at=date(2026, 8, 6),
            ends_at=ENDS_AT,
        )


def test_calendar_commands_require_valid_iana_timezone() -> None:
    """业务日期必须携带可解析 IANA 时区，不能依赖宿主机本地设置。"""
    with pytest.raises(ValueError):
        _create_command(timezone="Not/A_Real_Zone")


def test_attendees_are_normalized_deduplicated_and_limited() -> None:
    """参会人按域名规范化去重后最多保留 50 个。"""
    command = _create_command(
        attendees=(
            "owner@Example.test",
            "owner(comment)@example.TEST",
            '"owner"@example.test',
            "Owner@example.test",
        )
    )

    assert command.attendees == ("owner@example.test", "Owner@example.test")

    exactly_fifty = tuple(f"attendee-{index}@example.test" for index in range(50))
    assert len(_create_command(attendees=exactly_fifty).attendees) == 50
    with pytest.raises(ValueError):
        _create_command(attendees=exactly_fifty + ("attendee-50@example.test",))


@pytest.mark.parametrize("factory", (_update_command, _restore_command))
@pytest.mark.parametrize("field_name", ("provider_event_id", "base_etag"))
def test_update_and_restore_require_nonempty_provider_binding(
    factory: object,
    field_name: str,
) -> None:
    """修改和恢复必须绑定精确供应商事件、ETag 与修改前快照。"""
    with pytest.raises(ValueError):
        factory(**{field_name: ""})  # type: ignore[operator]


@pytest.mark.parametrize("factory", (_update_command, _restore_command))
@pytest.mark.parametrize(
    "changed_fields",
    (
        (),
        ("title", "description"),
        ("title", "title"),
        ("provider_extension",),
    ),
)
def test_update_and_restore_require_deterministic_changed_fields(
    factory: object,
    changed_fields: tuple[str, ...],
) -> None:
    """变更字段必须非空、已排序、唯一且只引用完整期望状态中的显式字段。"""
    with pytest.raises(ValueError):
        factory(changed_fields=changed_fields)  # type: ignore[operator]
