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
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarReader,
    CalendarSyncPage,
    ProviderCalendar,
)
from ai_employee.application.ports.encryption import EncryptedValue, Encryption
from ai_employee.domain.errors import (
    InternalInvariantError,
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)


class CalendarSyncStore(Protocol):
    """定义目录和单日历 scope 必须在同一事务完成的存储操作。"""

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

    async def mark_directory_success(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        calendars: tuple[ProviderCalendar, ...],
        expected_cursor: str | None,
        next_cursor: str,
        completed_at: datetime,
    ) -> tuple[str, ...]: ...

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
    """先同步目录，再按 provider calendar ID 原子写入每个日历及其游标。"""

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
        """读取目录或一个日历增量，并把游标失效限制在对应 scope。

        ``directory`` 是唯一的目录 owner。目录成功后会建立新发现日历的 NULL cursor
        placeholder，再逐一调用同一供应商中立事件路径；某个日历失败不会回滚已经成功提交的
        其他日历，最终把首个稳定错误交给 Durable Worker 重试失败 scope。
        """
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
        if scope_key == "directory":
            return await self._execute_directory(
                user_id=user_id,
                connection_id=connection_id,
                state=state,
                reader=reader,
            )
        return await self._execute_calendar_scope(
            user_id=user_id,
            connection_id=connection_id,
            scope_key=scope_key,
            state=state,
            reader=reader,
        )

    async def _execute_directory(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        state: CalendarConnectionState,
        reader: CalendarReader,
    ) -> CalendarSyncResult:
        """完成目录 CAS 后按稳定 calendar ID 顺序同步各事件 scope。"""
        used_full = state.cursor is None
        try:
            pages = await self._collect_directory(reader.directory_pages(state.cursor))
        except CalendarCursorExpiredError as error:
            if state.cursor is None:
                raise InternalInvariantError(
                    error_code="calendar_initial_directory_cursor_expired",
                    message="Calendar initial directory sync cannot have an expired cursor",
                ) from error
            if error.provider != state.provider or error.scope_key != "directory":
                raise InternalInvariantError(
                    error_code="calendar_directory_cursor_expiry_scope_mismatch",
                    message="Calendar directory cursor expiry does not match requested scope",
                ) from error
            async with self._stores() as store:
                await store.clear_cursor(
                    user_id=user_id,
                    connection_id=connection_id,
                    scope_key="directory",
                    expected_cursor=state.cursor,
                )
            used_full = True
            pages = await self._collect_directory(reader.directory_pages(None))
            state = CalendarConnectionState(
                provider=state.provider,
                scope_key="directory",
                cursor=None,
            )

        next_cursor = self._last_cursor(pages, state.cursor)
        calendars = self._validated_directory_calendars(pages)
        completed_at = datetime.now(UTC)
        async with self._stores() as store:
            calendar_ids = await store.mark_directory_success(
                user_id=user_id,
                connection_id=connection_id,
                calendars=calendars,
                expected_cursor=state.cursor,
                next_cursor=next_cursor,
                completed_at=completed_at,
            )

        total_events = 0
        first_error: Exception | None = None
        for calendar_id in calendar_ids:
            try:
                result = await self._sync_calendar_by_id(
                    user_id=user_id,
                    connection_id=connection_id,
                    calendar_id=calendar_id,
                    reader=reader,
                )
            except (
                CalendarConnectionNotFoundError,
                InternalInvariantError,
                PermanentProviderError,
                TransientProviderError,
                UserActionRequiredError,
            ) as error:
                # 目录已经是独立事务事实；继续处理其他日历，避免一个失效 cursor 阻断整
                # 个连接。只在循环完成后抛出第一个错误，确保失败 scope 可单独重试。
                if first_error is None:
                    first_error = error
            else:
                total_events += result.events_upserted
                used_full = used_full or result.used_full_resync
        if first_error is not None:
            raise first_error
        return CalendarSyncResult(total_events, next_cursor, used_full)

    async def _sync_calendar_by_id(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        calendar_id: str,
        reader: CalendarReader,
    ) -> CalendarSyncResult:
        """读取一个已由目录证明存在的日历，不重新触发目录发现。"""
        async with self._stores() as store:
            state = await store.get_state(
                user_id=user_id,
                connection_id=connection_id,
                scope_key=calendar_id,
            )
        if state is None:
            raise CalendarConnectionNotFoundError
        return await self._execute_calendar_scope(
            user_id=user_id,
            connection_id=connection_id,
            scope_key=calendar_id,
            state=state,
            reader=reader,
        )

    async def _execute_calendar_scope(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        state: CalendarConnectionState,
        reader: CalendarReader,
    ) -> CalendarSyncResult:
        """读取一个日历增量；游标失效时只清除同一 scope 并受限重同步。"""
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

    @staticmethod
    async def _collect_directory(
        pages: AsyncIterator[CalendarDirectoryPage],
    ) -> tuple[CalendarDirectoryPage, ...]:
        """在事务外收集有限目录分页，避免供应商网络请求持有数据库锁。"""
        return tuple([page async for page in pages])

    @staticmethod
    def _last_cursor(pages: tuple[CalendarDirectoryPage, ...], fallback: str | None) -> str:
        """取得目录最终 cursor；增量空页保留既有 opaque cursor。"""
        for page in reversed(pages):
            if page.next_cursor:
                return page.next_cursor
        if fallback:
            return fallback
        raise InternalInvariantError(
            error_code="calendar_directory_final_cursor_missing",
            message="Calendar directory pages are missing a final cursor",
        )

    @staticmethod
    def _validated_directory_calendars(
        pages: tuple[CalendarDirectoryPage, ...],
    ) -> tuple[ProviderCalendar, ...]:
        """合并目录页并拒绝空、重复或不稳定的 provider calendar ID。"""
        calendars = tuple(calendar for page in pages for calendar in page.calendars)
        ids = tuple(calendar.calendar_id for calendar in calendars)
        if any(calendar_id in {"", "directory"} for calendar_id in ids) or len(set(ids)) != len(
            ids
        ):
            raise InternalInvariantError(
                error_code="calendar_directory_scopes_invalid",
                message="Calendar directory contains invalid or duplicate IDs",
            )
        return tuple(sorted(calendars, key=lambda calendar: calendar.calendar_id))

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
