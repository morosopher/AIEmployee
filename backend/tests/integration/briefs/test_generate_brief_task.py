"""验证每日简报 Worker 的来源选择与原子持久化。"""

from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.errors import TransientProviderError
from ai_employee.infrastructure.db.models.briefs import (
    DailyBriefItemModel,
    DailyBriefModel,
    LLMInvocationModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    EmailAnalysisModel,
    EmailMessageModel,
    EmailThreadModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.integrations.llm.fake import FakeModelGateway
from ai_employee.workers.generate_brief import GenerateBriefTaskStep


@pytest.mark.asyncio
async def test_generate_brief_selects_local_day_events_after_stale_mail_sync_fails(
    database_url: str,
) -> None:
    """过期 Gmail 游标同步失败时，仍为本地日期重叠的日程生成部分简报与审计。"""
    sessions = build_session_factory(database_url)
    user_id, task_id, connection_id = uuid4(), uuid4(), uuid4()
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
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="gmail",
                        cursor=None,
                        last_success_at=None,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
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

        sync_attempts: list[str] = []

        async def sync_source(resource_kind: str, _: UUID, __: UUID) -> None:
            """模拟 Gmail 同步失败，确保单源故障不会阻断可用日历。"""
            sync_attempts.append(resource_kind)
            if resource_kind == "gmail":
                raise TransientProviderError(
                    error_code="synthetic_sync_failure",
                    message="synthetic sync failure",
                )

        step = GenerateBriefTaskStep(
            sessions,
            model_gateway=FakeModelGateway(),
            sync_source=sync_source,
            now=lambda: datetime(2026, 7, 31, 7, tzinfo=UTC),
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
                "missing:gmail;last_success:never;repair:retry",
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
        assert sync_attempts == ["gmail"]
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
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="gmail",
                        cursor="fresh-gmail",
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
