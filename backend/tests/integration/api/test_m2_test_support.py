"""以真实会话和 PostgreSQL 验证测试控制面隔离，不让 Fake 能力变成跨用户后门。"""

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update

from ai_employee.application.ports.trusted_actions import (
    ExecutionReference,
    ProviderWriteOutcomeKind,
)
from ai_employee.config import get_settings
from ai_employee.domain.actions import ToolExecutionStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode, MailSendCommand
from ai_employee.domain.tasks import ApprovalProposal
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.session import build_session_factory  # noqa: F401
from ai_employee.infrastructure.queue.redis_url import validate_test_redis_url
from ai_employee.infrastructure.testing.calendar_restore import (
    SyntheticCalendarRestoreReaderResolver,
)
from ai_employee.infrastructure.testing.m2_sources import seed_m2_source
from ai_employee.infrastructure.testing.scenarios import M2FakeActionAdapter
from ai_employee.infrastructure.testing.trusted_actions import SyntheticTrustedActionRegistry

from .conftest import AuthenticatedApiClients
from .test_action_editors import _headers
from .test_calendar_proposals import (
    _calendar_master_key_file,  # noqa: F401
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]
NOW = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _double_test_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    """只在双测试开关与已验证本地 Redis 下启用控制面，真实写门禁保持关闭。"""
    redis = validate_test_redis_url(os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("APP_TEST_MODE", "true")
    monkeypatch.setenv("REDIS_URL", redis.value)
    for name in ("EXTERNAL_WRITES_ENABLED", "GOOGLE_WRITES_ENABLED", "MICROSOFT_WRITES_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("WRITE_TEST_ACCOUNT_ALLOWLIST", "[]")
    get_settings.cache_clear()


async def _pending(clients: AuthenticatedApiClients, user_id: UUID, *, expired: bool = False) -> UUID:
    """由正式审批 store 冻结 M1 合成提案；fixture 只提供可领取的任务前提。"""
    task_id = uuid4()
    async with clients.session_factory.begin() as session:
        session.add(TaskRunModel(
            id=task_id, user_id=user_id, kind="fake_write", status="running",
            idempotency_key=f"synthetic-support-{task_id}", input_payload={},
            lease_owner="synthetic-support", lease_expires_at=NOW + timedelta(minutes=1),
        ))
    await SqlAlchemyApprovalStore(clients.session_factory).create_or_get_pending(
        task_id=task_id, lease_owner="synthetic-support",
        proposal=ApprovalProposal.create(action="fake.write", payload={}), preview_markdown="Synthetic approval",
        expires_at=datetime(2000, 1, 1, tzinfo=UTC) if expired else NOW + timedelta(days=365),
        checkpoint_recovery_at=NOW,
    )
    return task_id


async def test_expire_support_changes_only_exact_owned_task(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """同用户和外用户已有到期审批均不可被本次精确到期入口顺带扫描修改。"""
    clients = authenticated_api_clients
    target = await _pending(clients, clients.owner_id)
    own_other = await _pending(clients, clients.owner_id, expired=True)
    foreign = await _pending(clients, clients.other_id, expired=True)
    response = await clients.owner.post(
        "/api/v1/test-support/expire-approval", headers=_headers(clients.owner),
        json={"task_id": str(target)},
    )
    assert response.status_code == 204
    async with clients.session_factory() as session:
        statuses = dict((await session.execute(select(
            ApprovalRequestModel.task_id, ApprovalRequestModel.status,
        ))).tuples().all())
    assert statuses == {target: "expired", own_other: "pending", foreign: "pending"}


async def test_test_support_rejects_foreign_task_without_execution_or_state_change(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """真实Cookie/CSRF请求不能驱动另一用户的执行、核对、到期或读取旁证。"""
    clients = authenticated_api_clients
    task_id = await _pending(clients, clients.owner_id)
    for operation in ("execute-task", "reconcile-task", "expire-approval"):
        response = await clients.other.post(
            f"/api/v1/test-support/{operation}", headers=_headers(clients.other),
            json={"task_id": str(task_id)},
        )
        assert response.status_code == 404
    assert (await clients.other.get(f"/api/v1/test-support/m2-evidence/{task_id}")).status_code == 404
    async with clients.session_factory() as session:
        task = await session.get(TaskRunModel, task_id)
        approval = await session.scalar(select(ApprovalRequestModel).where(ApprovalRequestModel.task_id == task_id))
        assert task is not None and task.status == "waiting_approval"
        assert approval is not None and approval.status == "pending"
        assert (await session.scalars(select(ToolExecutionModel))).all() == []


@pytest.mark.parametrize("operation", ["seed-m2-source", "execute-task", "expire-approval", "reconcile-task"])
async def test_new_test_support_writes_require_csrf_and_reject_extra_payload(
    authenticated_api_clients: AuthenticatedApiClients, operation: str,
) -> None:
    """四个写入口既不能接受自报用户/结果，也不能依靠测试环境省略CSRF。"""
    clients = authenticated_api_clients
    path = f"/api/v1/test-support/{operation}"
    payload = {"provider": "google"} if operation == "seed-m2-source" else {"task_id": str(uuid4())}
    assert (await clients.owner.post(path, json=payload)).status_code == 403
    response = await clients.owner.post(path, headers=_headers(clients.owner), json={**payload, "user_id": str(clients.other_id)})
    assert response.status_code == 422


@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize("damage", ["foreign", "identity", "tenant", "email", "revoked", "disconnected", "credentials"])
async def test_synthetic_registry_rejects_non_owned_or_non_synthetic_connection(
    authenticated_api_clients: AuthenticatedApiClients, provider: str, damage: str,
) -> None:
    """真实凭据存在也必须通过归属、精确合成身份和读能力，之后才能解析离线adapter。"""
    clients = authenticated_api_clients
    source = await seed_m2_source(
        sessions=clients.session_factory, master_key_file=get_settings().app_master_key_file,
        user_id=clients.owner_id, provider=provider, clock=lambda: NOW,
    )
    registry = SyntheticTrustedActionRegistry(session_factory=clients.session_factory, settings=get_settings())
    await registry.validate_trusted_action_connection(
        user_id=clients.owner_id, connection_id=source.connection_id, provider=provider, action="calendar.update",
    )
    async with clients.session_factory.begin() as session:
        connection = await session.get(OAuthConnectionModel, source.connection_id)
        assert connection is not None
        if damage == "identity": connection.provider_account_id = "ordinary-synthetic-account"
        if damage == "tenant": connection.provider_tenant_id = "ordinary-synthetic-tenant"
        if damage == "email": connection.account_email = "ordinary@example.test"
        if damage == "disconnected": connection.status = "disconnected"
        if damage == "revoked":
            await session.execute(update(ConnectionCapabilityModel).where(
                ConnectionCapabilityModel.connection_id == source.connection_id,
                ConnectionCapabilityModel.capability == "calendar.read",
            ).values(status="disabled"))
        if damage == "credentials":
            await session.execute(delete(EncryptedCredentialModel).where(EncryptedCredentialModel.connection_id == source.connection_id))
    with pytest.raises(StateConflictError):
        await registry.validate_trusted_action_connection(
            user_id=clients.other_id if damage == "foreign" else clients.owner_id,
            connection_id=source.connection_id, provider=provider, action="calendar.update",
        )


@pytest.mark.parametrize("provider", ["google", "microsoft"])
async def test_missing_fake_ledger_and_calendar_marker_never_fabricate_applied_result(
    authenticated_api_clients: AuthenticatedApiClients, provider: str,
) -> None:
    """没有外部 ledger 时核对返回unknown；即使本地存在旧事件，精确GET也返回不可用。"""
    clients = authenticated_api_clients
    source = await seed_m2_source(
        sessions=clients.session_factory, master_key_file=get_settings().app_master_key_file,
        user_id=clients.owner_id, provider=provider, clock=lambda: NOW,
    )
    operation_id = uuid4()
    command = MailSendCommand(
        schema_version="mail_send.v1", action="mail.send", operation_id=operation_id,
        connection_id=source.connection_id, draft_id=uuid4(), draft_version=1,
        message_date=NOW, mode=MailMode.NEW, source_thread_id=None, source_message_id=None,
        to=("recipient@example.test",), cc=(), bcc=(), subject="Synthetic ledger subject",
        body_text="Synthetic ledger body", thread_headers=None,
    )
    reference = ExecutionReference(
        execution_id=uuid4(), task_id=uuid4(), step_id=uuid4(), approval_id=uuid4(),
        operation_id=operation_id, provider=provider, tool_name="mail.send", idempotency_key=str(operation_id),
        request_payload_hash="a" * 64, status=ToolExecutionStatus.RECONCILING, result_summary=None,
        request_started_at=NOW, write_attempt_count=1, provider_resource_id=None, provider_request_id=None,
        correlation_id=None,
    )
    adapter = M2FakeActionAdapter(redis_url=get_settings().redis_url, user_id=clients.owner_id, provider=provider)
    outcome = await adapter.reconcile(command, reference)
    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert outcome.provider_resource_id is None
    assert (await adapter.observations(operation_id)).write_calls == 0
    reader = await SyntheticCalendarRestoreReaderResolver(clients.session_factory, get_settings()).resolve(
        user_id=clients.owner_id, connection_id=source.connection_id, provider=provider, timezone="UTC",
    )
    assert await reader.get_current_event(source.calendar_id, f"synthetic-event-{source.event_id}") is None
