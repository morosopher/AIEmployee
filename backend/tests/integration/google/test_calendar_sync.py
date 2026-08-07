"""在 PostgreSQL 上验证 Calendar 加密、tombstone、游标与用户隔离。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time

import pytest
from sqlalchemy import func, select

from ai_employee.application.ports.calendar import CalendarEvent, CalendarSyncPage
from ai_employee.application.use_cases.sync_calendar import SyncCalendarUseCase
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry


class FakeCalendar:
    """以确定性页替代外部 Calendar，确保集成测试只覆盖 PostgreSQL 边界。"""

    def __init__(self, page: CalendarSyncPage) -> None:
        self.page = page

    async def initial_pages(self) -> AsyncIterator[CalendarSyncPage]:
        yield self.page

    async def sync_pages(self, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        del cursor
        yield self.page


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
    await sessions.dispose()
