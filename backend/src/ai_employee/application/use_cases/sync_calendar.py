"""协调供应商中立 Calendar 分页、字段加密和单日历游标提交。"""

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
from ai_employee.domain.errors import InternalInvariantError


class CalendarSyncStore(Protocol):
    """定义一个日历 scope 必须在同一事务完成的存储操作。"""

    async def get_state(
        self, *, user_id: UUID, connection_id: UUID, scope_key: str
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
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        expected_cursor: str,
    ) -> None: ...

    async def finish_sync(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        expected_cursor: str | None,
        next_cursor: str,
        event_count: int,
        used_full_resync: bool,
        completed_at: datetime,
    ) -> None: ...


class CalendarSyncStoreFactory(Protocol):
    """提供独立短事务，隔离应用层和 SQLAlchemy。"""

    def __call__(self) -> AbstractAsyncContextManager[CalendarSyncStore]: ...


class ReadAdapterRegistry(Protocol):
    """按连接 provider 返回固定日历读取端口。"""

    def calendar_reader(
        self, *, provider: str, connection_id: UUID, scope_key: str
    ) -> CalendarReader: ...


@dataclass(frozen=True, slots=True)
class CalendarSyncResult:
    """返回不含日程敏感字段的单 scope 同步聚合结果。"""

    events_upserted: int
    next_cursor: str
    used_full_resync: bool

    @property
    def cursor(self) -> str:
        """返回 M1 ``CalendarSyncResult.cursor`` 的只读兼容属性。"""
        return self.next_cursor


class CalendarConnectionNotFoundError(Exception):
    """连接不存在、归属不符、能力关闭或日历 scope 不可同步时抛出。"""


class SyncCalendarUseCase:
    """按连接 provider 选择适配器，并原子写入一个日历及其最终游标。"""

    def __init__(
        self,
        stores: CalendarSyncStoreFactory,
        registry: ReadAdapterRegistry,
        cipher: Encryption | None = None,
    ) -> None:
        """注入短事务、固定适配器注册表和字段加密边界。"""
        self._stores = stores
        self._registry = registry
        self._cipher = cipher

    async def execute(
        self, *, user_id: UUID, connection_id: UUID, scope_key: str
    ) -> CalendarSyncResult:
        """读取一个日历增量；游标失效时只清除同一 scope 并受限重同步。"""
        if scope_key == "":
            raise ValueError("scope_key must not be empty")
        async with self._stores() as store:
            state = await store.get_state(
                user_id=user_id,
                connection_id=connection_id,
                scope_key=scope_key,
            )
        if state is None:
            raise CalendarConnectionNotFoundError
        if state.scope_key != scope_key:
            raise InternalInvariantError(
                error_code="calendar_cursor_scope_mismatch",
                message="Calendar cursor scope does not match requested scope",
            )
        reader = self._registry.calendar_reader(
            provider=state.provider,
            connection_id=connection_id,
            scope_key=scope_key,
        )
        used_full = state.cursor is None
        try:
            pages = await self._collect(
                reader.initial_pages(scope_key)
                if state.cursor is None
                else reader.sync_pages(scope_key, state.cursor)
            )
        except CalendarCursorExpiredError as error:
            if state.cursor is None:
                raise InternalInvariantError(
                    error_code="calendar_initial_cursor_expired",
                    message="Calendar initial sync cannot have an expired cursor",
                ) from error
            if error.provider != state.provider or error.scope_key != scope_key:
                raise InternalInvariantError(
                    error_code="calendar_cursor_expiry_scope_mismatch",
                    message="Calendar cursor expiry does not match requested scope",
                ) from error
            async with self._stores() as store:
                await store.clear_cursor(
                    user_id=user_id,
                    connection_id=connection_id,
                    scope_key=scope_key,
                    expected_cursor=state.cursor,
                )
            used_full = True
            pages = await self._collect(reader.initial_pages(scope_key))
            state = CalendarConnectionState(
                provider=state.provider,
                scope_key=scope_key,
                cursor=None,
            )
        next_cursor = pages[-1].next_cursor if pages else None
        if next_cursor is None:
            raise InternalInvariantError(
                error_code="calendar_final_cursor_missing",
                message="Calendar final page is missing a sync cursor",
            )

        count = 0
        completed_at = datetime.now(UTC)
        async with self._stores() as store:
            for page in pages:
                for event in page.events:
                    if event.calendar_id != scope_key:
                        raise InternalInvariantError(
                            error_code="calendar_event_scope_mismatch",
                            message="Calendar event scope does not match requested scope",
                        )
                    description, location = self._encrypt_event_fields(
                        user_id,
                        connection_id,
                        event,
                    )
                    await store.upsert_event(
                        user_id=user_id,
                        connection_id=connection_id,
                        event=event,
                        encrypted_description=description,
                        encrypted_location=location,
                    )
                    count += 1
            await store.finish_sync(
                user_id=user_id,
                connection_id=connection_id,
                scope_key=scope_key,
                expected_cursor=state.cursor,
                next_cursor=next_cursor,
                event_count=count,
                used_full_resync=used_full,
                completed_at=completed_at,
            )
        return CalendarSyncResult(count, next_cursor, used_full)

    @staticmethod
    async def _collect(pages: AsyncIterator[CalendarSyncPage]) -> tuple[CalendarSyncPage, ...]:
        """在事务外完成有限分页，避免供应商网络请求持有数据库锁。"""
        return tuple([page async for page in pages])

    def _encrypt_event_fields(
        self,
        user_id: UUID,
        connection_id: UUID,
        event: CalendarEvent,
    ) -> tuple[EncryptedValue, EncryptedValue]:
        """使用事件和字段绑定 AAD 加密描述与地点。"""
        if self._cipher is None:
            raise InternalInvariantError(
                error_code="calendar_field_encryption_unavailable",
                message="Calendar field encryption is unavailable",
            )
        return (
            self._cipher.encrypt(
                event.description.encode("utf-8"),
                self._aad(user_id, connection_id, event.event_id, "description"),
            ),
            self._cipher.encrypt(
                event.location.encode("utf-8"),
                self._aad(user_id, connection_id, event.event_id, "location"),
            ),
        )

    @staticmethod
    def _aad(user_id: UUID, connection_id: UUID, event_id: str, field: str) -> bytes:
        """将密文与用户、连接、事件和字段种类精确绑定。"""
        return f"{user_id}:{connection_id}:{event_id}:{field}".encode("ascii")


__all__ = [
    "CalendarConnectionNotFoundError",
    "CalendarSyncResult",
    "CalendarSyncStore",
    "CalendarSyncStoreFactory",
    "SyncCalendarUseCase",
]
