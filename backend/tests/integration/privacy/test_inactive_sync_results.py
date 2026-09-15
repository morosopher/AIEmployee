"""同步网络结束后提交真实删除屏障，覆盖页面、目录、游标及失败状态的独立事务。"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from ai_employee.application.calendar_event_aad import calendar_event_field_aad_v2
from ai_employee.application.ports.calendar import (
    CalendarCursorExpiredError,
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarSyncPage,
    ProviderCalendar,
)
from ai_employee.application.ports.mail import (
    MailCursorExpiredError,
    MailRemoval,
    MailScope,
    MailSyncPage,
)
from ai_employee.application.use_cases.sync_calendar import CalendarConnectionNotFoundError
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.errors import DomainError, UserActionRequiredError
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    EmailMessageModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyMailSyncRepository
from ai_employee.integrations.google.fake import FakeGoogleOAuthClient
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter
from ai_employee.workers.sync_calendar import CalendarSyncTaskStep
from ai_employee.workers.sync_mail import MailSyncTaskStep
from tests.integration.microsoft.test_calendar_sync import (
    _event,
    _seed_microsoft_worker_connection,
)
from tests.integration.microsoft.test_mail_sync import _message, _upsert_repository_message
from tests.integration.privacy.inactive_barrier import (
    assert_facts_unchanged,
    commit_deletion_barrier,
    database_facts,
)

SCOPE = "synthetic-scope"
NOW = datetime(2030, 1, 8, tzinfo=UTC)


@dataclass
class _ReadBoundary:
    """仅替换供应商分页 I/O；第一次响应由事件精确控制，回退读取不再次阻塞。"""

    resource: str
    mode: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    requests: int = 0

    async def response(self) -> None:
        """在真实 Worker 完成前置数据库读取后返回规范页面或稳定供应商错误。"""
        self.requests += 1
        if self.requests > 1:
            return
        self.entered.set()
        await self.release.wait()
        if self.mode == "cursor":
            if self.resource == "mail":
                raise MailCursorExpiredError("microsoft", SCOPE)
            raise CalendarCursorExpiredError("microsoft", SCOPE)
        if self.mode in {"expired", "capability"}:
            code = (
                "microsoft_reauthorization_required"
                if self.mode == "expired"
                else f"microsoft_{self.resource}_permission_required"
            )
            raise UserActionRequiredError(error_code=code, message="Synthetic provider rejection")


@dataclass
class _MailReader:
    """返回真实领域消息/墓碑，持久化、授权和失败事务均由生产实现执行。"""

    boundary: _ReadBoundary

    async def list_sync_scopes(self) -> tuple[MailScope, ...]:
        """在 mailbox 目录 HTTP 窗口暂停，然后返回新 scope。"""
        await self.boundary.response()
        return (MailScope(SCOPE, "Synthetic scope"),)

    async def initial_pages(
        self, scope_key: str, *, since: datetime
    ) -> AsyncIterator[MailSyncPage]:
        """首次同步或失效回退使用与增量相同的领域页面。"""
        del since
        async for page in self.sync_pages(scope_key, ""):
            yield page

    async def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """分别覆盖消息、仅墓碑、空页，不能只靠首条消息的守卫挡住所有测试。"""
        del cursor
        assert scope_key == SCOPE
        await self.boundary.response()
        yield MailSyncPage(
            ()
            if self.boundary.mode in {"remove", "empty"}
            else (_message("new", scope_key=SCOPE),),
            None,
            "synthetic-next",
            removals=(MailRemoval("old", SCOPE),) if self.boundary.mode == "remove" else (),
        )


@dataclass
class _CalendarReader:
    """只替代 Calendar 网络；目录/事件密文、游标、审计由真实仓储提交。"""

    boundary: _ReadBoundary

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """完整目录允许微软空 cursor，但保持已知目标身份。"""
        assert cursor is None
        await self.boundary.response()
        yield CalendarDirectoryPage(
            (ProviderCalendar(SCOPE, "Synthetic calendar", "UTC", True, "owner", True),),
            None,
            None,
            True,
        )

    async def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """供目录后首次同步及 cursor 失效回退使用。"""
        async for page in self.sync_pages(calendar_id, ""):
            yield page

    async def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """成功和空页均需经过普通提交 get_state 的同事务屏障。"""
        del cursor
        assert calendar_id == SCOPE
        await self.boundary.response()
        event = _event(SCOPE)
        if self.boundary.mode == "remove":
            # 供应商墓碑不提供旧标题、时间或 ETag；数据库须更新同一个对象为取消状态。
            event = replace(
                event,
                title="",
                description="",
                location="",
                starts_at=None,
                ends_at=None,
                etag=None,
                status="cancelled",
                can_edit=False,
            )
        yield CalendarSyncPage(
            () if self.boundary.mode == "empty" else (event,), None, "synthetic-next"
        )

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """本组只测试同步，不构造可信精确读取。"""
        raise AssertionError("unexpected exact event read")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource,mode",
    (
        ("mail", "sync"),
        ("mail", "remove"),
        ("mail", "empty"),
        ("mail", "directory"),
        ("mail", "cursor"),
        ("mail", "expired"),
        ("mail", "capability"),
        ("calendar", "sync"),
        ("calendar", "remove"),
        ("calendar", "empty"),
        ("calendar", "directory"),
        ("calendar", "cursor"),
        ("calendar", "expired"),
        ("calendar", "capability"),
    ),
)
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_sync_response_cannot_commit_after_privacy_barrier(
    database_url: str, resource: str, mode: str, inactive: bool
) -> None:
    """先完成真实 Worker 认证/连接读取，再在供应商响应窗口提交真实删除 barrier。

    所有 ordinary 表和 winner 精确保持；active 对照证明正常页、游标回退及权限错误
    仍产生原有事实。屏障后相同任务再次交付也不能新增业务记录。
    """
    sessions, cipher, user_id, connection_id = await _seed_microsoft_worker_connection(database_url)
    boundary = _ReadBoundary(resource, mode)
    try:
        async with sessions.begin() as session:
            session.add(
                ProviderCalendarModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    provider_calendar_id=SCOPE,
                    name="Synthetic calendar",
                    timezone="UTC",
                    is_primary=True,
                    access_role="owner",
                    can_write=True,
                )
            )
            session.add_all(
                (
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="mail",
                        scope_key="mailbox",
                        cursor=None,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind=resource,
                        scope_key=SCOPE,
                        cursor="synthetic-old",
                    ),
                )
            )
        if resource == "mail":
            await _upsert_repository_message(
                sessions,
                cipher,
                user_id=user_id,
                connection_id=connection_id,
                message=_message("old", scope_key=SCOPE),
            )
        elif mode == "remove":
            event = _event(SCOPE)
            async with sessions.begin() as session:
                await SqlAlchemyCalendarSyncRepository(session).upsert_event(
                    user_id=user_id,
                    connection_id=connection_id,
                    event=event,
                    encrypted_description=cipher.encrypt(
                        event.description.encode(),
                        calendar_event_field_aad_v2(
                            user_id=str(user_id),
                            connection_id=str(connection_id),
                            calendar_id=SCOPE,
                            provider_event_id=event.event_id,
                            field="description",
                        ),
                    ),
                    encrypted_location=cipher.encrypt(
                        event.location.encode(),
                        calendar_event_field_aad_v2(
                            user_id=str(user_id),
                            connection_id=str(connection_id),
                            calendar_id=SCOPE,
                            provider_event_id=event.event_id,
                            field="location",
                        ),
                    ),
                )
        oauth = MicrosoftOAuthAdapter(
            "synthetic-client", "synthetic-secret", "https://app.example.test/callback"
        )
        step = (
            MailSyncTaskStep(
                session_factory=sessions,
                cipher=cipher,
                oauth=FakeGoogleOAuthClient(),
                microsoft_oauth=oauth,
                microsoft_reader=_MailReader(boundary),
            )
            if resource == "mail"
            else CalendarSyncTaskStep(
                session_factory=sessions,
                cipher=cipher,
                oauth=FakeGoogleOAuthClient(),
                microsoft_oauth=oauth,
                microsoft_reader=_CalendarReader(boundary),
            )
        )
        scope = ("mailbox" if resource == "mail" else "directory") if mode == "directory" else SCOPE
        task = LeasedTask(
            uuid4(),
            f"sync_{resource}",
            {"connection_id": str(connection_id), "scope_key": scope},
            NOW,
            user_id=user_id,
        )
        pending = asyncio.create_task(step.execute(task))
        try:
            await asyncio.wait_for(boundary.entered.wait(), timeout=5)
            if inactive:
                await commit_deletion_barrier(sessions, user_id=user_id)
            before = await database_facts(sessions)
            boundary.release.set()
            result = (
                await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=5)
            )[0]
            if inactive:
                assert_facts_unchanged(before, await database_facts(sessions))
                assert isinstance(result, (DomainError, CalendarConnectionNotFoundError))
                await asyncio.gather(step.execute(task), return_exceptions=True)
                assert_facts_unchanged(before, await database_facts(sessions))
            else:
                assert (isinstance(result, UserActionRequiredError)) == (
                    mode in {"expired", "capability"}
                )
                if mode not in {"expired", "capability"}:
                    assert result is None
                async with sessions() as session:
                    if mode == "expired":
                        assert (
                            await session.scalar(select(OAuthConnectionModel.status)) == "degraded"
                        )
                    elif mode == "capability":
                        status = await session.scalar(
                            select(ConnectionCapabilityModel.status).where(
                                ConnectionCapabilityModel.capability == f"{resource}.read"
                            )
                        )
                        assert status == "action_required"
                    else:
                        cursor = await session.scalar(
                            select(SyncCursorModel.cursor).where(
                                SyncCursorModel.resource_kind == resource,
                                SyncCursorModel.scope_key == SCOPE,
                            )
                        )
                        assert cursor == "synthetic-next"
                        if resource == "mail":
                            ids = set(
                                (
                                    await session.scalars(
                                        select(EmailMessageModel.provider_message_id)
                                    )
                                ).all()
                            )
                            assert ids == (
                                set()
                                if mode == "remove"
                                else {"old"}
                                if mode == "empty"
                                else {"old", "new"}
                            )
                        elif mode != "empty":
                            assert (
                                await session.scalar(
                                    select(CalendarEventModel.description_aad_version)
                                )
                                == 2
                            )
                            event = await session.scalar(select(CalendarEventModel))
                            assert event is not None
                            assert event.status == (
                                "cancelled" if mode == "remove" else "confirmed"
                            )
                            if mode == "remove":
                                assert event.starts_at is None and event.ends_at is None
                                assert event.etag is None and event.can_edit is False
        finally:
            boundary.release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    finally:
        await sessions.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ("mail", "calendar"))
async def test_sync_state_rejects_inactive_before_new_placeholder(
    database_url: str, resource: str
) -> None:
    """排队任务在屏障后读取状态不得创建日历占位游标，也不得得到可用同步授权。"""
    sessions, _, user_id, connection_id = await _seed_microsoft_worker_connection(database_url)
    try:
        async with sessions.begin() as session:
            session.add(
                ProviderCalendarModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    provider_calendar_id=SCOPE,
                    name="Synthetic calendar",
                    timezone="UTC",
                    is_primary=True,
                    access_role="owner",
                    can_write=True,
                )
            )
        await commit_deletion_barrier(sessions, user_id=user_id)
        before = await database_facts(sessions)
        async with sessions.begin() as session:
            repo = (
                SqlAlchemyMailSyncRepository(session)
                if resource == "mail"
                else SqlAlchemyCalendarSyncRepository(session)
            )
            state = await repo.get_state(
                user_id=user_id,
                connection_id=connection_id,
                scope_key=SCOPE,
            )
        assert_facts_unchanged(before, await database_facts(sessions))
        assert state is None
    finally:
        await sessions.engine.dispose()
