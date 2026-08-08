"""定义供应商中立的日历目录、事件分页与精确只读边界。"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)


@dataclass(frozen=True, slots=True)
class ProviderCalendar:
    """表示供应商日历目录中的一个 opaque 日历及只读权限投影。

    ``is_deleted`` 只承载目录增量明确返回的删除事实。删除项至少保留供应商日历 ID，
    其他展示和权限字段不得由应用层猜测；仓储据此撤销当前目录投影与来源缓存。
    """

    calendar_id: str
    display_name: str
    timezone: str
    is_primary: bool
    access_role: str
    can_write: bool
    provider_url: str | None = None
    is_deleted: bool = False
    # Microsoft 目录的共享能力、颜色与 owner 是展示/权限事实；旧 Google 行为空时保持
    # 默认值，避免为了供应商字段新增第二套模型或迁移。
    can_share: bool = False
    hex_color: str | None = None
    owner: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        """冻结 owner 映射，防止供应商边界事实被调用方在事务间篡改。"""
        if self.owner is not None:
            object.__setattr__(self, "owner", MappingProxyType(dict(self.owner)))


@dataclass(frozen=True, slots=True)
class CalendarDirectoryPage:
    """表示日历目录的一页及独立于事件游标的目录增量游标。"""

    calendars: tuple[ProviderCalendar, ...]
    next_page_token: str | None
    next_cursor: str | None

    def __post_init__(self) -> None:
        """复制日历集合，防止目录分页完成后被调用方篡改。"""
        object.__setattr__(self, "calendars", tuple(self.calendars))


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """表示可安全交给应用层持久化的一条规范化 Calendar 事件。"""

    event_id: str
    calendar_id: str
    title: str
    description: str
    location: str
    starts_at: datetime | None
    ends_at: datetime | None
    all_day: bool
    transparency: str
    status: str
    timezone: str
    recurring_event_id: str | None
    etag: str | None
    provider_url: str
    updated_at: datetime | None = None
    organizer: Mapping[str, str] | None = None
    attendees: tuple[Mapping[str, str], ...] = ()
    access_role: str | None = None
    can_edit: bool = False
    # Microsoft changeKey 是与 ETag 并列的版本事实；作为 provider-neutral 可选字段扩展，
    # 旧 Google/Fake 事件保持 None，当前数据库版本不把它扩散为供应商专属表。
    change_key: str | None = None
    recurrence_metadata: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        """冻结组织者和参会人嵌套结构，保持跨事务同步事实不可变。"""
        if self.organizer is not None:
            object.__setattr__(self, "organizer", MappingProxyType(dict(self.organizer)))
        if self.recurrence_metadata is not None:
            object.__setattr__(
                self,
                "recurrence_metadata",
                MappingProxyType(dict(self.recurrence_metadata)),
            )
        object.__setattr__(
            self,
            "attendees",
            tuple(MappingProxyType(dict(attendee)) for attendee in self.attendees),
        )


@dataclass(frozen=True, slots=True, init=False)
class CalendarSyncPage:
    """表示一个事件分页响应和仅在最终页有效的 scope 游标。"""

    events: tuple[CalendarEvent, ...]
    next_page_token: str | None
    next_cursor: str | None

    def __init__(
        self,
        events: Sequence[CalendarEvent],
        next_page_token: str | None,
        next_cursor: str | None = None,
        *,
        next_sync_token: str | None = None,
    ) -> None:
        """冻结事件集合，并兼容 M1 ``next_sync_token`` 关键字。

        Raises:
            ValueError: 新旧游标名称同时存在且内容不一致。
        """
        if (
            next_cursor is not None
            and next_sync_token is not None
            and next_cursor != next_sync_token
        ):
            raise ValueError("next_cursor conflicts with next_sync_token")
        object.__setattr__(self, "events", tuple(events))
        object.__setattr__(self, "next_page_token", next_page_token)
        object.__setattr__(
            self,
            "next_cursor",
            next_cursor if next_cursor is not None else next_sync_token,
        )

    @property
    def next_sync_token(self) -> str | None:
        """返回 M1 Google Calendar 使用的游标兼容属性。"""
        return self.next_cursor


@dataclass(frozen=True, slots=True)
class CalendarConnectionState:
    """表示已验证连接 provider 与一个精确 calendar scope 的持久游标。"""

    cursor: str | None
    provider: str = "google"
    scope_key: str = "primary"


class CalendarCursorExpiredError(PermanentProviderError):
    """表示单个日历 scope 的游标失效，需要受限窗口回退。"""

    def __init__(self, provider: str = "google", scope_key: str = "primary") -> None:
        """构造不含 sync token 或 Delta URL 的供应商中立稳定错误。"""
        self.provider = provider
        self.scope_key = scope_key
        super().__init__(
            error_code="calendar_cursor_expired",
            message="Calendar sync cursor expired",
            metadata={"provider": provider, "scope_key": scope_key},
        )


class CalendarReader(Protocol):
    """定义日历目录、按日历分页和精确当前事件读取端口。"""

    def directory_pages(self, cursor: str | None = None) -> AsyncIterator[CalendarDirectoryPage]:
        """读取可见日历目录及其独立增量游标。"""
        ...

    def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """读取一个 provider calendar ID 的受限初始事件窗口。"""
        ...

    def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """从一个日历自己的 opaque 游标读取增量事件。"""
        ...

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """精确读取供应商当前事件，供提案与恢复准备执行只读核对。"""
        ...


__all__ = [
    "CalendarConnectionState",
    "CalendarCursorExpiredError",
    "CalendarDirectoryPage",
    "CalendarEvent",
    "CalendarReader",
    "CalendarSyncPage",
    "ProviderCalendar",
    "TransientProviderError",
    "UserActionRequiredError",
]
