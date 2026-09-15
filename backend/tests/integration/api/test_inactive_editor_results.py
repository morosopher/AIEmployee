"""在真实认证结束后提交删除屏障，验证编辑与建议短事务不重建普通业务事实。"""

import asyncio
from typing import Literal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, func, select

from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
    MailDraftModel,
)
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.session import build_session_factory  # noqa: F401
from tests.integration.api.conftest import AuthenticatedApiClients
from tests.integration.api.test_action_editors import _headers
from tests.integration.api.test_calendar_proposals import (
    _calendar_master_key_file,  # noqa: F401
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _seed_restore_source,
)
from tests.integration.api.test_editor_commit_boundary import _create_editor
from tests.integration.privacy.inactive_barrier import (
    assert_facts_unchanged,
    commit_deletion_barrier,
    database_facts,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]


@pytest.mark.parametrize(
    ("kind", "operation"),
    (
        ("mail", "create"),
        ("mail", "update"),
        ("mail", "cancel"),
        ("calendar", "create"),
        ("calendar", "update"),
        ("calendar", "cancel"),
        ("calendar", "suggest"),
    ),
    ids=(
        "mail-create",
        "mail-update",
        "mail-cancel",
        "calendar-create",
        "calendar-update",
        "calendar-cancel",
        "calendar-suggest",
    ),
)
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_authenticated_editor_mutation_respects_deletion_barrier(
    authenticated_api_clients: AuthenticatedApiClients,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["mail", "calendar"],
    operation: str,
    inactive: bool,
) -> None:
    """暂停真实仓储入口而非认证替身；屏障后版本、状态、审计与Outbox必须整体不变。

    建议保存由独立写事务调用同一个版本仓储，必须同样拒绝已失效的读取结果。
    active 对照断言真实 PostgreSQL 版本/状态；inactive 还重投原 HTTP 请求验证幂等拒绝。
    """
    clients = authenticated_api_clients
    editor = await _create_editor(clients, kind)
    current = (await clients.owner.get(editor.path)).json()
    repository_type = (
        SqlAlchemyMailDraftRepository if kind == "mail" else SqlAlchemyCalendarProposalRepository
    )
    method = {
        "create": "create",
        "update": "save_next_version",
        "cancel": "cancel",
        "suggest": "save_next_version",
    }[operation]
    original = getattr(repository_type, method)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_before_mutation(repository: object, **kwargs: object) -> object:
        """只延迟原始方法，不改变输入、持久化行为、错误映射或外层事务。"""
        entered.set()
        await release.wait()
        return await original(repository, **kwargs)

    monkeypatch.setattr(repository_type, method, pause_before_mutation)
    headers = _headers(clients.owner)

    async def request() -> httpx.Response:
        """复用真实路由、CSRF与幂等键；不把测试结果写回数据库。"""
        if operation == "create":
            payload = (
                {"mode": "new", "connection_id": current["connection_id"]}
                if kind == "mail"
                else {
                    "operation_kind": "create",
                    "connection_id": current["connection_id"],
                    "calendar_id": current["calendar_id"],
                    "title": "Synthetic late meeting",
                    "starts_at": "2030-01-07T09:00:00Z",
                    "ends_at": "2030-01-07T10:00:00Z",
                    "timezone": "UTC",
                    "all_day": False,
                    "attendees": [],
                    "notification_policy": "all",
                }
            )
            return await clients.owner.post(
                editor.path.rsplit("/", 1)[0], headers=headers, json=payload
            )
        if operation == "cancel":
            return await clients.owner.delete(editor.path, headers=headers)
        if operation == "suggest":
            return await clients.owner.post(
                f"{editor.path}/suggest-times",
                headers=headers,
                json={"version": editor.version, "search_start": "2030-01-07T09:00:00Z"},
            )
        return await clients.owner.patch(
            editor.path,
            headers=headers,
            json={
                "version": editor.version,
                "subject" if kind == "mail" else "title": "Synthetic late revision",
            },
        )

    pending = asyncio.create_task(request())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if inactive:
            await commit_deletion_barrier(clients.session_factory, user_id=clients.owner_id)
        before = await database_facts(clients.session_factory)
        release.set()
        response = await asyncio.wait_for(pending, timeout=5)
        if inactive:
            assert_facts_unchanged(before, await database_facts(clients.session_factory))
            assert response.status_code == (409 if operation == "create" else 404)
            assert (await request()).status_code == 401
            assert_facts_unchanged(before, await database_facts(clients.session_factory))
        else:
            assert response.status_code == (201 if operation == "create" else 200)
            model = MailDraftModel if kind == "mail" else CalendarChangeProposalModel
            identifier = UUID(response.json()["id"]) if operation == "create" else editor.identifier
            async with clients.session_factory() as session:
                saved = await session.get(model, identifier)
                assert saved is not None
                assert saved.current_version == (2 if operation in {"update", "suggest"} else 1)
                assert saved.status == ("cancelled" if operation == "cancel" else "editing")
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_replayed_update_before_snapshot_save_respects_barrier(
    authenticated_api_clients: AuthenticatedApiClients,
    monkeypatch: pytest.MonkeyPatch,
    inactive: bool,
) -> None:
    """旧desired已存在但before缺失时，真实API重放不能绕过create守卫补写snapshot。

    只在合成setup移除原before模拟遗留中断；版本保持1，不能用非法未来版本制造假RED。
    """
    clients = authenticated_api_clients
    event_id, _, _ = await _seed_restore_source(clients)
    headers = _headers(clients.owner)
    payload = {"operation_kind": "update", "initialization": "shell", "event_id": str(event_id)}
    created = await clients.owner.post("/api/v1/calendar/proposals", headers=headers, json=payload)
    assert created.status_code == 201
    proposal_id = UUID(created.json()["id"])
    before_id = UUID(created.json()["before_snapshot_id"])
    async with clients.session_factory.begin() as session:
        await session.execute(
            delete(CalendarChangeSnapshotModel).where(
                CalendarChangeSnapshotModel.id == before_id,
                CalendarChangeSnapshotModel.user_id == clients.owner_id,
            )
        )
    original = SqlAlchemyCalendarProposalRepository.save_snapshot
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_before_snapshot(repository: object, **kwargs: object) -> object:
        """保持真实不可变snapshot协议，只暂停已有desired重放后的独立保存入口。"""
        entered.set()
        await release.wait()
        return await original(repository, **kwargs)

    monkeypatch.setattr(
        SqlAlchemyCalendarProposalRepository, "save_snapshot", pause_before_snapshot
    )
    pending = asyncio.create_task(
        clients.owner.post(
            "/api/v1/calendar/proposals",
            headers=headers,
            json=payload,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if inactive:
            await commit_deletion_barrier(clients.session_factory, user_id=clients.owner_id)
        before = await database_facts(clients.session_factory)
        release.set()
        response = await asyncio.wait_for(pending, timeout=5)
        if inactive:
            assert_facts_unchanged(before, await database_facts(clients.session_factory))
            assert response.status_code == 404
        else:
            assert response.status_code == 201
            assert UUID(response.json()["id"]) == proposal_id
            async with clients.session_factory() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(
                            CalendarChangeSnapshotModel,
                        )
                        .where(
                            CalendarChangeSnapshotModel.proposal_id == proposal_id,
                            CalendarChangeSnapshotModel.snapshot_kind == "before",
                        )
                    )
                    == 1
                )
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
