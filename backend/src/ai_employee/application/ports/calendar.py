"""定义 Google Calendar 同步的不可变供应商边界。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """表示可安全交给应用层持久化的一条规范化 Calendar 事件。"""

    event_id: str
    calendar_id: str
    title: str
    description: str
    location: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    transparency: str
    status: str
    timezone: str
    recurring_event_id: str | None
    etag: str
    provider_url: str


@dataclass(frozen=True, slots=True)
class CalendarSyncPage:
    """表示一个分页响应和仅在最后一页有效的同步游标。"""

    events: tuple[CalendarEvent, ...]
    next_page_token: str | None
    next_sync_token: str | None

    def __post_init__(self) -> None:
        """复制事件集合，避免调用方事后变更已读取同步事实。"""
        object.__setattr__(self, "events", tuple(self.events))


@dataclass(frozen=True, slots=True)
class CalendarConnectionState:
    """表示当前 Calendar ``nextSyncToken``；空值要求受限窗口首次同步。"""

    cursor: str | None


class CalendarCursorExpiredError(PermanentProviderError):
    """表示 Google 410，必须清除游标并在七日窗口中重新读取。"""

    def __init__(self) -> None:
        super().__init__(
            error_code="google_calendar_cursor_expired",
            message="Google Calendar sync token expired",
        )


class CalendarReader(Protocol):
    """定义 Calendar 只读分页边界，便于应用用例注入合成实现。"""

    def initial_pages(self) -> AsyncIterator[CalendarSyncPage]: ...
    def sync_pages(self, cursor: str) -> AsyncIterator[CalendarSyncPage]: ...
    async def execute_request(self, parameters: dict[str, str]) -> object: ...


__all__ = [
    "CalendarConnectionState",
    "CalendarCursorExpiredError",
    "CalendarEvent",
    "CalendarReader",
    "CalendarSyncPage",
    "TransientProviderError",
    "UserActionRequiredError",
]
