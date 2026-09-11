"""通过真实 HTTP/PostgreSQL 验证编辑器传输、显式确认与冻结账户历史。

测试只使用官方 orchestrator 的 disposable regular 数据库；外部供应商保持 Fake。
断言关注本地版本、用户归属和历史索引，不把敏感内容写入测试报告。
"""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from ai_employee.infrastructure.db.models.actions import MailDraftModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    TaskStepModel,
)
from ai_employee.infrastructure.db.session import build_session_factory  # noqa: F401

from .conftest import AuthenticatedApiClients
from .test_actions import (
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _seed_mail_action,
    _synthetic_key,  # noqa: F401
)
from .test_calendar_proposals import (
    _create_calendar_proposal,
    _seed_restore_source,
    _seed_writable_calendar,
)
from .test_mail_drafts import _enable_synthetic_mail_submission, _seed_send_connection

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]
CONFIRMATIONS = ["calendar", "time", "attendees", "notification_policy"]


@pytest.mark.parametrize("mode", ["reply", "reply_all"])
async def test_editor_reply_explicit_account_is_rejected_even_when_unchanged(
    authenticated_api_clients: AuthenticatedApiClients, mode: str
) -> None:
    """实际来源创建的回复拒绝显式账户字段，不能借同值写入打开未来重绑入口。"""
    from datetime import UTC, datetime

    from ai_employee.infrastructure.db.models.sources import EmailMessageModel, EmailThreadModel

    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    async with clients.session_factory.begin() as session:
        thread = EmailThreadModel(
            user_id=clients.owner_id,
            connection_id=connection_id,
            provider_thread_id=f"synthetic-thread-{uuid4()}",
            subject="Synthetic reply source",
            participants=[],
            latest_message_at=datetime(2030, 1, 1, tzinfo=UTC),
            provider_url="https://provider.example.test/thread/synthetic-reply-source",
        )
        session.add(thread)
        await session.flush()
        session.add(
            EmailMessageModel(
                user_id=clients.owner_id,
                connection_id=connection_id,
                thread_id=thread.id,
                provider_message_id=f"synthetic-message-{uuid4()}",
                received_at=datetime(2030, 1, 1, tzinfo=UTC),
                sender={"email": "source@example.test"},
                recipients=[{"email": "reader@example.test"}],
                subject="Synthetic reply source",
                snippet="",
                labels=[],
                headers={"message-id": "<synthetic-source@example.test>"},
                provider_url="https://provider.example.test/message/synthetic-reply-source",
            )
        )
        thread_id = thread.id
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers=_headers(clients.owner),
        json={"mode": mode, "source_thread_id": str(thread_id)},
    )
    assert created.status_code == 201
    path = f"/api/v1/mail/drafts/{created.json()['id']}"
    changed = await clients.owner.patch(
        path,
        headers=_headers(clients.owner),
        json={"version": 1, "connection_id": str(connection_id)},
    )
    assert changed.status_code == 409
    assert changed.json()["error_code"] == "mail_draft_binding_immutable"
    current = (await clients.owner.get(path)).json()
    assert current["version"] == 1 and current["connection_id"] == str(connection_id)


async def test_editor_concurrent_rebind_keeps_exactly_one_account_and_version(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """两个标签页对相同版本的账户保存只能有一个成功，失败方不能追加正文版本。"""
    import asyncio

    clients = authenticated_api_clients
    first = await _seed_send_connection(clients, provider_account_id=f"first-{uuid4()}")
    second = await _seed_send_connection(clients, provider_account_id=f"second-{uuid4()}")
    created = await clients.owner.post(
        "/api/v1/mail/drafts", headers=_headers(clients.owner), json={"connection_id": str(first)}
    )
    assert created.status_code == 201
    path = f"/api/v1/mail/drafts/{created.json()['id']}"
    results = await asyncio.gather(
        *(
            clients.owner.patch(
                path,
                headers=_headers(clients.owner),
                json={"version": 1, "connection_id": str(target)},
            )
            for target in (first, second)
        )
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    accepted = next(result.json() for result in results if result.status_code == 200)
    current = (await clients.owner.get(path)).json()
    assert current["version"] == 2
    assert current["connection_id"] == accepted["connection_id"]


async def test_editor_before_does_not_follow_current_event_and_marks_retention_unavailable(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """修改前值来自冻结 snapshot；供应商当前时段变化或历史清除不能被伪造为新 before。"""
    from datetime import UTC, datetime

    from ai_employee.infrastructure.db.models.actions import CalendarChangeSnapshotModel
    from ai_employee.infrastructure.db.models.sources import CalendarEventModel

    clients = authenticated_api_clients
    event_id, _, _ = await _seed_restore_source(clients)
    created = await clients.owner.post(
        "/api/v1/calendar/proposals",
        headers=_headers(clients.owner),
        json={"operation_kind": "update", "initialization": "shell", "event_id": str(event_id)},
    )
    assert created.status_code == 201
    proposal = created.json()
    path = f"/api/v1/calendar/proposals/{proposal['id']}"
    original = (await clients.owner.get(path)).json()["editor_facts"]["before"]
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(CalendarEventModel)
            .where(
                CalendarEventModel.id == event_id, CalendarEventModel.user_id == clients.owner_id
            )
            .values(
                starts_at=datetime(2030, 2, 1, 9, tzinfo=UTC),
                ends_at=datetime(2030, 2, 1, 10, tzinfo=UTC),
            )
        )
    reread = (await clients.owner.get(path)).json()["editor_facts"]
    assert reread["before"] == original and reread["before_status"] == "available"
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(CalendarChangeSnapshotModel)
            .where(
                CalendarChangeSnapshotModel.id == UUID(proposal["before_snapshot_id"]),
                CalendarChangeSnapshotModel.user_id == clients.owner_id,
            )
            .values(content_ciphertext=None, content_nonce=None, content_key_version=None)
        )
    retained = (await clients.owner.get(path)).json()["editor_facts"]
    assert retained["before"] is None and retained["before_status"] == "unavailable"


async def test_editor_conflict_calculation_releases_database_transaction(
    authenticated_api_clients: AuthenticatedApiClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 GET 在运行纯冲突算法前释放认证及编辑器读取事务，避免 CPU 工作占据连接。"""
    from fastapi import FastAPI
    from sqlalchemy import event

    from ai_employee.application.use_cases import calendar_editor
    from ai_employee.application.use_cases.action_views import (
        CalendarConflictPreview,
        CalendarPreviewFields,
    )
    from ai_employee.application.use_cases.calendar_proposals import CalendarAvailabilityContext

    from .test_calendar_proposals import _TransactionProbe

    clients = authenticated_api_clients
    connection_id = await _seed_writable_calendar(clients)
    created = await _create_calendar_proposal(
        clients, connection_id=connection_id, idempotency_key=str(uuid4())
    )
    assert created.status_code == 201
    transport = clients.owner._transport
    assert isinstance(transport, httpx.ASGITransport) and isinstance(transport.app, FastAPI)
    engine = transport.app.state.auth_session_factory.engine.sync_engine
    probe = _TransactionProbe()
    calculate = calendar_editor.calendar_conflict_previews
    calls: list[bool] = []

    def checked_calculation(
        fields: CalendarPreviewFields, context: CalendarAvailabilityContext
    ) -> list[CalendarConflictPreview]:
        """检查短事务已经结束，再执行同一生产纯算法，不以 Fake 冲突替代验证。"""
        assert probe.active == 0
        calls.append(True)
        return calculate(fields, context)

    monkeypatch.setattr(calendar_editor, "calendar_conflict_previews", checked_calculation)
    for name, listener in (
        ("begin", probe.began),
        ("commit", probe.ended),
        ("rollback", probe.ended),
    ):
        event.listen(engine, name, listener)
    try:
        response = await clients.owner.get(f"/api/v1/calendar/proposals/{created.json()['id']}")
    finally:
        for name, listener in (
            ("begin", probe.began),
            ("commit", probe.ended),
            ("rollback", probe.ended),
        ):
            event.remove(engine, name, listener)
    assert response.status_code == 200 and calls == [True]


def _headers(client: httpx.AsyncClient) -> dict[str, str]:
    """构造当前会话的 CSRF 和一次创建意图；不打印任何会话材料。"""
    return {
        "X-CSRF-Token": client.cookies.get("ai_employee_csrf") or "",
        "Idempotency-Key": str(uuid4()),
    }


async def _create_shell(clients: AuthenticatedApiClients) -> httpx.Response:
    """设置本人默认日历后，经公开显式 shell 变体创建待编辑对象。"""
    connection_id = await _seed_writable_calendar(clients)
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(UserModel)
            .where(UserModel.id == clients.owner_id)
            .values(
                default_calendar_connection_id=connection_id,
                default_calendar_id="task17-create-calendar",
            )
        )
    return await clients.owner.post(
        "/api/v1/calendar/proposals",
        headers=_headers(clients.owner),
        json={"operation_kind": "create", "initialization": "shell"},
    )


async def test_editor_calendar_shell_saves_without_confirming_and_uses_versioned_confirmation(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """完整字段保存也不能代替四项确认；混合/迟到确认均不能推进版本。"""
    clients = authenticated_api_clients
    created = await _create_shell(clients)
    assert created.status_code == 201
    shell = created.json()
    assert shell["status"] == "editing"
    assert shell["starts_at"] is None
    assert shell["required_confirmations"] == CONFIRMATIONS
    path = f"/api/v1/calendar/proposals/{shell['id']}"
    loaded = await clients.owner.get(path)
    assert loaded.headers["cache-control"] == "no-store"
    assert loaded.json()["editor_facts"] == {
        "before": None,
        "before_status": "not_applicable",
        "conflict_status": "incomplete",
        "conflicts": None,
        "restore_source": None,
    }
    saved = await clients.owner.patch(
        path,
        headers=_headers(clients.owner),
        json={
            "version": 1,
            "title": "Synthetic editor session",
            "starts_at": "2030-01-02T09:00:00Z",
            "ends_at": "2030-01-02T10:00:00Z",
            "timezone": "UTC",
            "all_day": False,
            "attendees": [],
            "notification_policy": "none",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == 2
    assert saved.json()["required_confirmations"] == CONFIRMATIONS
    mixed = await clients.owner.patch(
        path,
        headers=_headers(clients.owner),
        json={"version": 2, "title": "Synthetic mixed", "confirmation": {"kind": "time"}},
    )
    assert mixed.status_code == 422
    for index, kind in enumerate(CONFIRMATIONS):
        confirmation = {"kind": kind}
        if kind == "calendar":
            confirmation.update(
                connection_id=shell["connection_id"], calendar_id=shell["calendar_id"]
            )
        response = await clients.owner.patch(
            path,
            headers=_headers(clients.owner),
            json={"version": index + 2, "confirmation": confirmation},
        )
        assert response.status_code == 200
        assert response.json()["required_confirmations"] == CONFIRMATIONS[index + 1 :]
    stale = await clients.owner.patch(
        path,
        headers=_headers(clients.owner),
        json={"version": 2, "confirmation": {"kind": "time"}},
    )
    assert stale.status_code == 409
    facts = (await clients.owner.get(path)).json()["editor_facts"]
    assert facts["conflict_status"] == "checked"
    assert isinstance(facts["conflicts"], list)
    assert (await clients.other.get(path)).status_code == 404
    assert (
        await clients.owner.patch(path, json={"version": 6, "confirmation": {"kind": "time"}})
    ).status_code == 403
    async with clients.session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TaskRunModel)
                .where(TaskRunModel.user_id == clients.owner_id)
            )
            == 0
        )


async def test_editor_source_update_shell_uses_original_before_and_keeps_source_locked(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """来源 shell 保留 ETag/原 before，空 diff 不制造审批，来源归属不能换绑。"""
    clients = authenticated_api_clients
    event_id, _, _ = await _seed_restore_source(clients)
    created = await clients.owner.post(
        "/api/v1/calendar/proposals",
        headers=_headers(clients.owner),
        json={"operation_kind": "update", "initialization": "shell", "event_id": str(event_id)},
    )
    assert created.status_code == 201
    proposal = created.json()
    assert proposal["changed_fields"] == []
    assert proposal["base_etag"] is not None
    path = f"/api/v1/calendar/proposals/{proposal['id']}"
    before = (await clients.owner.get(path)).json()["editor_facts"]
    assert before["before_status"] == "available"
    assert before["before"]["starts_at"] == proposal["starts_at"]
    # 同账户换日历与跨账户换绑都受应用层来源锁保护，不允许 PATCH 伪造源事件。
    target = await _seed_writable_calendar(clients)
    rejected = await clients.owner.patch(
        path,
        headers=_headers(clients.owner),
        json={
            "version": 1,
            "confirmation": {
                "kind": "calendar",
                "connection_id": str(target),
                "calendar_id": "task17-create-calendar",
            },
        },
    )
    assert rejected.status_code == 409
    foreign = await clients.other.post(
        "/api/v1/calendar/proposals",
        headers=_headers(clients.other),
        json={"operation_kind": "update", "initialization": "shell", "event_id": str(event_id)},
    )
    assert foreign.status_code == 404


async def test_editor_mail_account_change_uses_one_cas_and_strict_optional_field(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """新邮件可原子换本人可发送账户；stale/null/他人账户均保留原版本。"""
    clients = authenticated_api_clients
    first = await _seed_send_connection(clients, provider_account_id=f"first-{uuid4()}")
    second = await _seed_send_connection(clients, provider_account_id=f"second-{uuid4()}")
    created = await clients.owner.post(
        "/api/v1/mail/drafts", headers=_headers(clients.owner), json={"connection_id": str(first)}
    )
    assert created.status_code == 201
    path = f"/api/v1/mail/drafts/{created.json()['id']}"
    changed = await clients.owner.patch(
        path, headers=_headers(clients.owner), json={"version": 1, "connection_id": str(second)}
    )
    assert changed.status_code == 200
    assert changed.json()["connection_id"] == str(second)
    assert changed.json()["version"] == 2
    assert (
        await clients.owner.patch(
            path, headers=_headers(clients.owner), json={"version": 1, "connection_id": str(first)}
        )
    ).status_code == 409
    assert (
        await clients.owner.patch(
            path, headers=_headers(clients.owner), json={"version": 2, "connection_id": None}
        )
    ).status_code == 422
    assert (
        await clients.other.patch(
            path, headers=_headers(clients.other), json={"version": 2, "connection_id": str(first)}
        )
    ).status_code == 404


async def test_editor_submission_writes_typed_frozen_step_index(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """真实提交事务从验证过的命令持久冻结账户，TaskRun 仍严格只含双 ID。"""
    clients = authenticated_api_clients
    account = f"indexed-{uuid4()}"
    connection_id = await _seed_send_connection(clients, provider_account_id=account)
    _enable_synthetic_mail_submission(clients, provider_account_id=account)
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers=_headers(clients.owner),
        json={
            "connection_id": str(connection_id),
            "to": ["receiver@example.test"],
            "subject": "Synthetic subject",
            "body_text": "Synthetic body",
        },
    )
    assert created.status_code == 201
    submitted = await clients.owner.post(
        f"/api/v1/mail/drafts/{created.json()['id']}/submit",
        headers=_headers(clients.owner),
        json={"version": 1},
    )
    assert submitted.status_code == 202
    async with clients.session_factory() as session:
        step = await session.scalar(
            select(TaskStepModel).where(TaskStepModel.task_id == UUID(submitted.json()["task_id"]))
        )
        assert step is not None
        assert step.input_summary == {
            "summary_version": "trusted_action_step.v1",
            "action": "mail.send",
            "proposal_version": 1,
            "frozen_connection_id": str(connection_id),
        }
        task = await session.get(TaskRunModel, step.task_id)
        assert task is not None and set(task.input_payload) == {"approval_id", "operation_id"}


async def _withdraw_legacy(clients: AuthenticatedApiClients) -> tuple[UUID, UUID, UUID, UUID]:
    """保留精确旧两字段摘要，通过现有撤回入口释放草稿编辑锁。"""
    action = await _seed_mail_action(clients)
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(TaskStepModel)
            .where(TaskStepModel.task_id == action.task_id)
            .values(input_summary={"action": "mail.send", "proposal_version": 1})
        )
    cancelled = await clients.owner.post(
        f"/api/v1/tasks/{action.task_id}/cancel", headers=_headers(clients.owner)
    )
    assert cancelled.status_code in {200, 202}
    return action.task_id, action.draft_id, action.approval_id, action.connection_id


async def test_editor_rebind_upgrades_legacy_and_preserves_frozen_provider_after_retention(
    authenticated_api_clients: AuthenticatedApiClients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次换绑在同一事务升级旧摘要；正文清除后列表筛选和详情仍显示旧供应商。"""
    clients = authenticated_api_clients
    task_id, draft_id, approval_id, original = await _withdraw_legacy(clients)
    replacement = await _seed_send_connection(clients, provider_account_id=f"replacement-{uuid4()}")
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(OAuthConnectionModel)
            .where(OAuthConnectionModel.id == replacement)
            .values(
                provider="microsoft",
                provider_tenant_id="synthetic-personal-tenant",
                account_type="personal",
            )
        )
    changed = await clients.owner.patch(
        f"/api/v1/mail/drafts/{draft_id}",
        headers=_headers(clients.owner),
        json={"version": 1, "connection_id": str(replacement)},
    )
    assert changed.status_code == 200
    async with clients.session_factory.begin() as session:
        step = await session.scalar(select(TaskStepModel).where(TaskStepModel.task_id == task_id))
        assert step is not None and step.input_summary["frozen_connection_id"] == str(original)
    full = await clients.owner.get(f"/api/v1/actions/{task_id}")
    assert full.status_code == 200
    assert full.json()["provider"] == "google"
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(ApprovalRequestModel)
            .where(ApprovalRequestModel.id == approval_id)
            .values(payload_ciphertext=None, payload_nonce=None, payload_key_version=None)
        )

    def unavailable_key(*_args: object, **_kwargs: object) -> None:
        """合法内容无关索引在保留清除后无需主密钥。"""
        raise AssertionError("content-free history must not decrypt")

    monkeypatch.setattr(
        "ai_employee.infrastructure.security.encryption.AeadCipher.from_file", unavailable_key
    )
    detail = await clients.owner.get(f"/api/v1/actions/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["provider"] == "google"
    assert detail.json()["approval"]["content_status"] == "redacted"
    google = await clients.owner.get(
        "/api/v1/actions", params={"item_kind": "trusted_task", "provider": "google", "limit": 1}
    )
    microsoft = await clients.owner.get(
        "/api/v1/actions", params={"item_kind": "trusted_task", "provider": "microsoft", "limit": 1}
    )
    assert [item["task_id"] for item in google.json()["items"]] == [str(task_id)]
    assert microsoft.json()["items"] == []


async def test_editor_unprovable_legacy_rebind_rolls_back_local_version(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """已清除且无冻结索引的旧历史不能被猜测修复；保留草稿身份与版本。"""
    clients = authenticated_api_clients
    _, draft_id, approval_id, original = await _withdraw_legacy(clients)
    replacement = await _seed_send_connection(clients, provider_account_id=f"replacement-{uuid4()}")
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(ApprovalRequestModel)
            .where(ApprovalRequestModel.id == approval_id)
            .values(payload_ciphertext=None, payload_nonce=None, payload_key_version=None)
        )
    changed = await clients.owner.patch(
        f"/api/v1/mail/drafts/{draft_id}",
        headers=_headers(clients.owner),
        json={"version": 1, "connection_id": str(replacement)},
    )
    assert changed.status_code == 409
    assert changed.json()["error_code"] == "historical_action_binding_unavailable"
    async with clients.session_factory() as session:
        draft = await session.get(MailDraftModel, draft_id)
        assert draft is not None and draft.connection_id == original and draft.current_version == 1


@pytest.mark.parametrize(
    "bad_summary",
    [
        {},
        {"action": "mail.send", "proposal_version": "not-a-number"},
        {
            "summary_version": "unknown",
            "action": "mail.send",
            "proposal_version": 1,
            "frozen_connection_id": "broken",
        },
        {
            "summary_version": "trusted_action_step.v1",
            "action": "mail.send",
            "proposal_version": True,
            "frozen_connection_id": "broken",
        },
        {"summary_version": "trusted_action_step.v1", "action": "mail.send", "proposal_version": 1},
    ],
)
async def test_editor_malformed_history_index_never_falls_back_or_raises_sql_cast_error(
    authenticated_api_clients: AuthenticatedApiClients,
    bad_summary: dict[str, object],
) -> None:
    """非法摘要在详情和分页前同样不可用，完整密文不能替它修补账户索引。"""
    clients = authenticated_api_clients
    action = await _seed_mail_action(clients)
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(TaskStepModel)
            .where(TaskStepModel.task_id == action.task_id)
            .values(input_summary=bad_summary)
        )
    detail = await clients.owner.get(f"/api/v1/actions/{action.task_id}")
    assert detail.status_code == 404
    listing = await clients.owner.get(
        "/api/v1/actions", params={"item_kind": "trusted_task", "limit": 1}
    )
    assert listing.status_code == 200
    assert listing.json()["items"] == []


async def test_editor_facts_are_returned_for_existing_complete_proposal(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """通过既有完整 create 建立对象，独立证明 GET 必须返回已检查的编辑事实。"""
    clients = authenticated_api_clients
    connection_id = await _seed_writable_calendar(clients)
    created = await _create_calendar_proposal(
        clients, connection_id=connection_id, idempotency_key=str(uuid4())
    )
    assert created.status_code == 201
    loaded = await clients.owner.get(f"/api/v1/calendar/proposals/{created.json()['id']}")
    assert loaded.status_code == 200
    assert "editor_facts" in loaded.json()
    facts = loaded.json()["editor_facts"]
    assert facts["before_status"] == "not_applicable"
    assert facts["conflict_status"] == "checked"
    assert isinstance(facts["conflicts"], list)


async def test_editor_confirmation_reaches_existing_shell_without_new_post_variant(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """直接用早已存在的应用 shell 创建能力播种，独立验证新的 HTTP 确认入口。"""
    from datetime import UTC, datetime

    from ai_employee.application.use_cases.calendar_proposals import CalendarProposalUseCase
    from ai_employee.config import get_settings
    from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
    from ai_employee.infrastructure.db.repositories.calendar_proposals import (
        SqlAlchemyCalendarProposalRepository,
    )
    from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
    from ai_employee.infrastructure.security.encryption import AeadCipher

    clients = authenticated_api_clients
    connection_id = await _seed_writable_calendar(clients)
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(UserModel)
            .where(UserModel.id == clients.owner_id)
            .values(
                default_calendar_connection_id=connection_id,
                default_calendar_id="task17-create-calendar",
            )
        )
        cipher = AeadCipher.from_file(get_settings().app_master_key_file)
        shell = await CalendarProposalUseCase(
            proposals=SqlAlchemyCalendarProposalRepository(session, ActionPayloadCipher(cipher)),
            calendar=SqlAlchemyCalendarSyncRepository(session, cipher),
            clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
        ).create_shell(user_id=clients.owner_id, idempotency_key=str(uuid4()))
    confirmed = await clients.owner.patch(
        f"/api/v1/calendar/proposals/{shell.proposal_id}",
        headers=_headers(clients.owner),
        json={
            "version": 1,
            "confirmation": {
                "kind": "calendar",
                "connection_id": str(connection_id),
                "calendar_id": "task17-create-calendar",
            },
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["version"] == 2
    assert confirmed.json()["required_confirmations"] == CONFIRMATIONS[1:]


async def test_editor_multiple_legacy_records_are_upgraded_atomically_or_all_rolled_back(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """一条历史命令的 local ID 被替换即拒绝整个重绑，其他有效摘要也不得部分升级。"""
    clients = authenticated_api_clients
    first_task, draft_id, _, original = await _withdraw_legacy(clients)
    _, _, second_approval, replacement = await _withdraw_legacy(clients)
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(ApprovalRequestModel)
            .where(ApprovalRequestModel.id == second_approval)
            .values(proposal_id=draft_id)
        )
    rejected = await clients.owner.patch(
        f"/api/v1/mail/drafts/{draft_id}",
        headers=_headers(clients.owner),
        json={"version": 1, "connection_id": str(replacement)},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "historical_action_binding_unavailable"
    async with clients.session_factory() as session:
        first = await session.scalar(
            select(TaskStepModel).where(TaskStepModel.task_id == first_task)
        )
        draft = await session.get(MailDraftModel, draft_id)
        assert first is not None and set(first.input_summary) == {"action", "proposal_version"}
        assert draft is not None and draft.connection_id == original and draft.current_version == 1


@pytest.mark.parametrize(
    "damage",
    [
        "foreign_connection",
        "extra_field",
        "float_version",
        "string_version",
        "bad_uuid",
        "wrong_action",
    ],
)
async def test_editor_index_validation_is_identical_before_pagination_and_in_detail(
    authenticated_api_clients: AuthenticatedApiClients,
    damage: str,
) -> None:
    """规范摘要之外的类型、身份和字段都不能消耗分页位置或从当前账户恢复。"""
    clients = authenticated_api_clients
    action = await _seed_mail_action(clients)
    summary: dict[str, object] = {
        "summary_version": "trusted_action_step.v1",
        "action": "mail.send",
        "proposal_version": 1,
        "frozen_connection_id": str(action.connection_id),
    }
    if damage == "foreign_connection":
        async with clients.session_factory.begin() as session:
            other = OAuthConnectionModel(
                user_id=clients.other_id,
                provider="google",
                provider_account_id=f"foreign-{uuid4()}",
                provider_tenant_id="",
                account_type="google",
                account_email="other@example.test",
                status="connected",
                scopes=[],
            )
            session.add(other)
            await session.flush()
            summary["frozen_connection_id"] = str(other.id)
    elif damage == "extra_field":
        summary["unexpected"] = "not allowed"
    elif damage == "float_version":
        summary["proposal_version"] = 1.0
    elif damage == "string_version":
        summary["proposal_version"] = "1"
    elif damage == "bad_uuid":
        summary["frozen_connection_id"] = "invalid UUID"
    else:
        summary["action"] = "calendar.create"
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(TaskStepModel)
            .where(TaskStepModel.task_id == action.task_id)
            .values(input_summary=summary)
        )
    detail = await clients.owner.get(f"/api/v1/actions/{action.task_id}")
    assert detail.status_code == 404
    listing = await clients.owner.get(
        "/api/v1/actions", params={"item_kind": "trusted_task", "limit": 1, "offset": 0}
    )
    assert listing.status_code == 200 and listing.json()["items"] == []


async def test_editor_calendar_same_account_retarget_preserves_legacy_calendar_preview(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """同账户仅换日历也必须升级全部 legacy；冻结预览继续引用原日历。"""
    from ai_employee.application.commands import trusted_command_hash
    from ai_employee.config import get_settings
    from ai_employee.domain.calendar_actions import calendar_client_event_id
    from ai_employee.infrastructure.db.models.sources import ProviderCalendarModel
    from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
    from ai_employee.infrastructure.security.encryption import AeadCipher

    clients = authenticated_api_clients
    created = await _create_shell(clients)
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    connection_id = created.json()["connection_id"]
    path = f"/api/v1/calendar/proposals/{proposal_id}"
    saved = await clients.owner.patch(
        path,
        headers=_headers(clients.owner),
        json={
            "version": 1,
            "title": "Synthetic calendar title",
            "description": None,
            "location": None,
            "starts_at": "2030-01-02T09:00:00Z",
            "ends_at": "2030-01-02T10:00:00Z",
            "timezone": "UTC",
            "all_day": False,
            "attendees": [],
            "notification_policy": "none",
        },
    )
    assert saved.status_code == 200
    task_id, _, approval_id, _ = await _withdraw_legacy(clients)
    async with clients.session_factory.begin() as session:
        task = await session.get(TaskRunModel, task_id)
        assert task is not None
        operation_id = UUID(str(task.input_payload["operation_id"]))
        command = {
            name: saved.json()[name]
            for name in (
                "title",
                "description",
                "location",
                "starts_at",
                "ends_at",
                "timezone",
                "all_day",
                "attendees",
                "notification_policy",
            )
        }
        command.update(
            schema_version="calendar_create.v1",
            action="calendar.create",
            operation_id=str(operation_id),
            connection_id=connection_id,
            calendar_id=created.json()["calendar_id"],
            client_event_id=calendar_client_event_id(operation_id),
        )
        cipher = ActionPayloadCipher(AeadCipher.from_file(get_settings().app_master_key_file))
        encrypted = cipher.encrypt_json(
            command,
            user_id=clients.owner_id,
            record_id=approval_id,
            content_kind="approval_command",
            action="calendar.create",
            schema_version="calendar_create.v1",
        )
        await session.execute(
            update(ApprovalRequestModel)
            .where(ApprovalRequestModel.id == approval_id)
            .values(
                action="calendar.create",
                schema_version="calendar_create.v1",
                proposal_kind="calendar_proposal",
                proposal_id=UUID(proposal_id),
                proposal_version=2,
                payload_hash=trusted_command_hash(command),
                payload_ciphertext=encrypted.ciphertext,
                payload_nonce=encrypted.nonce,
                payload_key_version=encrypted.key_version,
            )
        )
        await session.execute(
            update(TaskStepModel)
            .where(TaskStepModel.task_id == task_id)
            .values(input_summary={"action": "calendar.create", "proposal_version": 2})
        )
        session.add(
            ProviderCalendarModel(
                user_id=clients.owner_id,
                connection_id=UUID(connection_id),
                provider_calendar_id="synthetic-alternate-calendar",
                name="Synthetic alternate calendar",
                timezone="UTC",
                is_primary=False,
                access_role="owner",
                can_write=True,
                provider_url=None,
            )
        )
    changed = await clients.owner.patch(
        path,
        headers=_headers(clients.owner),
        json={
            "version": 2,
            "confirmation": {
                "kind": "calendar",
                "connection_id": connection_id,
                "calendar_id": "synthetic-alternate-calendar",
            },
        },
    )
    assert changed.status_code == 200
    assert changed.json()["calendar_id"] == "synthetic-alternate-calendar"
    history = await clients.owner.get(f"/api/v1/actions/{task_id}")
    assert history.status_code == 200
    assert history.json()["approval"]["preview"]["calendar_name"] == "Synthetic create calendar"
    async with clients.session_factory.begin() as session:
        step = await session.scalar(select(TaskStepModel).where(TaskStepModel.task_id == task_id))
        assert step is not None and step.input_summary["frozen_connection_id"] == connection_id
        await session.execute(
            update(ApprovalRequestModel)
            .where(ApprovalRequestModel.id == approval_id)
            .values(payload_ciphertext=None, payload_nonce=None, payload_key_version=None)
        )
    second = await clients.owner.patch(
        path,
        headers=_headers(clients.owner),
        json={
            "version": 3,
            "confirmation": {
                "kind": "calendar",
                "connection_id": connection_id,
                "calendar_id": created.json()["calendar_id"],
            },
        },
    )
    assert second.status_code == 200
