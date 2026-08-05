"""验证每日简报的真实 PostgreSQL 持久化与调度幂等边界。"""

from datetime import UTC, date, datetime, time
from uuid import UUID

import pytest
from sqlalchemy import select

from ai_employee.application.use_cases.briefs import PersistDailyBriefUseCase
from ai_employee.application.use_cases.schedules import DispatchDueDailyBriefsUseCase
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.domain.briefs import (
    BriefItem,
    BriefPriority,
    BriefSection,
    BriefSourceRef,
    DailyBriefContent,
)
from ai_employee.infrastructure.db.models.briefs import (
    DailyBriefItemModel,
    DailyBriefModel,
    LLMInvocationModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    EmailAnalysisModel,
    EmailThreadModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.briefs import (
    SqlAlchemyDailyBriefPersistenceStoreFactory,
)
from ai_employee.infrastructure.db.repositories.identity import SqlAlchemyActiveUserScheduleReader
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepositoryFactory
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory


class NoopDispatcher:
    """以无网络 dispatcher 保留真实 TaskRun/Outbox 事务路径。"""

    async def dispatch(self, task_id: UUID) -> str:
        """不触碰 Redis，仅返回测试所需的稳定状态。"""
        del task_id
        return "created"


async def _create_user_and_task(
    session_factory: ManagedAsyncSessionMaker, *, key: str, user_id: UUID | None = None
) -> tuple[UUID, UUID]:
    """写入简报归属用户和已持久化的生成任务。"""
    async with session_factory.begin() as session:
        if user_id is None:
            user = UserModel(
                email=f"brief-{key}@example.test",
                display_name="Brief owner",
                password_hash=None,
                timezone="Asia/Shanghai",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            user_id = user.id
        task = TaskRunModel(
            user_id=user_id,
            kind="daily_brief",
            status="running",
            idempotency_key=f"brief-task:{key}",
            input_payload={"schedule_kind": "manual"},
        )
        session.add(task)
        await session.flush()
        return user_id, task.id


def _content(*, completeness: str, headline: str) -> DailyBriefContent:
    """构造含有有序来源引用、部分成功警告和判断字段的合成简报。"""
    return DailyBriefContent(
        local_date=date(2026, 8, 4),
        source_cutoff=datetime(2026, 8, 4, 1, 5, tzinfo=UTC),
        completeness=completeness,
        headline=headline,
        warnings=["source_sync_failed:gmail"] if completeness == "partial" else [],
        items=[
            BriefItem(
                section=BriefSection.ATTENTION,
                priority=BriefPriority.HIGH,
                title="Respond to synthetic request",
                body_markdown="Synthetic body only.",
                source_refs=[
                    BriefSourceRef(
                        source_type="email_thread",
                        source_id="thread-synthetic",
                        provider_url="https://example.test/thread-synthetic",
                    )
                ],
            ),
            BriefItem(
                section=BriefSection.SCHEDULE,
                title="Synthetic calendar event",
                body_markdown="09:00 UTC",
                source_refs=[
                    BriefSourceRef(source_type="calendar_event", source_id="event-synthetic")
                ],
            ),
        ],
    )


async def _create_email_thread(
    session_factory: ManagedAsyncSessionMaker, *, user_id: UUID
) -> UUID:
    """构造受用户归属约束的合成邮件线程，供判断持久化验证使用。"""
    async with session_factory.begin() as session:
        connection = OAuthConnectionModel(
            user_id=user_id,
            provider="google",
            provider_account_id="synthetic-account",
            account_email="synthetic@example.test",
            scopes=["gmail.readonly"],
            status="connected",
            last_error_code=None,
        )
        session.add(connection)
        await session.flush()
        thread = EmailThreadModel(
            user_id=user_id,
            connection_id=connection.id,
            provider_thread_id="synthetic-thread",
            subject="Synthetic subject",
            participants=[],
            latest_message_at=datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
            provider_url="https://example.test/thread-synthetic",
            provider_updated_at=None,
        )
        session.add(thread)
        await session.flush()
        return thread.id


@pytest.mark.asyncio
async def test_manual_refresh_creates_next_version_and_persists_auditable_result(
    database_url: str,
) -> None:
    """手动任务创建新版本；结果、审计和模型元数据必须一起提交。"""
    session_factory = build_session_factory(database_url)
    try:
        user_id, first_task_id = await _create_user_and_task(session_factory, key="first")
        thread_id = await _create_email_thread(session_factory, user_id=user_id)
        first_id = await PersistDailyBriefUseCase(
            SqlAlchemyDailyBriefPersistenceStoreFactory(session_factory)
        ).execute(
            user_id=user_id,
            task_id=first_task_id,
            content=_content(completeness="complete", headline="First version"),
            markdown="# First version\n- safe synthetic result",
            email_analyses=(
                {
                    "thread_id": str(thread_id),
                    "category": "work",
                    "urgency": "urgent",
                    "needs_reply": True,
                    "deadline_at": datetime(2026, 8, 4, 2, 0, tzinfo=UTC),
                    "confidence": 0.9,
                    "reason_codes": ["synthetic_deadline"],
                    "input_hash": "b" * 64,
                },
            ),
            model_invocations=(
                {
                    "provider": "fake",
                    "model_name": "fake-model",
                    "prompt_version": "daily_brief_v1",
                    "input_hash": "a" * 64,
                    "output_schema": "DailyBriefContent",
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "latency_ms": 4,
                    "status": "succeeded",
                },
            ),
        )
        _, second_task_id = await _create_user_and_task(
            session_factory, key="second", user_id=user_id
        )
        second_id = await PersistDailyBriefUseCase(
            SqlAlchemyDailyBriefPersistenceStoreFactory(session_factory)
        ).execute(
            user_id=user_id,
            task_id=second_task_id,
            content=_content(completeness="partial", headline="Second version"),
            markdown="# Second version\n> source_sync_failed:gmail",
        )
        async with session_factory() as session:
            briefs = (await session.scalars(select(DailyBriefModel).order_by(DailyBriefModel.version))).all()
            items = (await session.scalars(select(DailyBriefItemModel).where(DailyBriefItemModel.brief_id == second_id).order_by(DailyBriefItemModel.position))).all()
            invocation = await session.scalar(select(LLMInvocationModel).where(LLMInvocationModel.task_id == first_task_id))
            judgement = await session.scalar(select(EmailAnalysisModel).where(EmailAnalysisModel.thread_id == thread_id))
            first_task = await session.get(TaskRunModel, first_task_id)
            events = (await session.scalars(select(AuditEventModel).where(AuditEventModel.task_id == first_task_id))).all()
        assert [brief.id for brief in briefs] == [first_id, second_id]
        assert [brief.version for brief in briefs] == [1, 2]
        assert briefs[1].completeness == "partial"
        assert briefs[1].warnings == ["source_sync_failed:gmail"]
        assert briefs[1].source_cutoff == datetime(2026, 8, 4, 1, 5, tzinfo=UTC)
        assert [item.position for item in items] == [0, 1]
        assert items[0].source_refs[0]["source_id"] == "thread-synthetic"
        assert first_task is not None and first_task.result_payload == {"brief_id": str(first_id), "completeness": "complete"}
        assert invocation is not None and invocation.input_hash == "a" * 64
        assert not hasattr(invocation, "prompt") and not hasattr(invocation, "input_prompt")
        assert judgement is not None and (judgement.category, judgement.urgency, judgement.needs_reply, judgement.confidence, judgement.reason_codes) == ("work", "urgent", True, 0.9, ["synthetic_deadline"])
        assert [(event.event_type, event.event_metadata) for event in events] == [("brief.ready", {"brief_id": str(first_id), "completeness": "complete"})]
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_replayed_task_and_scheduled_scan_are_idempotent(
    database_url: str,
) -> None:
    """同一 worker 重放复用简报，调度扫描按用户、本地日和 scheduled 种类复用任务。"""
    session_factory = build_session_factory(database_url)
    try:
        user_id, task_id = await _create_user_and_task(session_factory, key="replay")
        use_case = PersistDailyBriefUseCase(
            SqlAlchemyDailyBriefPersistenceStoreFactory(session_factory)
        )
        first_id = await use_case.execute(user_id=user_id, task_id=task_id, content=_content(completeness="complete", headline="Replay"), markdown="# Replay")
        assert await use_case.execute(user_id=user_id, task_id=task_id, content=_content(completeness="complete", headline="Changed"), markdown="# Changed") == first_id
        creator = CreateTaskUseCase(SqlAlchemyTaskRepositoryFactory(session_factory), NoopDispatcher())
        scanner = DispatchDueDailyBriefsUseCase(reader=SqlAlchemyActiveUserScheduleReader(session_factory), task_creator=creator)
        now = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
        assert await scanner.execute(now=now) == 1
        assert await scanner.execute(now=now) == 1
        async with session_factory() as session:
            persisted = (await session.scalars(select(DailyBriefModel).where(DailyBriefModel.user_id == user_id))).all()
            scheduled = (await session.scalars(select(TaskRunModel).where(TaskRunModel.user_id == user_id, TaskRunModel.input_payload["schedule_kind"].astext == "scheduled"))).all()
        assert [brief.id for brief in persisted] == [first_id]
        assert len(scheduled) == 1
        assert scheduled[0].input_payload == {"local_date": "2026-08-04", "schedule_kind": "scheduled"}
    finally:
        await session_factory.dispose()
