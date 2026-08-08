"""在 PostgreSQL 上验证 Microsoft Calendar 的用户隔离、三元身份与 scoped CAS。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time

import pytest
from sqlalchemy import select

from ai_employee.application.ports.calendar import CalendarEvent, CalendarSyncPage, ProviderCalendar
from ai_employee.application.use_cases.sync_calendar import SyncCalendarUseCase
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry


class _Reader:
    """提供不联网的 Microsoft directory/事件页，模拟两个独立日历 scope。"""

    async def directory_pages(self, cursor: str | None = None) -> AsyncIterator[object]:
        del cursor
        yield type(
            "DirectoryPage",
            (),
            {
                "calendars": (
                    ProviderCalendar("m-cal-1", "Primary", "UTC", True, "owner", True),
                    ProviderCalendar("m-cal-2", "Read only", "UTC", False, "reader", False),
                ),
                "next_page_token": None,
                "next_cursor": "directory-v1",
            },
        )()

    async def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        yield CalendarSyncPage((_event(calendar_id),), None, f"delta-{calendar_id}-1")

    async def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        yield CalendarSyncPage((), None, f"{cursor}-next")

    async def get_current_event(self, calendar_id: str, provider_event_id: str):
        del calendar_id, provider_event_id


def _event(calendar_id: str) -> CalendarEvent:
    """返回包含版本与加密字段的合成事件。"""
    return CalendarEvent(
        event_id="same-event-id",
        calendar_id=calendar_id,
        title=f"Event {calendar_id}",
        description="secret description",
        location="secret location",
        starts_at=datetime(2030, 1, 2, 1, tzinfo=UTC),
        ends_at=datetime(2030, 1, 2, 2, tzinfo=UTC),
        all_day=False,
        transparency="opaque",
        status="confirmed",
        timezone="UTC",
        recurring_event_id=None,
        etag=f"etag-{calendar_id}",
        provider_url=f"https://outlook.example.test/{calendar_id}/same-event-id",
        can_edit=True,
    )


@pytest.mark.asyncio
async def test_microsoft_calendar_sync_keeps_user_and_calendar_scopes_isolated(
    database_url: str,
) -> None:
    """同连接不同 calendar 可共享 event ID，但不同用户事实不能互相覆盖。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"m" * 32)
    async with sessions.begin() as session:
        owner = UserModel(
            email="microsoft-calendar-owner@example.test",
            display_name="Owner",
            password_hash=None,
            timezone="UTC",
            locale="zh-CN",
            brief_time=time(8),
            is_active=True,
        )
        foreign = UserModel(
            email="microsoft-calendar-foreign@example.test",
            display_name="Foreign",
            password_hash=None,
            timezone="UTC",
            locale="zh-CN",
            brief_time=time(8),
            is_active=True,
        )
        session.add_all((owner, foreign))
        await session.flush()
        owner_connection = OAuthConnectionModel(
            user_id=owner.id,
            provider="microsoft",
            provider_account_id="owner-subject",
            provider_tenant_id="tenant-owner",
            account_type="work_school",
            account_email=owner.email,
            scopes=["Calendars.Read"],
            status="connected",
            last_error_code=None,
        )
        foreign_connection = OAuthConnectionModel(
            user_id=foreign.id,
            provider="microsoft",
            provider_account_id="foreign-subject",
            provider_tenant_id="tenant-foreign",
            account_type="work_school",
            account_email=foreign.email,
            scopes=["Calendars.Read"],
            status="connected",
            last_error_code=None,
        )
        session.add_all((owner_connection, foreign_connection))
        await session.flush()
        for user, connection in ((owner, owner_connection), (foreign, foreign_connection)):
            session.add(
                ConnectionCapabilityModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    capability="calendar.read",
                    status="enabled",
                    actual_scopes=["Calendars.Read"],
                )
            )
            for calendar_id in ("m-cal-1", "m-cal-2"):
                session.add(
                    ProviderCalendarModel(
                        user_id=user.id,
                        connection_id=connection.id,
                        provider_calendar_id=calendar_id,
                        name=calendar_id,
                        timezone="UTC",
                        is_primary=calendar_id == "m-cal-1",
                        access_role="owner" if calendar_id == "m-cal-1" else "reader",
                        can_write=calendar_id == "m-cal-1",
                        provider_url=None,
                    )
                )
                session.add(
                    SyncCursorModel(
                        connection_id=connection.id,
                        resource_kind="calendar",
                        scope_key=calendar_id,
                        cursor=None,
                    )
                )
        owner_id, owner_connection_id = owner.id, owner_connection.id
        foreign_id, foreign_connection_id = foreign.id, foreign_connection.id

    @asynccontextmanager
    async def stores():
        async with sessions.begin() as session:
            yield SqlAlchemyCalendarSyncRepository(session)

    reader = _Reader()
    registry = ProviderAdapterRegistry(microsoft_calendar=reader)
    use_case = SyncCalendarUseCase(stores, registry, cipher)
    await use_case.execute(user_id=owner_id, connection_id=owner_connection_id, scope_key="m-cal-1")
    await use_case.execute(user_id=owner_id, connection_id=owner_connection_id, scope_key="m-cal-2")

    async with sessions() as session:
        owner_events = (
            await session.scalars(
                select(CalendarEventModel).where(
                    CalendarEventModel.user_id == owner_id,
                    CalendarEventModel.connection_id == owner_connection_id,
                )
            )
        ).all()
        foreign_events = (
            await session.scalars(
                select(CalendarEventModel).where(
                    CalendarEventModel.user_id == foreign_id,
                    CalendarEventModel.connection_id == foreign_connection_id,
                )
            )
        ).all()
        cursors = (
            await session.scalars(
                select(SyncCursorModel).where(
                    SyncCursorModel.connection_id == owner_connection_id,
                    SyncCursorModel.resource_kind == "calendar",
                )
            )
        ).all()
    assert {(row.calendar_id, row.provider_event_id) for row in owner_events} == {
        ("m-cal-1", "same-event-id"),
        ("m-cal-2", "same-event-id"),
    }
    assert foreign_events == []
    assert {row.scope_key: row.cursor for row in cursors} == {
        "m-cal-1": "delta-m-cal-1-1",
        "m-cal-2": "delta-m-cal-2-1",
    }
    await sessions.dispose()
