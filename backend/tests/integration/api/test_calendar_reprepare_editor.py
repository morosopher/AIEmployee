"""通过真实 HTTP/PostgreSQL 验证新版本入口；来源同步使用既有生产用例和合成只读页。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter
from sqlalchemy import func, select, update

from ai_employee.application.commands import trusted_command_hash
from ai_employee.application.ports.calendar import (
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarSyncPage,
)
from ai_employee.application.trusted_action_summary import TrustedActionStepSummary
from ai_employee.application.use_cases.action_views import (
    ActionListPage,
    ActionSnapshot,
    CalendarApprovalPreview,
)
from ai_employee.application.use_cases.sync_calendar import CalendarSyncStore, SyncCalendarUseCase
from ai_employee.config import get_settings
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
)
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.session import build_session_factory  # noqa: F401
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry

from .conftest import AuthenticatedApiClients
from .test_action_editors import _headers
from .test_actions import (
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _synthetic_key,  # noqa: F401
)
from .test_calendar_proposals import _seed_restore_source
from .test_connections import FakeOAuthAdapter

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]
RETAIN_UNTIL = datetime(2031, 1, 1, tzinfo=UTC)
CONFIRMATIONS = ["calendar", "time", "attendees", "notification_policy"]


class _ScopedCalendar:
    """按现有多日历接口提供精确合成页，禁止退回仅支持 primary 的 M1 Fake。"""

    def __init__(self, event: CalendarEvent) -> None:
        self.event = event
        self.calls: list[str] = []

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """本用例只验证已知 scope 的同步；目录空页也不触发任何外部读取。"""
        del cursor
        yield CalendarDirectoryPage((), None, "synthetic-directory", full_snapshot=True)

    async def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """只返回测试已绑定日历，错误 scope 必须让测试明确失败。"""
        assert calendar_id == self.event.calendar_id
        self.calls.append(calendar_id)
        yield CalendarSyncPage((self.event,), None, "synthetic-next")

    async def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """合成增量沿用同一精确 scope；cursor 不进入日志或断言输出。"""
        del cursor
        async for page in self.initial_pages(calendar_id):
            yield page

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """兼容现有只读端口，但此流程必须通过增量同步取得新本地事实。"""
        raise AssertionError("Reprepare must not call an exact provider reader")


async def _update_shell(clients: AuthenticatedApiClients) -> tuple[UUID, httpx.Response]:
    """只从本人本地事件经现有 HTTP shell 入口创建，before 与来源绑定均由生产代码产生。"""
    event_id, _, _ = await _seed_restore_source(clients, retain_until=RETAIN_UNTIL)
    response = await clients.owner.post(
        "/api/v1/calendar/proposals",
        headers=_headers(clients.owner),
        json={"operation_kind": "update", "initialization": "shell", "event_id": str(event_id)},
    )
    assert response.status_code == 201
    assert response.json()["editor_facts"] is None
    return event_id, response


async def test_reprepare_editor_update_shell_requires_independent_confirmation(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """来源字段齐全或保存新标题都不能代替逐项确认；每次确认使用新版本且不覆盖 before。"""
    clients = authenticated_api_clients
    _, response = await _update_shell(clients)
    shell = response.json()
    assert shell["required_confirmations"] == CONFIRMATIONS
    path = f"/api/v1/calendar/proposals/{shell['id']}"
    before = (await clients.owner.get(path)).json()["editor_facts"]["before"]
    saved = await clients.owner.patch(
        path,
        headers=_headers(clients.owner),
        json={"version": 1, "title": "Synthetic independently reviewed update"},
    )
    assert saved.status_code == 200
    assert saved.json()["required_confirmations"] == CONFIRMATIONS
    denied = await clients.owner.post(
        f"{path}/submit", headers=_headers(clients.owner), json={"version": 2}
    )
    assert denied.status_code == 409
    assert denied.json()["error_code"] == "proposal_version_conflict"
    for index, kind in enumerate(CONFIRMATIONS):
        confirmation = {"kind": kind}
        if kind == "calendar":
            confirmation.update(
                connection_id=shell["connection_id"], calendar_id=shell["calendar_id"]
            )
        confirmed = await clients.owner.patch(
            path,
            headers=_headers(clients.owner),
            json={"version": index + 2, "confirmation": confirmation},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["version"] == index + 3
        assert confirmed.json()["required_confirmations"] == CONFIRMATIONS[index + 1 :]
    final = (await clients.owner.get(path)).json()
    assert final["status"] == "editing"
    assert final["editor_facts"]["before"] == before
    async with clients.session_factory() as session:
        for model in (ApprovalRequestModel, ToolExecutionModel):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("shell_first", [True, False])
async def test_reprepare_editor_creation_intent_does_not_mix_shell_and_full_update(
    authenticated_api_clients: AuthenticatedApiClients, shell_first: bool
) -> None:
    """相同来源/字段的 shell 与完整 update 不共用创建哈希；各自精确重放仍沿用原对象。"""
    clients = authenticated_api_clients
    event_id, source = await _update_shell(clients)
    shell_payload = {
        "operation_kind": "update",
        "initialization": "shell",
        "event_id": str(event_id),
    }
    full_payload = {
        "operation_kind": "update",
        "event_id": str(event_id),
        "title": source.json()["title"],
    }
    payload, changed = (
        (shell_payload, full_payload) if shell_first else (full_payload, shell_payload)
    )
    headers = _headers(clients.owner)
    created = await clients.owner.post("/api/v1/calendar/proposals", headers=headers, json=payload)
    assert created.status_code == 201
    replay = await clients.owner.post("/api/v1/calendar/proposals", headers=headers, json=payload)
    assert replay.status_code == 201 and replay.json() == created.json()
    rejected = await clients.owner.post("/api/v1/calendar/proposals", headers=headers, json=changed)
    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "idempotency_key_payload_mismatch"
    assert created.json()["required_confirmations"] == (CONFIRMATIONS if shell_first else [])


async def _seed_stale_history(clients: AuthenticatedApiClients, proposal_id: UUID) -> UUID:
    """播种已确认命令遇到 ETag 冲突后的合成历史，不打开写入开关或调用执行器。

    命令从真实 HTTP 编辑/确认后的不可变内容形成并经过现有哈希与 AEAD 边界；历史行只供
    验证合法操作中心历史可读且重新准备不会改写原审批、执行和审计，不能作为真实执行成功的证据。
    """
    cipher = ActionPayloadCipher(AeadCipher.from_file(get_settings().app_master_key_file))
    task_id, step_id, approval_id, execution_id = (uuid4() for _ in range(4))
    observed_at = datetime(2030, 1, 1, tzinfo=UTC)
    async with clients.session_factory.begin() as session:
        snapshot = await SqlAlchemyCalendarProposalRepository(session, cipher).get_current(
            user_id=clients.owner_id, proposal_id=proposal_id
        )
        assert snapshot is not None
        content = snapshot.desired_snapshot.content
        assert content["required_confirmations"] == []
        command = {
            "schema_version": "calendar_update.v1",
            "action": "calendar.update",
            "operation_id": content["operation_id"],
            "connection_id": str(snapshot.connection_id),
            "calendar_id": snapshot.calendar_id,
            "provider_event_id": snapshot.target_event_id,
            "base_etag": snapshot.base_etag,
            "before_snapshot_id": str(snapshot.before_snapshot_id),
            **{
                field: content[field]
                for field in (
                    "title",
                    "description",
                    "location",
                    "starts_at",
                    "ends_at",
                    "timezone",
                    "all_day",
                    "attendees",
                    "notification_policy",
                    "changed_fields",
                )
            },
        }
        payload_hash = trusted_command_hash(command)
        encrypted = cipher.encrypt_json(
            command,
            user_id=clients.owner_id,
            record_id=approval_id,
            content_kind="approval_command",
            action="calendar.update",
            schema_version="calendar_update.v1",
        )
        operation_id = UUID(str(content["operation_id"]))
        session.add(
            TaskRunModel(
                id=task_id,
                user_id=clients.owner_id,
                kind="trusted_action",
                status="failed",
                idempotency_key=str(task_id),
                input_payload={"approval_id": str(approval_id), "operation_id": str(operation_id)},
                error_code="calendar_event_version_conflict",
            )
        )
        await session.flush()
        session.add(
            TaskStepModel(
                id=step_id,
                task_id=task_id,
                sequence=1,
                name="execute_calendar_update",
                kind="trusted_action",
                status="failed",
                input_summary=TrustedActionStepSummary(
                    action="calendar.update",
                    proposal_version=snapshot.current_version,
                    frozen_connection_id=snapshot.connection_id,
                ).as_json(),
                error_code="calendar_event_version_conflict",
            )
        )
        await session.flush()
        session.add(
            ApprovalRequestModel(
                id=approval_id,
                task_id=task_id,
                step_id=step_id,
                version=1,
                action="calendar.update",
                schema_version="calendar_update.v1",
                risk_level="high",
                payload={"storage": "encrypted"},
                payload_hash=payload_hash,
                payload_ciphertext=encrypted.ciphertext,
                payload_nonce=encrypted.nonce,
                payload_key_version=encrypted.key_version,
                proposal_kind="calendar_proposal",
                proposal_id=proposal_id,
                proposal_version=snapshot.current_version,
                preview_markdown="",
                status="approved",
                expires_at=RETAIN_UNTIL,
                decided_at=observed_at,
                decided_by_user_id=clients.owner_id,
            )
        )
        session.add(
            ToolExecutionModel(
                id=execution_id,
                task_id=task_id,
                step_id=step_id,
                tool_name="calendar.update",
                idempotency_key=f"calendar.update:{task_id}:{approval_id}:1:{operation_id}",
                operation_id=operation_id,
                request_payload_hash=payload_hash,
                provider="google",
                status="failed",
                error_code="calendar_event_version_conflict",
                write_attempt_count=1,
                claimed_at=observed_at,
                request_started_at=observed_at,
                completed_at=observed_at,
                result_summary={"kind": "not_applied", "retryable": False},
            )
        )
        session.add(
            AuditEventModel(
                user_id=clients.owner_id,
                task_id=task_id,
                event_type="tool.failed",
                actor_type="system",
                actor_id="synthetic-worker",
                event_metadata={"error_code": "calendar_event_version_conflict"},
            )
        )
        await session.execute(
            update(CalendarChangeProposalModel)
            .where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == clients.owner_id,
            )
            .values(status="stale")
        )
    return task_id


async def _history_fingerprints(
    clients: AuthenticatedApiClients, *, proposal_id: UUID, task_id: UUID
) -> tuple[str, ...]:
    """比较原对象及全部历史列的摘要；测试失败时不把密文、正文或审批载荷写入输出。"""
    statements = (
        select(CalendarChangeProposalModel.__table__).where(
            CalendarChangeProposalModel.id == proposal_id
        ),
        select(CalendarChangeSnapshotModel.__table__).where(
            CalendarChangeSnapshotModel.proposal_id == proposal_id
        ),
        select(TaskRunModel.__table__).where(TaskRunModel.id == task_id),
        select(TaskStepModel.__table__).where(TaskStepModel.task_id == task_id),
        select(ApprovalRequestModel.__table__).where(ApprovalRequestModel.task_id == task_id),
        select(ToolExecutionModel.__table__).where(ToolExecutionModel.task_id == task_id),
        select(AuditEventModel.__table__).where(AuditEventModel.task_id == task_id),
    )
    async with clients.session_factory() as session:
        fingerprints = []
        for statement in statements:
            rows = (await session.execute(statement)).all()
            assert rows
            fingerprints.append(sha256(repr(sorted(map(repr, rows))).encode("utf-8")).hexdigest())
        return tuple(fingerprints)


async def _history_projection(
    clients: AuthenticatedApiClients,
    *,
    proposal_id: UUID,
    task_id: UUID,
    connection_id: UUID,
    proposal_version: int,
) -> tuple[str, str]:
    """经真实详情和列表验证合成历史可读，并摘要比较冻结内容及其账户归属。

    当前冲突是每次 GET 按本地同步结果重算的只读事实，不属于冻结历史；仅该字段不参与
    前后投影摘要。其余详情、列表和独立的全部数据库列指纹必须保持不变，失败不输出内容。
    """
    detail = await clients.owner.get(f"/api/v1/actions/{task_id}")
    listing = await clients.owner.get(
        "/api/v1/actions",
        params={"item_kind": "trusted_task", "provider": "google", "action": "calendar.update"},
    )
    assert listing.status_code == 200
    assert (detail.status_code, [item["task_id"] for item in listing.json()["items"]]) == (
        200,
        [str(task_id)],
    )
    adapter = TypeAdapter(ActionSnapshot)
    snapshot = adapter.validate_python(detail.json())
    page = ActionListPage.model_validate(listing.json())
    assert snapshot.task_id == task_id and snapshot.status == "failed"
    assert snapshot.action == "calendar.update" and snapshot.provider == "google"
    assert snapshot.local_action is not None and snapshot.local_action.id == proposal_id
    assert snapshot.local_action.version == proposal_version
    assert snapshot.approval is not None
    assert snapshot.approval.proposal_version == proposal_version
    assert snapshot.approval.content_status == "available"
    preview = snapshot.approval.preview
    assert isinstance(preview, CalendarApprovalPreview)
    assert preview.operation == "update" and preview.provider == "google"
    assert snapshot.execution is not None and snapshot.execution.status == "failed"
    assert snapshot.execution.error_code == "calendar_event_version_conflict"
    async with clients.session_factory() as session:
        frozen_account = await session.scalar(
            select(OAuthConnectionModel.account_email).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == clients.owner_id,
            )
        )
    assert frozen_account is not None
    assert (
        sha256(preview.account_email.encode()).digest() == sha256(frozen_account.encode()).digest()
    )
    return (
        sha256(
            adapter.dump_json(snapshot, exclude={"approval": {"preview": {"conflicts"}}})
        ).hexdigest(),
        sha256(page.model_dump_json().encode()).hexdigest(),
    )


@pytest.mark.parametrize(
    ("status", "eligible", "requires_sync"),
    [
        ("editing", True, False),
        ("cancelled", True, False),
        ("stale", True, True),
        ("awaiting_approval", False, False),
        ("executing", False, False),
        ("needs_attention", False, False),
        ("applied", False, False),
    ],
)
async def test_reprepare_editor_projects_only_eligible_update_states(
    authenticated_api_clients: AuthenticatedApiClients,
    status: str,
    eligible: bool,
    requires_sync: bool,
) -> None:
    """当前 ETag 相同只阻断 stale；审批中、执行中、未知结果和已应用状态均不得复制入口。"""
    clients = authenticated_api_clients
    event_id, created = await _update_shell(clients)
    proposal_id = UUID(created.json()["id"])
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(CalendarChangeProposalModel)
            .where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == clients.owner_id,
            )
            .values(status=status)
        )
    path = f"/api/v1/calendar/proposals/{proposal_id}"
    response = await clients.owner.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["editor_facts"]["reprepare_source"] == (
        {"event_id": str(event_id), "requires_sync": requires_sync} if eligible else None
    )
    assert (await clients.other.get(path)).status_code == 404
    async with clients.session_factory() as session:
        for model in (ApprovalRequestModel, ToolExecutionModel):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "damage",
    [
        "empty_source",
        "multiple_sources",
        "provider_id",
        "noncanonical_uuid",
        "uppercase_uuid",
        "missing_source",
        "other_local_event",
        "foreign_event",
        "changed_provider_event",
        "changed_calendar",
        "cleared_before",
        "expired_before",
        "wrong_before_kind",
    ],
)
async def test_reprepare_editor_rejects_unprovable_original_source(
    authenticated_api_clients: AuthenticatedApiClients, damage: str
) -> None:
    """认证密文中的错误来源、本人另一事件及损坏 before 不能借同用户或供应商 ID 猜测。"""
    clients = authenticated_api_clients
    event_id, created = await _update_shell(clients)
    proposal_id = UUID(created.json()["id"])
    replacement_id = uuid4()
    if damage in {"other_local_event", "foreign_event"}:
        owner = clients
        if damage == "foreign_event":
            owner = replace(
                clients,
                owner=clients.other,
                other=clients.owner,
                owner_id=clients.other_id,
                other_id=clients.owner_id,
            )
        replacement_id, _, _ = await _seed_restore_source(owner, retain_until=RETAIN_UNTIL)
    source_values = {
        "empty_source": [],
        "multiple_sources": [str(event_id), str(replacement_id)],
        "provider_id": ["task17-provider-event"],
        "noncanonical_uuid": [event_id.hex],
        "uppercase_uuid": [str(event_id).upper()],
        "missing_source": [str(replacement_id)],
        "other_local_event": [str(replacement_id)],
        "foreign_event": [str(replacement_id)],
    }
    cipher = ActionPayloadCipher(AeadCipher.from_file(get_settings().app_master_key_file))
    async with clients.session_factory.begin() as session:
        if damage in source_values:
            repository = SqlAlchemyCalendarProposalRepository(session, cipher)
            current = await repository.get_current(
                user_id=clients.owner_id, proposal_id=proposal_id
            )
            assert current is not None
            content = dict(current.desired_snapshot.content)
            content["source_event_ids"] = source_values[damage]
            await repository.save_next_version(
                snapshot_id=uuid4(),
                user_id=clients.owner_id,
                proposal_id=proposal_id,
                expected_version=1,
                desired_state=content,
                retain_until=RETAIN_UNTIL,
            )
        if damage in {"changed_provider_event", "changed_calendar"}:
            await session.execute(
                update(CalendarEventModel)
                .where(
                    CalendarEventModel.id == event_id,
                    CalendarEventModel.user_id == clients.owner_id,
                )
                .values(
                    **{
                        "provider_event_id"
                        if damage == "changed_provider_event"
                        else "calendar_id": "synthetic-different-source"
                    }
                )
            )
        if damage in {"cleared_before", "expired_before", "wrong_before_kind"}:
            snapshot = await session.get(
                CalendarChangeSnapshotModel, UUID(created.json()["before_snapshot_id"])
            )
            assert snapshot is not None
            if damage == "cleared_before":
                snapshot.content_ciphertext = None
                snapshot.content_nonce = None
                snapshot.content_key_version = None
            elif damage == "expired_before":
                snapshot.retain_until = datetime(2000, 1, 1, tzinfo=UTC)
            else:
                # 避开现有 desired v1 唯一位置；本例只验证 GET 不能把错误 kind 认证为 before。
                snapshot.version = 99
                snapshot.snapshot_kind = "desired"
    response = await clients.owner.get(f"/api/v1/calendar/proposals/{proposal_id}")
    assert response.status_code == 200
    assert response.json()["editor_facts"]["reprepare_source"] is None


@pytest.mark.parametrize("etag", [None, "", "   "])
async def test_reprepare_editor_requires_sync_when_current_etag_is_unusable(
    authenticated_api_clients: AuthenticatedApiClients, etag: str | None
) -> None:
    """缺失或空白当前 ETag 不能被宣称为可准备；不修改原提案的基础版本。"""
    clients = authenticated_api_clients
    event_id, created = await _update_shell(clients)
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(CalendarEventModel)
            .where(
                CalendarEventModel.id == event_id, CalendarEventModel.user_id == clients.owner_id
            )
            .values(etag=etag)
        )
    response = await clients.owner.get(f"/api/v1/calendar/proposals/{created.json()['id']}")
    assert response.status_code == 200
    assert response.json()["editor_facts"]["reprepare_source"] == {
        "event_id": str(event_id),
        "requires_sync": True,
    }


@pytest.mark.parametrize("with_history", [False, True])
async def test_reprepare_editor_syncs_then_creates_independent_before_and_etag(
    authenticated_api_clients: AuthenticatedApiClients,
    monkeypatch: pytest.MonkeyPatch,
    with_history: bool,
) -> None:
    """真实 GET→既有同步任务/生产同步用例→POST 新 editing；空或已有审批执行历史均不被改写。"""
    clients = authenticated_api_clients
    event_id, created = await _update_shell(clients)
    original = created.json()
    proposal_id = UUID(original["id"])
    connection_id = UUID(original["connection_id"])
    path = f"/api/v1/calendar/proposals/{proposal_id}"
    history_task_id = None
    if with_history:
        reviewed = await clients.owner.patch(
            path,
            headers=_headers(clients.owner),
            json={"version": 1, "title": "Synthetic archived confirmed update"},
        )
        assert reviewed.status_code == 200
        for index, kind in enumerate(CONFIRMATIONS):
            confirmation = {"kind": kind}
            if kind == "calendar":
                confirmation.update(
                    connection_id=original["connection_id"], calendar_id=original["calendar_id"]
                )
            reviewed = await clients.owner.patch(
                path,
                headers=_headers(clients.owner),
                json={"version": index + 2, "confirmation": confirmation},
            )
            assert reviewed.status_code == 200
        original = reviewed.json()
        history_task_id = await _seed_stale_history(clients, proposal_id)
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(CalendarChangeProposalModel)
            .where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == clients.owner_id,
            )
            .values(status="stale")
        )
        session.add(
            SyncCursorModel(
                connection_id=connection_id,
                resource_kind="calendar",
                scope_key=original["calendar_id"],
                cursor=None,
            )
        )
    old = (await clients.owner.get(path)).json()
    history = (
        await _history_fingerprints(clients, proposal_id=proposal_id, task_id=history_task_id)
        if history_task_id is not None
        else None
    )
    history_projection = (
        await _history_projection(
            clients,
            proposal_id=proposal_id,
            task_id=history_task_id,
            connection_id=connection_id,
            proposal_version=original["version"],
        )
        if history_task_id is not None
        else None
    )
    assert old["editor_facts"]["reprepare_source"] == {
        "event_id": str(event_id),
        "requires_sync": True,
    }
    # 旧提案仍拒绝写入；恢复入口绝不把 stale 改回 editing。
    rejected = await clients.owner.patch(
        path,
        headers=_headers(clients.owner),
        json={"version": 1, "title": "Synthetic changed title"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "calendar_proposal_not_editable"
    transport = clients.owner._transport
    assert isinstance(transport, httpx.ASGITransport) and isinstance(transport.app, FastAPI)
    # 仅替换已有 OAuth 供应商边界，手动同步的认证、入队和 Outbox 仍走真实 API/事务。
    monkeypatch.setattr(
        transport.app.state, "oauth_adapters", {"google": FakeOAuthAdapter()}, raising=False
    )
    receipt = await clients.owner.post(
        f"/api/v1/connections/{connection_id}/sync", headers=_headers(clients.owner)
    )
    assert receipt.status_code == 202 and receipt.json()["calendar_task_id"]

    @asynccontextmanager
    async def stores() -> AsyncIterator[CalendarSyncStore]:
        """生产同步仍使用受管短事务，只将外部读取页替换为合成 Fake。"""
        async with clients.session_factory.begin() as session:
            yield SqlAlchemyCalendarSyncRepository(session)

    event = CalendarEvent(
        event_id=original["target_event_id"],
        calendar_id=original["calendar_id"],
        title="Synthetic updated calendar title",
        description="Synthetic local description",
        location="Synthetic room",
        starts_at=datetime(2030, 1, 2, 9, tzinfo=UTC),
        ends_at=datetime(2030, 1, 2, 10, tzinfo=UTC),
        all_day=False,
        transparency="opaque",
        status="confirmed",
        timezone="UTC",
        recurring_event_id=None,
        etag='W/"synthetic-fresh-etag"',
        provider_url="https://calendar.example.test/event",
        access_role="owner",
        can_edit=True,
    )
    reader = _ScopedCalendar(event)
    result = await SyncCalendarUseCase(
        stores,
        ProviderAdapterRegistry(google_calendar=reader),
        AeadCipher.from_file(get_settings().app_master_key_file),
    ).execute(
        user_id=clients.owner_id, connection_id=connection_id, scope_key=original["calendar_id"]
    )
    assert result.events_upserted == 1
    assert reader.calls == [original["calendar_id"]]
    refreshed = (await clients.owner.get(path)).json()
    assert refreshed["editor_facts"]["reprepare_source"] == {
        "event_id": str(event_id),
        "requires_sync": False,
    }
    assert refreshed["editor_facts"]["before"] == old["editor_facts"]["before"]
    headers = _headers(clients.owner)
    payload = {"operation_kind": "update", "initialization": "shell", "event_id": str(event_id)}
    prepared = await clients.owner.post("/api/v1/calendar/proposals", headers=headers, json=payload)
    assert prepared.status_code == 201
    new = prepared.json()
    assert new["id"] != old["id"] and new["status"] == "editing"
    assert new["base_etag"] == event.etag and new["base_etag"] != old["base_etag"]
    assert new["before_snapshot_id"] != old["before_snapshot_id"]
    assert new["changed_fields"] == [] and new["required_confirmations"]
    assert new["editor_facts"] is None
    new_read = (await clients.owner.get(f"/api/v1/calendar/proposals/{new['id']}")).json()
    assert new_read["editor_facts"]["before"]["starts_at"] == event.starts_at.isoformat()
    replay = await clients.owner.post("/api/v1/calendar/proposals", headers=headers, json=payload)
    assert replay.status_code == 201 and replay.json()["id"] == new["id"]
    final_old = (await clients.owner.get(path)).json()
    assert {key: value for key, value in final_old.items() if key != "editor_facts"} == {
        key: value for key, value in old.items() if key != "editor_facts"
    }
    assert final_old["editor_facts"]["before"] == old["editor_facts"]["before"]
    if history_task_id is not None:
        assert (
            await _history_fingerprints(clients, proposal_id=proposal_id, task_id=history_task_id)
            == history
        )
        assert (
            await _history_projection(
                clients,
                proposal_id=proposal_id,
                task_id=history_task_id,
                connection_id=connection_id,
                proposal_version=original["version"],
            )
            == history_projection
        )
    async with clients.session_factory() as session:
        for model in (ApprovalRequestModel, ToolExecutionModel):
            assert await session.scalar(select(func.count()).select_from(model)) == int(
                with_history
            )
