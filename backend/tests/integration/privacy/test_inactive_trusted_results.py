"""以真实claim/request-start和删除barrier验证可信回包及独立失败事务的普通写屏障。"""

import asyncio
from datetime import timedelta
from typing import Literal

import pytest
from sqlalchemy import func, select

from ai_employee.application.ports.trusted_actions import ProviderWriteOutcome
from ai_employee.application.use_cases.trusted_actions import (
    TrustedActionAttemptAbandoned,
    TrustedActionUserInactive,
)
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.errors import StateConflictError, TransientProviderError
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.actions import MailDraftModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionReconciliationRecoveryStore,
    SqlAlchemyTrustedActionRepository,
    SqlAlchemyTrustedActionTaskExecutionStore,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.integrations.registry import ProviderAdapterRegistry
from tests.integration.m2.test_tool_execution_claim import (
    ACTION_CIPHER,
    NOW,
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _RecordingAdapter,
    _Seed,
    _seed_action,
    _seed_calendar_action,
    _workflow,
)
from tests.integration.privacy.inactive_barrier import (
    assert_facts_unchanged,
    commit_deletion_barrier,
    database_facts,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]


def _outcome(result: str) -> ProviderWriteOutcome:
    """只构造合成、内容无关结果；重试必须绑定明确未应用证明。"""
    kind = {
        "applied": ProviderWriteOutcomeKind.CONFIRMED_APPLIED,
        "failed": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
        "retryable": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
        "unknown": ProviderWriteOutcomeKind.UNKNOWN,
    }[result]
    return ProviderWriteOutcome(
        kind=kind,
        retryable=result == "retryable",
        retry_after_seconds=7 if result == "retryable" else None,
        provider_resource_id="synthetic-applied-resource" if result == "applied" else None,
        provider_request_id="synthetic-request",
        correlation_id="synthetic-result",
        provider_url=None,
        error_code=None if result == "applied" else "synthetic_provider_result",
    )


@pytest.mark.parametrize("kind", ("mail", "calendar"))
@pytest.mark.parametrize("result", ("applied", "failed", "retryable", "unknown"))
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_provider_reply_and_refresh_task_respect_deletion_barrier(
    database_url: str,
    kind: Literal["mail", "calendar"],
    result: str,
    inactive: bool,
) -> None:
    """真实claim和request-start完成后暂停provider端口；回包及异常cleanup不能越过屏障。

    特别比较全部普通Audit/Outbox和成功后的source-refresh Task，不能只检查执行状态。
    本测试只调用Fake端口；再次进入原工作流必须零新增写调用且不改变隐私赢家。
    """
    seed, sessions = _Seed(), build_session_factory(database_url)
    payload_hash = await (
        _seed_action(database_url, seed)
        if kind == "mail"
        else _seed_calendar_action(database_url, seed)
    )
    entered, release = asyncio.Event(), asyncio.Event()
    adapter = _RecordingAdapter(_outcome(result), started=entered, release=release)
    registry = (
        ProviderAdapterRegistry(google_mail_action=adapter)
        if kind == "mail"
        else ProviderAdapterRegistry(google_calendar_action=adapter)
    )
    workflow = _workflow(database_url, adapter, registry=registry)

    async def execute() -> None:
        """执行原工作流并保留其稳定失败/延期控制流；最终断言检查真实持久结果。"""
        try:
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        except (
            StateConflictError,
            TransientProviderError,
            TrustedActionAttemptAbandoned,
            TrustedActionUserInactive,
        ):
            pass

    pending: asyncio.Task[None] | None = None
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        pending = asyncio.create_task(execute())
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with sessions() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(
                    ToolExecutionModel.task_id == seed.task_id,
                )
            )
            assert execution is not None and execution.request_started_at is not None
            assert execution.write_attempt_count == 1
        if inactive:
            await commit_deletion_barrier(sessions, user_id=seed.user_id)
        before = await database_facts(sessions)
        release.set()
        await asyncio.wait_for(pending, timeout=5)
        if inactive:
            assert_facts_unchanged(before, await database_facts(sessions))
            await execute()
            assert_facts_unchanged(before, await database_facts(sessions))
        else:
            async with sessions() as session:
                execution = await session.scalar(
                    select(ToolExecutionModel).where(
                        ToolExecutionModel.task_id == seed.task_id,
                    )
                )
                assert execution is not None
                assert (
                    execution.status
                    == {
                        "applied": "succeeded",
                        "failed": "confirmed_failed",
                        "retryable": "retryable_failed",
                        "unknown": "reconciling",
                    }[result]
                )
                assert await session.scalar(
                    select(func.count())
                    .select_from(TaskRunModel)
                    .where(
                        TaskRunModel.kind == ("sync_mail" if kind == "mail" else "sync_calendar"),
                    )
                ) == (1 if result == "applied" else 0)
        assert adapter.write_calls == 1 and adapter.reconcile_calls == 0
    finally:
        release.set()
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await workflow.dispose()
        await sessions.dispose()


@pytest.mark.parametrize(
    "mode",
    (
        "unclaimed",
        "abandon",
        "integrity",
        "rejected",
        "oauth-unknown",
        "oauth-known",
        "pre-request",
        "pre-request-claimed",
        "converge",
    ),
)
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_trusted_failure_and_convergence_transactions_respect_barrier(
    database_url: str,
    mode: str,
    inactive: bool,
) -> None:
    """每个独立提交入口使用合法前态和旧快照，屏障后连失败元数据/lease也必须保持不变。

    合成setup仅选择该入口可达的既有持久状态；屏障始终由真实Worker提交，绝不直接
    设置inactive。active对照确认具体终态或租约释放，防止因fixture不满足前提而假通过。
    """
    seed, sessions = _Seed(), build_session_factory(database_url)
    status = (
        ToolExecutionStatus.EXECUTING
        if mode == "abandon"
        else ToolExecutionStatus.CLAIMED
        if mode in {"integrity", "pre-request-claimed"}
        else ToolExecutionStatus.RETRYABLE_FAILED
        if mode.startswith("oauth-")
        else ToolExecutionStatus.RECONCILING
        if mode == "converge"
        else None
    )
    started = NOW - timedelta(seconds=1) if mode == "abandon" or mode.startswith("oauth-") else None
    await _seed_action(
        database_url,
        seed,
        existing_execution_status=status,
        request_started_at=started,
        task_status=TaskStatus.RECONCILING if mode == "converge" else TaskStatus.RUNNING,
        existing_result_summary={"kind": "confirmed_not_applied", "retryable": False}
        if mode.startswith("oauth-")
        else None,
    )
    try:
        async with sessions.begin() as session:
            if mode == "rejected":
                approval = await session.get(ApprovalRequestModel, seed.approval_id)
                assert approval is not None
                approval.status = "rejected"
            if mode == "converge" or mode.startswith("oauth-"):
                execution = await session.get(ToolExecutionModel, seed.execution_id)
                assert execution is not None
                execution.error_code = (
                    "connection_capability_disabled"
                    if mode == "converge"
                    else "google_reauthorization_required"
                )
            if mode == "converge":
                draft = await session.get(MailDraftModel, seed.draft_id)
                task = await session.get(TaskRunModel, seed.task_id)
                assert draft is not None and task is not None
                draft.status = "needs_attention"
                task.error_code = "connection_capability_disabled"
        async with sessions.begin() as session:
            repository = SqlAlchemyTrustedActionRepository(session, ACTION_CIPHER)
            unclaimed = (
                await repository.lock_execution(
                    task_id=seed.task_id,
                    approval_id=seed.approval_id,
                    operation_id=seed.operation_id,
                )
                if mode == "unclaimed"
                else None
            )
            dispatch = (
                await repository.load_dispatch(
                    task_id=seed.task_id,
                    approval_id=seed.approval_id,
                    operation_id=seed.operation_id,
                )
                if status is not None
                else None
            )
        if inactive:
            await commit_deletion_barrier(sessions, user_id=seed.user_id)
        before = await database_facts(sessions)

        async def submit() -> bool | None:
            """保持每个生产入口原有事务归属与None/False/稳定冲突语义。"""
            if mode.startswith("pre-request"):
                return await SqlAlchemyTrustedActionTaskExecutionStore(sessions).finish(
                    task_id=seed.task_id,
                    lease_owner=seed.owner,
                    status=TaskStatus.FAILED,
                    finished_at=NOW,
                    error_code="synthetic_pre_request_failure",
                )
            if mode == "converge":
                return await SqlAlchemyTrustedActionReconciliationRecoveryStore(
                    sessions,
                ).converge_pre_request_reconciliation(task_id=seed.task_id)
            async with sessions.begin() as session:
                repository = SqlAlchemyTrustedActionRepository(session, ACTION_CIPHER)
                if mode == "unclaimed":
                    assert unclaimed is not None
                    await repository.fail_unclaimed_action(
                        snapshot=unclaimed,
                        error_code="synthetic_claim_failure",
                        failed_at=NOW,
                    )
                elif mode == "rejected":
                    await repository.finalize_rejected(
                        task_id=seed.task_id,
                        approval_id=seed.approval_id,
                        operation_id=seed.operation_id,
                        lease_owner=seed.owner,
                        finished_at=NOW,
                    )
                else:
                    assert dispatch is not None
                    if mode == "abandon":
                        return await repository.abandon_started_attempt(
                            snapshot=dispatch,
                            lease_owner=seed.owner,
                        )
                    if mode == "integrity":
                        await repository.fail_claimed_integrity(
                            snapshot=dispatch,
                            failed_at=NOW,
                            lease_owner=seed.owner,
                        )
                    else:
                        await repository.resolve_oauth_write_retry(
                            snapshot=dispatch,
                            lease_owner=seed.owner,
                            ready=None,
                            error_code="oauth_refresh_result_unknown"
                            if mode == "oauth-unknown"
                            else "oauth_credential_state_conflict",
                        )
            return None

        for _ in range(2 if inactive else 1):
            submitted = None
            try:
                submitted = await submit()
            except StateConflictError as error:
                assert inactive and error.error_code == "trusted_action_unavailable"
            if inactive:
                assert_facts_unchanged(before, await database_facts(sessions))
            if mode in {"abandon", "converge", "pre-request", "pre-request-claimed"}:
                expected = not inactive
                assert submitted == expected
        if not inactive:
            async with sessions() as session:
                task = await session.get(TaskRunModel, seed.task_id)
                assert task is not None
                assert task.status == (
                    "running"
                    if mode == "abandon"
                    else "succeeded"
                    if mode == "rejected"
                    else "needs_attention"
                    if mode == "oauth-unknown"
                    else "failed"
                )
                assert task.lease_owner is None
                if mode != "abandon":
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(AuditEventModel)
                            .where(AuditEventModel.task_id == seed.task_id)
                        )
                        > 0
                    )
    finally:
        await sessions.dispose()
