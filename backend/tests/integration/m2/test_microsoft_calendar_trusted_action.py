"""在真实 PostgreSQL 上验证 Microsoft Calendar adapter 的可信执行闭环。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select, update

from ai_employee.application.use_cases.trusted_actions import (
    TrustedActionAttemptAbandoned,
    TrustedActionExecutionUseCase,
)
from ai_employee.config import Settings
from ai_employee.domain.actions import ToolExecutionStatus
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    canonical_provider_identity_key,
)
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl as ValidatedTestDatabaseUrl,
)
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.integrations.microsoft.calendar_write import (
    MICROSOFT_GRAPH_BASE_URL,
    MicrosoftCalendarWriteAdapter,
)
from ai_employee.integrations.registry import ProviderAdapterRegistry

# Task 19 已经集中维护完整且合成的可信动作外键骨架；Task 20 的 reconciliation
# 集成测试也复用同一组 helper。这里仅把连接切换为 Microsoft 并使用真实 adapter，
# 避免为 Task 24 再复制一套审批、加密命令和 ToolExecution 建模逻辑。
from tests.integration.m2.test_tool_execution_claim import (
    ACTION_CIPHER,
    NOW,
    _Seed,
    _seed_calendar_action,
)

CALENDAR_ID = "calendar-primary"
EVENTS_URL = f"{MICROSOFT_GRAPH_BASE_URL}/me/calendars/{CALENDAR_ID}/events"
CALENDAR_VIEW_URL = f"{MICROSOFT_GRAPH_BASE_URL}/me/calendars/{CALENDAR_ID}/calendarView"

# 本模块与其他 Cycle 5 动作集成测试共用受 provenance 保护的 regular 数据库；
# tracked fixture 会在异常路径先释放本模块创建的全部 pool，再清理应用表。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """把 Microsoft 可信动作测试绑定到已迁移的 disposable regular 数据库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """覆盖通用迁移 fixture，禁止把 roles-absent anchor 当作普通目标升级。"""
    del cycle5_regular_database_url
    yield


async def _seed_microsoft_create(database_url: str) -> tuple[_Seed, str]:
    """创建一条仅含合成账号与内容的已批准 Microsoft 日历创建动作。"""
    seed = _Seed()
    seed.provider = "microsoft"
    seed.provider_tenant_id = "synthetic-tenant"
    # Microsoft 稳定账户 ID 必须绑定同一 tenant；该值不是邮箱或真实 Graph 标识。
    seed.provider_account_id = "synthetic-tenant:graph-user"
    seed.account_type = "work_school"
    payload_hash = await _seed_calendar_action(
        database_url,
        seed,
        calendar_id=CALENDAR_ID,
    )
    return seed, payload_hash


def _write_settings(
    seed: _Seed,
    *,
    external_enabled: bool = True,
    provider_enabled: bool = True,
    allowed_account_id: str | None = None,
) -> Settings:
    """按参数构造 Microsoft 全局、供应商与精确测试账户写入门禁。"""
    return Settings(
        _env_file=None,
        app_env="staging",
        external_writes_enabled=external_enabled,
        microsoft_writes_enabled=provider_enabled,
        write_test_account_allowlist=[
            canonical_provider_identity_key(
                seed.provider,
                seed.provider_tenant_id,
                allowed_account_id or seed.provider_account_id,
            )
        ],
    )


def _workflow(
    database_url: str,
    seed: _Seed,
    *,
    settings: Settings,
) -> TrustedActionExecutionUseCase:
    """显式组合真实事务工厂、Microsoft adapter、registry 与确定性时钟。"""
    session_factory = build_session_factory(database_url)
    adapter = MicrosoftCalendarWriteAdapter(
        connection_id=seed.connection_id,
        access_token="synthetic-access-token",
    )
    return TrustedActionExecutionUseCase(
        transactions=SqlAlchemyTrustedActionRepositoryFactory(
            session_factory,
            ACTION_CIPHER,
        ),
        adapters=ProviderAdapterRegistry(microsoft_calendar_action=adapter),
        write_policy=settings,
        clock=lambda: NOW,
        dispose=session_factory.dispose,
    )


def _matching_graph_event(seed: _Seed) -> dict[str, object]:
    """返回与冻结创建命令逐字段一致的完整合成 Graph event。"""
    return {
        "id": "synthetic-event",
        "transactionId": str(seed.operation_id),
        "subject": "Synthetic meeting",
        "body": {"contentType": "text", "content": "Synthetic description"},
        "location": {"displayName": "Synthetic room"},
        "start": {"dateTime": "2030-01-01T01:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2030-01-01T02:00:00", "timeZone": "UTC"},
        "isAllDay": False,
        "attendees": [
            {
                "type": "required",
                "emailAddress": {"address": "owner@example.test"},
            }
        ],
        "type": "singleInstance",
        "seriesMasterId": None,
        "recurrence": None,
        "isCancelled": False,
        "isOrganizer": True,
        "canEdit": True,
        "@odata.etag": 'W/"synthetic-version"',
        "changeKey": "synthetic-version",
        "webLink": "https://outlook.example.test/synthetic-event",
    }


async def _claim_reconciliation(
    database_url: str,
    seed: _Seed,
    *,
    scheduled_for: object,
) -> None:
    """用真实事务工厂为到期任务取得一次只读核对租约。"""
    # scheduled_for 来自带时区的 PostgreSQL TaskRun 投影；局部断言先把动态 ORM
    # 类型收窄，避免测试用宿主机时间猜测核对到期点。
    assert isinstance(scheduled_for, datetime)
    session_factory = build_session_factory(database_url)
    try:
        transactions = SqlAlchemyTrustedActionRepositoryFactory(
            session_factory,
            ACTION_CIPHER,
        )
        async with transactions() as transaction:
            snapshot = await transaction.claim_reconciliation(
                task_id=seed.task_id,
                lease_owner="microsoft-reconciliation-worker",
                now=scheduled_for,
                lease_expires_at=scheduled_for + timedelta(minutes=1),
            )
        assert snapshot is not None
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_unknown_create_reconciles_through_persisted_single_execution(
    database_url: str,
) -> None:
    """真实 claim、unknown 与只读恢复只能产生一行执行事实和一次 Graph POST。"""
    seed, payload_hash = await _seed_microsoft_create(database_url)
    create_route = respx.post(EVENTS_URL).mock(return_value=httpx.Response(503))
    view_route = respx.get(CALENDAR_VIEW_URL).mock(
        return_value=httpx.Response(
            200,
            json={"value": [_matching_graph_event(seed)]},
            headers={"request-id": "synthetic-reconciliation-request"},
        )
    )
    workflow = _workflow(database_url, seed, settings=_write_settings(seed))
    try:
        # 同一冻结审批被重复交付时，第二次 claim 必须复用唯一 ToolExecution。
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )

        session_factory = build_session_factory(database_url)
        try:
            async with session_factory() as session:
                approval = await session.get(ApprovalRequestModel, seed.approval_id)
                claimed_executions = tuple(
                    (
                        await session.scalars(
                            select(ToolExecutionModel).where(
                                ToolExecutionModel.task_id == seed.task_id
                            )
                        )
                    ).all()
                )
                claimed_audits = await session.scalar(
                    select(func.count())
                    .select_from(AuditEventModel)
                    .where(
                        AuditEventModel.task_id == seed.task_id,
                        AuditEventModel.event_type == "tool.claimed",
                    )
                )
        finally:
            await session_factory.dispose()
        assert approval is not None and approval.payload_hash == payload_hash
        assert len(claimed_executions) == 1
        assert claimed_executions[0].request_payload_hash == payload_hash
        assert claimed_audits == 1

        # Graph 已接收 POST 后返回 5xx，可信链只能落库 unknown 并放弃通用完成路径。
        with pytest.raises(TrustedActionAttemptAbandoned):
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )

        session_factory = build_session_factory(database_url)
        try:
            async with session_factory() as session:
                reconciling_task = await session.get(TaskRunModel, seed.task_id)
                reconciling_execution = await session.scalar(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                )
        finally:
            await session_factory.dispose()
        assert reconciling_task is not None
        assert reconciling_task.status == TaskStatus.RECONCILING.value
        assert reconciling_task.scheduled_for is not None
        assert reconciling_execution is not None
        assert reconciling_execution.status == ToolExecutionStatus.RECONCILING.value
        assert reconciling_execution.write_attempt_count == 1
        assert create_route.call_count == 1
        assert view_route.call_count == 0

        await _claim_reconciliation(
            database_url,
            seed,
            scheduled_for=reconciling_task.scheduled_for,
        )
        await workflow.reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner="microsoft-reconciliation-worker",
        )

        # 已确认终态的普通重复投递和专用核对重放都只能复用 PostgreSQL 结论。
        await workflow.execute_or_reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner="duplicate-delivery-worker",
        )
        await workflow.reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner="duplicate-reconciliation-worker",
        )

        session_factory = build_session_factory(database_url)
        try:
            async with session_factory() as session:
                final_task = await session.get(TaskRunModel, seed.task_id)
                final_executions = tuple(
                    (
                        await session.scalars(
                            select(ToolExecutionModel).where(
                                ToolExecutionModel.task_id == seed.task_id
                            )
                        )
                    ).all()
                )
        finally:
            await session_factory.dispose()
    finally:
        await workflow.dispose()

    assert final_task is not None and final_task.status == TaskStatus.SUCCEEDED.value
    assert len(final_executions) == 1
    assert final_executions[0].status == ToolExecutionStatus.SUCCEEDED.value
    assert final_executions[0].write_attempt_count == 1
    assert final_executions[0].reconciliation_attempt_count == 1
    assert create_route.call_count == 1
    assert view_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_payload_hash_mismatch_fails_before_claim_or_graph_request(
    database_url: str,
) -> None:
    """Graph/checkpoint 哈希不匹配时保留冻结哈希，且不创建执行或调用供应商。"""
    seed, payload_hash = await _seed_microsoft_create(database_url)
    graph_route = respx.route().mock(return_value=httpx.Response(500))
    workflow = _workflow(database_url, seed, settings=_write_settings(seed))
    try:
        with pytest.raises(StateConflictError) as raised:
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash="f" * 64,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "trusted_action_unavailable"

        session_factory = build_session_factory(database_url)
        try:
            async with session_factory() as session:
                task = await session.get(TaskRunModel, seed.task_id)
                approval = await session.get(ApprovalRequestModel, seed.approval_id)
                execution_count = await session.scalar(
                    select(func.count())
                    .select_from(ToolExecutionModel)
                    .where(ToolExecutionModel.task_id == seed.task_id)
                )
        finally:
            await session_factory.dispose()
    finally:
        await workflow.dispose()

    assert task is not None
    assert task.status == TaskStatus.FAILED.value
    assert task.error_code == "trusted_action_unavailable"
    assert approval is not None and approval.payload_hash == payload_hash
    assert execution_count == 0
    assert graph_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("gate", "expected_error_code"),
    (
        ("default_disabled", "external_writes_disabled"),
        ("global_disabled", "external_writes_disabled"),
        ("provider_disabled", "external_writes_disabled"),
        ("account_not_allowed", "external_write_account_not_allowed"),
        ("connection_disconnected", "connection_capability_disabled"),
        ("calendar_read_revoked", "connection_capability_disabled"),
        ("calendar_write_revoked", "connection_scope_missing"),
    ),
)
async def test_claim_gate_failure_persists_without_any_graph_request(
    database_url: str,
    gate: str,
    expected_error_code: str,
) -> None:
    """任一写开关、连接或 calendar capability 撤销都必须在 Graph 前失败。"""
    seed, payload_hash = await _seed_microsoft_create(database_url)
    if gate == "default_disabled":
        settings = Settings(_env_file=None, app_env="staging")
    elif gate == "global_disabled":
        settings = _write_settings(seed, external_enabled=False)
    elif gate == "provider_disabled":
        settings = _write_settings(seed, provider_enabled=False)
    elif gate == "account_not_allowed":
        settings = _write_settings(
            seed,
            allowed_account_id="synthetic-tenant:other-graph-user",
        )
    else:
        settings = _write_settings(seed)
        session_factory = build_session_factory(database_url)
        try:
            async with session_factory.begin() as session:
                if gate == "connection_disconnected":
                    await session.execute(
                        update(OAuthConnectionModel)
                        .where(OAuthConnectionModel.id == seed.connection_id)
                        .values(status="disconnected")
                    )
                else:
                    capability = (
                        ConnectionCapability.CALENDAR_READ
                        if gate == "calendar_read_revoked"
                        else ConnectionCapability.CALENDAR_WRITE
                    )
                    values: dict[str, object] = {
                        "status": CapabilityStatus.REVOKED.value,
                        "actual_scopes": [],
                    }
                    if capability is ConnectionCapability.CALENDAR_WRITE:
                        # write scope 撤销使用统一 reauthorization 信号；read capability
                        # 缺失则保持普通 capability-disabled 分类。
                        values["last_error_code"] = "connection_scope_missing"
                    await session.execute(
                        update(ConnectionCapabilityModel)
                        .where(
                            ConnectionCapabilityModel.connection_id == seed.connection_id,
                            ConnectionCapabilityModel.capability == capability.value,
                        )
                        .values(**values)
                    )
        finally:
            await session_factory.dispose()

    graph_route = respx.route().mock(return_value=httpx.Response(500))
    workflow = _workflow(database_url, seed, settings=settings)
    try:
        with pytest.raises(StateConflictError) as raised:
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == expected_error_code

        session_factory = build_session_factory(database_url)
        try:
            async with session_factory() as session:
                task = await session.get(TaskRunModel, seed.task_id)
                approval = await session.get(ApprovalRequestModel, seed.approval_id)
                execution_count = await session.scalar(
                    select(func.count())
                    .select_from(ToolExecutionModel)
                    .where(ToolExecutionModel.task_id == seed.task_id)
                )
        finally:
            await session_factory.dispose()
    finally:
        await workflow.dispose()

    assert task is not None
    assert task.status == TaskStatus.FAILED.value
    assert task.error_code == expected_error_code
    assert approval is not None and approval.status == ApprovalStatus.INVALIDATED.value
    assert execution_count == 0
    assert graph_route.call_count == 0
