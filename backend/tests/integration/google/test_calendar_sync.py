"""在 PostgreSQL 上验证 Calendar 加密、tombstone、游标与用户隔离。"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.calendar import (
    CalendarCursorExpiredError,
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarSyncPage,
    ProviderCalendar,
)
from ai_employee.application.use_cases.sync_calendar import (
    CalendarConnectionNotFoundError,
    CalendarSyncStoreFactory,
    SyncCalendarUseCase,
)
from ai_employee.domain.errors import (
    InternalInvariantError,
    PermanentProviderError,
    TransientProviderError,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories.calendar import (
    SqlAlchemyCalendarSyncRepository,
    SqlAlchemyEnabledSyncScopeReader,
)
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStore
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.calendar import (
    GOOGLE_CALENDAR_LIST_URL,
    GoogleCalendarAdapter,
)
from ai_employee.integrations.registry import ProviderAdapterRegistry
from ai_employee.workers.generate_brief import GenerateBriefTaskStep


class FakeCalendar:
    """以确定性页替代外部 Calendar，确保集成测试只覆盖 PostgreSQL 边界。"""

    def __init__(self, page: CalendarSyncPage) -> None:
        self.page = page

    async def initial_pages(self) -> AsyncIterator[CalendarSyncPage]:
        yield self.page

    async def sync_pages(self, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        del cursor
        yield self.page


@dataclass(slots=True)
class DirectoryCalendarReader:
    """以可配置目录和事件游标模拟 Google 多日历读取端口。"""

    calendars: tuple[ProviderCalendar, ...]
    directory_tokens: tuple[str, ...] = ("directory-token-1", "directory-token-2")
    event_generation: int = 1
    expire_directory_once: bool = False
    expire_event_once: str | None = None
    fail_event: str | None = None
    emit_events: bool = True
    empty_delta_scopes: frozenset[str] = frozenset()
    directory_pages_override: tuple[CalendarDirectoryPage, ...] | None = None
    directory_calls: list[str | None] = field(default_factory=list)
    initial_calls: list[str] = field(default_factory=list)
    sync_calls: list[tuple[str, str]] = field(default_factory=list)
    _directory_expired: bool = False
    _event_expired: bool = False

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """按调用序列返回目录页，并可精确模拟一次 CalendarList 410。"""
        self.directory_calls.append(cursor)
        if cursor is not None and self.expire_directory_once and not self._directory_expired:
            self._directory_expired = True
            raise CalendarCursorExpiredError("google", "directory")
        if self.directory_pages_override is not None:
            for page in self.directory_pages_override:
                yield page
            return
        token_index = min(len(self.directory_calls) - 1, len(self.directory_tokens) - 1)
        yield CalendarDirectoryPage(
            self.calendars,
            None,
            self.directory_tokens[token_index],
        )

    async def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """返回一个含敏感字段、权限投影和参与者事实的合成完整页。"""
        self.initial_calls.append(calendar_id)
        yield CalendarSyncPage(
            (self._event(calendar_id),) if self.emit_events else (),
            None,
            f"{calendar_id}-token-{self.event_generation}",
        )

    async def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """验证每个日历收到自己的 cursor，并可模拟单 scope 410 或暂态失败。"""
        self.sync_calls.append((calendar_id, cursor))
        if self.fail_event == calendar_id:
            raise TransientProviderError(
                error_code="synthetic_calendar_unavailable",
                message="Synthetic calendar is temporarily unavailable",
            )
        if self.expire_event_once == calendar_id and not self._event_expired:
            self._event_expired = True
            raise CalendarCursorExpiredError("google", calendar_id)
        yield CalendarSyncPage(
            (
                (self._event(calendar_id),)
                if self.emit_events and calendar_id not in self.empty_delta_scopes
                else ()
            ),
            None,
            f"{calendar_id}-token-{self.event_generation}",
        )

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """按精确日历和事件 ID 返回当前规范化事实。"""
        event = self._event(calendar_id)
        return event if event.event_id == provider_event_id else None

    @staticmethod
    def _event(calendar_id: str) -> CalendarEvent:
        """创建 ID 跨日历不冲突的不可变合成事件。"""
        suffix = calendar_id.replace("@", "-").replace("/", "-")
        organizer: Mapping[str, str] = {
            "email": "organizer@example.test",
            "displayName": "Synthetic Organizer",
        }
        attendees: tuple[Mapping[str, str], ...] = (
            {
                "email": "attendee@example.test",
                "responseStatus": "accepted",
            },
        )
        return CalendarEvent(
            event_id=f"event-{suffix}",
            calendar_id=calendar_id,
            title=f"Synthetic {suffix}",
            description=f"private description {suffix}",
            location=f"private location {suffix}",
            starts_at=datetime(2030, 1, 2, 1, tzinfo=UTC),
            ends_at=datetime(2030, 1, 2, 2, tzinfo=UTC),
            all_day=False,
            transparency="opaque",
            status="confirmed",
            timezone="UTC",
            recurring_event_id=None,
            etag=f"etag-{suffix}",
            provider_url=f"https://calendar.example.test/events/{suffix}",
            updated_at=datetime(2030, 1, 1, tzinfo=UTC),
            organizer=organizer,
            attendees=attendees,
            can_edit=True,
        )


def _directory_calendars() -> tuple[ProviderCalendar, ...]:
    """返回一个可写主日历和一个只读日历的合成目录。"""
    return (
        ProviderCalendar(
            calendar_id="primary",
            display_name="Primary",
            timezone="UTC",
            is_primary=True,
            access_role="owner",
            can_write=True,
            provider_url="https://calendar.example.test/primary",
        ),
        ProviderCalendar(
            calendar_id="readonly@example.test",
            display_name="Readonly",
            timezone="UTC",
            is_primary=False,
            access_role="reader",
            can_write=False,
            provider_url="https://calendar.example.test/readonly",
        ),
    )


def _deleted_calendar(calendar_id: str) -> ProviderCalendar:
    """创建只保留 opaque ID 的供应商中立目录删除事实。"""
    return ProviderCalendar(
        calendar_id=calendar_id,
        display_name=calendar_id,
        timezone="UTC",
        is_primary=False,
        access_role="unknown",
        can_write=False,
        provider_url=None,
        is_deleted=True,
    )


def _isolation_calendar_event(
    *, user_id: UUID, connection_id: UUID, suffix: str
) -> CalendarEventModel:
    """创建同 calendar ID 的合成缓存事件，用于证明全量清理不会越过所有权边界。"""
    return CalendarEventModel(
        user_id=user_id,
        connection_id=connection_id,
        provider_event_id=f"isolation-event-{suffix}",
        calendar_id="readonly@example.test",
        title=f"Isolation {suffix}",
        description_ciphertext=None,
        description_nonce=None,
        description_key_version=None,
        location_ciphertext=None,
        location_nonce=None,
        location_key_version=None,
        starts_at=datetime(2030, 1, 2, 3, tzinfo=UTC),
        ends_at=datetime(2030, 1, 2, 4, tzinfo=UTC),
        all_day=False,
        transparency="opaque",
        status="confirmed",
        timezone="UTC",
        recurring_event_id=None,
        etag=f"isolation-etag-{suffix}",
        organizer=None,
        attendees=[],
        access_role="owner",
        can_edit=True,
        provider_url=f"https://calendar.example.test/isolation/{suffix}",
        provider_updated_at=datetime(2030, 1, 1, tzinfo=UTC),
    )


async def _seed_calendar_isolation_controls(
    sessions: ManagedAsyncSessionMaker,
    *,
    owner_user_id: UUID,
) -> tuple[tuple[UUID, UUID], tuple[UUID, UUID]]:
    """种入同用户其他连接与其他用户连接的同名日历/事件控制组。"""
    async with sessions.begin() as session:
        sibling_connection = OAuthConnectionModel(
            user_id=owner_user_id,
            provider="google",
            provider_account_id="calendar-sibling-subject",
            account_email="calendar-sibling@example.test",
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
            status="connected",
            last_error_code=None,
        )
        foreign_user = UserModel(
            email="calendar-foreign@example.test",
            display_name="Foreign Calendar Owner",
            password_hash=None,
            timezone="UTC",
            locale="zh-CN",
            brief_time=time(8),
            is_active=True,
        )
        session.add_all((sibling_connection, foreign_user))
        await session.flush()
        foreign_connection = OAuthConnectionModel(
            user_id=foreign_user.id,
            provider="google",
            provider_account_id="calendar-foreign-subject",
            account_email="calendar-foreign@example.test",
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
            status="connected",
            last_error_code=None,
        )
        session.add(foreign_connection)
        await session.flush()
        controls = (
            (owner_user_id, sibling_connection.id, "sibling"),
            (foreign_user.id, foreign_connection.id, "foreign"),
        )
        for control_user_id, control_connection_id, suffix in controls:
            session.add_all(
                (
                    ProviderCalendarModel(
                        user_id=control_user_id,
                        connection_id=control_connection_id,
                        provider_calendar_id="readonly@example.test",
                        name=f"Isolation {suffix}",
                        timezone="UTC",
                        is_primary=False,
                        access_role="owner",
                        can_write=True,
                        provider_url=f"https://calendar.example.test/isolation/{suffix}",
                    ),
                    _isolation_calendar_event(
                        user_id=control_user_id,
                        connection_id=control_connection_id,
                        suffix=suffix,
                    ),
                )
            )
    return (
        (owner_user_id, sibling_connection.id),
        (foreign_user.id, foreign_connection.id),
    )


async def _seed_google_calendar_connection(
    database_url: str,
) -> tuple[ManagedAsyncSessionMaker, AeadCipher, UUID, UUID]:
    """创建启用 calendar.read 且仅有 directory placeholder 的真实连接。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"d" * 32)
    async with sessions.begin() as session:
        user = UserModel(
            email="calendar-directory@example.test",
            display_name="Directory Owner",
            password_hash=None,
            timezone="UTC",
            locale="zh-CN",
            brief_time=time(8),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        connection = OAuthConnectionModel(
            user_id=user.id,
            provider="google",
            provider_account_id="calendar-directory-subject",
            account_email="calendar-directory@example.test",
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
            status="connected",
            last_error_code=None,
        )
        session.add(connection)
        await session.flush()
        session.add_all(
            (
                ConnectionCapabilityModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    capability="calendar.read",
                    status="enabled",
                    actual_scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                ),
                SyncCursorModel(
                    connection_id=connection.id,
                    resource_kind="calendar",
                    scope_key="directory",
                    cursor=None,
                ),
            )
        )
        user_id, connection_id = user.id, connection.id
    return sessions, cipher, user_id, connection_id


def _calendar_stores(sessions: ManagedAsyncSessionMaker) -> CalendarSyncStoreFactory:
    """把真实会话工厂包装为 Calendar 用例所需的短事务 factory。"""

    @asynccontextmanager
    async def stores() -> AsyncIterator[SqlAlchemyCalendarSyncRepository]:
        async with sessions.begin() as session:
            yield SqlAlchemyCalendarSyncRepository(session)

    return stores


@dataclass(frozen=True, slots=True)
class _CalendarPersistenceSnapshot:
    """保存一次同步前后的非敏感数据库事实，用于断言永久解析错误完全不落库。"""

    calendars: tuple[tuple[str, str, str, bool], ...]
    events: tuple[tuple[str, str, str, str | None, datetime | None], ...]
    cursors: tuple[tuple[str, str | None, datetime | None, datetime | None, str | None], ...]
    audit_count: int


async def _calendar_persistence_snapshot(
    sessions: ManagedAsyncSessionMaker,
    *,
    user_id: UUID,
    connection_id: UUID,
) -> _CalendarPersistenceSnapshot:
    """读取目录、事件、全部日历游标成功时间与审计数量的稳定快照。"""
    async with sessions() as session:
        calendar_rows = (
            await session.execute(
                select(
                    ProviderCalendarModel.provider_calendar_id,
                    ProviderCalendarModel.name,
                    ProviderCalendarModel.access_role,
                    ProviderCalendarModel.can_write,
                )
                .where(
                    ProviderCalendarModel.user_id == user_id,
                    ProviderCalendarModel.connection_id == connection_id,
                )
                .order_by(ProviderCalendarModel.provider_calendar_id)
            )
        ).all()
        event_rows = (
            await session.execute(
                select(
                    CalendarEventModel.provider_event_id,
                    CalendarEventModel.calendar_id,
                    CalendarEventModel.status,
                    CalendarEventModel.etag,
                    CalendarEventModel.provider_updated_at,
                )
                .where(
                    CalendarEventModel.user_id == user_id,
                    CalendarEventModel.connection_id == connection_id,
                )
                .order_by(CalendarEventModel.calendar_id, CalendarEventModel.provider_event_id)
            )
        ).all()
        cursor_rows = (
            await session.execute(
                select(
                    SyncCursorModel.scope_key,
                    SyncCursorModel.cursor,
                    SyncCursorModel.last_success_at,
                    SyncCursorModel.last_attempt_at,
                    SyncCursorModel.last_error_code,
                )
                .where(
                    SyncCursorModel.connection_id == connection_id,
                    SyncCursorModel.resource_kind == "calendar",
                )
                .order_by(SyncCursorModel.scope_key)
            )
        ).all()
        audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditEventModel)
            .where(AuditEventModel.user_id == user_id)
        )
    return _CalendarPersistenceSnapshot(
        calendars=tuple(
            (row.provider_calendar_id, row.name, row.access_role, row.can_write)
            for row in calendar_rows
        ),
        events=tuple(
            (
                row.provider_event_id,
                row.calendar_id,
                row.status,
                row.etag,
                row.provider_updated_at,
            )
            for row in event_rows
        ),
        cursors=tuple(
            (
                row.scope_key,
                row.cursor,
                row.last_success_at,
                row.last_attempt_at,
                row.last_error_code,
            )
            for row in cursor_rows
        ),
        audit_count=int(audit_count or 0),
    )


async def _postgres_backend_pid(session: AsyncSession) -> int:
    """返回当前测试会话的 PostgreSQL backend PID，供锁等待关系做确定性断言。"""
    value = await session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(value, int), "PostgreSQL backend PID is unavailable"
    return value


async def _wait_for_postgres_blockers(
    sessions: ManagedAsyncSessionMaker,
    *,
    waiting_pid: int,
) -> tuple[int, ...]:
    """轮询 ``pg_blocking_pids`` 直到目标事务真实进入锁等待，禁止依赖固定 sleep。"""
    async with asyncio.timeout(5):
        async with sessions() as observer:
            while True:
                blockers = await observer.scalar(
                    text("SELECT pg_blocking_pids(:waiting_pid)"),
                    {"waiting_pid": waiting_pid},
                )
                normalized = tuple(int(pid) for pid in blockers or ())
                if normalized:
                    return normalized
                # 这里只让出事件循环；继续条件轮询数据库真实锁状态，不把墙钟延时当屏障。
                await asyncio.sleep(0)


def _event(status: str = "confirmed") -> CalendarEvent:
    """创建包含敏感字段的合成事件，用于断言数据库只保存密文。"""
    return CalendarEvent(
        "event-1",
        "primary",
        "Synthetic",
        "secret description",
        "secret location",
        datetime(2030, 1, 2, 1, tzinfo=UTC),
        datetime(2030, 1, 2, 2, tzinfo=UTC),
        False,
        "opaque",
        status,
        "UTC",
        None,
        "etag-1",
        "https://calendar.example.test/e/1",
    )


@pytest.mark.asyncio
async def test_calendar_sync_encrypts_upserts_tombstone_and_advances_cursor(
    database_url: str,
) -> None:
    """同一连接重复同步只保留一行，取消状态保存 tombstone，最后才提交 cursor。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"a" * 32)
    async with sessions.begin() as session:
        user = UserModel(
            email="calendar-owner@example.test",
            display_name="Owner",
            password_hash=None,
            timezone="UTC",
            locale="zh-CN",
            brief_time=time(8),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        connection = OAuthConnectionModel(
            user_id=user.id,
            provider="google",
            provider_account_id="calendar-subject",
            account_email="calendar-owner@example.test",
            scopes=[],
            status="connected",
            last_error_code=None,
        )
        session.add(connection)
        await session.flush()
        session.add_all(
            [
                ConnectionCapabilityModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    capability="calendar.read",
                    status="enabled",
                    actual_scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                ),
                SyncCursorModel(
                    connection_id=connection.id,
                    resource_kind="calendar",
                    scope_key="primary",
                    cursor=None,
                ),
                ProviderCalendarModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    provider_calendar_id="primary",
                    name="Primary",
                    timezone="UTC",
                    is_primary=True,
                    access_role="owner",
                    can_write=True,
                    provider_url="https://calendar.example.test/primary",
                ),
            ]
        )
        user_id, connection_id = user.id, connection.id

    @asynccontextmanager
    async def stores():
        async with sessions.begin() as session:
            yield SqlAlchemyCalendarSyncRepository(session)

    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(
            google_calendar=FakeCalendar(CalendarSyncPage((_event(),), None, "token-1"))
        ),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="primary")
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(
            google_calendar=FakeCalendar(CalendarSyncPage((_event("cancelled"),), None, "token-2"))
        ),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="primary")
    minimal_tombstone = CalendarEvent(
        "event-1",
        "primary",
        "",
        "",
        "",
        None,
        None,
        False,
        "opaque",
        "cancelled",
        "UTC",
        "series-1",
        None,
        "",
    )
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(
            google_calendar=FakeCalendar(CalendarSyncPage((minimal_tombstone,), None, "token-3"))
        ),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="primary")
    async with sessions() as session:
        row = await session.scalar(
            select(CalendarEventModel).where(CalendarEventModel.connection_id == connection_id)
        )
        cursor = await session.scalar(
            select(SyncCursorModel.cursor).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
            )
        )
        count = await session.scalar(
            select(func.count())
            .select_from(CalendarEventModel)
            .where(CalendarEventModel.connection_id == connection_id)
        )
    assert (
        row is not None
        and row.status == "cancelled"
        and row.starts_at is None
        and row.ends_at is None
    )
    assert row.description_ciphertext != b"secret description" and count == 1
    assert cursor == "token-3"
    await sessions.dispose()


@pytest.mark.asyncio
async def test_calendar_cursor_updates_are_isolated_by_calendar_scope(database_url: str) -> None:
    """同一连接的两个日历游标必须按 scope_key 独立 CAS，不能互相覆盖。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"b" * 32)
    async with sessions.begin() as session:
        user = UserModel(
            email="calendar-scopes@example.test",
            display_name="Scope Owner",
            password_hash=None,
            timezone="UTC",
            locale="zh-CN",
            brief_time=time(8),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        connection = OAuthConnectionModel(
            user_id=user.id,
            provider="google",
            provider_account_id="calendar-scope-subject",
            account_email="calendar-scopes@example.test",
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
            status="connected",
            last_error_code=None,
        )
        session.add(connection)
        await session.flush()
        session.add_all(
            [
                ConnectionCapabilityModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    capability="calendar.read",
                    status="enabled",
                    actual_scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                ),
                SyncCursorModel(
                    connection_id=connection.id,
                    resource_kind="calendar",
                    scope_key="calendar-a",
                    cursor="cursor-a-1",
                ),
                SyncCursorModel(
                    connection_id=connection.id,
                    resource_kind="calendar",
                    scope_key="calendar-b",
                    cursor="cursor-b-1",
                ),
                ProviderCalendarModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    provider_calendar_id="calendar-a",
                    name="Calendar A",
                    timezone="UTC",
                    is_primary=True,
                    access_role="owner",
                    can_write=True,
                    provider_url="https://calendar.example.test/calendar-a",
                ),
                ProviderCalendarModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    provider_calendar_id="calendar-b",
                    name="Calendar B",
                    timezone="UTC",
                    is_primary=False,
                    access_role="reader",
                    can_write=False,
                    provider_url="https://calendar.example.test/calendar-b",
                ),
            ]
        )
        user_id, connection_id = user.id, connection.id

    @asynccontextmanager
    async def stores():
        """为每次状态读取和最终 CAS 提供真实独立事务。"""
        async with sessions.begin() as session:
            yield SqlAlchemyCalendarSyncRepository(session)

    class ScopedCalendar:
        """直接实现 provider-neutral 日历端口，返回 calendar-a 的空增量页。"""

        async def directory_pages(self, cursor: str | None = None) -> AsyncIterator[object]:
            del cursor
            if False:
                yield object()

        async def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
            del calendar_id
            if False:
                yield CalendarSyncPage((), None, None)

        async def sync_pages(
            self, calendar_id: str, cursor: str
        ) -> AsyncIterator[CalendarSyncPage]:
            assert (calendar_id, cursor) == ("calendar-a", "cursor-a-1")
            yield CalendarSyncPage((), None, "cursor-a-2")

        async def get_current_event(
            self, calendar_id: str, provider_event_id: str
        ) -> CalendarEvent | None:
            del calendar_id, provider_event_id
            return None

    class ScopedRegistry:
        """为 Google 连接返回已经支持多日历签名的合成读取器。"""

        def calendar_reader(self, **kwargs: object) -> ScopedCalendar:
            """确认 provider 来自连接行，并保留精确 scope 选择事实。"""
            assert kwargs == {
                "provider": "google",
                "connection_id": connection_id,
                "scope_key": "calendar-a",
            }
            return ScopedCalendar()

    await SyncCalendarUseCase(
        stores,
        ScopedRegistry(),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="calendar-a")

    async with sessions() as session:
        rows = tuple(
            (
                await session.execute(
                    select(SyncCursorModel.scope_key, SyncCursorModel.cursor)
                    .where(
                        SyncCursorModel.connection_id == connection_id,
                        SyncCursorModel.resource_kind == "calendar",
                    )
                    .order_by(SyncCursorModel.scope_key)
                )
            ).all()
        )
    assert rows == (("calendar-a", "cursor-a-2"), ("calendar-b", "cursor-b-1"))
    scheduled = await SqlAlchemyEnabledSyncScopeReader(sessions).enabled_scopes()
    assert [(scope.provider, scope.resource_kind, scope.scope_key) for scope in scheduled] == [
        ("google", "calendar", "directory")
    ]
    await sessions.dispose()


@pytest.mark.asyncio
async def test_clear_cursor_and_finish_sync_share_connection_then_cursor_lock_order(
    database_url: str,
) -> None:
    """clear 与 finish 必须先锁 connection 再锁 cursor，避免反向等待形成数据库死锁。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    await SyncCalendarUseCase(
        _calendar_stores(sessions),
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    blocker_session = sessions()
    await blocker_session.begin()
    blocker_pid = await _postgres_backend_pid(blocker_session)
    locked_cursor = await blocker_session.scalar(
        select(SyncCursorModel)
        .where(
            SyncCursorModel.connection_id == connection_id,
            SyncCursorModel.resource_kind == "calendar",
            SyncCursorModel.scope_key == "primary",
        )
        .with_for_update()
    )
    assert locked_cursor is not None
    assert locked_cursor.cursor == "primary-token-1"

    loop = asyncio.get_running_loop()
    clear_pid_ready: asyncio.Future[int] = loop.create_future()
    finish_pid_ready: asyncio.Future[int] = loop.create_future()

    async def clear_cursor() -> None:
        """在独立事务中执行失效 CAS，并把 backend PID 暴露给锁图断言。"""
        async with sessions.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            clear_pid_ready.set_result(await _postgres_backend_pid(session))
            await SqlAlchemyCalendarSyncRepository(session).clear_cursor(
                user_id=user_id,
                connection_id=connection_id,
                scope_key="primary",
                expected_cursor="primary-token-1",
            )

    async def finish_sync() -> None:
        """并发执行正常完成 CAS；统一锁序下它应先等待 clear 持有的 connection。"""
        async with sessions.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            finish_pid_ready.set_result(await _postgres_backend_pid(session))
            await SqlAlchemyCalendarSyncRepository(session).finish_sync(
                user_id=user_id,
                connection_id=connection_id,
                scope_key="primary",
                expected_cursor="primary-token-1",
                next_cursor="primary-token-finished",
                event_count=0,
                used_full_resync=False,
                completed_at=datetime(2030, 1, 2, tzinfo=UTC),
            )

    clear_task = asyncio.create_task(clear_cursor())
    finish_task: asyncio.Task[None] | None = None
    results: tuple[object, ...] = ()
    try:
        clear_pid = await asyncio.wait_for(clear_pid_ready, timeout=2)
        clear_blockers = await _wait_for_postgres_blockers(
            sessions,
            waiting_pid=clear_pid,
        )
        assert blocker_pid in clear_blockers

        finish_task = asyncio.create_task(finish_sync())
        finish_pid = await asyncio.wait_for(finish_pid_ready, timeout=2)
        finish_blockers = await _wait_for_postgres_blockers(
            sessions,
            waiting_pid=finish_pid,
        )
    finally:
        # 无论锁序断言是否成立，都先释放第三会话并回收两个任务，避免失败测试污染连接池。
        await blocker_session.rollback()
        await blocker_session.close()
        tasks = (clear_task,) if finish_task is None else (clear_task, finish_task)
        results = tuple(
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=7,
            )
        )

    assert finish_blockers == (clear_pid,)
    assert results[0] is None
    assert isinstance(results[1], TransientProviderError)
    assert results[1].error_code == "calendar_sync_cursor_conflict"
    async with sessions() as session:
        cursor = await session.scalar(
            select(SyncCursorModel.cursor).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "primary",
            )
        )
    assert cursor is None
    await sessions.dispose()


@pytest.mark.parametrize("boundary", ("foreign-user", "disabled-capability"))
@pytest.mark.asyncio
async def test_clear_cursor_preserves_ownership_capability_and_error_contract(
    database_url: str,
    boundary: str,
) -> None:
    """拆分锁查询后仍须隐藏跨用户连接，并在能力撤销时保持原稳定冲突错误。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    await SyncCalendarUseCase(
        _calendar_stores(sessions),
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    actor_user_id = user_id
    async with sessions.begin() as session:
        if boundary == "foreign-user":
            foreign = UserModel(
                email="calendar-clear-foreign@example.test",
                display_name="Foreign Clear Actor",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
            session.add(foreign)
            await session.flush()
            actor_user_id = foreign.id
        else:
            capability = await session.scalar(
                select(ConnectionCapabilityModel).where(
                    ConnectionCapabilityModel.user_id == user_id,
                    ConnectionCapabilityModel.connection_id == connection_id,
                    ConnectionCapabilityModel.capability == "calendar.read",
                )
            )
            assert capability is not None
            capability.status = "disabled"

    with pytest.raises(TransientProviderError) as raised:
        async with sessions.begin() as session:
            await SqlAlchemyCalendarSyncRepository(session).clear_cursor(
                user_id=actor_user_id,
                connection_id=connection_id,
                scope_key="primary",
                expected_cursor="primary-token-1",
            )

    async with sessions() as session:
        cursor = await session.scalar(
            select(SyncCursorModel.cursor).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "primary",
            )
        )
    assert raised.value.error_code == "calendar_sync_cursor_conflict"
    assert cursor == "primary-token-1"
    await sessions.dispose()


@pytest.mark.asyncio
async def test_directory_sync_persists_roles_events_and_is_idempotent_on_replay(
    database_url: str,
) -> None:
    """目录 owner 应建立独立 scopes、保存规范字段，并让重复投递只覆盖原事件。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    first_reader = DirectoryCalendarReader(_directory_calendars())
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=first_reader),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    replay_reader = DirectoryCalendarReader(
        _directory_calendars(),
        directory_tokens=("directory-token-2",),
        event_generation=2,
    )
    replay = await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=replay_reader),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        directory_rows = tuple(
            (
                await session.execute(
                    select(
                        ProviderCalendarModel.provider_calendar_id,
                        ProviderCalendarModel.access_role,
                        ProviderCalendarModel.can_write,
                    )
                    .where(ProviderCalendarModel.connection_id == connection_id)
                    .order_by(ProviderCalendarModel.provider_calendar_id)
                )
            ).all()
        )
        cursor_rows = tuple(
            (
                await session.execute(
                    select(SyncCursorModel.scope_key, SyncCursorModel.cursor)
                    .where(
                        SyncCursorModel.connection_id == connection_id,
                        SyncCursorModel.resource_kind == "calendar",
                    )
                    .order_by(SyncCursorModel.scope_key)
                )
            ).all()
        )
        event_rows = tuple(
            (
                await session.scalars(
                    select(CalendarEventModel)
                    .where(CalendarEventModel.connection_id == connection_id)
                    .order_by(CalendarEventModel.calendar_id)
                )
            ).all()
        )

    assert directory_rows == (
        ("primary", "owner", True),
        ("readonly@example.test", "reader", False),
    )
    assert cursor_rows == (
        ("directory", "directory-token-2"),
        ("primary", "primary-token-2"),
        ("readonly@example.test", "readonly@example.test-token-2"),
    )
    assert replay.events_upserted == 2
    assert len(event_rows) == 2
    assert all(row.provider_updated_at == datetime(2030, 1, 1, tzinfo=UTC) for row in event_rows)
    assert all(
        row.organizer
        == {
            "email": "organizer@example.test",
            "displayName": "Synthetic Organizer",
        }
        for row in event_rows
    )
    assert all(
        row.attendees
        == [
            {
                "email": "attendee@example.test",
                "responseStatus": "accepted",
            }
        ]
        for row in event_rows
    )
    assert [(row.calendar_id, row.access_role, row.can_edit) for row in event_rows] == [
        ("primary", "owner", True),
        ("readonly@example.test", "reader", False),
    ]
    assert all(b"private description" not in row.description_ciphertext for row in event_rows)
    await sessions.dispose()


@pytest.mark.asyncio
async def test_directory_tombstone_removes_projection_events_and_future_scope_sync(
    database_url: str,
) -> None:
    """目录删除事实必须撤销 selector 投影、来源缓存，并停止访问已移除 scope。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    reader = DirectoryCalendarReader(
        (_deleted_calendar("readonly@example.test"),),
        directory_tokens=("directory-token-2",),
        event_generation=2,
    )
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=reader),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        snapshot = await SqlAlchemyConnectionStore(session).get_capability_snapshot(
            user_id=user_id,
            connection_id=connection_id,
        )
        cached_calendar_ids = tuple(
            (
                await session.scalars(
                    select(CalendarEventModel.calendar_id)
                    .where(CalendarEventModel.connection_id == connection_id)
                    .order_by(CalendarEventModel.calendar_id)
                )
            ).all()
        )
        brief_sources = await GenerateBriefTaskStep(sessions)._events_for_local_day(
            session,
            user_id,
            date(2030, 1, 2),
            "UTC",
            connection_id,
        )

    assert snapshot is not None
    assert [calendar.id for calendar in snapshot.provider_calendars] == ["primary"]
    assert cached_calendar_ids == ("primary",)
    assert {item["provider_url"] for item in brief_sources} == {
        "https://calendar.example.test/events/primary"
    }
    assert reader.sync_calls == [("primary", "primary-token-1")]
    await sessions.dispose()


@pytest.mark.asyncio
async def test_reappearing_calendar_discards_retained_cursor_and_rebuilds_event_cache(
    database_url: str,
) -> None:
    """目录删除后重新出现的日历必须走完整窗口，不能用保留 cursor 的空 delta 恢复缓存。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(
            google_calendar=DirectoryCalendarReader(
                (_deleted_calendar("readonly@example.test"),),
                directory_tokens=("directory-token-2",),
                event_generation=2,
            )
        ),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    reappearing = DirectoryCalendarReader(
        (_directory_calendars()[1],),
        directory_tokens=("directory-token-3",),
        event_generation=3,
        empty_delta_scopes=frozenset({"readonly@example.test"}),
    )
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=reappearing),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        event_calendar_ids = tuple(
            (
                await session.scalars(
                    select(CalendarEventModel.calendar_id)
                    .where(CalendarEventModel.connection_id == connection_id)
                    .order_by(CalendarEventModel.calendar_id)
                )
            ).all()
        )
        cursor_rows = dict(
            (
                await session.execute(
                    select(SyncCursorModel.scope_key, SyncCursorModel.cursor).where(
                        SyncCursorModel.connection_id == connection_id,
                        SyncCursorModel.resource_kind == "calendar",
                    )
                )
            ).all()
        )

    assert reappearing.initial_calls == ["readonly@example.test"]
    assert reappearing.sync_calls == [("primary", "primary-token-2")]
    assert event_calendar_ids == ("primary", "readonly@example.test")
    assert cursor_rows == {
        "directory": "directory-token-3",
        "primary": "primary-token-3",
        "readonly@example.test": "readonly@example.test-token-3",
    }
    await sessions.dispose()


@pytest.mark.asyncio
async def test_first_directory_discovery_resets_legacy_primary_cursor_without_projection(
    database_url: str,
) -> None:
    """历史 primary cursor 没有目录证明时必须视为首次发现并通过完整窗口重建。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    async with sessions.begin() as session:
        session.add(
            SyncCursorModel(
                connection_id=connection_id,
                resource_kind="calendar",
                scope_key="primary",
                cursor="legacy-primary-token",
                last_success_at=datetime(2029, 12, 31, tzinfo=UTC),
            )
        )
    reader = DirectoryCalendarReader(
        (_directory_calendars()[0],),
        empty_delta_scopes=frozenset({"primary"}),
    )

    await SyncCalendarUseCase(
        _calendar_stores(sessions),
        ProviderAdapterRegistry(google_calendar=reader),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        event_count = await session.scalar(
            select(func.count())
            .select_from(CalendarEventModel)
            .where(
                CalendarEventModel.connection_id == connection_id,
                CalendarEventModel.calendar_id == "primary",
            )
        )
        cursor = await session.scalar(
            select(SyncCursorModel.cursor).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "primary",
            )
        )

    assert reader.initial_calls == ["primary"]
    assert reader.sync_calls == []
    assert event_count == 1
    assert cursor == "primary-token-1"
    await sessions.dispose()


@pytest.mark.asyncio
async def test_directory_410_full_snapshot_removes_absent_calendar_with_owner_isolation(
    database_url: str,
) -> None:
    """410 后完整目录只保留本次可见项，并精确隔离同名的其他用户/连接数据。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    control_owners = set(await _seed_calendar_isolation_controls(sessions, owner_user_id=user_id))
    reader = DirectoryCalendarReader(
        (_directory_calendars()[0],),
        directory_tokens=("ignored-after-410", "directory-token-reset"),
        event_generation=2,
        expire_directory_once=True,
    )

    result = await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=reader),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        snapshot = await SqlAlchemyConnectionStore(session).get_capability_snapshot(
            user_id=user_id,
            connection_id=connection_id,
        )
        target_event_calendar_ids = tuple(
            (
                await session.scalars(
                    select(CalendarEventModel.calendar_id)
                    .where(CalendarEventModel.connection_id == connection_id)
                    .order_by(CalendarEventModel.calendar_id)
                )
            ).all()
        )
        cursor_rows = dict(
            (
                await session.execute(
                    select(SyncCursorModel.scope_key, SyncCursorModel.cursor).where(
                        SyncCursorModel.connection_id == connection_id,
                        SyncCursorModel.resource_kind == "calendar",
                    )
                )
            ).all()
        )
        brief_sources = await GenerateBriefTaskStep(sessions)._events_for_local_day(
            session,
            user_id,
            date(2030, 1, 2),
            "UTC",
            connection_id,
        )
        remaining_calendar_owners = set(
            (
                await session.execute(
                    select(
                        ProviderCalendarModel.user_id,
                        ProviderCalendarModel.connection_id,
                    ).where(ProviderCalendarModel.provider_calendar_id == "readonly@example.test")
                )
            ).all()
        )
        remaining_event_owners = set(
            (
                await session.execute(
                    select(
                        CalendarEventModel.user_id,
                        CalendarEventModel.connection_id,
                    ).where(CalendarEventModel.calendar_id == "readonly@example.test")
                )
            ).all()
        )

    assert snapshot is not None
    assert [calendar.id for calendar in snapshot.provider_calendars] == ["primary"]
    assert target_event_calendar_ids == ("primary",)
    assert {item["provider_url"] for item in brief_sources} == {
        "https://calendar.example.test/events/primary"
    }
    assert reader.directory_calls == ["directory-token-1", None]
    assert reader.sync_calls == [("primary", "primary-token-1")]
    assert cursor_rows == {
        "directory": "directory-token-reset",
        "primary": "primary-token-2",
        "readonly@example.test": "readonly@example.test-token-1",
    }
    assert result.events_upserted == 1
    assert remaining_calendar_owners == control_owners
    assert remaining_event_owners == control_owners
    await sessions.dispose()


@pytest.mark.asyncio
async def test_full_empty_directory_snapshot_removes_only_current_cache_and_keeps_cursors_audit(
    database_url: str,
) -> None:
    """全量空快照撤销当前目录/缓存，但保留所有事件 cursor 与追加审计。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    reader = DirectoryCalendarReader(
        (),
        directory_tokens=("ignored-after-410", "directory-empty-reset"),
        event_generation=2,
        expire_directory_once=True,
    )

    result = await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=reader),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        calendar_count = await session.scalar(
            select(func.count())
            .select_from(ProviderCalendarModel)
            .where(
                ProviderCalendarModel.user_id == user_id,
                ProviderCalendarModel.connection_id == connection_id,
            )
        )
        event_count = await session.scalar(
            select(func.count())
            .select_from(CalendarEventModel)
            .where(
                CalendarEventModel.user_id == user_id,
                CalendarEventModel.connection_id == connection_id,
            )
        )
        cursor_rows = dict(
            (
                await session.execute(
                    select(SyncCursorModel.scope_key, SyncCursorModel.cursor).where(
                        SyncCursorModel.connection_id == connection_id,
                        SyncCursorModel.resource_kind == "calendar",
                    )
                )
            ).all()
        )
        directory_audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditEventModel)
            .where(
                AuditEventModel.user_id == user_id,
                AuditEventModel.event_type == "source.calendar.directory_discovered",
            )
        )

    assert calendar_count == 0
    assert event_count == 0
    assert reader.sync_calls == []
    assert cursor_rows == {
        "directory": "directory-empty-reset",
        "primary": "primary-token-1",
        "readonly@example.test": "readonly@example.test-token-1",
    }
    assert directory_audit_count == 2
    assert result.events_upserted == 0
    await sessions.dispose()


@pytest.mark.asyncio
async def test_incremental_empty_directory_delta_preserves_every_visible_calendar(
    database_url: str,
) -> None:
    """增量空变更只推进目录 token，未返回的既有日历仍须保留并继续同步。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    reader = DirectoryCalendarReader(
        (),
        directory_tokens=("directory-token-2",),
        event_generation=2,
    )

    result = await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=reader),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        calendar_ids = tuple(
            (
                await session.scalars(
                    select(ProviderCalendarModel.provider_calendar_id)
                    .where(ProviderCalendarModel.connection_id == connection_id)
                    .order_by(ProviderCalendarModel.provider_calendar_id)
                )
            ).all()
        )
        event_calendar_ids = tuple(
            (
                await session.scalars(
                    select(CalendarEventModel.calendar_id)
                    .where(CalendarEventModel.connection_id == connection_id)
                    .order_by(CalendarEventModel.calendar_id)
                )
            ).all()
        )

    assert calendar_ids == ("primary", "readonly@example.test")
    assert event_calendar_ids == ("primary", "readonly@example.test")
    assert reader.sync_calls == [
        ("primary", "primary-token-1"),
        ("readonly@example.test", "readonly@example.test-token-1"),
    ]
    assert result.events_upserted == 2
    await sessions.dispose()


@pytest.mark.asyncio
async def test_directory_acl_downgrade_tightens_cached_events_without_event_delta(
    database_url: str,
) -> None:
    """目录 owner→reader 即使事件增量为空，也必须立即收紧历史事件修改权限。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    downgraded = ProviderCalendar(
        calendar_id="primary",
        display_name="Primary",
        timezone="UTC",
        is_primary=True,
        access_role="reader",
        can_write=False,
        provider_url="https://calendar.example.test/primary",
    )

    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(
            google_calendar=DirectoryCalendarReader(
                (downgraded,),
                directory_tokens=("directory-token-2",),
                event_generation=2,
                emit_events=False,
            )
        ),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        event = await session.scalar(
            select(CalendarEventModel).where(
                CalendarEventModel.connection_id == connection_id,
                CalendarEventModel.calendar_id == "primary",
            )
        )

    assert event is not None
    assert event.access_role == "reader"
    assert event.can_edit is False
    await sessions.dispose()


@pytest.mark.asyncio
async def test_directory_acl_upgrade_does_not_blindly_relax_cached_event(
    database_url: str,
) -> None:
    """目录 reader→owner 只更新角色；缺少新事件 locked 事实时不得把 can_edit 提升为 true。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    upgraded = ProviderCalendar(
        calendar_id="readonly@example.test",
        display_name="Readonly upgraded",
        timezone="UTC",
        is_primary=False,
        access_role="owner",
        can_write=True,
        provider_url="https://calendar.example.test/readonly",
    )

    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(
            google_calendar=DirectoryCalendarReader(
                (upgraded,),
                directory_tokens=("directory-token-2",),
                event_generation=2,
                emit_events=False,
            )
        ),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        event = await session.scalar(
            select(CalendarEventModel).where(
                CalendarEventModel.connection_id == connection_id,
                CalendarEventModel.calendar_id == "readonly@example.test",
            )
        )

    assert event is not None
    assert event.access_role == "owner"
    assert event.can_edit is False
    await sessions.dispose()


@pytest.mark.asyncio
async def test_unknown_calendar_scope_is_rejected_without_cursor_provider_call_or_event(
    database_url: str,
) -> None:
    """未被目录证明的显式维修 scope 必须在仓储边界 fail closed。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    reader = DirectoryCalendarReader(_directory_calendars())

    with pytest.raises(CalendarConnectionNotFoundError):
        await SyncCalendarUseCase(
            _calendar_stores(sessions),
            ProviderAdapterRegistry(google_calendar=reader),
            cipher,
        ).execute(
            user_id=user_id,
            connection_id=connection_id,
            scope_key="unlisted@example.test",
        )

    async with sessions() as session:
        unknown_cursor_count = await session.scalar(
            select(func.count())
            .select_from(SyncCursorModel)
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "unlisted@example.test",
            )
        )
        unknown_event_count = await session.scalar(
            select(func.count())
            .select_from(CalendarEventModel)
            .where(
                CalendarEventModel.connection_id == connection_id,
                CalendarEventModel.calendar_id == "unlisted@example.test",
            )
        )

    assert reader.initial_calls == []
    assert reader.sync_calls == []
    assert unknown_cursor_count == 0
    assert unknown_event_count == 0
    await sessions.dispose()


@pytest.mark.asyncio
async def test_discovered_calendar_scope_can_be_synced_explicitly(database_url: str) -> None:
    """已发现的 provider calendar ID 仍可由显式维修任务推进自己的 cursor。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    reader = DirectoryCalendarReader(
        _directory_calendars(),
        event_generation=2,
    )

    result = await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=reader),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="primary")

    assert reader.sync_calls == [("primary", "primary-token-1")]
    assert result.next_cursor == "primary-token-2"
    await sessions.dispose()


@pytest.mark.asyncio
async def test_initial_directory_requires_sync_token_on_final_page(database_url: str) -> None:
    """初始目录不得接受前页 token；最终页缺 token 时不能写目录或启动事件读取。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    reader = DirectoryCalendarReader(
        _directory_calendars(),
        directory_pages_override=(
            CalendarDirectoryPage(
                _directory_calendars(),
                "directory-page-2",
                "token-illegally-on-first-page",
            ),
            CalendarDirectoryPage((), None, None),
        ),
    )

    with pytest.raises(InternalInvariantError) as raised:
        await SyncCalendarUseCase(
            _calendar_stores(sessions),
            ProviderAdapterRegistry(google_calendar=reader),
            cipher,
        ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        directory_cursor = await session.scalar(
            select(SyncCursorModel).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "directory",
            )
        )
        calendar_count = await session.scalar(
            select(func.count())
            .select_from(ProviderCalendarModel)
            .where(ProviderCalendarModel.connection_id == connection_id)
        )

    assert raised.value.error_code == "calendar_directory_final_cursor_missing"
    assert directory_cursor is not None
    assert directory_cursor.cursor is None
    assert directory_cursor.last_success_at is None
    assert calendar_count == 0
    assert reader.initial_calls == []
    assert reader.sync_calls == []
    await sessions.dispose()


@pytest.mark.asyncio
async def test_incremental_directory_missing_final_token_preserves_old_success(
    database_url: str,
) -> None:
    """增量最终页缺 token 时不得复用旧 cursor、推进成功时间或读取事件。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    async with sessions() as session:
        before = await session.scalar(
            select(SyncCursorModel).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "directory",
            )
        )
        assert before is not None
        previous_success_at = before.last_success_at

    reader = DirectoryCalendarReader(
        _directory_calendars(),
        directory_pages_override=(CalendarDirectoryPage((), None, None),),
    )
    with pytest.raises(InternalInvariantError) as raised:
        await SyncCalendarUseCase(
            stores,
            ProviderAdapterRegistry(google_calendar=reader),
            cipher,
        ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        after = await session.scalar(
            select(SyncCursorModel).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "directory",
            )
        )

    assert raised.value.error_code == "calendar_directory_final_cursor_missing"
    assert after is not None
    assert after.cursor == "directory-token-1"
    assert after.last_success_at == previous_success_at
    assert reader.initial_calls == []
    assert reader.sync_calls == []
    await sessions.dispose()


@pytest.mark.parametrize(
    ("scope_key", "url", "malformed_payload"),
    (
        ("directory", GOOGLE_CALENDAR_LIST_URL, {"items": {}}),
        (
            "primary",
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            {"items": [], "nextSyncToken": ""},
        ),
    ),
    ids=("directory-page", "event-page"),
)
@pytest.mark.asyncio
@respx.mock
async def test_malformed_google_calendar_page_preserves_all_persisted_sync_facts(
    database_url: str,
    scope_key: str,
    url: str,
    malformed_payload: dict[str, object],
) -> None:
    """畸形目录页或事件页必须在供应商 I/O 边界失败，不得触碰任何数据库事实。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    before = await _calendar_persistence_snapshot(
        sessions,
        user_id=user_id,
        connection_id=connection_id,
    )
    respx.get(url).mock(return_value=httpx.Response(200, json=malformed_payload))

    with pytest.raises(PermanentProviderError) as raised:
        await SyncCalendarUseCase(
            stores,
            ProviderAdapterRegistry(
                google_calendar=GoogleCalendarAdapter(
                    access_token="synthetic",
                    user_timezone="UTC",
                )
            ),
            cipher,
        ).execute(user_id=user_id, connection_id=connection_id, scope_key=scope_key)

    after = await _calendar_persistence_snapshot(
        sessions,
        user_id=user_id,
        connection_id=connection_id,
    )
    assert raised.value.error_code == "google_calendar_response_invalid"
    assert after == before
    await sessions.dispose()


@pytest.mark.asyncio
async def test_directory_cursor_410_preserves_every_event_cursor(database_url: str) -> None:
    """CalendarList 410 只能清 directory；事件 scopes 必须继续从原 token 增量读取。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    reader = DirectoryCalendarReader(
        _directory_calendars(),
        directory_tokens=("ignored-after-410", "directory-token-reset"),
        event_generation=2,
        expire_directory_once=True,
    )
    result = await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=reader),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        cursor_rows = dict(
            (
                await session.execute(
                    select(SyncCursorModel.scope_key, SyncCursorModel.cursor).where(
                        SyncCursorModel.connection_id == connection_id,
                        SyncCursorModel.resource_kind == "calendar",
                    )
                )
            ).all()
        )
    assert reader.directory_calls == ["directory-token-1", None]
    assert reader.initial_calls == []
    assert reader.sync_calls == [
        ("primary", "primary-token-1"),
        ("readonly@example.test", "readonly@example.test-token-1"),
    ]
    assert cursor_rows == {
        "directory": "directory-token-reset",
        "primary": "primary-token-2",
        "readonly@example.test": "readonly@example.test-token-2",
    }
    assert result.used_full_resync is True
    await sessions.dispose()


@pytest.mark.asyncio
async def test_event_cursor_410_resets_only_that_calendar_and_continues_others(
    database_url: str,
) -> None:
    """单日历 syncToken 410 应只全量重读该 scope，其他日历仍走自身增量 token。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    reader = DirectoryCalendarReader(
        _directory_calendars(),
        directory_tokens=("directory-token-2",),
        event_generation=2,
        expire_event_once="primary",
    )
    result = await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=reader),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        cursor_rows = dict(
            (
                await session.execute(
                    select(SyncCursorModel.scope_key, SyncCursorModel.cursor).where(
                        SyncCursorModel.connection_id == connection_id,
                        SyncCursorModel.resource_kind == "calendar",
                    )
                )
            ).all()
        )
    assert reader.sync_calls == [
        ("primary", "primary-token-1"),
        ("readonly@example.test", "readonly@example.test-token-1"),
    ]
    assert reader.initial_calls == ["primary"]
    assert cursor_rows == {
        "directory": "directory-token-2",
        "primary": "primary-token-2",
        "readonly@example.test": "readonly@example.test-token-2",
    }
    assert result.used_full_resync is True
    await sessions.dispose()


@pytest.mark.asyncio
async def test_directory_sync_keeps_other_calendar_success_when_one_scope_fails(
    database_url: str,
) -> None:
    """一个日历暂态失败不能回滚目录事实或已成功提交的其他日历 cursor。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    stores = _calendar_stores(sessions)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")
    reader = DirectoryCalendarReader(
        _directory_calendars(),
        directory_tokens=("directory-token-2",),
        event_generation=2,
        fail_event="primary",
    )

    with pytest.raises(TransientProviderError) as raised:
        await SyncCalendarUseCase(
            stores,
            ProviderAdapterRegistry(google_calendar=reader),
            cipher,
        ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        cursor_rows = dict(
            (
                await session.execute(
                    select(SyncCursorModel.scope_key, SyncCursorModel.cursor).where(
                        SyncCursorModel.connection_id == connection_id,
                        SyncCursorModel.resource_kind == "calendar",
                    )
                )
            ).all()
        )
        event_calendars = tuple(
            (
                await session.scalars(
                    select(CalendarEventModel.calendar_id)
                    .where(CalendarEventModel.connection_id == connection_id)
                    .order_by(CalendarEventModel.calendar_id)
                )
            ).all()
        )

    assert raised.value.error_code == "synthetic_calendar_unavailable"
    assert cursor_rows == {
        "directory": "directory-token-2",
        "primary": "primary-token-1",
        "readonly@example.test": "readonly@example.test-token-2",
    }
    assert event_calendars == ("primary", "readonly@example.test")
    await sessions.dispose()


@pytest.mark.asyncio
async def test_daily_brief_source_query_sees_events_from_every_discovered_calendar(
    database_url: str,
) -> None:
    """目录同步后的主日历与只读日历事件都必须进入同一用户的简报来源查询。"""
    sessions, cipher, user_id, connection_id = await _seed_google_calendar_connection(database_url)
    await SyncCalendarUseCase(
        _calendar_stores(sessions),
        ProviderAdapterRegistry(google_calendar=DirectoryCalendarReader(_directory_calendars())),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        sources = await GenerateBriefTaskStep(sessions)._events_for_local_day(
            session,
            user_id,
            date(2030, 1, 2),
            "UTC",
            connection_id,
        )

    assert len(sources) == 2
    assert {item["provider_url"] for item in sources} == {
        "https://calendar.example.test/events/primary",
        "https://calendar.example.test/events/readonly-example.test",
    }
    await sessions.dispose()
