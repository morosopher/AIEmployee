"""验证真实恢复API、受限结果投影和新审批边界；供应商精确读取始终使用合成Fake。"""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from ai_employee.api.sse import _snapshot_event
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import get_settings
from ai_employee.domain.actions import CalendarProposalStatus
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
)
from ai_employee.infrastructure.db.models.sources import CalendarEventModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.session import build_session_factory  # noqa: F401
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.workers.prepare_calendar_restore import PrepareCalendarRestoreTaskStep

from ..m2.test_calendar_proposal_versions import (
    _AssertingReader,
    _current_provider_event,
    _install_probe,
    _ReaderResolver,
    _TransactionProbe,
)
from .conftest import AuthenticatedApiClients
from .test_action_editors import _headers
from .test_actions import (
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _synthetic_key,  # noqa: F401
)
from .test_calendar_proposals import _seed_restore_source

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]
NOW = datetime(2030, 1, 1, tzinfo=UTC)
RETAIN_UNTIL = datetime(2031, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class _PreparedRestore:
    """只携带合成记录标识，避免把正文或凭据带到断言和证据输出。"""

    task_id: UUID
    proposal_id: UUID
    event_id: UUID
    source_snapshot_id: UUID


async def _prepare_restore(clients: AuthenticatedApiClients) -> _PreparedRestore:
    """走真实202入队与生产准备步骤；Fixture只赋予合成租约和明确终态，不运行真实供应商。"""
    event_id, source_snapshot_id, _ = await _seed_restore_source(clients, retain_until=RETAIN_UNTIL)
    response = await clients.owner.post(
        f"/api/v1/calendar/events/{event_id}/restore-proposal",
        headers=_headers(clients.owner),
        json={"snapshot_id": str(source_snapshot_id)},
    )
    assert response.status_code == 202
    assert response.headers["Cache-Control"] == "no-store"
    assert set(response.json()) == {"task_id", "status"}
    assert response.json()["status"] == "queued"
    task_id = UUID(response.json()["task_id"])
    async with clients.session_factory.begin() as session:
        task = await session.get(TaskRunModel, task_id)
        assert task is not None and task.result_payload is None
        task.status = "running"
        task.lease_owner = "synthetic-restore-editor-worker"
        task.lease_expires_at = NOW + timedelta(minutes=1)
        payload = dict(task.input_payload)
        assert await session.scalar(select(func.count()).select_from(ApprovalRequestModel)) == 0
        assert await session.scalar(select(func.count()).select_from(ToolExecutionModel)) == 0
        assert (
            await session.scalar(
                select(CalendarChangeProposalModel.id).where(
                    CalendarChangeProposalModel.user_id == clients.owner_id,
                    CalendarChangeProposalModel.creation_idempotency_key
                    == payload["creation_idempotency_key"],
                )
            )
            is None
        )
    probe = _TransactionProbe()
    remove_probe = _install_probe(clients.session_factory.engine, probe)
    current = replace(
        _current_provider_event(),
        calendar_id="task17-calendar",
        event_id="task17-provider-event",
        etag='W/"task17-etag"',
    )
    reader = _AssertingReader(probe, current)
    try:
        await PrepareCalendarRestoreTaskStep(
            clients.session_factory,
            action_cipher=ActionPayloadCipher(
                AeadCipher.from_file(get_settings().app_master_key_file)
            ),
            source_cipher=AeadCipher.from_file(get_settings().app_master_key_file),
            reader_resolver=_ReaderResolver(reader),
            clock=lambda: NOW,
        ).execute(
            LeasedTask(
                task_id=task_id,
                user_id=clients.owner_id,
                kind="calendar.restore.prepare",
                input_payload=payload,
                started_at=NOW,
                lease_owner="synthetic-restore-editor-worker",
            )
        )
    finally:
        remove_probe()
    assert reader.calls == [("task17-calendar", "task17-provider-event")]
    assert probe.active == 0
    async with clients.session_factory.begin() as session:
        task = await session.get(TaskRunModel, task_id)
        assert task is not None and task.result_payload is not None
        proposal_id = UUID(str(task.result_payload["calendar_proposal_id"]))
        task.status = "succeeded"
        task.lease_owner = None
        task.lease_expires_at = None
    return _PreparedRestore(task_id, proposal_id, event_id, source_snapshot_id)


async def test_restore_editor_source_returns_local_identity_and_never_enqueues_on_get(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """编辑GET只返回原before和本地UUID；读取不会创建准备任务、审批或工具执行。"""
    clients = authenticated_api_clients
    event_id, snapshot_id, proposal_id = await _seed_restore_source(
        clients, retain_until=RETAIN_UNTIL
    )
    path = f"/api/v1/calendar/proposals/{proposal_id}"
    response = await clients.owner.get(path)
    assert response.status_code == 200
    assert response.json()["target_event_id"] == "task17-provider-event"
    assert response.json()["editor_facts"]["restore_source"] == {
        "event_id": str(event_id),
        "snapshot_id": str(snapshot_id),
    }
    assert (await clients.other.get(path)).status_code == 404
    async with clients.session_factory() as session:
        for model in (TaskRunModel, ApprovalRequestModel, ToolExecutionModel):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "unavailable", ["editing", "restore", "expired", "cleared", "missing_event"]
)
async def test_restore_editor_hides_ineligible_or_expired_source(
    authenticated_api_clients: AuthenticatedApiClients,
    unavailable: str,
) -> None:
    """已失效来源保持明确不可用，不从provider ID或当前事件内容猜测可恢复身份。"""
    clients = authenticated_api_clients
    event_id, snapshot_id, proposal_id = await _seed_restore_source(
        clients,
        retain_until=RETAIN_UNTIL,
        proposal_status=CalendarProposalStatus.EDITING
        if unavailable == "editing"
        else CalendarProposalStatus.APPLIED,
        proposal_operation="restore" if unavailable == "restore" else "update",
    )
    async with clients.session_factory.begin() as session:
        if unavailable in {"expired", "cleared"}:
            values = (
                {"retain_until": datetime(2000, 1, 1, tzinfo=UTC)}
                if unavailable == "expired"
                else {
                    "content_ciphertext": None,
                    "content_nonce": None,
                    "content_key_version": None,
                }
            )
            await session.execute(
                update(CalendarChangeSnapshotModel)
                .where(
                    CalendarChangeSnapshotModel.id == snapshot_id,
                    CalendarChangeSnapshotModel.user_id == clients.owner_id,
                )
                .values(**values)
            )
        if unavailable == "missing_event":
            await session.execute(
                update(CalendarEventModel)
                .where(
                    CalendarEventModel.id == event_id,
                    CalendarEventModel.user_id == clients.owner_id,
                )
                .values(provider_event_id="synthetic-different-provider-event")
            )
    response = await clients.owner.get(f"/api/v1/calendar/proposals/{proposal_id}")
    assert response.status_code == 200
    assert response.json()["editor_facts"]["restore_source"] is None


async def test_restore_editor_submission_requires_explicit_notification_confirmation(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """直接到达真实submit边界，防止结果投影断言提前失败而掩盖通知的RED。"""
    clients = authenticated_api_clients
    result = await _prepare_restore(clients)
    rejected = await clients.owner.post(
        f"/api/v1/calendar/proposals/{result.proposal_id}/submit",
        headers=_headers(clients.owner),
        json={"version": 1},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "proposal_version_conflict"


async def test_restore_editor_real_prepare_result_matches_get_sse_and_requires_new_confirmation(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """真实Worker marker经同一GET/SSE投影恢复精确链接，准备本身不能创建审批。"""
    clients = authenticated_api_clients
    result = await _prepare_restore(clients)
    task_path = f"/api/v1/tasks/{result.task_id}"
    response = await clients.owner.get(task_path)
    assert response.status_code == 200
    assert response.json()["calendar_restore_proposal_id"] == str(result.proposal_id)
    assert "result_payload" not in response.json() and "input_payload" not in response.json()
    assert (await clients.other.get(task_path)).status_code == 404
    transport = clients.owner._transport
    assert isinstance(transport, httpx.ASGITransport) and isinstance(transport.app, FastAPI)
    snapshot = await transport.app.state.get_task_use_case.execute(
        task_id=result.task_id, user_id=clients.owner_id
    )
    assert snapshot is not None
    event = _snapshot_event(
        task_id=result.task_id, snapshot=snapshot, event_id=snapshot.event_cursor
    )
    assert json.loads(event.data)["payload"]["calendar_restore_proposal_id"] == str(
        result.proposal_id
    )
    proposal_path = f"/api/v1/calendar/proposals/{result.proposal_id}"
    proposal = (await clients.owner.get(proposal_path)).json()
    assert proposal["operation_kind"] == "restore" and proposal["status"] == "editing"
    assert proposal["required_confirmations"] == ["notification_policy"]
    rejected = await clients.owner.post(
        f"{proposal_path}/submit",
        headers=_headers(clients.owner),
        json={"version": proposal["version"]},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "proposal_version_conflict"
    confirmed = await clients.owner.patch(
        proposal_path,
        headers=_headers(clients.owner),
        json={"version": proposal["version"], "confirmation": {"kind": "notification_policy"}},
    )
    assert confirmed.status_code == 200 and confirmed.json()["required_confirmations"] == []
    # 读取任务仍验证初始v1；用户确认推进当前版本后，原准备结果不能失去绑定。
    assert (await clients.owner.get(task_path)).json()["calendar_restore_proposal_id"] == str(
        result.proposal_id
    )
    async with clients.session_factory() as session:
        for model in (ApprovalRequestModel, ToolExecutionModel):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "invalid",
    [
        "kind",
        "running",
        "extra_result",
        "bad_uuid",
        "extra_input",
        "wrong_key",
        "same_event_other_before",
        "cleared_initial",
        "bad_creation_hash",
        "foreign_task",
    ],
)
async def test_restore_editor_task_result_rejects_broken_persistent_binding(
    authenticated_api_clients: AuthenticatedApiClients,
    invalid: str,
) -> None:
    """坏marker、同事件不同before、密文清除和跨用户结果均不给猜测链接。"""
    clients = authenticated_api_clients
    result = await _prepare_restore(clients)
    other_before = None
    if invalid == "same_event_other_before":
        other = await clients.owner.post(
            "/api/v1/calendar/proposals",
            headers=_headers(clients.owner),
            json={
                "operation_kind": "update",
                "initialization": "shell",
                "event_id": str(result.event_id),
            },
        )
        assert other.status_code == 201
        other_before = other.json()["before_snapshot_id"]
        assert other_before != str(result.source_snapshot_id)
    read_task_id = result.task_id
    async with clients.session_factory.begin() as session:
        task = await session.get(TaskRunModel, result.task_id)
        proposal = await session.get(CalendarChangeProposalModel, result.proposal_id)
        assert task is not None and proposal is not None
        if invalid == "kind":
            task.kind = "daily_brief"
        elif invalid == "running":
            task.status = "running"
        elif invalid == "extra_result":
            task.result_payload = {"calendar_proposal_id": str(result.proposal_id), "extra": True}
        elif invalid == "bad_uuid":
            task.result_payload = {"calendar_proposal_id": "synthetic-provider-event"}
        elif invalid == "extra_input":
            task.input_payload = {
                **task.input_payload,
                "snapshot_id": str(result.source_snapshot_id),
            }
        elif invalid == "wrong_key":
            proposal.creation_idempotency_key = f"synthetic-unrelated-{uuid4()}"
        elif invalid == "same_event_other_before":
            task.input_payload = {**task.input_payload, "source_snapshot_id": other_before}
        elif invalid == "bad_creation_hash":
            proposal.creation_payload_hash = "0" * 64
        elif invalid == "foreign_task":
            # 原任务的审计组合外键不允许换主人。创建另一合法用户任务来检验结果隔离，
            # 而不是修改原任务/审计归属或规避该数据库约束。
            read_task_id = uuid4()
            session.add(
                TaskRunModel(
                    id=read_task_id,
                    user_id=clients.other_id,
                    kind=task.kind,
                    status=task.status,
                    idempotency_key=f"synthetic-foreign-result-{read_task_id}",
                    input_payload=dict(task.input_payload),
                    result_payload=dict(task.result_payload or {}),
                )
            )
        elif invalid == "cleared_initial":
            await session.execute(
                update(CalendarChangeSnapshotModel)
                .where(
                    CalendarChangeSnapshotModel.proposal_id == result.proposal_id,
                    CalendarChangeSnapshotModel.user_id == clients.owner_id,
                    CalendarChangeSnapshotModel.version == 1,
                    CalendarChangeSnapshotModel.snapshot_kind == "desired",
                )
                .values(content_ciphertext=None, content_nonce=None, content_key_version=None)
            )
    reader = clients.other if invalid == "foreign_task" else clients.owner
    response = await reader.get(f"/api/v1/tasks/{read_task_id}")
    assert response.status_code == 200
    assert response.json()["calendar_restore_proposal_id"] is None
