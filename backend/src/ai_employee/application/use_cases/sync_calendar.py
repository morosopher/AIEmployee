"""协调 Calendar 页读取、字段加密和最终游标提交。"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from ai_employee.application.ports.calendar import (
    CalendarConnectionState,
    CalendarCursorExpiredError,
    CalendarEvent,
    CalendarReader,
    CalendarSyncPage,
)
from ai_employee.application.ports.encryption import EncryptedValue, Encryption


class CalendarSyncStore(Protocol):
    """定义日历同步必须在同一事务中完成的存储操作。"""

    async def get_state(
        self, *, user_id: UUID, connection_id: UUID
    ) -> CalendarConnectionState | None: ...
    async def upsert_event(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        event: CalendarEvent,
        encrypted_description: EncryptedValue,
        encrypted_location: EncryptedValue,
    ) -> None: ...
    async def clear_cursor(
        self, *, user_id: UUID, connection_id: UUID, expected_cursor: str
    ) -> None: ...
    async def finish_sync(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        expected_cursor: str | None,
        next_sync_token: str,
        event_count: int,
        used_full_resync: bool,
        completed_at: datetime,
    ) -> None: ...


class CalendarSyncStoreFactory(Protocol):
    """提供独立短事务，隔离应用层和 SQLAlchemy。"""

    def __call__(self) -> AbstractAsyncContextManager[CalendarSyncStore]: ...


@dataclass(frozen=True, slots=True)
class CalendarSyncResult:
    """返回不会泄露日程字段的同步聚合结果。"""

    events_upserted: int
    cursor: str
    used_full_resync: bool


class CalendarConnectionNotFoundError(Exception):
    """连接不存在、归属不符或不是 connected 时抛出。"""


class SyncCalendarUseCase:
    """在供应商 I/O 结束后一次性写入所有事件和最终 cursor。"""

    def __init__(
        self, stores: CalendarSyncStoreFactory, cipher: Encryption, calendar: CalendarReader
    ) -> None:
        self._stores, self._cipher, self._calendar = stores, cipher, calendar

    async def execute(self, *, user_id: UUID, connection_id: UUID) -> CalendarSyncResult:
        """读取增量；410 使用窗口重同步，任何写入异常均回滚 cursor 与审计。"""
        async with self._stores() as store:
            state = await store.get_state(user_id=user_id, connection_id=connection_id)
        if state is None:
            raise CalendarConnectionNotFoundError
        used_full = state.cursor is None
        try:
            pages = await self._collect(
                self._calendar.initial_pages()
                if state.cursor is None
                else self._calendar.sync_pages(state.cursor)
            )
        except CalendarCursorExpiredError:
            if state.cursor is None:
                raise RuntimeError("Calendar initial sync cannot have an expired cursor")
            async with self._stores() as store:
                await store.clear_cursor(
                    user_id=user_id, connection_id=connection_id, expected_cursor=state.cursor
                )
            used_full = True
            pages = await self._collect(self._calendar.initial_pages())
            state = CalendarConnectionState(None)
        token = pages[-1].next_sync_token if pages else None
        if token is None:
            raise RuntimeError("Calendar final page is missing nextSyncToken")
        count = 0
        async with self._stores() as store:
            for page in pages:
                for event in page.events:
                    await store.upsert_event(
                        user_id=user_id,
                        connection_id=connection_id,
                        event=event,
                        encrypted_description=self._cipher.encrypt(
                            event.description.encode(),
                            self._aad(user_id, connection_id, event.event_id, "description"),
                        ),
                        encrypted_location=self._cipher.encrypt(
                            event.location.encode(),
                            self._aad(user_id, connection_id, event.event_id, "location"),
                        ),
                    )
                    count += 1
            await store.finish_sync(
                user_id=user_id,
                connection_id=connection_id,
                expected_cursor=state.cursor,
                next_sync_token=token,
                event_count=count,
                used_full_resync=used_full,
                completed_at=datetime.now(UTC),
            )
        return CalendarSyncResult(count, token, used_full)

    @staticmethod
    async def _collect(pages: AsyncIterator[CalendarSyncPage]) -> tuple[CalendarSyncPage, ...]:
        """在事务外完成有限分页，避免网络持锁。"""
        return tuple([page async for page in pages])

    @staticmethod
    def _aad(user_id: UUID, connection_id: UUID, event_id: str, field: str) -> bytes:
        """将密文与用户、连接、事件和字段种类精确绑定。"""
        return f"{user_id}:{connection_id}:{event_id}:{field}".encode("ascii")
