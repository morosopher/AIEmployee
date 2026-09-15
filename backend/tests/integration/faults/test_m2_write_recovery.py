"""四种冻结命令在真实 PostgreSQL 提交边界崩溃后只能安全写一次或只读收敛。"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select

from ai_employee.api.routers.test_support import TestScenarioRequest as ScenarioRequest
from ai_employee.api.routers.test_support import TestScenarioStore as ScenarioStore
from ai_employee.application.use_cases.trusted_actions import (
    TrustedActionAttemptAbandoned,
    TrustedActionExecutionUseCase,
)
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as DatabaseUrl
from ai_employee.infrastructure.db.models.actions import CalendarChangeProposalModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.testing import scenarios
from ai_employee.integrations.registry import ProviderAdapterRegistry
from tests.integration.faults.conftest import M2FaultAction, seed_m2_fault_action
from tests.integration.m2.test_tool_execution_claim import NOW, _settings


@pytest.mark.parametrize("point", ["after_provider_return", "before_queue_ack"])
async def test_m2_worker_process_kill_preserves_request_and_reuses_pending_message(
    database_url: DatabaseUrl, redis_url: str, tmp_path: Path, point: str,
) -> None:
    """真实 Taskiq 子进程被SIGKILL后由同一Redis pending消息恢复；写次数仍严格为一。

    两个进程级演练补充下面的四动作事务矩阵，不把 BaseException 注入误称为 OS 崩溃。
    """
    from tests.integration.faults.m2_process_drill import exercise_worker_crash

    result = await exercise_worker_crash(database_url=database_url, redis_url=redis_url,
                                         point=point, directory=tmp_path)
    assert result["killed_exit"] == -9
    assert result["committed_facts_preserved"] is True
    assert result["write_calls"] == 1 and result["pending_after_recovery"] == 0

pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")
ACTIONS = ("mail.send", "calendar.create", "calendar.update", "calendar.restore")
SCENARIOS = (
    "confirmed_applied", "confirmed_not_applied", "timeout_after_accept", "ambiguous_5xx",
    "delayed_reconciliation_success", "never_resolved", "etag_conflict", "capability_revoked",
    "duplicate_delivery",
)
# 四个明确只读槽位均有独立崩溃案例；不能用一次泛化“核对中断”代表所有预算位置。
CRASH_POINTS = (
    "before_claim", "after_claim_before_request_start_commit",
    "after_request_start_before_response", "after_provider_response_before_result_commit",
    "after_result_commit_before_queue_acknowledgement",
    "during_reconciliation_attempt_1", "during_reconciliation_attempt_2",
    "during_reconciliation_attempt_3", "during_reconciliation_attempt_4",
)
# 请求前后的五个边界分别验证已应用与明确未应用；核对预算四个槽位保持未知结果。
CRASH_CASES = tuple((point, scenario) for point in CRASH_POINTS[:5]
                    for scenario in ("confirmed_applied", "confirmed_not_applied")) + tuple(
    (point, "never_resolved") for point in CRASH_POINTS[5:]
)
# 四种命令各验证失败、超时后核对与重复投递；延迟/5xx共用只读分支选邮件和日程代表。
# ETag仅适用于有版本的update/restore，不向mail/create制造无意义的版本冲突。
SCENARIO_ACTION_CASES = tuple((action, scenario) for action in ACTIONS for scenario in (
    "confirmed_not_applied", "timeout_after_accept", "duplicate_delivery", "capability_revoked",
)) + tuple((action, scenario) for action in ("mail.send", "calendar.update") for scenario in (
    "ambiguous_5xx", "delayed_reconciliation_success",
)) + tuple((action, "etag_conflict") for action in ("calendar.update", "calendar.restore"))


@pytest.fixture(scope="module", name="database_url")
def database_url(m2_fault_database_url: DatabaseUrl) -> DatabaseUrl:
    """覆写通用目标，令每个真实测试都使用官方管理的 regular 临时库。"""
    return m2_fault_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def migrated_database(m2_fault_migrated_database: None) -> Iterator[None]:
    """阻止会话默认迁移触碰原始 anchor。"""
    del m2_fault_migrated_database
    yield


def _registered(scenario: str) -> None:
    """首先验证真正的 HTTP 输入边界；未登记场景必须产生明确业务 RED。"""
    try:
        ScenarioRequest(scenario=scenario)
    except ValidationError:
        pytest.fail("M2 fault scenario is not registered", pytrace=False)


class InjectedCrash(BaseException):
    """模拟进程控制流退出，不能被普通业务异常处理伪装成已知供应商失败。"""


async def _facts(database_url: str, case: M2FaultAction):
    """从独立新会话读取已提交执行、任务和审计，避免 ORM identity map 伪造提交证据。"""
    factory = build_session_factory(database_url)
    try:
        async with factory() as session:
            executions = tuple((await session.scalars(select(ToolExecutionModel).where(
                ToolExecutionModel.task_id == case.task_id,
            ))).all())
            task = await session.get(TaskRunModel, case.task_id)
            audits = tuple((await session.scalars(select(AuditEventModel.event_type).where(
                AuditEventModel.task_id == case.task_id,
            ))).all())
        assert task is not None
        return executions, task, audits
    finally:
        await factory.dispose()


def _workflow(database_url: str, case: M2FaultAction, adapter, *, now=NOW):
    """只替换外部供应商；claim、请求资格、结果事务、核对和尾节点均为生产实现。"""
    factory = build_session_factory(database_url)
    slots = {
        f"{case.provider}_mail_action": adapter,
        f"{case.provider}_calendar_action": adapter,
    }
    return TrustedActionExecutionUseCase(
        transactions=SqlAlchemyTrustedActionRepositoryFactory(factory, case.cipher),
        adapters=ProviderAdapterRegistry(**slots),
        write_policy=_settings(provider=case.provider, tenant="" if case.provider == "google" else "synthetic-tenant", account="synthetic-account" if case.provider == "google" else "synthetic-tenant:synthetic-account"),
        clock=lambda: now,
        dispose=factory.dispose,
    )


async def _resume(database_url: str, case: M2FaultAction, workflow, owner: str, *, now) -> None:
    """按已提交状态取得普通或专用核对租约，绝不把未知结果改回可重发状态。"""
    _, task, _ = await _facts(database_url, case)
    if task.status in {"succeeded", "failed", "needs_attention"}:
        await workflow.execute_or_reconcile(**case.execution_arguments(owner))
        return
    factory = build_session_factory(database_url)
    try:
        if task.status == "reconciling":
            async with factory.begin() as session:
                claimed = await SqlAlchemyTrustedActionRepository(session, case.cipher).claim_reconciliation(
                    task_id=case.task_id, lease_owner=owner, now=now,
                    lease_expires_at=now + timedelta(minutes=1),
                )
            assert claimed is not None
        else:
            lease = await SqlAlchemyTaskExecutionStore(factory).acquire(
                task_id=case.task_id, lease_owner=owner, now=now,
                lease_expires_at=now + timedelta(minutes=1),
            )
            assert lease is not None
            await workflow.claim(**case.execution_arguments(owner))
        try:
            await workflow.execute_or_reconcile(**case.execution_arguments(owner), may_retry_write=False)
        except TrustedActionAttemptAbandoned:
            pass
        await workflow.finalize(**case.execution_arguments(owner), decision="approved")
    finally:
        await factory.dispose()


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.asyncio
async def test_m2_scenario_is_consumed_atomically_once(redis_url: str, scenario: str) -> None:
    """并发消费者只有一个取得场景，另一个没有值；正文或供应商载荷不进入控制面。"""
    import asyncio
    from uuid import uuid4

    _registered(scenario)
    client = Redis.from_url(redis_url)
    user_id = uuid4()
    try:
        store = ScenarioStore(client)
        await store.set(user_id=user_id, scenario=scenario)
        values = await asyncio.gather(store.consume(user_id=user_id), store.consume(user_id=user_id))
        assert sorted(value for value in values if value is not None) == [scenario]
        assert values.count(None) == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize("provider", ("google", "microsoft"))
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("crash_point,scenario", CRASH_CASES)
@pytest.mark.asyncio
async def test_m2_four_actions_converge_after_every_crash_boundary(
    database_url: str, redis_url: str, monkeypatch: pytest.MonkeyPatch,
    provider: str, action: str, crash_point: str, scenario: str,
) -> None:
    """112 个精确边界保持唯一 ToolExecution 与最多一次供应商写，不以崩溃猜测结果。

    每次恢复创建新的用例与会话；事务内故障先执行真实 SQL 再抛出，证明未提交结果确实
    回滚。四个核对槽位分别中断，最后预算耗尽必须保留 UNKNOWN/needs_attention。
    """
    _registered(scenario)
    case = await seed_m2_fault_action(database_url, action=action, provider=provider)
    client = Redis.from_url(redis_url)
    try:
        await ScenarioStore(client).set(user_id=case.user_id, scenario=scenario)
    finally:
        await client.aclose()
    adapter = scenarios.M2FakeActionAdapter(redis_url=redis_url, user_id=case.user_id, provider=provider)
    original_start = SqlAlchemyTrustedActionRepository.mark_request_started
    original_outcome = SqlAlchemyTrustedActionRepository.persist_provider_outcome
    original_execute = adapter.execute
    original_reconcile = adapter.reconcile
    fired = False

    async def start_then_crash(repository, **kwargs):
        """请求开始 SQL 已执行但外层事务未提交时退出；回滚不得授权一次外部请求。"""
        nonlocal fired
        result = await original_start(repository, **kwargs)
        if not fired and crash_point == "after_claim_before_request_start_commit":
            fired = True
            raise InjectedCrash
        return result

    async def response_then_crash(repository, **kwargs):
        """真实结果写入后、commit 前中断，下一会话必须仍视为未知结果。"""
        nonlocal fired
        result = await original_outcome(repository, **kwargs)
        if not fired and crash_point == "after_provider_response_before_result_commit":
            fired = True
            raise InjectedCrash
        return result

    async def accept_then_crash(command):
        """供应商已接收但响应尚未到应用时退出，外部 ledger 保留一次调用。"""
        nonlocal fired
        result = await original_execute(command)
        if not fired and crash_point == "after_request_start_before_response":
            fired = True
            raise InjectedCrash
        return result

    async def reconcile_then_crash(command, execution):
        """在指定只读核对槽位返回前中断；不修改数据库核对预算或业务结果。"""
        nonlocal fired
        result = await original_reconcile(command, execution)
        if not fired and crash_point == f"during_reconciliation_attempt_{execution.reconciliation_attempt_count + 1}":
            fired = True
            raise InjectedCrash
        return result

    monkeypatch.setattr(SqlAlchemyTrustedActionRepository, "mark_request_started", start_then_crash)
    monkeypatch.setattr(SqlAlchemyTrustedActionRepository, "persist_provider_outcome", response_then_crash)
    monkeypatch.setattr(adapter, "execute", accept_then_crash)
    monkeypatch.setattr(adapter, "reconcile", reconcile_then_crash)
    workflow = _workflow(database_url, case, adapter)
    try:
        try:
            if crash_point == "before_claim":
                fired = True
                raise InjectedCrash
            await workflow.claim(**case.execution_arguments("worker-a"))
            await workflow.execute_or_reconcile(**case.execution_arguments("worker-a"), may_retry_write=False)
            await workflow.finalize(**case.execution_arguments("worker-a"), decision="approved")
            if crash_point == "after_result_commit_before_queue_acknowledgement":
                fired = True
                raise InjectedCrash
        except (InjectedCrash, TrustedActionAttemptAbandoned):
            pass
    finally:
        await workflow.dispose()
    executions, _, _ = await _facts(database_url, case)
    assert len(executions) == (0 if crash_point == "before_claim" else 1)
    observations = await adapter.observations(case.operation_id)
    initial_calls = 0 if crash_point in {"before_claim", "after_claim_before_request_start_commit"} else 1
    assert observations.write_calls == initial_calls
    if executions and crash_point != "after_result_commit_before_queue_acknowledgement":
        assert executions[0].status not in {"succeeded", "confirmed_failed"}
    for attempt in range(7):
        # 进程崩溃跳过 finally 时旧专用 lease 会保留；推进注入 Clock 到明确到期之后，
        # 不能手工删租约或用同一时刻让第二 Worker 绕过 takeover 准入。
        recovered_at = NOW + timedelta(minutes=2 * (attempt + 1))
        workflow = _workflow(database_url, case, adapter, now=recovered_at)
        try:
            try:
                await _resume(database_url, case, workflow, f"recovered-worker-{attempt}", now=recovered_at)
            except InjectedCrash:
                pass
        finally:
            await workflow.dispose()
        executions, task, audits = await _facts(database_url, case)
        if task.status in {"succeeded", "failed", "needs_attention"}:
            break
    assert fired
    assert len(executions) == 1 and executions[0].write_attempt_count == 1
    observations = await adapter.observations(case.operation_id)
    assert observations.write_calls == 1
    assert audits.count("tool.claimed") == 1
    if scenario == "never_resolved":
        assert task.status == executions[0].status == "needs_attention"
        assert executions[0].reconciliation_attempt_count == 4
        assert observations.reconcile_calls == 5
        assert executions[0].result_summary["kind"] == "unknown"
    elif scenario == "confirmed_applied":
        assert task.status == executions[0].status == "succeeded"
        assert executions[0].result_summary["kind"] == "confirmed_applied"
    else:
        assert task.status == "failed" and executions[0].status == "confirmed_failed"
        assert executions[0].result_summary["kind"] == "confirmed_not_applied"


@pytest.mark.parametrize("provider", ("google", "microsoft"))
@pytest.mark.parametrize("action,scenario", SCENARIO_ACTION_CASES)
@pytest.mark.asyncio
async def test_m2_scenario_outcomes_use_real_execution_and_read_only_reconciliation(
    database_url: str, redis_url: str, provider: str, action: str, scenario: str,
) -> None:
    """场景必须穿过生产claim/请求/结果事务及专用lease；只设置Redis枚举不构成执行证据。"""
    case = await seed_m2_fault_action(database_url, action=action, provider=provider)
    client = Redis.from_url(redis_url)
    try:
        await ScenarioStore(client).set(user_id=case.user_id, scenario=scenario)
    finally:
        await client.aclose()
    adapter = scenarios.M2FakeActionAdapter(redis_url=redis_url, user_id=case.user_id, provider=provider)
    workflow = _workflow(database_url, case, adapter)
    try:
        await workflow.claim(**case.execution_arguments("worker-a"))
        try:
            await workflow.execute_or_reconcile(**case.execution_arguments("worker-a"), may_retry_write=False)
        except TrustedActionAttemptAbandoned:
            pass
        await workflow.finalize(**case.execution_arguments("worker-a"), decision="approved")
    finally:
        await workflow.dispose()
    for attempt in range(4):
        _, task, _ = await _facts(database_url, case)
        if task.status in {"succeeded", "failed", "needs_attention"}:
            break
        current = NOW + timedelta(minutes=2 * (attempt + 1))
        workflow = _workflow(database_url, case, adapter, now=current)
        try:
            await _resume(database_url, case, workflow, f"scenario-reconciler-{attempt}", now=current)
        finally:
            await workflow.dispose()
    executions, task, audits = await _facts(database_url, case)
    expected_reads = {"timeout_after_accept": 1, "delayed_reconciliation_success": 2, "ambiguous_5xx": 4}.get(scenario, 0)
    expected_kind = "unknown" if scenario == "ambiguous_5xx" else (
        "confirmed_not_applied" if scenario in {"confirmed_not_applied", "etag_conflict", "capability_revoked"}
        else "confirmed_applied"
    )
    assert len(executions) == 1 and executions[0].write_attempt_count == 1
    assert executions[0].result_summary["kind"] == expected_kind
    assert executions[0].reconciliation_attempt_count == expected_reads
    expected_task, expected_execution = {
        "unknown": ("needs_attention", "needs_attention"),
        "confirmed_applied": ("succeeded", "succeeded"),
        "confirmed_not_applied": ("failed", "confirmed_failed"),
    }[expected_kind]
    assert (task.status, executions[0].status) == (expected_task, expected_execution)
    assert audits.count("tool.claimed") == 1
    if scenario in {"etag_conflict", "capability_revoked"}:
        assert task.error_code == {
            "etag_conflict": "calendar_event_version_conflict", "capability_revoked": "connection_scope_missing",
        }[scenario]
    if scenario == "etag_conflict" and action == "calendar.update":
        factory = build_session_factory(database_url)
        try:
            async with factory() as session:
                proposal = await session.scalar(select(CalendarChangeProposalModel)
                    .join(ApprovalRequestModel, ApprovalRequestModel.proposal_id == CalendarChangeProposalModel.id)
                    .where(ApprovalRequestModel.task_id == case.task_id, CalendarChangeProposalModel.user_id == case.user_id))
            assert proposal is not None and proposal.status == "stale"
        finally:
            await factory.dispose()
    # 真正再次投递同一生产执行入口；Fake不做幂等去重，任何重发都会使外部计数超限。
    workflow = _workflow(database_url, case, adapter, now=NOW + timedelta(minutes=9))
    try:
        await workflow.execute_or_reconcile(**case.execution_arguments("duplicate-worker"))
    finally:
        await workflow.dispose()
    external = await adapter.observations(case.operation_id)
    assert external.write_calls == 1 and external.reconcile_calls == expected_reads
