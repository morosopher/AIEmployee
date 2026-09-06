"""验证统一动作 REST 的精确冻结预览、用户隔离及人工 CAS 边界。"""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from ai_employee.application.commands import trusted_command_hash
from ai_employee.config import get_settings
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.actions import MailDraftModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher

from .conftest import AuthenticatedApiClients
from .test_mail_drafts import _seed_send_connection

pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")
NOW = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """只使用官方 orchestrator 创建的 regular disposable database。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """复用受准入保护的迁移，不把 anchor 当作普通测试库。"""
    del cycle5_regular_database_url
    yield


@pytest.fixture(autouse=True)
def _synthetic_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """注入仅用于合成内容的临时密钥文件，所有真实写开关保持默认关闭。"""
    import base64

    key_file = tmp_path / "action-view-key"
    key_file.write_bytes(base64.b64encode(b"m" * 32))
    monkeypatch.setenv("APP_MASTER_KEY_FILE", str(key_file))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass(frozen=True)
class _Action:
    """保存测试断言需要的精确对象身份，避免混淆 TaskRun 与 ToolExecution。"""

    task_id: UUID
    draft_id: UUID
    approval_id: UUID
    execution_id: UUID
    connection_id: UUID


async def _seed_mail_action(
    clients: AuthenticatedApiClients, *, needs_attention: bool = False
) -> _Action:
    """经真实草稿 API 创建本地内容，再播种不触达外部网络的冻结审批事实。"""
    connection_id = await _seed_send_connection(
        clients, provider_account_id=f"synthetic-account-{uuid4()}"
    )
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": str(uuid4()),
        },
        json={
            "connection_id": str(connection_id),
            "to": ["to@example.test"],
            "cc": ["cc@example.test"],
            "bcc": ["bcc@example.test"],
            "subject": "Synthetic subject",
            "body_text": "Synthetic body",
        },
    )
    assert created.status_code == 201
    draft_id = UUID(created.json()["id"])
    task_id, step_id, approval_id, execution_id, operation_id = (uuid4() for _ in range(5))
    command = {
        "schema_version": "mail_send.v1",
        "action": "mail.send",
        "operation_id": str(operation_id),
        "connection_id": str(connection_id),
        "draft_id": str(draft_id),
        "draft_version": 1,
        "message_date": "2030-01-01T00:00:00Z",
        "mode": "new",
        "source_thread_id": None,
        "source_message_id": None,
        "thread_headers": None,
        "to": ["to@example.test"],
        "cc": ["cc@example.test"],
        "bcc": ["bcc@example.test"],
        "subject": "Synthetic subject",
        "body_text": "Synthetic body",
    }
    encrypted = ActionPayloadCipher.from_key(b"m" * 32).encrypt_json(
        command,
        user_id=clients.owner_id,
        record_id=approval_id,
        content_kind="approval_command",
        action="mail.send",
        schema_version="mail_send.v1",
    )
    payload_hash = trusted_command_hash(command)
    async with clients.session_factory.begin() as session:
        session.add(
            TaskRunModel(
                id=task_id,
                user_id=clients.owner_id,
                kind="trusted_action",
                status="needs_attention" if needs_attention else "waiting_approval",
                idempotency_key=str(task_id),
                input_payload={"approval_id": str(approval_id), "operation_id": str(operation_id)},
            )
        )
        await session.flush()
        session.add(
            TaskStepModel(
                id=step_id,
                task_id=task_id,
                sequence=1,
                name="await_approval",
                kind="trusted_action",
                status="running",
                input_summary={},
            )
        )
        await session.flush()
        session.add(
            ApprovalRequestModel(
                id=approval_id,
                task_id=task_id,
                step_id=step_id,
                version=1,
                action="mail.send",
                schema_version="mail_send.v1",
                risk_level="high",
                payload={"storage": "encrypted"},
                payload_hash=payload_hash,
                payload_ciphertext=encrypted.ciphertext,
                payload_nonce=encrypted.nonce,
                payload_key_version=encrypted.key_version,
                proposal_kind="mail_draft",
                proposal_id=draft_id,
                proposal_version=1,
                preview_markdown="",
                status="approved" if needs_attention else "pending",
                expires_at=NOW + timedelta(minutes=10),
            )
        )
        await session.execute(
            update(MailDraftModel)
            .where(MailDraftModel.id == draft_id)
            .values(
                status="needs_attention" if needs_attention else "awaiting_approval",
            )
        )
        if needs_attention:
            session.add(
                ToolExecutionModel(
                    id=execution_id,
                    task_id=task_id,
                    step_id=step_id,
                    tool_name="mail.send",
                    idempotency_key=f"mail.send:{task_id}:{approval_id}:1:{operation_id}",
                    operation_id=operation_id,
                    request_payload_hash=payload_hash,
                    provider="google",
                    status="needs_attention",
                    write_attempt_count=1,
                    reconciliation_attempt_count=4,
                    claimed_at=NOW,
                    request_started_at=NOW,
                    result_summary={
                        "kind": "unknown",
                        "retryable": False,
                        "provider_url": "https://mail.example.test/synthetic-resource",
                    },
                )
            )
        session.add(
            AuditEventModel(
                user_id=clients.owner_id,
                task_id=task_id,
                event_type="action.submitted",
                actor_type="user",
                actor_id=str(clients.owner_id),
                event_metadata={"action": "mail.send", "provider": "google"},
            )
        )
    return _Action(task_id, draft_id, approval_id, execution_id, connection_id)


@pytest.mark.asyncio
async def test_action_snapshot_decrypts_typed_preview_but_sets_no_store(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """只有当前用户的精确冻结命令进入结构化预览；版本由同一审计快照提供。"""
    clients = authenticated_api_clients
    action = await _seed_mail_action(clients)
    response = await clients.owner.get(f"/api/v1/actions/{action.task_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"

    payload = response.json()
    assert payload["approval"]["content_status"] == "available"
    assert payload["approval"]["preview"] == {
        "kind": "mail",
        "provider": "google",
        "account_email": "task15-owner@example.test",
        "mode": "new",
        "to": ["to@example.test"],
        "cc": ["cc@example.test"],
        "bcc": ["bcc@example.test"],
        "subject": "Synthetic subject",
        "body_text": "Synthetic body",
        "irreversible": True,
    }
    assert payload["task_version"] == payload["event_cursor"]
    assert isinstance(payload["task_version"], str)
    assert payload["task_version"] == str(int(payload["task_version"]))
    assert payload["timeline"][-1]["id"] == payload["event_cursor"]
    assert payload["local_action"]["id"] == str(action.draft_id)


@pytest.mark.asyncio
async def test_retained_action_has_redacted_preview_without_legacy_fallback(
    authenticated_api_clients: AuthenticatedApiClients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AEAD 整体清除后的历史占位可读取，旧明文永远不能恢复为审批正文。"""
    clients = authenticated_api_clients
    action = await _seed_mail_action(clients)
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(ApprovalRequestModel)
            .where(ApprovalRequestModel.id == action.approval_id)
            .values(
                payload_ciphertext=None,
                payload_nonce=None,
                payload_key_version=None,
                payload={"body_text": "forbidden legacy plaintext"},
            )
        )

    def unavailable_key(*_args: object, **_kwargs: object) -> None:
        """历史占位无需 Secret；任何加载都应使测试立即失败。"""
        raise AssertionError("redacted action must not load a key")

    monkeypatch.setattr(
        "ai_employee.infrastructure.security.encryption.AeadCipher.from_file", unavailable_key
    )
    response = await clients.owner.get(f"/api/v1/actions/{action.task_id}")
    assert response.status_code == 200
    assert response.json()["approval"]["content_status"] == "redacted"
    assert response.json()["approval"]["preview"] is None
    assert "forbidden legacy" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_action_list_uses_local_ids_and_content_free_filtered_pagination(
    authenticated_api_clients: AuthenticatedApiClients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未提交草稿保留自身编辑 ID；列表分页不能伪造任务或泄漏地址、标题与正文。"""
    clients = authenticated_api_clients
    action = await _seed_mail_action(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    local = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "local-only"},
        json={"connection_id": str(action.connection_id), "subject": "local-sensitive-subject"},
    )
    assert local.status_code == 201

    def unavailable_key(*_args: object, **_kwargs: object) -> None:
        """无内容列表不能读取 Secret，即使个别审批正文仍然可用。"""
        raise AssertionError("action list must not load a key")

    monkeypatch.setattr(
        "ai_employee.infrastructure.security.encryption.AeadCipher.from_file", unavailable_key
    )
    response = await clients.owner.get(
        "/api/v1/actions", params={"limit": 1, "offset": 0, "status": "editing"}
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["item_kind"] == "mail_draft"
    assert item["id"] == local.json()["id"] and item["task_id"] is None
    assert item["editor_url"] == f"/mail/drafts/{local.json()['id']}"
    assert (
        "subject" not in response.text and "@" not in response.text and "body" not in response.text
    )
    tasks = await clients.owner.get(
        "/api/v1/actions",
        params={"item_kind": "trusted_task", "provider": "google", "action": "mail.send"},
    )
    assert tasks.status_code == 200
    assert [item["task_id"] for item in tasks.json()["items"]] == [str(action.task_id)]
    empty = await clients.owner.get("/api/v1/actions", params={"offset": 100})
    assert empty.json()["items"] == []
    assert (await clients.owner.get("/api/v1/actions?provider=unbounded-value")).status_code == 422
    assert (await clients.other.get("/api/v1/actions")).json()["items"] == []


@pytest.mark.asyncio
async def test_reconcile_returns_task_id_and_preserves_one_execution(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """HTTP 202 返回原 TaskRun 标识，不能把用例返回的执行 UUID 冒充 task_id。"""
    clients = authenticated_api_clients
    action = await _seed_mail_action(clients, needs_attention=True)
    response = await clients.owner.post(
        f"/api/v1/actions/{action.task_id}/reconcile",
        headers={"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""},
    )
    assert response.status_code == 202
    assert response.json() == {"task_id": str(action.task_id)}
    async with clients.session_factory() as session:
        executions = tuple(
            (
                await session.scalars(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == action.task_id)
                )
            ).all()
        )
        assert len(executions) == 1 and executions[0].id == action.execution_id
        assert executions[0].write_attempt_count == 1
        queued = await session.scalar(
            select(func.count())
            .select_from(OutboxEventModel)
            .where(
                OutboxEventModel.aggregate_id == action.task_id,
                OutboxEventModel.topic == "task.execute",
            )
        )
        assert queued == 1


@pytest.mark.asyncio
async def test_manual_resolution_rejects_extra_fields_versions_and_csrf(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """人工确认只消费枚举与 canonical 版本，拒绝敏感自由文本且不自动重发。"""
    clients = authenticated_api_clients
    action = await _seed_mail_action(clients, needs_attention=True)
    path = f"/api/v1/actions/{action.task_id}/manual-resolution"
    snapshot = await clients.owner.get(f"/api/v1/actions/{action.task_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["provider_url"] == "https://mail.example.test/synthetic-resource"
    version = snapshot.json()["task_version"]
    headers = {"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""}
    data = {"resolution": "confirmed_not_executed", "task_version": version}
    assert (await clients.owner.post(path, json=data)).status_code == 403
    for invalid in (
        {**data, "evidence": "forbidden-free-text"},
        {**data, "resolution": "resend"},
        {**data, "task_version": 1},
        {**data, "task_version": "01"},
        {**data, "task_version": "9223372036854775808"},
    ):
        assert (await clients.owner.post(path, headers=headers, json=invalid)).status_code == 422
    stale = await clients.owner.post(path, headers=headers, json={**data, "task_version": "0"})
    assert stale.status_code == 409 and stale.json()["error_code"] == "manual_resolution_conflict"
    assert stale.headers.get("cache-control") == "no-store"
    results = await asyncio.gather(
        *(clients.owner.post(path, headers=headers, json=data) for _ in range(2))
    )
    assert sorted(response.status_code for response in results) == [200, 409]
    async with clients.session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_id == action.task_id,
                    OutboxEventModel.topic == "task.execute",
                )
            )
            == 0
        )
        execution = await session.get(ToolExecutionModel, action.execution_id)
        assert execution is not None and execution.write_attempt_count == 1
        assert execution.manual_resolution == "confirmed_not_executed"


@pytest.mark.asyncio
async def test_action_routes_hide_other_users_and_require_authentication(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """读与两个 mutation 都把跨用户和不存在统一为 404，不能泄漏审批身份。"""
    clients = authenticated_api_clients
    action = await _seed_mail_action(clients, needs_attention=True)
    for identity in (action.task_id, uuid4()):
        assert (await clients.other.get(f"/api/v1/actions/{identity}")).status_code == 404
        for suffix, data in (
            ("reconcile", {}),
            ("manual-resolution", {"resolution": "confirmed_executed", "task_version": "0"}),
        ):
            response = await clients.other.post(
                f"/api/v1/actions/{identity}/{suffix}",
                json=data,
                headers={"X-CSRF-Token": clients.other.cookies.get("ai_employee_csrf") or ""},
            )
            assert response.status_code == 404
    clients.owner.cookies.clear()
    assert (await clients.owner.get("/api/v1/actions")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "long_interval", "all_day"),
    [
        ("create", False, False),
        ("update", False, False),
        ("restore", False, False),
        ("create", True, False),
        ("create", True, True),
    ],
)
async def test_calendar_preview_preserves_before_after_notification_and_conflicts(
    authenticated_api_clients: AuthenticatedApiClients,
    operation: str,
    long_interval: bool,
    all_day: bool,
) -> None:
    """冻结前后值及整个时间范围参与冲突检测，不能漏掉长日程尾部或误报目标自身。"""
    from ai_employee.domain.calendar_actions import calendar_client_event_id
    from ai_employee.infrastructure.db.models.actions import CalendarChangeProposalModel
    from ai_employee.infrastructure.db.models.identity import UserModel
    from ai_employee.infrastructure.db.models.sources import (
        CalendarEventModel,
        ConnectionCapabilityModel,
        ProviderCalendarModel,
        SyncCursorModel,
    )
    from ai_employee.infrastructure.db.repositories.calendar_proposals import (
        SqlAlchemyCalendarProposalRepository,
    )

    clients = authenticated_api_clients
    seeded = await _seed_mail_action(clients)
    proposal_id, operation_id, before_id = uuid4(), uuid4(), uuid4()
    starts_at = datetime(2030, 1, 6, 0 if all_day else 1, tzinfo=UTC)
    ends_at = starts_at + (timedelta(days=21) if long_interval else timedelta(hours=1))
    fields = {
        "title": "Synthetic meeting",
        "description": "Synthetic description",
        "location": "Synthetic room",
        "starts_at": starts_at.date().isoformat() if all_day else starts_at.isoformat(),
        "ends_at": ends_at.date().isoformat() if all_day else ends_at.isoformat(),
        "timezone": "UTC",
        "all_day": all_day,
        "attendees": [],
    }
    before = {**fields, "title": "Synthetic previous meeting"}
    command = {
        **fields,
        "action": f"calendar.{operation}",
        "schema_version": f"calendar_{operation}.v1",
        "operation_id": str(operation_id),
        "connection_id": str(seeded.connection_id),
        "calendar_id": "synthetic-calendar",
        "notification_policy": "none",
    }
    if operation == "create":
        command["client_event_id"] = calendar_client_event_id(operation_id)
    else:
        command.update(
            {
                "provider_event_id": "synthetic-event",
                "base_etag": "synthetic-etag",
                "before_snapshot_id": str(before_id),
                "changed_fields": ["title"],
            }
        )
    cipher = ActionPayloadCipher.from_key(b"m" * 32)
    encrypted = cipher.encrypt_json(
        command,
        user_id=clients.owner_id,
        record_id=seeded.approval_id,
        content_kind="approval_command",
        action=f"calendar.{operation}",
        schema_version=f"calendar_{operation}.v1",
    )
    async with clients.session_factory.begin() as session:
        session.add(
            ProviderCalendarModel(
                user_id=clients.owner_id,
                connection_id=seeded.connection_id,
                provider_calendar_id="synthetic-calendar",
                name="Synthetic calendar",
                timezone="UTC",
                is_primary=True,
                can_write=True,
                access_role="owner",
            )
        )
        session.add_all(
            ConnectionCapabilityModel(
                user_id=clients.owner_id,
                connection_id=seeded.connection_id,
                capability=capability,
                status="enabled",
                actual_scopes=[],
            )
            for capability in ("calendar.read", "calendar.write")
        )
        session.add(
            CalendarChangeProposalModel(
                id=proposal_id,
                user_id=clients.owner_id,
                connection_id=seeded.connection_id,
                creation_idempotency_key=str(proposal_id),
                creation_payload_hash="c" * 64,
                calendar_id="synthetic-calendar",
                operation_kind=operation,
                target_event_id=None if operation == "create" else "synthetic-event",
                base_etag=None if operation == "create" else "synthetic-etag",
                current_version=1,
                status="awaiting_approval",
                retain_until=NOW + timedelta(days=180),
            )
        )
        await session.flush()
        if operation != "create":
            await SqlAlchemyCalendarProposalRepository(session, cipher).save_snapshot(
                snapshot_id=before_id,
                user_id=clients.owner_id,
                proposal_id=proposal_id,
                version=1,
                snapshot_kind="before",
                content=before,
                retain_until=NOW + timedelta(days=180),
            )
        await session.execute(
            update(TaskRunModel)
            .where(TaskRunModel.id == seeded.task_id)
            .values(
                input_payload={
                    "approval_id": str(seeded.approval_id),
                    "operation_id": str(operation_id),
                }
            )
        )
        await session.execute(
            update(ApprovalRequestModel)
            .where(ApprovalRequestModel.id == seeded.approval_id)
            .values(
                action=f"calendar.{operation}",
                schema_version=f"calendar_{operation}.v1",
                payload_ciphertext=encrypted.ciphertext,
                payload_nonce=encrypted.nonce,
                payload_key_version=encrypted.key_version,
                payload_hash=trusted_command_hash(command),
                proposal_kind="calendar_proposal",
                proposal_id=proposal_id,
                risk_level="medium",
            )
        )
    response = await clients.owner.get(f"/api/v1/actions/{seeded.task_id}")
    assert response.status_code == 200
    preview = response.json()["approval"]["preview"]
    assert preview["kind"] == "calendar" and preview["operation"] == operation
    assert preview["calendar_name"] == "Synthetic calendar"
    # 时间表示可规范化为 +00:00，实际日期与字段必须与冻结命令相同。
    assert preview["after"]["title"] == "Synthetic meeting"
    assert preview["after"]["starts_at"] == fields["starts_at"]
    assert preview["after"]["ends_at"] == fields["ends_at"]
    assert preview["after"]["all_day"] is all_day
    assert (
        preview["before"] is None
        if operation == "create"
        else preview["before"]["title"] == "Synthetic previous meeting"
    )
    assert preview["notification_policy"] == "none"
    assert preview["base_etag"] == (None if operation == "create" else "synthetic-etag")
    assert preview["compensation_available"] == (operation == "update")
    assert preview["provider_warnings"] == ["google_send_updates_none_external_sync"]
    assert {item["kind"] for item in preview["conflicts"]} >= {
        "outside_working_hours",
        "partial_sources",
    }
    assert response.headers["cache-control"] == "no-store"
    async with clients.session_factory.begin() as session:
        session.add_all(
            SyncCursorModel(
                connection_id=seeded.connection_id,
                resource_kind="calendar",
                scope_key=scope,
                cursor="synthetic-cursor",
                last_success_at=NOW,
            )
            for scope in ("directory", "synthetic-calendar")
        )
        if operation != "create":
            session.add(
                CalendarEventModel(
                    user_id=clients.owner_id,
                    connection_id=seeded.connection_id,
                    calendar_id="synthetic-calendar",
                    provider_event_id="synthetic-event",
                    title="Synthetic source",
                    starts_at=starts_at,
                    ends_at=ends_at,
                    all_day=False,
                    transparency="opaque",
                    status="confirmed",
                    timezone="UTC",
                    provider_url="https://calendar.example.test/synthetic-event",
                )
            )
    refreshed = await clients.owner.get(f"/api/v1/actions/{seeded.task_id}")
    assert refreshed.status_code == 200
    assert "overlap" not in {
        item["kind"] for item in refreshed.json()["approval"]["preview"]["conflicts"]
    }
    # 另一事件原时段在提案结束后十分钟；十五分钟会议缓冲使其与提案尾部重叠。
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(UserModel)
            .where(UserModel.id == clients.owner_id)
            .values(meeting_buffer_minutes=15)
        )
        session.add(
            CalendarEventModel(
                user_id=clients.owner_id,
                connection_id=seeded.connection_id,
                calendar_id="synthetic-calendar",
                provider_event_id="synthetic-neighbor",
                title="Synthetic neighboring event",
                starts_at=ends_at + timedelta(minutes=10),
                ends_at=ends_at + timedelta(minutes=40),
                all_day=False,
                transparency="opaque",
                status="confirmed",
                timezone="UTC",
                provider_url="https://calendar.example.test/synthetic-neighbor",
            )
        )
    conflicted = await clients.owner.get(f"/api/v1/actions/{seeded.task_id}")
    assert conflicted.status_code == 200
    overlaps = [
        item
        for item in conflicted.json()["approval"]["preview"]["conflicts"]
        if item["kind"] == "overlap"
    ]
    assert overlaps == [
        {
            "kind": "overlap",
            "starts_at": (ends_at - timedelta(minutes=5)).isoformat(),
            "ends_at": (ends_at + timedelta(minutes=55)).isoformat(),
            "missing_connection_ids": [],
        }
    ]


@pytest.mark.asyncio
async def test_action_snapshot_cursor_uses_same_repeatable_read_as_task(
    authenticated_api_clients: AuthenticatedApiClients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务读后并发提交新事实时，审批/时间线/游标必须仍来自原 PostgreSQL 快照。"""
    from ai_employee.infrastructure.db.repositories.action_views import (
        SqlAlchemyActionViewRepository,
    )

    clients = authenticated_api_clients
    action = await _seed_mail_action(clients)
    async with clients.session_factory() as session:
        initial = await session.scalar(
            select(func.max(AuditEventModel.id)).where(AuditEventModel.task_id == action.task_id)
        )
    original = SqlAlchemyActionViewRepository._current_cursor

    async def interleave(
        self: SqlAlchemyActionViewRepository, *, user_id: UUID, task_id: UUID
    ) -> int:
        """只在最小快照已读任务后提交另一个真实事务，验证隔离而非 mock 返回值。"""
        async with clients.session_factory.begin() as session:
            await session.execute(
                update(TaskRunModel).where(TaskRunModel.id == task_id).values(status="queued")
            )
            session.add(
                AuditEventModel(
                    user_id=user_id,
                    task_id=task_id,
                    event_type="task.queued",
                    actor_type="system",
                    actor_id=None,
                    event_metadata={"status": "queued"},
                )
            )
        return await original(self, user_id=user_id, task_id=task_id)

    monkeypatch.setattr(SqlAlchemyActionViewRepository, "_current_cursor", interleave)
    response = await clients.owner.get(f"/api/v1/actions/{action.task_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "waiting_approval"
    assert payload["event_cursor"] == payload["task_version"] == str(initial)
    assert payload["timeline"][-1]["id"] == str(initial)


@pytest.mark.asyncio
async def test_m2_approval_metrics_follow_committed_api_and_expiration(
    authenticated_api_clients: AuthenticatedApiClients,
    database_url: str,
) -> None:
    """真实决定与到期事务各记录一次；失败的重复决定不能增加计数。"""
    from prometheus_client import CollectorRegistry

    from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
    from ai_employee.infrastructure.observability.metrics import Metrics
    from tests.integration.m2.test_encrypted_approval_submission import _persist_approval_interrupt

    clients = authenticated_api_clients
    action = await _seed_mail_action(clients)
    await _persist_approval_interrupt(database_url, task_id=action.task_id)
    async with clients.session_factory() as session:
        approval = await session.get(ApprovalRequestModel, action.approval_id)
        assert approval is not None
        payload_hash = approval.payload_hash
    headers = {"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""}
    response = await clients.owner.post(
        f"/api/v1/approvals/{action.approval_id}/decision",
        headers=headers,
        json={"decision": "rejected", "version": 1, "payload_hash": payload_hash},
    )
    assert response.status_code == 204
    rendered = await clients.owner.get("/metrics")
    assert (
        'ai_employee_approval_decisions_total{action="mail.send",decision="rejected"} 1.0'
        in rendered.text
    )
    repeated = await clients.owner.post(
        f"/api/v1/approvals/{action.approval_id}/decision",
        headers=headers,
        json={"decision": "rejected", "version": 1, "payload_hash": payload_hash},
    )
    assert repeated.status_code == 409
    assert (await clients.owner.get("/metrics")).text.count('decision="rejected"} 1.0') == 1
    other = await _seed_mail_action(clients)
    registry = CollectorRegistry()
    store = SqlAlchemyApprovalStore(clients.session_factory, metrics=Metrics(registry))
    assert await store.expire_overdue(now=NOW + timedelta(days=1), limit=100) == 1
    assert await store.expire_overdue(now=NOW + timedelta(days=1), limit=100) == 0
    assert (
        registry.get_sample_value("ai_employee_approval_expired_total", {"action": "mail.send"})
        == 1
    )
    assert other.task_id != action.task_id


@pytest.mark.asyncio
async def test_m2_worker_metrics_observe_write_and_read_only_paths(
    database_url: str, tmp_path: Path
) -> None:
    """经生产 Worker 组合根记录 UNKNOWN 写请求与后续只读收敛，拒绝把核对计为再次写入。"""
    import base64

    from prometheus_client import CollectorRegistry

    from ai_employee.application.ports.trusted_actions import ProviderWriteOutcome
    from ai_employee.application.use_cases.trusted_actions import TrustedActionAttemptAbandoned
    from ai_employee.domain.actions import ProviderWriteOutcomeKind
    from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
    from ai_employee.infrastructure.observability.metrics import Metrics
    from ai_employee.integrations.registry import ProviderAdapterRegistry
    from ai_employee.workers.reconcile_actions import execute_reconciliation_task
    from ai_employee.workers.trusted_actions import build_trusted_action_task_step
    from tests.integration.m2.test_tool_execution_claim import NOW as CLAIM_NOW
    from tests.integration.m2.test_tool_execution_claim import (
        _RecordingAdapter,
        _Seed,
        _seed_action,
        _settings,
    )

    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    key_file = tmp_path / "worker-synthetic-key"
    key_file.write_bytes(base64.b64encode(b"x" * 32))
    settings = _settings().model_copy(update={"app_master_key_file": key_file})
    adapter = _RecordingAdapter(
        ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.UNKNOWN,
            retryable=False,
            retry_after_seconds=None,
            provider_resource_id=None,
            provider_request_id=None,
            correlation_id="synthetic-correlation",
            provider_url=None,
            error_code="provider_write_outcome_unknown",
        )
    )
    adapters = ProviderAdapterRegistry(google_mail_action=adapter)
    registry = CollectorRegistry()
    metrics = Metrics(registry)
    factory = build_session_factory(database_url)
    try:
        step = build_trusted_action_task_step(
            session_factory=factory,
            settings=settings,
            approval_store=SqlAlchemyApprovalStore(factory),
            resume=None,
            max_transient_retries=3,
            adapters=adapters,
            metrics=metrics,
        )
        workflow = step._workflow
        args = {
            "task_id": seed.task_id,
            "approval_id": seed.approval_id,
            "operation_id": seed.operation_id,
            "expected_payload_hash": payload_hash,
            "lease_owner": seed.owner,
        }
        await workflow.claim(**args)
        with pytest.raises(TrustedActionAttemptAbandoned):
            await workflow.execute_or_reconcile(**args)
        assert (
            registry.get_sample_value(
                "ai_employee_provider_write_requests_total",
                {
                    "provider": "google",
                    "action": "mail.send",
                    "outcome": "unknown",
                },
            )
            == 1
        )
        adapter.outcome = ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.CONFIRMED_APPLIED,
            retryable=False,
            retry_after_seconds=None,
            provider_resource_id="synthetic-resource",
            provider_request_id=None,
            correlation_id="synthetic-correlation",
            provider_url=None,
            error_code=None,
        )
        assert await execute_reconciliation_task(
            task_id=seed.task_id,
            session_factory=factory,
            settings=settings,
            adapters=adapters,
            now=CLAIM_NOW + timedelta(minutes=1),
            metrics=metrics,
        )
        assert (
            registry.get_sample_value(
                "ai_employee_tool_reconciliation_total",
                {
                    "provider": "google",
                    "action": "mail.send",
                    "outcome": "confirmed_applied",
                },
            )
            == 1
        )
        assert adapter.write_calls == 1 and adapter.reconcile_calls == 1
    finally:
        await factory.dispose()


@pytest.mark.asyncio
async def test_m2_health_metrics_scan_real_unresolved_and_capability_state(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """只读健康扫描从 PostgreSQL 聚合未决操作和能力，终态后的 Gauge 必须清零。"""
    from prometheus_client import CollectorRegistry

    from ai_employee.infrastructure.observability.metrics import Metrics
    from ai_employee.workers.observability import refresh_trusted_action_metrics

    clients = authenticated_api_clients
    action = await _seed_mail_action(clients, needs_attention=True)
    registry = CollectorRegistry()
    metrics = Metrics(registry)
    await refresh_trusted_action_metrics(
        session_factory=clients.session_factory, metrics=metrics, now=NOW + timedelta(hours=1)
    )
    assert (
        registry.get_sample_value(
            "ai_employee_needs_attention_tasks",
            {
                "provider": "google",
                "action": "mail.send",
            },
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "ai_employee_connection_capability_state",
            {
                "provider": "google",
                "capability": "mail.send",
                "state": "enabled",
            },
        )
        == 1
    )
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(ToolExecutionModel)
            .where(ToolExecutionModel.id == action.execution_id)
            .values(status="succeeded")
        )
        await session.execute(
            update(TaskRunModel).where(TaskRunModel.id == action.task_id).values(status="succeeded")
        )
    await refresh_trusted_action_metrics(
        session_factory=clients.session_factory, metrics=metrics, now=NOW + timedelta(hours=2)
    )
    assert (
        registry.get_sample_value(
            "ai_employee_needs_attention_tasks", {"provider": "google", "action": "mail.send"}
        )
        == 0
    )


@pytest.mark.asyncio
async def test_revoke_backlog_metrics_clear_after_exact_operator_remediation(
    authenticated_api_clients: AuthenticatedApiClients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产维护 wrapper 从追加审计恢复积压，并在精确人工补救后恢复原基线。"""
    from prometheus_client import CollectorRegistry

    from ai_employee.application.use_cases.connections import OAuthRevokeMaintenanceUseCase
    from ai_employee.infrastructure.db.repositories.connections import (
        SqlAlchemyConnectionStore,
        SqlAlchemyConnectionStoreFactory,
    )
    from ai_employee.infrastructure.observability.metrics import Metrics
    from ai_employee.workers import schedules

    class FixedClock:
        """为维护审计提供明确 UTC 瞬间，不执行网络或读取系统本地时区。"""

        def now(self) -> datetime:
            """返回本例唯一合成时间。"""
            return NOW

    clients = authenticated_api_clients
    action = await _seed_mail_action(clients)
    registry = CollectorRegistry()
    metrics = Metrics(registry)
    monkeypatch.setattr(schedules, "session_factory", clients.session_factory)
    monkeypatch.setattr(schedules, "_scheduler_metrics", None)
    monkeypatch.setattr(schedules, "get_worker_metrics", lambda: metrics)
    await schedules.monitor_oauth_revoke_backlog()
    labels = {"kind": "oauth_revoke_google"}
    baseline = registry.get_sample_value("ai_employee_stuck_tasks", labels)
    assert baseline is not None
    async with clients.session_factory.begin() as session:
        await SqlAlchemyConnectionStore(session).record_oauth_revoke_unresolved(
            user_id=clients.owner_id,
            connection_id=action.connection_id,
            provider="google",
            error_code="google_oauth_unavailable",
            occurred_at=NOW,
        )
    await schedules.monitor_oauth_revoke_backlog()
    assert registry.get_sample_value("ai_employee_stuck_tasks", labels) == baseline + 1
    async with clients.session_factory() as session:
        event_id = await session.scalar(
            select(AuditEventModel.id).where(
                AuditEventModel.user_id == clients.owner_id,
                AuditEventModel.event_type == "oauth.revoke_unresolved",
            )
        )
    assert event_id is not None
    maintenance = OAuthRevokeMaintenanceUseCase(
        SqlAlchemyConnectionStoreFactory(clients.session_factory), FixedClock()
    )
    assert await maintenance.record_remediation(
        user_id=clients.owner_id,
        connection_id=action.connection_id,
        unresolved_event_id=event_id,
    )
    await schedules.monitor_oauth_revoke_backlog()
    assert registry.get_sample_value("ai_employee_stuck_tasks", labels) == baseline
