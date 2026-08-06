"""定义 M2 日历创建、修改与恢复命令的纯领域值和确定性约束。"""

import base64
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ai_employee.domain.mail_actions import normalize_mailbox_address

_MUTABLE_CALENDAR_FIELDS = frozenset(
    {
        "all_day",
        "attendees",
        "description",
        "ends_at",
        "location",
        "notification_policy",
        "starts_at",
        "timezone",
        "title",
    }
)


class NotificationPolicy(StrEnum):
    """日历写入时允许的稳定通知策略。"""

    ALL = "all"
    NONE = "none"


def calendar_client_event_id(operation_id: UUID) -> str:
    """从操作 UUID 生成稳定、无填充且小写的 Base32hex 创建标识。

    Args:
        operation_id: 与审批和幂等执行共用的稳定 UUID。

    Returns:
        以 ``a`` 开头、只含 ``0-9a-v`` 的稳定供应商创建标识。

    Raises:
        TypeError: ``operation_id`` 不是 UUID。
    """
    if not isinstance(operation_id, UUID):
        raise TypeError("calendar operation_id must be UUID")
    encoded = base64.b32hexencode(operation_id.bytes).decode("ascii").lower().rstrip("=")
    return f"a{encoded}"


def _normalize_attendees(attendees: tuple[str, ...]) -> tuple[str, ...]:
    """规范化并按首次出现顺序去重最多五十个参会人。"""
    if not isinstance(attendees, tuple):
        raise TypeError("calendar attendees must be a tuple")
    normalized: list[str] = []
    seen: set[str] = set()
    for attendee in attendees:
        canonical = normalize_mailbox_address(attendee)
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)
    if len(normalized) > 50:
        raise ValueError("calendar attendees must contain at most 50 unique addresses")
    return tuple(normalized)


def _validate_common_fields(
    *,
    operation_id: UUID,
    connection_id: UUID,
    calendar_id: str,
    title: str,
    description: str | None,
    location: str | None,
    starts_at: datetime | date,
    ends_at: datetime | date,
    timezone: str,
    all_day: bool,
    attendees: tuple[str, ...],
    notification_policy: NotificationPolicy,
) -> tuple[str, ...]:
    """校验三种日历命令共享的目标、文本、时间、时区与参会人约束。

    Returns:
        规范化并去重的参会人 tuple。

    Raises:
        ValueError: 目标、文本、时区或时间区间不满足可信命令约束。
        TypeError: UUID、布尔、枚举或参会人字段不是声明的领域类型。
    """
    if not isinstance(operation_id, UUID) or not isinstance(connection_id, UUID):
        raise TypeError("calendar operation_id and connection_id must be UUID values")
    if not _is_nonempty_safe_text(calendar_id):
        raise ValueError("calendar_id must be a nonempty string without CR or LF")
    if not isinstance(title, str) or not title:
        raise ValueError("calendar title must be a nonempty string")
    if description is not None and not isinstance(description, str):
        raise TypeError("calendar description must be a string or None")
    if location is not None and not isinstance(location, str):
        raise TypeError("calendar location must be a string or None")
    if type(all_day) is not bool:
        raise TypeError("calendar all_day must be bool")
    if not isinstance(timezone, str) or not timezone:
        raise ValueError("calendar timezone must be a nonempty IANA name")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("calendar timezone must be a valid IANA name") from exc
    if not isinstance(notification_policy, NotificationPolicy):
        raise TypeError("calendar notification_policy must be NotificationPolicy")

    _validate_calendar_interval(
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
    )
    return _normalize_attendees(attendees)


def _validate_calendar_interval(
    *,
    starts_at: datetime | date,
    ends_at: datetime | date,
    all_day: bool,
) -> None:
    """强制全天纯日期与定时 aware datetime 两种表示严格互斥且正向。"""
    if all_day:
        # ``datetime`` 是 ``date`` 子类，必须用精确类型判断阻止混入定时值。
        if type(starts_at) is not date or type(ends_at) is not date:
            raise ValueError("all-day calendar intervals must use pure date values")
        if ends_at <= starts_at:
            raise ValueError("calendar ends_at must be after starts_at")
        return

    if not isinstance(starts_at, datetime) or not isinstance(ends_at, datetime):
        # 字段声明允许 date/datetime 联合；此处失败是 all_day 与表示组合不一致，
        # 属于领域值错误而非调用方传入了声明外 Python 类型。
        raise ValueError(  # noqa: TRY004
            "timed calendar intervals must use datetime values"
        )
    if (
        starts_at.tzinfo is None
        or starts_at.utcoffset() is None
        or ends_at.tzinfo is None
        or ends_at.utcoffset() is None
    ):
        raise ValueError("timed calendar intervals must be timezone-aware")

    # Python 在两个 datetime 共享同一 tzinfo 对象时按墙上时间比较并忽略 fold；
    # 可信命令必须按真实瞬间排序，避免秋季回拨的重复小时被接受或误拒绝。
    try:
        starts_at_utc = starts_at.astimezone(UTC)
        ends_at_utc = ends_at.astimezone(UTC)
    except (OverflowError, ValueError):
        # RFC3339 本地年份仍可能因极端 offset 越过 datetime 的 UTC 年份边界；
        # 离开 except 后抛固定领域错误，避免泄漏底层转换异常及原始时间值。
        pass
    else:
        if ends_at_utc <= starts_at_utc:
            raise ValueError("calendar ends_at must be after starts_at")
        return
    raise ValueError("timed calendar instants must be representable in UTC")


def _validate_changed_fields(changed_fields: tuple[str, ...]) -> None:
    """校验差异字段为非空、排序、唯一且仅引用显式完整期望状态。"""
    if not isinstance(changed_fields, tuple) or not changed_fields:
        raise ValueError("calendar changed_fields must be a nonempty tuple")
    if tuple(sorted(changed_fields)) != changed_fields:
        raise ValueError("calendar changed_fields must be deterministically sorted")
    if len(set(changed_fields)) != len(changed_fields):
        raise ValueError("calendar changed_fields must not contain duplicates")
    if not set(changed_fields).issubset(_MUTABLE_CALENDAR_FIELDS):
        raise ValueError("calendar changed_fields contains an unsupported field")


def _validate_update_binding(
    *,
    provider_event_id: str,
    base_etag: str,
    before_snapshot_id: UUID,
    changed_fields: tuple[str, ...],
) -> None:
    """校验修改/恢复命令的精确事件、并发版本、快照和差异绑定。"""
    if not _is_nonempty_safe_text(provider_event_id):
        raise ValueError("provider_event_id must be a nonempty safe string")
    if not _is_nonempty_safe_text(base_etag):
        raise ValueError("base_etag must be a nonempty safe string")
    if not isinstance(before_snapshot_id, UUID):
        raise TypeError("before_snapshot_id must be UUID")
    _validate_changed_fields(changed_fields)


def _is_nonempty_safe_text(value: str) -> bool:
    """判断标识或 ETag 是否非空且不会向日志/Header 边界注入换行。"""
    return isinstance(value, str) and bool(value) and "\r" not in value and "\n" not in value


@dataclass(frozen=True, slots=True)
class CalendarCreateCommand:
    """冻结的非重复日程创建命令，携带完整期望状态和稳定创建标识。"""

    schema_version: str
    action: str
    operation_id: UUID
    connection_id: UUID
    calendar_id: str
    title: str
    description: str | None
    location: str | None
    starts_at: datetime | date
    ends_at: datetime | date
    timezone: str
    all_day: bool
    attendees: tuple[str, ...]
    notification_policy: NotificationPolicy
    client_event_id: str

    def __post_init__(self) -> None:
        """校验创建协议、完整状态与 operation_id 派生标识。

        Raises:
            ValueError: 协议常量、时间状态或创建标识不满足约束。
            TypeError: 领域字段不是声明的不可变类型。
        """
        if self.schema_version != "calendar_create.v1":
            raise ValueError("calendar create schema_version must be calendar_create.v1")
        if self.action != "calendar.create":
            raise ValueError("calendar create action must be calendar.create")
        attendees = _validate_common_fields(
            operation_id=self.operation_id,
            connection_id=self.connection_id,
            calendar_id=self.calendar_id,
            title=self.title,
            description=self.description,
            location=self.location,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            timezone=self.timezone,
            all_day=self.all_day,
            attendees=self.attendees,
            notification_policy=self.notification_policy,
        )
        if self.client_event_id != calendar_client_event_id(self.operation_id):
            raise ValueError("client_event_id must be derived from operation_id")
        object.__setattr__(self, "attendees", attendees)


@dataclass(frozen=True, slots=True)
class CalendarUpdateCommand:
    """冻结的非重复日程条件修改命令，包含完整期望状态与修改前快照绑定。"""

    schema_version: str
    action: str
    operation_id: UUID
    connection_id: UUID
    calendar_id: str
    title: str
    description: str | None
    location: str | None
    starts_at: datetime | date
    ends_at: datetime | date
    timezone: str
    all_day: bool
    attendees: tuple[str, ...]
    notification_policy: NotificationPolicy
    provider_event_id: str
    base_etag: str
    before_snapshot_id: UUID
    changed_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        """校验修改协议、完整状态、ETag、快照和确定性差异字段。

        Raises:
            ValueError: 协议常量、状态或并发绑定不满足约束。
            TypeError: 领域字段不是声明的不可变类型。
        """
        if self.schema_version != "calendar_update.v1":
            raise ValueError("calendar update schema_version must be calendar_update.v1")
        if self.action != "calendar.update":
            raise ValueError("calendar update action must be calendar.update")
        attendees = _validate_common_fields(
            operation_id=self.operation_id,
            connection_id=self.connection_id,
            calendar_id=self.calendar_id,
            title=self.title,
            description=self.description,
            location=self.location,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            timezone=self.timezone,
            all_day=self.all_day,
            attendees=self.attendees,
            notification_policy=self.notification_policy,
        )
        _validate_update_binding(
            provider_event_id=self.provider_event_id,
            base_etag=self.base_etag,
            before_snapshot_id=self.before_snapshot_id,
            changed_fields=self.changed_fields,
        )
        object.__setattr__(self, "attendees", attendees)


@dataclass(frozen=True, slots=True)
class CalendarRestoreCommand:
    """冻结的日程恢复命令，以当前 ETag 将历史快照恢复为完整期望状态。"""

    schema_version: str
    action: str
    operation_id: UUID
    connection_id: UUID
    calendar_id: str
    title: str
    description: str | None
    location: str | None
    starts_at: datetime | date
    ends_at: datetime | date
    timezone: str
    all_day: bool
    attendees: tuple[str, ...]
    notification_policy: NotificationPolicy
    provider_event_id: str
    base_etag: str
    before_snapshot_id: UUID
    changed_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        """校验恢复协议、当前并发版本、快照和确定性差异字段。

        Raises:
            ValueError: 协议常量、状态或恢复绑定不满足约束。
            TypeError: 领域字段不是声明的不可变类型。
        """
        if self.schema_version != "calendar_restore.v1":
            raise ValueError("calendar restore schema_version must be calendar_restore.v1")
        if self.action != "calendar.restore":
            raise ValueError("calendar restore action must be calendar.restore")
        attendees = _validate_common_fields(
            operation_id=self.operation_id,
            connection_id=self.connection_id,
            calendar_id=self.calendar_id,
            title=self.title,
            description=self.description,
            location=self.location,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            timezone=self.timezone,
            all_day=self.all_day,
            attendees=self.attendees,
            notification_policy=self.notification_policy,
        )
        _validate_update_binding(
            provider_event_id=self.provider_event_id,
            base_etag=self.base_etag,
            before_snapshot_id=self.before_snapshot_id,
            changed_fields=self.changed_fields,
        )
        object.__setattr__(self, "attendees", attendees)


type CalendarCommand = CalendarCreateCommand | CalendarUpdateCommand | CalendarRestoreCommand

__all__ = [
    "CalendarCommand",
    "CalendarCreateCommand",
    "CalendarRestoreCommand",
    "CalendarUpdateCommand",
    "NotificationPolicy",
    "calendar_client_event_id",
]
