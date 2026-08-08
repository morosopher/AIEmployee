"""在 PostgreSQL 上验证 Calendar 加密、tombstone、游标与用户隔离。"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from uuid import UUID

import pytest
from sqlalchemy import func, select

from ai_employee.application.ports.calendar import (
    CalendarCursorExpiredError,
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarSyncPage,
    ProviderCalendar,
)
from ai_employee.application.use_cases.sync_calendar import (
    CalendarSyncStoreFactory,
    SyncCalendarUseCase,
)
from ai_employee.domain.errors import TransientProviderError
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.calendar import (
    SqlAlchemyCalendarSyncRepository,
    SqlAlchemyEnabledSyncScopeReader,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
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
            (self._event(calendar_id),),
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
            (self._event(calendar_id),),
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
