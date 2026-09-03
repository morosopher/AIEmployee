"""在合成 PostgreSQL 上验证人工核对 CAS、重开和并发竞争。"""

import asyncio
from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from ai_employee.application.use_cases.action_views import (
    ManualResolutionUseCase,
    RequestActionReconciliationUseCase,
)
from ai_employee.domain.actions import MailDraftStatus, ToolExecutionStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.actions import MailDraftModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.action_views import (
    SqlAlchemyActionViewRepository,
    SqlAlchemyActionViewRepositoryFactory,
)
from ai_employee.infrastructure.db.session import build_session_factory

# 复用 Task 19 的合成 seed；其中的内容只用于本地 AEAD 测试，不会进入日志或真实 provider。
from tests.integration.m2.test_tool_execution_claim import (
    NOW,
    _Seed,
    _seed_action,
)

# 复用 Task 19 的 Cycle 5 regular 生命周期；本模块不在共享 roles-absent anchor 上隐式迁移。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """把人工 CAS 测试绑定到受清理租约保护的 regular 数据库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """覆盖通用迁移 fixture，避免测试启动时改变数据库维护事实。"""
    del cycle5_regular_database_url
    yield


async def _seed_needs_attention(database_url: str, seed: _Seed) -> str:
    """建立一个已耗尽自动核对预算、等待人工结论的合成动作。"""
    payload_hash = await _seed_action(
        database_url,
        seed,
        existing_execution_status=ToolExecutionStatus.NEEDS_ATTENTION,
        request_started_at=NOW,
        task_status=TaskStatus.NEEDS_ATTENTION,
    )
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            await session.execute(
                update(MailDraftModel)
                .where(MailDraftModel.id == seed.draft_id)
                .values(status=MailDraftStatus.NEEDS_ATTENTION.value)
            )
            await session.execute(
                update(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == seed.task_id)
                .values(
                    reconciliation_attempt_count=4,
                    error_code="provider_reconciliation_failed",
                    result_summary={"kind": "unknown", "retryable": False},
                )
            )
            session.add(
                AuditEventModel(
                    user_id=seed.user_id,
                    task_id=seed.task_id,
                    event_type="tool.needs_attention",
                    actor_type="worker",
                    actor_id="synthetic-worker",
                    event_metadata={
                        "outcome": "unknown",
                        "reconciliation_attempt_count": 4,
                    },
                )
            )
    finally:
        await session_factory.dispose()
    return payload_hash


async def _snapshot_version(database_url: str, seed: _Seed) -> str:
    """读取操作视图公开的 canonical audit cursor。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            snapshot = await SqlAlchemyActionViewRepository(session).get(
                user_id=seed.user_id,
                task_id=seed.task_id,
            )
            assert snapshot is not None
            assert snapshot.event_cursor == snapshot.task_version
            return snapshot.task_version
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_manual_not_executed_is_terminal_without_provider_or_refresh(
    database_url: str,
) -> None:
    """confirmed_not_executed 只收敛本地事实，不重新排队写入。"""
    seed = _Seed()
    await _seed_needs_attention(database_url, seed)
    version = await _snapshot_version(database_url, seed)
    factory = build_session_factory(database_url)
    try:
        result = await ManualResolutionUseCase(
            SqlAlchemyActionViewRepositoryFactory(factory)
        ).execute(
            user_id=seed.user_id,
            task_id=seed.task_id,
            task_version=version,
            resolution="confirmed_not_executed",
            now=NOW,
        )
        assert result.isdecimal() and result == str(int(result))

        async with factory() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            refresh_count = await session.scalar(
                select(func.count())
                .select_from(TaskRunModel)
                .where(
                    TaskRunModel.user_id == seed.user_id,
                    TaskRunModel.kind == "sync_mail",
                )
            )
            outbox = (
                await session.scalars(
                    select(OutboxEventModel)
                    .where(OutboxEventModel.aggregate_id == seed.task_id)
                    .order_by(OutboxEventModel.created_at, OutboxEventModel.id)
                )
            ).all()
            audits = (
                await session.scalars(
                    select(AuditEventModel).where(AuditEventModel.task_id == seed.task_id)
                )
            ).all()
    finally:
        await factory.dispose()

    assert task is not None and task.status == TaskStatus.FAILED.value
    assert draft is not None and draft.status == MailDraftStatus.EDITING.value
    assert draft.current_version == 1
    assert execution is not None
    assert execution.status == ToolExecutionStatus.CONFIRMED_FAILED.value
    assert execution.manual_resolution == "confirmed_not_executed"
    assert execution.write_attempt_count == 1
    assert refresh_count == 0
    assert sum(event.topic == "tool.manually_resolved" for event in outbox) == 1
    manual_events = [event for event in audits if event.event_type == "tool.manually_resolved"]
    assert len(manual_events) == 1
    assert set(manual_events[0].event_metadata) == {
        "resolution",
        "source",
        "operation_id",
        "approval_id",
        "resolved_at",
    }
    assert all(
        "synthetic-sensitive-command-body" not in str(event.event_metadata) for event in audits
    )


@pytest.mark.asyncio
async def test_manual_confirmed_executed_enqueues_one_source_refresh_and_safe_url(
    database_url: str,
) -> None:
    """人工确认已执行也必须只创建一次只读 mailbox refresh，并传播安全入口。"""
    seed = _Seed()
    await _seed_needs_attention(database_url, seed)
    factory = build_session_factory(database_url)
    try:
        async with factory.begin() as session:
            await session.execute(
                update(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == seed.task_id)
                .values(
                    result_summary={
                        "kind": "unknown",
                        "retryable": False,
                        "provider_url": "https://provider.example.test/synthetic/resource",
                    }
                )
            )
        version = await _snapshot_version(database_url, seed)
        result = await ManualResolutionUseCase(
            SqlAlchemyActionViewRepositoryFactory(factory)
        ).execute(
            user_id=seed.user_id,
            task_id=seed.task_id,
            task_version=version,
            resolution="confirmed_executed",
            now=NOW,
        )
        assert result.isdecimal()
        async with factory() as session:
            refreshes = (
                await session.scalars(
                    select(TaskRunModel).where(
                        TaskRunModel.user_id == seed.user_id,
                        TaskRunModel.kind == "sync_mail",
                    )
                )
            ).all()
            manual_audit = await session.scalar(
                select(AuditEventModel)
                .where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "tool.manually_resolved",
                )
                .order_by(AuditEventModel.id.desc())
            )
    finally:
        await factory.dispose()

    assert len(refreshes) == 1
    assert refreshes[0].input_payload == {
        "connection_id": str(seed.connection_id),
        "scope_key": "mailbox",
    }
    assert manual_audit is not None
    assert manual_audit.event_metadata["provider_url"] == (
        "https://provider.example.test/synthetic/resource"
    )


@pytest.mark.asyncio
async def test_manual_resolution_rejects_stale_cursor_and_cross_user_request(
    database_url: str,
) -> None:
    """过期游标与跨用户访问统一返回无内容冲突，且不改变未决事实。"""
    seed = _Seed()
    await _seed_needs_attention(database_url, seed)
    version = await _snapshot_version(database_url, seed)
    factory = build_session_factory(database_url)
    try:
        use_case = ManualResolutionUseCase(SqlAlchemyActionViewRepositoryFactory(factory))
        with pytest.raises(StateConflictError) as stale:
            await use_case.execute(
                user_id=seed.user_id,
                task_id=seed.task_id,
                task_version="0",
                resolution="confirmed_executed",
                now=NOW,
            )
        assert stale.value.error_code == "manual_resolution_conflict"
        with pytest.raises(StateConflictError) as cross_user:
            await use_case.execute(
                user_id=UUID(int=seed.user_id.int ^ 1),
                task_id=seed.task_id,
                task_version=version,
                resolution="confirmed_executed",
                now=NOW,
            )
        assert cross_user.value.error_code == "manual_resolution_conflict"
    finally:
        await factory.dispose()

    assert await _snapshot_version(database_url, seed) == version


@pytest.mark.asyncio
async def test_manual_resolution_and_second_manual_request_have_one_cas_winner(
    database_url: str,
) -> None:
    """两个并发人工结论只能有一个 winner，loser 不能调用 provider 或覆盖状态。"""
    seed = _Seed()
    await _seed_needs_attention(database_url, seed)
    version = await _snapshot_version(database_url, seed)
    first_factory = build_session_factory(database_url)
    second_factory = build_session_factory(database_url)
    try:
        first = ManualResolutionUseCase(SqlAlchemyActionViewRepositoryFactory(first_factory))
        second = ManualResolutionUseCase(SqlAlchemyActionViewRepositoryFactory(second_factory))
        results = await asyncio.gather(
            first.execute(
                user_id=seed.user_id,
                task_id=seed.task_id,
                task_version=version,
                resolution="confirmed_executed",
                now=NOW,
            ),
            second.execute(
                user_id=seed.user_id,
                task_id=seed.task_id,
                task_version=version,
                resolution="confirmed_not_executed",
                now=NOW,
            ),
            return_exceptions=True,
        )
    finally:
        await first_factory.dispose()
        await second_factory.dispose()

    successes = [item for item in results if isinstance(item, str)]
    conflicts = [
        item
        for item in results
        if isinstance(item, StateConflictError) and item.error_code == "manual_resolution_conflict"
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            manual_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "tool.manually_resolved",
                )
            )
    finally:
        await session_factory.dispose()
    assert execution is not None and execution.manual_resolution in {
        "confirmed_executed",
        "confirmed_not_executed",
    }
    assert manual_count == 1


@pytest.mark.asyncio
async def test_user_requested_reconciliation_reuses_execution_identity(
    database_url: str,
) -> None:
    """用户重开只读核对时保留原 ToolExecution ID，不制造新的写授权。"""
    seed = _Seed()
    await _seed_needs_attention(database_url, seed)
    factory = build_session_factory(database_url)
    try:
        execution_id = await RequestActionReconciliationUseCase(
            SqlAlchemyActionViewRepositoryFactory(factory)
        ).execute(user_id=seed.user_id, task_id=seed.task_id, now=NOW)
    finally:
        await factory.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            task_execute = (
                await session.scalars(
                    select(OutboxEventModel).where(
                        OutboxEventModel.aggregate_id == seed.task_id,
                        OutboxEventModel.topic == "task.execute",
                    )
                )
            ).all()
    finally:
        await session_factory.dispose()
    assert task is not None and task.status == TaskStatus.RECONCILING.value
    assert execution is not None
    assert execution.id == execution_id
    assert execution.status == ToolExecutionStatus.RECONCILING.value
    assert len(task_execute) == 1
