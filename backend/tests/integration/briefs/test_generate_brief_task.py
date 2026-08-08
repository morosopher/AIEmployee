"""验证每日简报 Worker 的来源选择与原子持久化。"""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.errors import TransientProviderError
from ai_employee.infrastructure.db.models.briefs import (
    DailyBriefItemModel,
    DailyBriefModel,
    LLMInvocationModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    EmailAnalysisModel,
    EmailMessageModel,
    EmailThreadModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.integrations.llm.fake import FakeModelGateway
from ai_employee.workers.generate_brief import (
    GenerateBriefTaskStep,
    build_generate_brief_task_step,
)


@pytest.mark.asyncio
async def test_generate_brief_selects_local_day_events_after_stale_mail_sync_fails(
    database_url: str,
) -> None:
    """过期规范邮件游标同步失败时，仍为本地日期重叠的日程生成部分简报与审计。"""
    sessions = build_session_factory(database_url)
    user_id, task_id, connection_id = uuid4(), uuid4(), uuid4()
    stale_mail_success = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="brief-owner@example.test",
                    display_name="Brief Owner",
                    password_hash=None,
                    timezone="America/Los_Angeles",
                    locale="en-US",
                    brief_time=time(8),
                    is_active=True,
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id="brief-subject",
                    account_email="brief-owner@example.test",
                    scopes=[],
                    status="connected",
                    last_error_code=None,
                )
            )
            await session.flush()
            session.add_all(
                [
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability="mail.read",
                        status="enabled",
                        actual_scopes=[],
                        last_verified_at=None,
                        last_error_code=None,
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability="calendar.read",
                        status="enabled",
                        actual_scopes=[],
                        last_verified_at=None,
                        last_error_code=None,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="mail",
                        scope_key="mailbox",
                        cursor="mail-cursor",
                        last_success_at=stale_mail_success,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        scope_key="primary",
                        cursor="calendar-cursor",
                        last_success_at=datetime(2026, 7, 31, 6, 55, tzinfo=UTC),
                    ),
                    TaskRunModel(
                        id=task_id,
                        user_id=user_id,
                        kind="daily_brief",
                        status="running",
                        idempotency_key="brief-task",
                        input_payload={"local_date": "2026-07-30", "schedule_kind": "manual"},
                    ),
                ]
            )
            thread = EmailThreadModel(
                user_id=user_id,
                connection_id=connection_id,
                provider_thread_id="thread",
                subject="Status update",
                participants=[],
                latest_message_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
                provider_url="https://example.test/thread",
            )
            session.add(thread)
            await session.flush()
            session.add(
                EmailMessageModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    thread_id=thread.id,
                    provider_message_id="message",
                    received_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
                    sender={"email": "sender@example.test"},
                    recipients=[],
                    subject="Status update",
                    snippet="",
                    body_ciphertext=b"x",
                    body_nonce=b"x" * 12,
                    body_key_version=1,
                    labels=[],
                    headers={},
                    provider_url="https://example.test/message",
                )
            )
            session.add_all(
                [
                    _event(
                        user_id,
                        connection_id,
                        "today-event",
                        datetime(2026, 7, 31, 6, 30, tzinfo=UTC),
                        datetime(2026, 7, 31, 7, 30, tzinfo=UTC),
                    ),
                    _event(
                        user_id,
                        connection_id,
                        "tomorrow-event",
                        datetime(2026, 7, 31, 7, 30, tzinfo=UTC),
                        datetime(2026, 7, 31, 8, 30, tzinfo=UTC),
                    ),
                ]
            )

        sync_attempts: list[tuple[str, UUID, UUID, str]] = []

        async def sync_source(
            resource_kind: str,
            callback_connection_id: UUID,
            callback_user_id: UUID,
            scope_key: str,
        ) -> None:
            """记录精确 scope 并模拟邮件失败，证明单源故障不会阻断可用日历。"""
            sync_attempts.append(
                (resource_kind, callback_connection_id, callback_user_id, scope_key)
            )
            if resource_kind == "mail":
                raise TransientProviderError(
                    error_code="synthetic_sync_failure",
                    message="synthetic sync failure",
                )

        step = GenerateBriefTaskStep(
            sessions,
            model_gateway=FakeModelGateway(),
            sync_source=sync_source,
            now=lambda: datetime(2026, 7, 31, 7, tzinfo=UTC),
            checkpoint_database_url=database_url,
        )
        await step.execute(
            LeasedTask(
                task_id=task_id,
                user_id=user_id,
                kind="daily_brief",
                input_payload={"local_date": "2026-07-30", "schedule_kind": "manual"},
                started_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )

        async with sessions() as session:
            brief = await session.scalar(select(DailyBriefModel))
            assert brief is not None and brief.version == 1 and brief.completeness == "partial"
            assert brief.warnings == [
                "missing:mail;last_success:2026-07-31T06:00:00+00:00;repair:retry",
            ]
            items = tuple((await session.scalars(select(DailyBriefItemModel))).all())
            assert len(items) == 2
            assert any(item.source_refs[0]["source_type"] == "calendar_event" for item in items)
            assert (
                await session.scalar(
                    select(AuditEventModel).where(AuditEventModel.event_type == "brief.ready")
                )
                is not None
            )
            task = await session.get(TaskRunModel, task_id)
            assert (
                task is not None
                and task.result_payload is not None
                and task.result_payload["completeness"] == "partial"
            )
            invocation = await session.scalar(select(LLMInvocationModel))
            assert invocation is not None and invocation.output_schema == "EmailJudgement"
            assert (
                await session.scalar(
                    select(EmailAnalysisModel).where(EmailAnalysisModel.thread_id == thread.id)
                )
                is not None
            )
        assert sync_attempts == [
            ("mail", connection_id, user_id, "mailbox"),
            ("calendar", connection_id, user_id, "directory"),
        ]
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_brief_refresh_dispatches_exact_provider_neutral_sync_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """简报刷新必须选择规范 Worker，并把调用方给出的精确 scope 原样写入任务载荷。"""
    captured: list[LeasedTask] = []

    class CapturingStep:
        """记录简报内部派发的合成租约任务，不访问数据库或外部供应商。"""

        async def execute(self, task: LeasedTask) -> None:
            """保存精确任务种类与载荷，供测试断言 canonical 路由。"""
            captured.append(task)

    from ai_employee.workers import sync_calendar, sync_mail

    monkeypatch.setattr(sync_mail, "build_mail_sync_task_step", lambda **_kwargs: CapturingStep())
    monkeypatch.setattr(
        sync_calendar,
        "build_calendar_sync_task_step",
        lambda **_kwargs: CapturingStep(),
    )
    step = build_generate_brief_task_step(
        session_factory=object(),  # type: ignore[arg-type]
        settings=Settings(app_test_mode=True),
    )
    sync_source = step._sync_source
    assert sync_source is not None
    user_id, connection_id = uuid4(), uuid4()

    await sync_source("mail", connection_id, user_id, "mailbox")
    await sync_source("calendar", connection_id, user_id, "team-calendar")

    assert len(captured) == 2
    assert captured[0].kind == "sync_mail"
    assert captured[0].input_payload == {
        "connection_id": str(connection_id),
        "scope_key": "mailbox",
    }
    assert captured[1].kind == "sync_calendar"
    assert captured[1].input_payload == {
        "connection_id": str(connection_id),
        "scope_key": "team-calendar",
    }


@pytest.mark.asyncio
async def test_brief_stale_refresh_and_last_success_keep_calendar_scopes_independent(
    database_url: str,
) -> None:
    """secondary 过期时只刷新同一 scope，并读取该 scope 的最后成功时间。"""
    sessions = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    cutoff = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)
    team_last_success = cutoff - timedelta(hours=1)
    sync_attempts: list[tuple[str, UUID, UUID, str]] = []

    async def sync_source(
        resource_kind: str,
        callback_connection_id: UUID,
        callback_user_id: UUID,
        scope_key: str,
    ) -> None:
        """记录 stale scope 后模拟临时失败，使告警路径读取同一 scope 的持久时间。"""
        sync_attempts.append((resource_kind, callback_connection_id, callback_user_id, scope_key))
        raise TransientProviderError(
            error_code="synthetic_calendar_sync_failure",
            message="synthetic calendar sync failure",
        )

    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="scoped-brief@example.test",
                    display_name="Scoped Brief",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8),
                    is_active=True,
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="microsoft",
                    provider_account_id="scoped-brief-account",
                    provider_tenant_id="synthetic-tenant",
                    account_type="personal",
                    account_email="scoped-brief@example.test",
                    scopes=[],
                    status="connected",
                    last_error_code=None,
                )
            )
            await session.flush()
            session.add_all(
                [
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability="calendar.read",
                        status="enabled",
                        actual_scopes=[],
                        last_verified_at=cutoff,
                        last_error_code=None,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        scope_key="primary",
                        cursor="primary-cursor",
                        last_success_at=cutoff - timedelta(minutes=5),
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        scope_key="team-calendar",
                        cursor="team-cursor",
                        last_success_at=team_last_success,
                    ),
                ]
            )

        step = GenerateBriefTaskStep(sessions, sync_source=sync_source, now=lambda: cutoff)
        async with sessions() as session:
            stale = await step._stale_resources(session, user_id, cutoff, None)

        assert stale == (("calendar", connection_id, "team-calendar"),)
        assert (
            await step._last_source_success(
                user_id=user_id,
                connection_id=connection_id,
                resource_kind="calendar",
                scope_key="team-calendar",
            )
            == team_last_success
        )
        assert await step._refresh_stale_sources(user_id, stale) == [
            "missing:calendar;last_success:2026-08-02T07:00:00+00:00;repair:retry"
        ]
        assert sync_attempts == [
            ("calendar", connection_id, user_id, "team-calendar"),
        ]
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_google_brief_aggregates_stale_calendars_through_directory_owner(
    database_url: str,
) -> None:
    """Google 多个日历任一陈旧时，简报刷新只能调用一次 directory owner。"""
    sessions = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    cutoff = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)
    sync_attempts: list[tuple[str, UUID, UUID, str]] = []

    async def sync_source(
        resource_kind: str,
        callback_connection_id: UUID,
        callback_user_id: UUID,
        scope_key: str,
    ) -> None:
        """记录简报触发的唯一 Google Calendar 目录 owner。"""
        sync_attempts.append((resource_kind, callback_connection_id, callback_user_id, scope_key))

    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="google-directory-brief@example.test",
                    display_name="Google Directory Brief",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8),
                    is_active=True,
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id="google-directory-brief",
                    account_email="google-directory-brief@example.test",
                    scopes=[],
                    status="connected",
                    last_error_code=None,
                )
            )
            await session.flush()
            session.add_all(
                [
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability="calendar.read",
                        status="enabled",
                        actual_scopes=[],
                        last_verified_at=cutoff,
                        last_error_code=None,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        scope_key="directory",
                        cursor="directory-token",
                        last_success_at=cutoff - timedelta(minutes=5),
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        scope_key="primary",
                        cursor="primary-token",
                        last_success_at=cutoff - timedelta(hours=1),
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        scope_key="team-calendar",
                        cursor="team-token",
                        last_success_at=cutoff - timedelta(hours=2),
                    ),
                ]
            )

        step = GenerateBriefTaskStep(sessions, sync_source=sync_source, now=lambda: cutoff)
        async with sessions() as session:
            stale = await step._stale_resources(session, user_id, cutoff, None)

        assert stale == (("calendar", connection_id, "directory"),)
        assert await step._refresh_stale_sources(user_id, stale) == []
        assert sync_attempts == [("calendar", connection_id, user_id, "directory")]
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_kind", "capability", "scope_key"),
    (
        ("mail", "mail.read", "mailbox"),
        ("calendar", "calendar.read", "team-calendar"),
    ),
)
async def test_disabled_read_capability_never_enters_brief_refresh(
    database_url: str,
    resource_kind: str,
    capability: str,
    scope_key: str,
) -> None:
    """disabled mail/calendar read capability 必须同时阻止 stale、readback 与 refresh。"""
    sessions = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    cutoff = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
    sync_attempts: list[tuple[str, UUID, UUID, str]] = []

    async def sync_source(
        callback_resource_kind: str,
        callback_connection_id: UUID,
        callback_user_id: UUID,
        callback_scope_key: str,
    ) -> None:
        """记录任何意外刷新；正确实现不会调用此边界。"""
        sync_attempts.append(
            (
                callback_resource_kind,
                callback_connection_id,
                callback_user_id,
                callback_scope_key,
            )
        )

    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email=f"disabled-{resource_kind}@example.test",
                    display_name="Disabled Read",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8),
                    is_active=True,
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id=f"disabled-{resource_kind}",
                    account_email=f"disabled-{resource_kind}@example.test",
                    scopes=[],
                    status="connected",
                    last_error_code=None,
                )
            )
            await session.flush()
            session.add_all(
                [
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability=capability,
                        status="disabled",
                        actual_scopes=[],
                        last_verified_at=cutoff,
                        last_error_code=None,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind=resource_kind,
                        scope_key=scope_key,
                        cursor=f"{resource_kind}-cursor",
                        last_success_at=cutoff - timedelta(hours=1),
                    ),
                ]
            )

        step = GenerateBriefTaskStep(sessions, sync_source=sync_source, now=lambda: cutoff)
        async with sessions() as session:
            stale = await step._stale_resources(session, user_id, cutoff, None)

        assert stale == ()
        assert await step._refresh_stale_sources(user_id, stale) == []
        assert sync_attempts == []
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_kind", "connection_status", "capability_status"),
    (
        ("mail", "connected", "disabled"),
        ("mail", "disconnected", "enabled"),
        ("calendar", "connected", "disabled"),
        ("calendar", "disconnected", "enabled"),
    ),
)
async def test_brief_readback_excludes_disconnected_and_capability_disabled_caches(
    database_url: str,
    resource_kind: str,
    connection_status: str,
    capability_status: str,
) -> None:
    """断开连接或禁用对应 read capability 后，旧缓存不得再进入简报来源。"""
    sessions = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    local_date = date(2026, 8, 2)
    cutoff = datetime(2026, 8, 2, 23, 0, tzinfo=UTC)
    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="brief-cache-boundary@example.test",
                    display_name="Brief Cache Boundary",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8),
                    is_active=True,
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id=(
                        f"brief-cache-{resource_kind}-{connection_status}-{capability_status}"
                    ),
                    account_email=f"brief-cache-{resource_kind}@example.test",
                    scopes=[],
                    status=connection_status,
                    last_error_code=(
                        "synthetic_disconnect" if connection_status == "disconnected" else None
                    ),
                )
            )
            await session.flush()
            session.add(
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=f"{resource_kind}.read",
                    status=capability_status,
                    actual_scopes=[],
                    last_verified_at=cutoff,
                    last_error_code=None,
                )
            )

            if resource_kind == "mail":
                thread = EmailThreadModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    provider_thread_id="brief-cache-blocked",
                    subject="Brief cache blocked",
                    participants=[],
                    latest_message_at=datetime(2026, 8, 2, 9, tzinfo=UTC),
                    provider_url="https://example.test/thread/blocked",
                )
                session.add(thread)
                await session.flush()
                session.add(
                    EmailMessageModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        thread_id=thread.id,
                        provider_message_id="brief-cache-message-blocked",
                        received_at=datetime(2026, 8, 2, 9, tzinfo=UTC),
                        sender={"email": "blocked@example.test"},
                        recipients=[],
                        subject="Brief cache blocked",
                        snippet="Synthetic blocked cache",
                        body_ciphertext=b"x",
                        body_nonce=b"x" * 12,
                        body_key_version=1,
                        labels=[],
                        headers={},
                        provider_url="https://example.test/message/blocked",
                    )
                )
            else:
                session.add(
                    _event(
                        user_id,
                        connection_id,
                        "brief-cache-event-blocked",
                        datetime(2026, 8, 2, 10, tzinfo=UTC),
                        datetime(2026, 8, 2, 11, tzinfo=UTC),
                    )
                )

        step = GenerateBriefTaskStep(sessions, now=lambda: cutoff)
        async with sessions() as session:
            if resource_kind == "mail":
                sources = await step._mail_threads_for_local_day(
                    session,
                    user_id,
                    local_date,
                    "UTC",
                    cutoff,
                    None,
                )
            else:
                sources = await step._events_for_local_day(
                    session,
                    user_id,
                    local_date,
                    "UTC",
                    None,
                )

        assert sources == []
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "capability", "expected"),
    (
        ("google", "mail.read", ("mail", "mailbox")),
        ("google", "calendar.read", ("calendar", "directory")),
        ("microsoft", "mail.read", ("mail", "mailbox")),
        ("microsoft", "calendar.read", ("calendar", None)),
    ),
)
async def test_missing_cursor_defaults_only_for_enabled_legacy_google_scope(
    database_url: str,
    provider: str,
    capability: str,
    expected: tuple[str, str | None],
) -> None:
    """Google 补目录 owner；Microsoft mail 缺目录时只触发固定 mailbox owner。"""
    sessions = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    cutoff = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)
    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email=f"missing-{provider}-{capability}@example.test",
                    display_name="Missing Cursor",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8),
                    is_active=True,
                )
            )
            connection_kwargs: dict[str, object] = {}
            if provider == "microsoft":
                connection_kwargs.update(
                    provider_tenant_id="synthetic-tenant",
                    account_type="personal",
                )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider=provider,
                    provider_account_id=f"missing-{provider}-{capability}",
                    account_email=f"missing-{provider}-{capability}@example.test",
                    scopes=[],
                    status="connected",
                    last_error_code=None,
                    **connection_kwargs,
                )
            )
            await session.flush()
            session.add(
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=capability,
                    status="enabled",
                    actual_scopes=[],
                    last_verified_at=cutoff,
                    last_error_code=None,
                )
            )

        step = GenerateBriefTaskStep(sessions, now=lambda: cutoff)
        async with sessions() as session:
            stale = await step._stale_resources(session, user_id, cutoff, None)

        assert stale == ((expected[0], connection_id, expected[1]),)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_generate_brief_refreshes_missing_microsoft_mail_through_mailbox_owner(
    database_url: str,
) -> None:
    """Microsoft enabled 邮件源缺目录时只调用一次 mailbox owner，并可恢复完整简报。"""
    sessions = build_session_factory(database_url)
    user_id, task_id, connection_id = uuid4(), uuid4(), uuid4()
    cutoff = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    sync_attempts: list[tuple[str, UUID, UUID, str]] = []

    async def sync_source(
        resource_kind: str,
        callback_connection_id: UUID,
        callback_user_id: UUID,
        scope_key: str,
    ) -> None:
        """记录目录 owner 调用；真实 folder key 必须由该 owner 从 Graph 发现。"""
        sync_attempts.append((resource_kind, callback_connection_id, callback_user_id, scope_key))

    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="missing-microsoft-cursor@example.test",
                    display_name="Missing Microsoft Cursor",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8),
                    is_active=True,
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="microsoft",
                    provider_account_id="missing-microsoft-cursor",
                    provider_tenant_id="synthetic-tenant",
                    account_type="personal",
                    account_email="missing-microsoft-cursor@example.test",
                    scopes=[],
                    status="connected",
                    last_error_code=None,
                )
            )
            await session.flush()
            session.add_all(
                [
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability="mail.read",
                        status="enabled",
                        actual_scopes=[],
                        last_verified_at=cutoff,
                        last_error_code=None,
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability="calendar.read",
                        status="enabled",
                        actual_scopes=[],
                        last_verified_at=cutoff,
                        last_error_code=None,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        scope_key="team-calendar",
                        cursor="fresh-calendar-cursor",
                        last_success_at=cutoff - timedelta(minutes=5),
                    ),
                    TaskRunModel(
                        id=task_id,
                        user_id=user_id,
                        kind="daily_brief",
                        status="running",
                        idempotency_key="missing-microsoft-cursor-task",
                        input_payload={"local_date": "2026-08-02", "schedule_kind": "manual"},
                    ),
                    _event(
                        user_id,
                        connection_id,
                        "fresh-microsoft-calendar-event",
                        datetime(2026, 8, 2, 9, tzinfo=UTC),
                        datetime(2026, 8, 2, 10, tzinfo=UTC),
                    ),
                ]
            )

        await GenerateBriefTaskStep(
            sessions,
            model_gateway=FakeModelGateway(),
            sync_source=sync_source,
            now=lambda: cutoff,
        ).execute(
            LeasedTask(
                task_id=task_id,
                user_id=user_id,
                kind="daily_brief",
                input_payload={"local_date": "2026-08-02", "schedule_kind": "manual"},
                started_at=cutoff - timedelta(seconds=1),
            )
        )

        async with sessions() as session:
            brief = await session.scalar(
                select(DailyBriefModel).where(DailyBriefModel.task_id == task_id)
            )
        assert brief is not None
        assert brief.completeness == "complete"
        assert brief.warnings == []
        assert sync_attempts == [("mail", connection_id, user_id, "mailbox")]
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_generate_brief_preserves_persisted_deterministic_reply_facts(
    database_url: str,
) -> None:
    """真实简报步骤必须把既有确定性待回复与截止时间带入 Graph 并再次审计持久化。"""
    sessions = build_session_factory(database_url)
    user_id, task_id, connection_id = uuid4(), uuid4(), uuid4()
    deadline = datetime(2026, 8, 1, 15, tzinfo=UTC)
    received_at = datetime(2026, 8, 1, 9, tzinfo=UTC)
    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="deterministic-owner@example.test",
                    display_name="Deterministic Owner",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8),
                    is_active=True,
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id="deterministic-subject",
                    account_email="deterministic-owner@example.test",
                    scopes=[],
                    status="connected",
                    last_error_code=None,
                )
            )
            await session.flush()
            session.add_all(
                (
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability="mail.read",
                        status="enabled",
                        actual_scopes=[],
                        last_verified_at=received_at,
                        last_error_code=None,
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability="calendar.read",
                        status="enabled",
                        actual_scopes=[],
                        last_verified_at=received_at,
                        last_error_code=None,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="mail",
                        scope_key="mailbox",
                        cursor="fresh-mail",
                        last_success_at=received_at,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        cursor="fresh-calendar",
                        last_success_at=received_at,
                    ),
                    TaskRunModel(
                        id=task_id,
                        user_id=user_id,
                        kind="daily_brief",
                        status="running",
                        idempotency_key="deterministic-facts-task",
                        input_payload={"local_date": "2026-08-01", "schedule_kind": "manual"},
                    ),
                )
            )
            thread = EmailThreadModel(
                user_id=user_id,
                connection_id=connection_id,
                provider_thread_id="deterministic-thread",
                subject="Newsletter",
                participants=[],
                latest_message_at=received_at,
                provider_url="https://example.test/deterministic-thread",
            )
            session.add(thread)
            await session.flush()
            session.add(
                EmailMessageModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    thread_id=thread.id,
                    provider_message_id="deterministic-message",
                    received_at=received_at,
                    sender={"email": "sender@example.test"},
                    recipients=[],
                    subject="Newsletter",
                    snippet="Synthetic source fact",
                    body_ciphertext=b"x",
                    body_nonce=b"x" * 12,
                    body_key_version=1,
                    labels=[],
                    headers={"List-Unsubscribe": "<https://example.test/unsubscribe>"},
                    provider_url="https://example.test/deterministic-message",
                )
            )
            session.add(
                EmailAnalysisModel(
                    user_id=user_id,
                    thread_id=thread.id,
                    category="notification",
                    urgency="normal",
                    needs_reply=True,
                    deadline_at=deadline,
                    confidence=1.0,
                    reason_codes=["synthetic_deterministic_fact"],
                    model_name="deterministic",
                    prompt_version="email_rules_v1",
                    input_hash="d" * 64,
                    created_at=received_at,
                )
            )

        await GenerateBriefTaskStep(
            sessions,
            model_gateway=FakeModelGateway(),
            now=lambda: deadline,
        ).execute(
            LeasedTask(
                task_id=task_id,
                user_id=user_id,
                kind="daily_brief",
                input_payload={"local_date": "2026-08-01", "schedule_kind": "manual"},
                started_at=received_at - timedelta(seconds=1),
            )
        )

        async with sessions() as session:
            analyses = list(
                (
                    await session.scalars(
                        select(EmailAnalysisModel)
                        .where(EmailAnalysisModel.thread_id == thread.id)
                        .order_by(EmailAnalysisModel.created_at)
                    )
                ).all()
            )
        assert len(analyses) == 2
        assert all(analysis.needs_reply for analysis in analyses)
        assert all(analysis.deadline_at == deadline for analysis in analyses)
    finally:
        await sessions.dispose()


def _event(
    user_id: UUID,
    connection_id: UUID,
    provider_event_id: str,
    starts_at: datetime,
    ends_at: datetime,
) -> CalendarEventModel:
    """构造不含敏感描述的合成 Calendar 事件。"""
    return CalendarEventModel(
        user_id=user_id,
        connection_id=connection_id,
        provider_event_id=provider_event_id,
        calendar_id="primary",
        title="Synthetic event",
        description_ciphertext=None,
        description_nonce=None,
        description_key_version=None,
        location_ciphertext=None,
        location_nonce=None,
        location_key_version=None,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=False,
        transparency="opaque",
        status="confirmed",
        timezone="America/Los_Angeles",
        recurring_event_id=None,
        etag=None,
        provider_url=f"https://example.test/{provider_event_id}",
        provider_updated_at=None,
    )
