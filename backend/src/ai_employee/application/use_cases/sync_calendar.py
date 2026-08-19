"""协调供应商中立 Calendar 分页、字段加密和单日历游标提交。"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from ai_employee.application.calendar_event_aad import calendar_event_field_aad_v2
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
        full_snapshot: bool,
        expected_cursor: str | None,
        expected_revision: datetime | None,
        next_cursor: str | None,
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
    next_cursor: str | None
    used_full_resync: bool

    @property
    def cursor(self) -> str | None:
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
                revision=state.revision,
            )

        full_snapshot = self._directory_snapshot_mode(pages)
        next_cursor = self._last_directory_cursor(
            pages,
            full_snapshot=full_snapshot,
            provider=state.provider,
        )
        calendars = self._validated_directory_calendars(pages)
        completed_at = datetime.now(UTC)
        async with self._stores() as store:
            calendar_ids = await store.mark_directory_success(
                user_id=user_id,
                connection_id=connection_id,
                calendars=calendars,
                full_snapshot=full_snapshot,
                expected_cursor=state.cursor,
                expected_revision=state.revision,
                next_cursor=next_cursor,
                completed_at=completed_at,
            )

        total_events = 0
        used_full = full_snapshot
        first_error: Exception | None = None
        for calendar_id in calendar_ids:
            try:
                result = await self._sync_calendar_by_id(
                    user_id=user_id,
                    connection_id=connection_id,
                    calendar_id=calendar_id,
                    reader=reader,
                )
            except UserActionRequiredError:
                # 权限撤销或重新授权要求是连接级安全状态；继续读取其他日历会扩大已知无权
                # 访问后的供应商请求面，也会延迟 Worker 对能力状态的持久化，因此立即停止。
                raise
            except (
                CalendarConnectionNotFoundError,
                InternalInvariantError,
                PermanentProviderError,
                TransientProviderError,
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
            # 供应商 I/O 期间目录 ACL 可能被另一任务撤销。提交前重新锁定目录证明，
            # 确保 tombstone 与事件写入按同一 ProviderCalendar 行串行，未知 scope 不落库。
            current_state = await store.get_state(
                user_id=user_id,
                connection_id=connection_id,
                scope_key=scope_key,
            )
            if current_state is None:
                raise CalendarConnectionNotFoundError
            if current_state.provider != state.provider or current_state.scope_key != scope_key:
                raise InternalInvariantError(
                    error_code="calendar_cursor_scope_mismatch",
                    message="Calendar cursor scope changed during provider read",
                )
            if current_state.cursor != state.cursor:
                raise TransientProviderError(
                    error_code="calendar_sync_cursor_conflict",
                    message="Calendar sync cursor changed during provider read",
                    retry_after=1,
                )
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
    def _directory_snapshot_mode(pages: tuple[CalendarDirectoryPage, ...]) -> bool:
        """返回整条目录链的显式快照模式并拒绝分页中途切换。

        Raises:
            InternalInvariantError: 供应商没有返回页面，或同一分页链混用了完整与增量语义。
        """
        if not pages:
            raise InternalInvariantError(
                error_code="calendar_directory_pages_missing",
                message="Calendar directory reader returned no pages",
            )
        full_snapshot = pages[0].full_snapshot
        if any(page.full_snapshot is not full_snapshot for page in pages[1:]):
            raise InternalInvariantError(
                error_code="calendar_directory_snapshot_mode_mismatch",
                message="Calendar directory pages changed snapshot mode",
            )
        return full_snapshot

    @staticmethod
    def _last_directory_cursor(
        pages: tuple[CalendarDirectoryPage, ...],
        *,
        full_snapshot: bool,
        provider: str,
    ) -> str | None:
        """验证最终目录 provider cursor，同时允许无 cursor 的完整快照。

        Google CalendarList 仅保证最终页的 ``nextSyncToken`` 有效；扫描前页或复用旧 token
        会把不完整增量分页错误记录为成功。Microsoft 完整 collection 没有 provider cursor，
        因而只有显式 ``full_snapshot=True`` 的链可以在最终页返回 ``None``。
        """
        next_cursor = pages[-1].next_cursor
        if next_cursor == "":
            raise InternalInvariantError(
                error_code="calendar_directory_final_cursor_invalid",
                message="Calendar directory final cursor is invalid",
            )
        if provider == "microsoft":
            if full_snapshot and next_cursor is None:
                return None
            raise InternalInvariantError(
                error_code="calendar_directory_provider_contract_invalid",
                message="Calendar directory provider contract is invalid",
            )
        if next_cursor is not None:
            return next_cursor
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
                calendar_event_field_aad_v2(
                    user_id=str(user_id),
                    connection_id=str(connection_id),
                    calendar_id=event.calendar_id,
                    provider_event_id=event.event_id,
                    field="description",
                ),
            ),
            self._cipher.encrypt(
                event.location.encode("utf-8"),
                calendar_event_field_aad_v2(
                    user_id=str(user_id),
                    connection_id=str(connection_id),
                    calendar_id=event.calendar_id,
                    provider_event_id=event.event_id,
                    field="location",
                ),
            ),
        )


__all__ = [
    "CalendarConnectionNotFoundError",
    "CalendarSyncResult",
    "CalendarSyncStore",
    "CalendarSyncStoreFactory",
    "SyncCalendarUseCase",
]
