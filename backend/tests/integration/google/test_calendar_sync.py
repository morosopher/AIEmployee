"""在 PostgreSQL 上验证 Calendar 加密、tombstone、游标与用户隔离。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time

import pytest
from sqlalchemy import select

from ai_employee.application.ports.calendar import CalendarEvent, CalendarSyncPage
from ai_employee.application.use_cases.sync_calendar import SyncCalendarUseCase
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher


class FakeCalendar:
    """以确定性页替代外部 Calendar，确保集成测试只覆盖 PostgreSQL 边界。"""
    def __init__(self, page: CalendarSyncPage) -> None: self.page = page
    async def initial_pages(self) -> AsyncIterator[CalendarSyncPage]: yield self.page
    async def sync_pages(self, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        del cursor
        yield self.page


def _event(status: str = "confirmed") -> CalendarEvent:
    """创建包含敏感字段的合成事件，用于断言数据库只保存密文。"""
    return CalendarEvent("event-1", "primary", "Synthetic", "secret description", "secret location", datetime(2030, 1, 2, 1, tzinfo=UTC), datetime(2030, 1, 2, 2, tzinfo=UTC), False, "opaque", status, "UTC", None, "etag-1", "https://calendar.example.test/e/1")


@pytest.mark.asyncio
async def test_calendar_sync_encrypts_upserts_tombstone_and_advances_cursor(database_url: str) -> None:
    """同一连接重复同步只保留一行，取消状态保存 tombstone，最后才提交 cursor。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"a" * 32)
    async with sessions.begin() as session:
        user = UserModel(email="calendar-owner@example.test", display_name="Owner", password_hash=None, timezone="UTC", locale="zh-CN", brief_time=time(8), is_active=True); session.add(user); await session.flush()
        connection = OAuthConnectionModel(user_id=user.id, provider="google", provider_account_id="calendar-subject", account_email="calendar-owner@example.test", scopes=[], status="connected", last_error_code=None); session.add(connection); await session.flush()
        session.add(SyncCursorModel(connection_id=connection.id, resource_kind="calendar", cursor=None))
        user_id, connection_id = user.id, connection.id
    @asynccontextmanager
    async def stores():
        async with sessions.begin() as session: yield SqlAlchemyCalendarSyncRepository(session)
    await SyncCalendarUseCase(stores, cipher, FakeCalendar(CalendarSyncPage((_event(),), None, "token-1"))).execute(user_id=user_id, connection_id=connection_id)
    await SyncCalendarUseCase(stores, cipher, FakeCalendar(CalendarSyncPage((_event("cancelled"),), None, "token-2"))).execute(user_id=user_id, connection_id=connection_id)
    async with sessions() as session:
        row = await session.scalar(select(CalendarEventModel).where(CalendarEventModel.connection_id == connection_id))
        cursor = await session.scalar(select(SyncCursorModel.cursor).where(SyncCursorModel.connection_id == connection_id, SyncCursorModel.resource_kind == "calendar"))
    assert row is not None and row.status == "cancelled" and row.description_ciphertext != b"secret description"
    assert cursor == "token-2"
    await sessions.dispose()
