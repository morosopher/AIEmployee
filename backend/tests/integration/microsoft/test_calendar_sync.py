"""在 PostgreSQL 上验证 Microsoft Calendar 的用户隔离、三元身份与 scoped CAS。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select

from ai_employee.application.ports.calendar import (
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarSyncPage,
    ProviderCalendar,
)
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.oauth import OAuthTokenSet
from ai_employee.application.use_cases.sync_calendar import SyncCalendarUseCase
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.errors import (
    PermanentProviderError,
    StateConflictError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories.calendar import (
    SqlAlchemyCalendarSyncRepository,
    SqlAlchemyEnabledSyncScopeReader,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.observability.metrics import create_metrics
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.fake import FakeGoogleOAuthClient
from ai_employee.integrations.microsoft.calendar import MicrosoftCalendarAdapter
from ai_employee.integrations.registry import ProviderAdapterRegistry
from ai_employee.workers.sync_calendar import CalendarSyncTaskStep

GRAPH_CALENDARS_URL = "https://graph.microsoft.com/v1.0/me/calendars"


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


@dataclass(slots=True)
class _FullSnapshotReader:
    """模拟 Microsoft 每轮从固定 collection 开始的完整目录与事件读取。"""

    calendars: tuple[ProviderCalendar, ...]
    event_generation: int
    initial_calls: list[str] = field(default_factory=list)
    sync_calls: list[tuple[str, str]] = field(default_factory=list)

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """Microsoft provider cursor 必须始终为空，页面显式声明完整快照。"""
        assert cursor is None
        yield CalendarDirectoryPage(
            calendars=self.calendars,
            next_page_token=None,
            next_cursor=None,
            full_snapshot=True,
        )

    async def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """重新发现的日历必须使用有界初始窗口重建缓存。"""
        self.initial_calls.append(calendar_id)
        yield CalendarSyncPage(
            (_event(calendar_id),),
            None,
            f"delta-{calendar_id}-{self.event_generation}",
        )

    async def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """持续可见日历可以继续使用自己的 CalendarView deltaLink。"""
        self.sync_calls.append((calendar_id, cursor))
        yield CalendarSyncPage((), None, f"delta-{calendar_id}-{self.event_generation}")

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """目录集成测试不使用精确事件读取。"""
        del calendar_id, provider_event_id
        return None


@dataclass(slots=True)
class _SequenceMicrosoftOAuth:
    """按顺序返回合成刷新结果，并记录 Worker 实际使用的 refresh token。"""

    responses: list[OAuthTokenSet | Exception]
    refresh_calls: list[str] = field(default_factory=list)

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """消费一个预设结果；异常用于验证授权拒绝和暂态分类。"""
        self.refresh_calls.append(refresh_token)
        if not self.responses:
            raise AssertionError("unexpected Microsoft token refresh")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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


async def _seed_microsoft_directory_connection(
    database_url: str,
) -> tuple[ManagedAsyncSessionMaker, AeadCipher, UUID, UUID]:
    """创建只含 directory placeholder 的 Microsoft calendar.read 连接。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"r" * 32)
    async with sessions.begin() as session:
        user = UserModel(
            email="microsoft-directory@example.test",
            display_name="Microsoft Directory Owner",
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
            provider="microsoft",
            provider_account_id="microsoft-directory-subject",
            provider_tenant_id="microsoft-directory-tenant",
            account_type="work_school",
            account_email=user.email,
            scopes=["Calendars.Read"],
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
                    actual_scopes=["Calendars.Read"],
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


async def _seed_microsoft_worker_connection(
    database_url: str,
) -> tuple[ManagedAsyncSessionMaker, AeadCipher, UUID, UUID]:
    """创建含 AEAD token、日历/邮件能力和 directory cursor 的 Worker 连接。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"w" * 32)
    user_id = UUID("00000000-0000-0000-0000-00000000c101")
    connection_id = UUID("00000000-0000-0000-0000-00000000c102")
    async with sessions.begin() as session:
        session.add(
            UserModel(
                id=user_id,
                email="microsoft-calendar-worker@example.test",
                display_name="Microsoft Calendar Worker",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
        )
        session.add(
            OAuthConnectionModel(
                id=connection_id,
                user_id=user_id,
                provider="microsoft",
                provider_account_id="microsoft-calendar-worker-subject",
                provider_tenant_id="microsoft-calendar-worker-tenant",
                account_type="work_school",
                account_email="microsoft-calendar-worker@example.test",
                scopes=["Calendars.Read", "Mail.Read", "offline_access"],
                status="connected",
                last_error_code=None,
            )
        )
        await session.flush()
        access = cipher.encrypt(
            b"initial-access",
            f"{user_id}:{connection_id}:access_token".encode("ascii"),
        )
        refresh = cipher.encrypt(
            b"initial-refresh",
            f"{user_id}:{connection_id}:refresh_token".encode("ascii"),
        )
        session.add_all(
            (
                EncryptedCredentialModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    credential_kind="access_token",
                    ciphertext=access.ciphertext,
                    nonce=access.nonce,
                    key_version=access.key_version,
                    token_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                ),
                EncryptedCredentialModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    credential_kind="refresh_token",
                    ciphertext=refresh.ciphertext,
                    nonce=refresh.nonce,
                    key_version=refresh.key_version,
                    token_expires_at=None,
                ),
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability="calendar.read",
                    status="enabled",
                    actual_scopes=["Calendars.Read"],
                ),
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability="mail.read",
                    status="enabled",
                    actual_scopes=["Mail.Read"],
                ),
                SyncCursorModel(
                    connection_id=connection_id,
                    resource_kind="calendar",
                    scope_key="directory",
                    cursor=None,
                ),
            )
        )
    return sessions, cipher, user_id, connection_id


def _worker_task(*, user_id: UUID, connection_id: UUID) -> LeasedTask:
    """构造显式 directory owner 的合成 durable task 租约。"""
    return LeasedTask(
        task_id=UUID("00000000-0000-0000-0000-00000000c103"),
        kind="sync_calendar",
        input_payload={"connection_id": str(connection_id), "scope_key": "directory"},
        started_at=datetime(2030, 1, 1, tzinfo=UTC),
        user_id=user_id,
    )


def _directory_payload(*calendar_ids: str) -> dict[str, object]:
    """返回真实 `/me/calendars` 形状的最小完整 collection。"""
    return {
        "value": [
            {
                "id": calendar_id,
                "name": calendar_id,
                "isDefaultCalendar": index == 0,
                "canEdit": True,
                "canShare": False,
                "timeZone": "UTC",
            }
            for index, calendar_id in enumerate(calendar_ids)
        ]
    }


def _empty_delta_payload(calendar_id: str, generation: int) -> dict[str, object]:
    """返回绑定单 calendar path 的空 CalendarView Delta 最终页。"""
    return {
        "value": [],
        "@odata.deltaLink": (
            f"{GRAPH_CALENDARS_URL}/{calendar_id}/calendarView/delta"
            f"?$deltatoken=generation-{generation}"
        ),
    }


@pytest.mark.asyncio
async def test_microsoft_full_snapshot_deletes_absent_cache_and_reappearance_rebuilds(
    database_url: str,
) -> None:
    """空完整快照清缓存但留恢复行，重新出现时必须丢弃旧 delta 并完整重建。"""
    sessions, cipher, user_id, connection_id = await _seed_microsoft_directory_connection(
        database_url
    )

    @asynccontextmanager
    async def stores():
        async with sessions.begin() as session:
            yield SqlAlchemyCalendarSyncRepository(session)

    calendar = ProviderCalendar("m-cal-1", "Primary", "UTC", True, "owner", True)
    first = _FullSnapshotReader((calendar,), event_generation=1)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(microsoft_calendar=first),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    empty = _FullSnapshotReader((), event_generation=2)
    await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(microsoft_calendar=empty),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    reappearing = _FullSnapshotReader((calendar,), event_generation=3)
    result = await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(microsoft_calendar=reappearing),
        cipher,
    ).execute(user_id=user_id, connection_id=connection_id, scope_key="directory")

    async with sessions() as session:
        calendar_ids = tuple(
            (
                await session.scalars(
                    select(ProviderCalendarModel.provider_calendar_id).where(
                        ProviderCalendarModel.connection_id == connection_id
                    )
                )
            ).all()
        )
        event_ids = tuple(
            (
                await session.scalars(
                    select(CalendarEventModel.provider_event_id).where(
                        CalendarEventModel.connection_id == connection_id
                    )
                )
            ).all()
        )
        cursor_rows = {
            scope_key: (cursor, last_success_at)
            for scope_key, cursor, last_success_at in (
                await session.execute(
                    select(
                        SyncCursorModel.scope_key,
                        SyncCursorModel.cursor,
                        SyncCursorModel.last_success_at,
                    ).where(
                        SyncCursorModel.connection_id == connection_id,
                        SyncCursorModel.resource_kind == "calendar",
                    )
                )
            ).all()
        }

    assert first.initial_calls == ["m-cal-1"]
    assert empty.initial_calls == [] and empty.sync_calls == []
    assert reappearing.initial_calls == ["m-cal-1"] and reappearing.sync_calls == []
    assert calendar_ids == ("m-cal-1",)
    assert event_ids == ("same-event-id",)
    assert cursor_rows["directory"][0] is None
    assert cursor_rows["directory"][1] is not None
    assert cursor_rows["m-cal-1"][0] == "delta-m-cal-1-3"
    assert result.next_cursor is None and result.cursor is None
    await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completed_at",
    (
        pytest.param(datetime(2030, 1, 1, tzinfo=UTC), id="equal-revision"),
        pytest.param(datetime(2029, 12, 31, tzinfo=UTC), id="clock-rollback"),
    ),
)
async def test_directory_revision_strictly_advances_when_completion_clock_does_not(
    database_url: str,
    completed_at: datetime,
) -> None:
    """等值或回拨完成时间只改变本地 CAS revision，不得改写真实完成 cutoff。"""
    sessions, _, user_id, connection_id = await _seed_microsoft_directory_connection(database_url)
    observed_revision = datetime(2030, 1, 1, tzinfo=UTC)
    async with sessions.begin() as session:
        cursor = await session.scalar(
            select(SyncCursorModel).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "directory",
            )
        )
        assert cursor is not None
        cursor.last_success_at = observed_revision
        cursor.last_attempt_at = observed_revision

    async with sessions.begin() as session:
        await SqlAlchemyCalendarSyncRepository(session).mark_directory_success(
            user_id=user_id,
            connection_id=connection_id,
            calendars=(),
            full_snapshot=True,
            expected_cursor=None,
            expected_revision=observed_revision,
            next_cursor=None,
            completed_at=completed_at,
        )

    async with sessions() as session:
        persisted_revision, persisted_attempt = (
            await session.execute(
                select(
                    SyncCursorModel.last_success_at,
                    SyncCursorModel.last_attempt_at,
                ).where(
                    SyncCursorModel.connection_id == connection_id,
                    SyncCursorModel.resource_kind == "calendar",
                    SyncCursorModel.scope_key == "directory",
                )
            )
        ).one()
        audit_metadata = await session.scalar(
            select(AuditEventModel.event_metadata)
            .where(
                AuditEventModel.user_id == user_id,
                AuditEventModel.event_type == "source.calendar.directory_discovered",
            )
            .order_by(AuditEventModel.id.desc())
        )
    await sessions.dispose()

    assert persisted_revision == observed_revision + timedelta(microseconds=1)
    assert persisted_attempt == completed_at
    assert audit_metadata is not None
    assert audit_metadata["cutoff"] == completed_at.isoformat()


@pytest.mark.asyncio
async def test_directory_revision_upper_bound_fails_closed_without_partial_facts(
    database_url: str,
) -> None:
    """Python datetime 上界不能递增时返回固定冲突，且不得写入审计或部分目录事实。"""
    sessions, _, user_id, connection_id = await _seed_microsoft_directory_connection(database_url)
    maximum_revision = datetime.max.replace(tzinfo=UTC)
    async with sessions.begin() as session:
        cursor = await session.scalar(
            select(SyncCursorModel).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "directory",
            )
        )
        assert cursor is not None
        cursor.last_success_at = maximum_revision

    # 从 ORM 重新读取上界，避免 asyncpg 对 PostgreSQL infinity/时区表示的驱动差异影响
    # 测试本身；仓储随后必须在这个真实持久值上安全处理 ``+1 微秒`` 溢出。
    async with sessions.begin() as session:
        state = await SqlAlchemyCalendarSyncRepository(session).get_state(
            user_id=user_id,
            connection_id=connection_id,
            scope_key="directory",
        )
    assert state is not None and state.revision is not None
    maximum_revision = state.revision

    error_code: str | None = None
    try:
        async with sessions.begin() as session:
            await SqlAlchemyCalendarSyncRepository(session).mark_directory_success(
                user_id=user_id,
                connection_id=connection_id,
                calendars=(ProviderCalendar("m-cal-1", "Unsafe", "UTC", True, "owner", True),),
                full_snapshot=True,
                expected_cursor=None,
                expected_revision=maximum_revision,
                next_cursor=None,
                completed_at=maximum_revision,
            )
    except StateConflictError as error:
        error_code = error.error_code

    async with sessions() as session:
        persisted_revision = await session.scalar(
            select(SyncCursorModel.last_success_at).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "directory",
            )
        )
        calendar_ids = tuple(
            (
                await session.scalars(
                    select(ProviderCalendarModel.provider_calendar_id).where(
                        ProviderCalendarModel.connection_id == connection_id
                    )
                )
            ).all()
        )
        audit_ids = tuple(
            (
                await session.scalars(
                    select(AuditEventModel.id).where(AuditEventModel.user_id == user_id)
                )
            ).all()
        )
    await sessions.dispose()

    assert (
        error_code,
        persisted_revision,
        calendar_ids,
        audit_ids,
    ) == (
        "calendar_directory_revision_exhausted",
        maximum_revision,
        (),
        (),
    )


@pytest.mark.asyncio
async def test_microsoft_directory_revision_cas_rejects_equal_time_stale_snapshot(
    database_url: str,
) -> None:
    """同一 revision 的第二个完整快照不能删除先提交的目录与事件事实。"""
    sessions, cipher, user_id, connection_id = await _seed_microsoft_directory_connection(
        database_url
    )
    observed_revision = datetime(2030, 1, 1, tzinfo=UTC)
    committed_calendar = ProviderCalendar(
        "m-cal-1", "Committed snapshot", "UTC", True, "owner", True
    )
    stale_calendar = ProviderCalendar("m-cal-2", "Stale snapshot", "UTC", False, "reader", False)
    async with sessions.begin() as session:
        cursor = await session.scalar(
            select(SyncCursorModel).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "directory",
            )
        )
        assert cursor is not None
        cursor.last_success_at = observed_revision

    async with sessions.begin() as session:
        first_state = await SqlAlchemyCalendarSyncRepository(session).get_state(
            user_id=user_id,
            connection_id=connection_id,
            scope_key="directory",
        )
    async with sessions.begin() as session:
        second_state = await SqlAlchemyCalendarSyncRepository(session).get_state(
            user_id=user_id,
            connection_id=connection_id,
            scope_key="directory",
        )
    assert first_state is not None and second_state is not None
    assert first_state.cursor is None and second_state.cursor is None
    assert first_state.revision == observed_revision
    assert second_state.revision == observed_revision

    async with sessions.begin() as session:
        await SqlAlchemyCalendarSyncRepository(session).mark_directory_success(
            user_id=user_id,
            connection_id=connection_id,
            calendars=(committed_calendar,),
            full_snapshot=True,
            expected_cursor=None,
            expected_revision=first_state.revision,
            next_cursor=None,
            completed_at=observed_revision,
        )

    protected_event = _event(committed_calendar.calendar_id)
    async with sessions.begin() as session:
        await SqlAlchemyCalendarSyncRepository(session).upsert_event(
            user_id=user_id,
            connection_id=connection_id,
            event=protected_event,
            encrypted_description=cipher.encrypt(
                protected_event.description.encode("utf-8"),
                b"synthetic-directory-cas-description",
            ),
            encrypted_location=cipher.encrypt(
                protected_event.location.encode("utf-8"),
                b"synthetic-directory-cas-location",
            ),
        )

    conflict_error_code: str | None = None
    try:
        async with sessions.begin() as session:
            await SqlAlchemyCalendarSyncRepository(session).mark_directory_success(
                user_id=user_id,
                connection_id=connection_id,
                calendars=(stale_calendar,),
                full_snapshot=True,
                expected_cursor=None,
                expected_revision=second_state.revision,
                next_cursor=None,
                completed_at=observed_revision + timedelta(days=1),
            )
    except TransientProviderError as error:
        conflict_error_code = error.error_code

    async with sessions() as session:
        persisted_revision = await session.scalar(
            select(SyncCursorModel.last_success_at).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "directory",
            )
        )
        calendar_facts = tuple(
            (
                await session.execute(
                    select(
                        ProviderCalendarModel.provider_calendar_id,
                        ProviderCalendarModel.name,
                    )
                    .where(ProviderCalendarModel.connection_id == connection_id)
                    .order_by(ProviderCalendarModel.provider_calendar_id)
                )
            ).all()
        )
        event_facts = tuple(
            (
                await session.execute(
                    select(
                        CalendarEventModel.calendar_id,
                        CalendarEventModel.provider_event_id,
                    )
                    .where(CalendarEventModel.connection_id == connection_id)
                    .order_by(CalendarEventModel.calendar_id, CalendarEventModel.provider_event_id)
                )
            ).all()
        )
        audit_ids = tuple(
            (
                await session.scalars(
                    select(AuditEventModel.id).where(AuditEventModel.user_id == user_id)
                )
            ).all()
        )
    await sessions.dispose()

    assert (
        conflict_error_code,
        persisted_revision,
        calendar_facts,
        event_facts,
        len(audit_ids),
    ) == (
        "calendar_directory_revision_conflict",
        observed_revision + timedelta(microseconds=1),
        (("m-cal-1", "Committed snapshot"),),
        (("m-cal-1", "same-event-id"),),
        1,
    )


@pytest.mark.asyncio
@respx.mock
async def test_malformed_microsoft_event_fails_before_persistence_and_cursor_advance(
    database_url: str,
) -> None:
    """超出共享列的 Graph 标量必须在 adapter 永久失败，不能下沉为数据库 DataError。"""
    sessions, cipher, user_id, connection_id = await _seed_microsoft_directory_connection(
        database_url
    )
    old_cursor = (
        "https://graph.microsoft.com/v1.0/me/calendars/m-cal-1/calendarView/delta?$deltatoken=old"
    )
    next_cursor = (
        "https://graph.microsoft.com/v1.0/me/calendars/m-cal-1/calendarView/delta?$deltatoken=new"
    )
    async with sessions.begin() as session:
        session.add_all(
            (
                ProviderCalendarModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    provider_calendar_id="m-cal-1",
                    name="Primary",
                    timezone="UTC",
                    is_primary=True,
                    access_role="owner",
                    can_write=True,
                    provider_url=None,
                ),
                SyncCursorModel(
                    connection_id=connection_id,
                    resource_kind="calendar",
                    scope_key="m-cal-1",
                    cursor=old_cursor,
                ),
            )
        )
    respx.get(old_cursor).respond(
        200,
        json={
            "value": [
                {
                    "id": "event-overlong",
                    "subject": "Synthetic",
                    "body": {"contentType": "text", "content": ""},
                    "location": {"displayName": ""},
                    "start": {"dateTime": "2030-01-02T09:00:00", "timeZone": "UTC"},
                    "end": {"dateTime": "2030-01-02T10:00:00", "timeZone": "UTC"},
                    "isAllDay": False,
                    "showAs": "x" * 33,
                    "type": "singleInstance",
                    "webLink": "https://outlook.example.test/event-overlong",
                }
            ],
            "@odata.deltaLink": next_cursor,
        },
    )

    @asynccontextmanager
    async def stores():
        async with sessions.begin() as session:
            yield SqlAlchemyCalendarSyncRepository(session)

    adapter = MicrosoftCalendarAdapter(access_token="synthetic", user_timezone="UTC")
    with pytest.raises(PermanentProviderError) as raised:
        await SyncCalendarUseCase(
            stores,
            ProviderAdapterRegistry(microsoft_calendar=adapter),
            cipher,
        ).execute(user_id=user_id, connection_id=connection_id, scope_key="m-cal-1")

    async with sessions() as session:
        persisted_cursor = await session.scalar(
            select(SyncCursorModel.cursor).where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "m-cal-1",
            )
        )
        event_count = len(
            (
                await session.scalars(
                    select(CalendarEventModel.id).where(
                        CalendarEventModel.connection_id == connection_id,
                        CalendarEventModel.calendar_id == "m-cal-1",
                    )
                )
            ).all()
        )
    assert raised.value.error_code == "microsoft_calendar_invalid_response"
    assert persisted_cursor == old_cursor
    assert event_count == 0
    await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_worker_uses_rotated_refresh_token_for_later_401_and_preserves_on_omission(
    database_url: str,
) -> None:
    """独立资源链必须使用最新 refresh token，后续响应省略轮换值时保留既有密文。"""
    sessions, cipher, user_id, connection_id = await _seed_microsoft_worker_connection(database_url)
    oauth = _SequenceMicrosoftOAuth(
        responses=[
            OAuthTokenSet(
                access_token="refreshed-access-1",
                refresh_token="rotated-refresh-1",
                expires_in=3600,
                granted_scopes=frozenset({"Calendars.Read"}),
            ),
            OAuthTokenSet(
                access_token="refreshed-access-2",
                refresh_token=None,
                expires_in=3600,
                granted_scopes=frozenset({"Calendars.Read"}),
            ),
        ]
    )
    directory = respx.get(GRAPH_CALENDARS_URL).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(
                200,
                json=_directory_payload("calendar-primary", "calendar-readonly"),
            ),
        ]
    )
    primary_url = f"{GRAPH_CALENDARS_URL}/calendar-primary/calendarView/delta"
    primary = respx.get(primary_url).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=_empty_delta_payload("calendar-primary", 1)),
        ]
    )
    readonly_url = f"{GRAPH_CALENDARS_URL}/calendar-readonly/calendarView/delta"
    readonly = respx.get(readonly_url).respond(
        200,
        json=_empty_delta_payload("calendar-readonly", 1),
    )
    metrics = create_metrics()

    await CalendarSyncTaskStep(
        session_factory=sessions,
        cipher=cipher,
        oauth=FakeGoogleOAuthClient(),
        microsoft_oauth=oauth,  # type: ignore[arg-type]
        metrics=metrics,
    ).execute(_worker_task(user_id=user_id, connection_id=connection_id))

    async with sessions() as session:
        credentials = (
            await session.scalars(
                select(EncryptedCredentialModel).where(
                    EncryptedCredentialModel.user_id == user_id,
                    EncryptedCredentialModel.connection_id == connection_id,
                )
            )
        ).all()
    by_kind = {row.credential_kind: row for row in credentials}
    access_row = by_kind["access_token"]
    refresh_row = by_kind["refresh_token"]
    access = cipher.decrypt(
        EncryptedValue(access_row.ciphertext, access_row.nonce, access_row.key_version),
        f"{user_id}:{connection_id}:access_token".encode("ascii"),
    ).decode("utf-8")
    refresh = cipher.decrypt(
        EncryptedValue(refresh_row.ciphertext, refresh_row.nonce, refresh_row.key_version),
        f"{user_id}:{connection_id}:refresh_token".encode("ascii"),
    ).decode("utf-8")

    assert oauth.refresh_calls == ["initial-refresh", "rotated-refresh-1"]
    assert access == "refreshed-access-2"
    assert refresh == "rotated-refresh-1"
    assert [call.request.headers["Authorization"] for call in directory.calls] == [
        "Bearer initial-access",
        "Bearer refreshed-access-1",
    ]
    assert [call.request.headers["Authorization"] for call in primary.calls] == [
        "Bearer refreshed-access-1",
        "Bearer refreshed-access-2",
    ]
    assert readonly.calls[0].request.headers["Authorization"] == "Bearer refreshed-access-2"
    assert 'provider="microsoft",resource="calendar"' in metrics.render().body.decode("utf-8")
    await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_worker_refresh_rejection_marks_connection_expired(
    database_url: str,
) -> None:
    """OAuth refresh 明确拒绝时必须停止 Graph 链并持久化连接级重新授权状态。"""
    sessions, cipher, user_id, connection_id = await _seed_microsoft_worker_connection(database_url)
    oauth = _SequenceMicrosoftOAuth(
        responses=[
            PermanentProviderError(
                error_code="microsoft_oauth_rejected",
                message="Microsoft OAuth request was rejected",
            )
        ]
    )
    directory = respx.get(GRAPH_CALENDARS_URL).respond(401)

    with pytest.raises(UserActionRequiredError) as raised:
        await CalendarSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=FakeGoogleOAuthClient(),
            microsoft_oauth=oauth,  # type: ignore[arg-type]
            metrics=create_metrics(),
        ).execute(_worker_task(user_id=user_id, connection_id=connection_id))

    async with sessions() as session:
        connection = await session.get(OAuthConnectionModel, connection_id)
    assert raised.value.error_code == "microsoft_reauthorization_required"
    assert oauth.refresh_calls == ["initial-refresh"]
    assert directory.call_count == 1
    assert connection is not None and connection.status == "degraded"
    assert connection.last_error_code == "oauth_revoked"
    await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_worker_second_resource_401_marks_connection_expired(
    database_url: str,
) -> None:
    """资源刷新后再次 401 必须连接级过期，不能盲目发起第二次 refresh。"""
    sessions, cipher, user_id, connection_id = await _seed_microsoft_worker_connection(database_url)
    oauth = _SequenceMicrosoftOAuth(
        responses=[
            OAuthTokenSet(
                access_token="refreshed-access",
                refresh_token=None,
                expires_in=3600,
                granted_scopes=frozenset({"Calendars.Read"}),
            )
        ]
    )
    respx.get(GRAPH_CALENDARS_URL).respond(
        200,
        json=_directory_payload("calendar-primary"),
    )
    primary_url = f"{GRAPH_CALENDARS_URL}/calendar-primary/calendarView/delta"
    primary = respx.get(primary_url).mock(side_effect=[httpx.Response(401), httpx.Response(401)])

    with pytest.raises(UserActionRequiredError) as raised:
        await CalendarSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=FakeGoogleOAuthClient(),
            microsoft_oauth=oauth,  # type: ignore[arg-type]
            metrics=create_metrics(),
        ).execute(_worker_task(user_id=user_id, connection_id=connection_id))

    async with sessions() as session:
        connection = await session.get(OAuthConnectionModel, connection_id)
    assert raised.value.error_code == "microsoft_reauthorization_required"
    assert oauth.refresh_calls == ["initial-refresh"]
    assert primary.call_count == 2
    assert connection is not None and connection.status == "degraded"
    assert connection.last_error_code == "oauth_revoked"
    await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_worker_calendar_403_stops_remaining_reads_and_isolates_capability(
    database_url: str,
) -> None:
    """任一 CalendarView 403 立即停止连接，仅 calendar.read 进入 action_required。"""
    sessions, cipher, user_id, connection_id = await _seed_microsoft_worker_connection(database_url)
    oauth = _SequenceMicrosoftOAuth(responses=[])
    respx.get(GRAPH_CALENDARS_URL).respond(
        200,
        json=_directory_payload("calendar-primary", "calendar-readonly"),
    )
    primary_url = f"{GRAPH_CALENDARS_URL}/calendar-primary/calendarView/delta"
    primary = respx.get(primary_url).respond(
        403,
        json={"error": {"code": "ErrorAccessDenied"}},
    )
    readonly_url = f"{GRAPH_CALENDARS_URL}/calendar-readonly/calendarView/delta"
    readonly = respx.get(readonly_url).respond(
        200,
        json=_empty_delta_payload("calendar-readonly", 1),
    )

    with pytest.raises(UserActionRequiredError) as raised:
        await CalendarSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=FakeGoogleOAuthClient(),
            microsoft_oauth=oauth,  # type: ignore[arg-type]
            metrics=create_metrics(),
        ).execute(_worker_task(user_id=user_id, connection_id=connection_id))

    async with sessions() as session:
        connection = await session.get(OAuthConnectionModel, connection_id)
        capabilities = {
            capability.capability: capability
            for capability in (
                await session.scalars(
                    select(ConnectionCapabilityModel).where(
                        ConnectionCapabilityModel.user_id == user_id,
                        ConnectionCapabilityModel.connection_id == connection_id,
                    )
                )
            ).all()
        }
    assert raised.value.error_code == "microsoft_calendar_permission_required"
    assert primary.call_count == 1
    assert readonly.call_count == 0
    assert oauth.refresh_calls == []
    assert connection is not None and connection.status == "connected"
    assert capabilities["calendar.read"].status == "action_required"
    assert capabilities["calendar.read"].last_error_code == "microsoft_calendar_permission_required"
    assert capabilities["mail.read"].status == "enabled"
    assert capabilities["mail.read"].last_error_code is None
    await sessions.dispose()


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
    scheduled = await SqlAlchemyEnabledSyncScopeReader(sessions).enabled_scopes()
    assert [
        (scope.provider, scope.resource_kind, scope.scope_key)
        for scope in scheduled
        if scope.connection_id == owner_connection_id
        and scope.provider == "microsoft"
        and scope.resource_kind == "calendar"
    ] == [("microsoft", "calendar", "directory")]
    await sessions.dispose()
