"""在合成 PostgreSQL 上验证未知结果的有界只读核对与终态收敛。"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from ai_employee.application.ports.trusted_actions import ProviderWriteOutcome
from ai_employee.application.use_cases.trusted_actions import TrustedActionAttemptAbandoned
from ai_employee.config import Settings
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.actions import MailDraftModel
from ai_employee.infrastructure.db.models.sources import EmailMessageModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionReconciliationRecoveryStore,
    SqlAlchemyTrustedActionRepository,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.integrations.registry import ProviderAdapterRegistry

# 复用 Task 19 已验证的合成 seed/adapter；这些 helper 只生成合成账号和内容，绝不触达
# Google/Microsoft。把 fixture 保持在既有测试模块可避免再次手工复制完整外键骨架。
from tests.integration.m2.test_tool_execution_claim import (
    ACTION_CIPHER,
    NOW,
    _RecordingAdapter,
    _Seed,
    _seed_action,
    _seed_calendar_action,
    _workflow,
)

# 与 Task 19 共用受 provenance 保护的 Cycle 5 regular 数据库；覆盖通用迁移 fixture，
# 避免 focused reconciliation 测试把 roles-absent anchor 当作可直接升级的目标。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """把动作核对测试绑定到已迁移且受清理租约保护的临时库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """阻止通用 fixture 对 regular 数据库重复执行维护迁移。"""
    del cycle5_regular_database_url
    yield


def _outcome(
    kind: ProviderWriteOutcomeKind,
    *,
    provider_url: str | None = None,
    error_code: str | None = None,
) -> ProviderWriteOutcome:
    """构造不含真实供应商响应的固定合成结果。"""
    return ProviderWriteOutcome(
        kind=kind,
        retryable=False,
        retry_after_seconds=None,
        provider_resource_id=(
            "synthetic-resource" if kind is not ProviderWriteOutcomeKind.UNKNOWN else None
        ),
        provider_request_id="synthetic-request",
        correlation_id="synthetic-correlation",
        provider_url=provider_url,
        error_code=error_code,
    )


async def _claim_reconciliation(
    database_url: str,
    seed: _Seed,
    *,
    owner: str,
    now: datetime,
) -> None:
    """在专用 repository 事务取得一轮到期只读租约。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            repository = SqlAlchemyTrustedActionRepository(session, ACTION_CIPHER)
            snapshot = await repository.claim_reconciliation(
                task_id=seed.task_id,
                lease_owner=owner,
                now=now,
                lease_expires_at=now + timedelta(minutes=1),
            )
            assert snapshot is not None
    finally:
        await session_factory.dispose()


async def _scheduled_for(database_url: str, seed: _Seed) -> datetime:
    """读取下一次核对的持久 UTC 调度时刻。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            assert task is not None and task.scheduled_for is not None
            value = task.scheduled_for
            assert value.tzinfo is not None and value.utcoffset() is not None
            return value.astimezone(UTC)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_scheduler_recovery_is_postgres_only_when_master_key_is_unavailable(
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis 丢失后的核对恢复只读 PostgreSQL，不因缺少主密钥而阻塞。"""
    seed = _Seed()
    await _seed_action(
        database_url,
        seed,
        existing_execution_status=ToolExecutionStatus.RECONCILING,
        request_started_at=NOW,
        task_status=TaskStatus.RECONCILING,
    )
    missing_key = tmp_path / "missing-app-master-key"
    settings = Settings(_env_file=None, app_master_key_file=missing_key)
    assert settings.app_master_key_file == missing_key
    assert not missing_key.exists()

    # 如果恢复 store 误构造 ActionPayloadCipher，这个替身会立刻失败；合约要求该维护
    # 路径只读取 TaskRun/ToolExecution/Outbox 调度事实。
    def _unexpected_key_read(*_args: object, **_kwargs: object) -> object:
        pytest.fail("scheduler recovery must not read the application master key")

    from ai_employee.infrastructure.security.encryption import AeadCipher

    monkeypatch.setattr(AeadCipher, "from_file", _unexpected_key_read)
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == seed.task_id)
                .values(scheduled_for=NOW)
            )
        store = SqlAlchemyTrustedActionReconciliationRecoveryStore(session_factory)
        first = await store.recover_due_reconciliations(now=NOW, limit=10)
        second = await store.recover_due_reconciliations(now=NOW, limit=10)
        async with session_factory() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            events = (
                await session.scalars(
                    select(OutboxEventModel).where(
                        OutboxEventModel.aggregate_id == seed.task_id,
                        OutboxEventModel.topic == "task.execute",
                    )
                )
            ).all()
    finally:
        await session_factory.dispose()

    assert first == 1
    assert second == 0
    assert task is not None and task.status == TaskStatus.RECONCILING.value
    assert len(events) == 1
    assert events[0].payload == {"task_id": str(seed.task_id)}


@pytest.mark.asyncio
async def test_unknown_outcome_enters_reconciling_and_four_read_only_attempts_are_bounded(
    database_url: str,
) -> None:
    """UNKNOWN 首次转入 reconciling，四次核对后才进入 needs_attention，且不重写。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter(_outcome(ProviderWriteOutcomeKind.UNKNOWN, error_code="opaque"))
    workflow = _workflow(database_url, adapter)
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        with pytest.raises(TrustedActionAttemptAbandoned):
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )

        for attempt in range(4):
            state = await _scheduled_for(database_url, seed)
            owner = f"reconcile-{attempt}"
            await _claim_reconciliation(database_url, seed, owner=owner, now=state)
            with pytest.raises(TrustedActionAttemptAbandoned):
                await workflow.reconcile(
                    task_id=seed.task_id,
                    approval_id=seed.approval_id,
                    operation_id=seed.operation_id,
                    expected_payload_hash=payload_hash,
                    lease_owner=owner,
                )

        session_factory = build_session_factory(database_url)
        try:
            async with session_factory() as session:
                task = await session.get(TaskRunModel, seed.task_id)
                draft = await session.get(MailDraftModel, seed.draft_id)
                execution = await session.scalar(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                )
                audits = (
                    await session.scalars(
                        select(AuditEventModel.event_type)
                        .where(AuditEventModel.task_id == seed.task_id)
                        .order_by(AuditEventModel.id)
                    )
                ).all()
                outbox_events = (
                    await session.scalars(
                        select(OutboxEventModel)
                        .where(OutboxEventModel.aggregate_id == seed.task_id)
                        .order_by(OutboxEventModel.created_at, OutboxEventModel.id)
                    )
                ).all()
        finally:
            await session_factory.dispose()
    finally:
        await workflow.dispose()

    assert task is not None and task.status == TaskStatus.NEEDS_ATTENTION.value
    assert task.error_code == "provider_reconciliation_failed"
    assert task.scheduled_for is None and task.lease_owner is None
    assert draft is not None and draft.status == "needs_attention"
    assert execution is not None
    assert execution.status == ToolExecutionStatus.NEEDS_ATTENTION.value
    assert execution.error_code == "provider_reconciliation_failed"
    assert execution.write_attempt_count == 1
    assert execution.reconciliation_attempt_count == 4
    assert adapter.write_calls == 1
    assert adapter.reconcile_calls == 4
    assert audits.count("tool.reconciling") == 4
    assert audits[-1] == "tool.needs_attention"
    outbox_topics = [event.topic for event in outbox_events]
    assert outbox_topics.count("tool.reconciling") == 4
    assert outbox_topics[-1] == "tool.needs_attention"
    # 每个 task.execute 都是有界只读核对投递；没有 retry/新的 approval 或第二次写意图。
    reconcile_keys = [
        event.deduplication_key for event in outbox_events if event.topic == "task.execute"
    ]
    assert len(reconcile_keys) == 4
    assert all(":reconcile:" in key for key in reconcile_keys)


@pytest.mark.asyncio
async def test_applied_reconciliation_enqueues_one_safe_source_refresh_and_preserves_url(
    database_url: str,
) -> None:
    """确认应用只创建一次只读 refresh 意图，不凭命令伪造 EmailMessage。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter(_outcome(ProviderWriteOutcomeKind.UNKNOWN, error_code="opaque"))
    workflow = _workflow(database_url, adapter)
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        with pytest.raises(TrustedActionAttemptAbandoned):
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        state = await _scheduled_for(database_url, seed)
        await _claim_reconciliation(database_url, seed, owner="reconcile-applied", now=state)
        adapter.outcome = _outcome(
            ProviderWriteOutcomeKind.CONFIRMED_APPLIED,
            provider_url="https://provider.example.test/synthetic/resource",
        )
        await workflow.reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner="reconcile-applied",
        )
        # 终态重放只能复用持久结论，不能第二次调用 adapter 或 enqueue refresh。
        await workflow.execute_or_reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner="replay",
        )

        session_factory = build_session_factory(database_url)
        try:
            async with session_factory() as session:
                task = await session.get(TaskRunModel, seed.task_id)
                execution = await session.scalar(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                )
                refreshes = (
                    await session.scalars(
                        select(TaskRunModel).where(
                            TaskRunModel.user_id == seed.user_id,
                            TaskRunModel.kind == "sync_mail",
                        )
                    )
                ).all()
                source_messages = await session.scalar(
                    select(func.count()).select_from(EmailMessageModel)
                )
        finally:
            await session_factory.dispose()
    finally:
        await workflow.dispose()

    assert task is not None and task.status == TaskStatus.SUCCEEDED.value
    assert execution is not None and execution.status == ToolExecutionStatus.SUCCEEDED.value
    assert execution.result_summary == {
        "kind": ProviderWriteOutcomeKind.CONFIRMED_APPLIED.value,
        "retryable": False,
        "provider_url": "https://provider.example.test/synthetic/resource",
    }
    assert len(refreshes) == 1
    assert source_messages == 0
    assert adapter.write_calls == 1
    assert adapter.reconcile_calls == 1


@pytest.mark.asyncio
async def test_calendar_applied_reconciliation_refreshes_exact_calendar_scope(
    database_url: str,
) -> None:
    """日历已应用结果的 refresh 必须绑定精确 calendar_id，而非目录或全量范围。"""
    seed = _Seed()
    calendar_id = "synthetic-calendar-exact"
    payload_hash = await _seed_calendar_action(database_url, seed, calendar_id=calendar_id)
    adapter = _RecordingAdapter(_outcome(ProviderWriteOutcomeKind.UNKNOWN, error_code="opaque"))
    workflow = _workflow(
        database_url,
        adapter,
        registry=ProviderAdapterRegistry(google_calendar_action=adapter),
    )
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        with pytest.raises(TrustedActionAttemptAbandoned):
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        state = await _scheduled_for(database_url, seed)
        await _claim_reconciliation(database_url, seed, owner="reconcile-calendar", now=state)
        adapter.outcome = _outcome(ProviderWriteOutcomeKind.CONFIRMED_APPLIED)
        await workflow.reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner="reconcile-calendar",
        )
        session_factory = build_session_factory(database_url)
        try:
            async with session_factory() as session:
                refreshes = (
                    await session.scalars(
                        select(TaskRunModel).where(
                            TaskRunModel.user_id == seed.user_id,
                            TaskRunModel.kind == "sync_calendar",
                        )
                    )
                ).all()
        finally:
            await session_factory.dispose()
    finally:
        await workflow.dispose()

    assert len(refreshes) == 1
    assert refreshes[0].input_payload == {
        "connection_id": str(seed.connection_id),
        "scope_key": calendar_id,
    }


@pytest.mark.asyncio
async def test_confirmed_not_applied_reconciliation_does_not_refresh_or_queue_write(
    database_url: str,
) -> None:
    """只读核对确认未应用时回到 editing，且不创建 refresh/重试写队列。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter(_outcome(ProviderWriteOutcomeKind.UNKNOWN, error_code="opaque"))
    workflow = _workflow(database_url, adapter)
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        with pytest.raises(TrustedActionAttemptAbandoned):
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        state = await _scheduled_for(database_url, seed)
        await _claim_reconciliation(database_url, seed, owner="reconcile-not-applied", now=state)
        adapter.outcome = _outcome(
            ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
            error_code="provider_not_applied",
        )
        await workflow.reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner="reconcile-not-applied",
        )

        session_factory = build_session_factory(database_url)
        try:
            async with session_factory() as session:
                task = await session.get(TaskRunModel, seed.task_id)
                draft = await session.get(MailDraftModel, seed.draft_id)
                refresh_count = await session.scalar(
                    select(func.count())
                    .select_from(TaskRunModel)
                    .where(
                        TaskRunModel.user_id == seed.user_id,
                        TaskRunModel.kind == "sync_mail",
                    )
                )
                task_execute_count = await session.scalar(
                    select(func.count())
                    .select_from(OutboxEventModel)
                    .where(
                        OutboxEventModel.aggregate_id == seed.task_id,
                        OutboxEventModel.topic == "task.execute",
                    )
                )
        finally:
            await session_factory.dispose()
    finally:
        await workflow.dispose()

    assert task is not None and task.status == TaskStatus.FAILED.value
    assert draft is not None and draft.status == "editing"
    assert refresh_count == 0
    # UNKNOWN 初次会排一个核对 task.execute；确认未应用后不得再追加第二条。
    assert task_execute_count == 1
    assert adapter.write_calls == 1
    assert adapter.reconcile_calls == 1
