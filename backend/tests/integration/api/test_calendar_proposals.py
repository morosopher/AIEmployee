"""日历提案 API 的认证、幂等、恢复和用户设置集成测试。"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select

from ai_employee.api.deps import get_submit_calendar_proposal_use_case
from ai_employee.config import get_settings
from ai_employee.domain.actions import CalendarProposalStatus
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
)
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
)
from ai_employee.infrastructure.db.models.tasks import OutboxEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.session import build_session_factory  # noqa: F401
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher

from .conftest import AuthenticatedApiClients

pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """把本模块绑定到受生命周期保护的 Cycle 5 regular head。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """复用 regular fixture 已验证的迁移，不触碰 anchor 数据库。"""
    del cycle5_regular_database_url
    yield


@pytest.fixture(autouse=True)
def _calendar_master_key_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """为 API 与 seed 共用合成主密钥，禁止读取真实部署 Secret。"""
    master_key = tmp_path / "task17-calendar-master-key"
    master_key.write_text(
        "bW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW0=",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_MASTER_KEY_FILE", str(master_key))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_restore_source(
    clients: AuthenticatedApiClients,
    *,
    source_kind: Literal["before", "desired"] = "before",
    proposal_status: CalendarProposalStatus = CalendarProposalStatus.APPLIED,
    proposal_operation: Literal["update", "restore"] = "update",
    retain_until: datetime | None = None,
    event_id: UUID | None = None,
    recurring_event_id: str | None = None,
    etag: str | None = 'W/"task17-etag"',
    calendar_can_write: bool = True,
) -> tuple[UUID, UUID, UUID]:
    """写入可审计合成事件、update 提案和历史 snapshot，供 API 恢复测试使用。"""
    connection_id = uuid4()
    local_event_id = event_id or uuid4()
    proposal_id = uuid4()
    desired_id = uuid4()
    source_snapshot_id = uuid4() if source_kind == "before" else desired_id
    retention = retain_until or (datetime.now(UTC) + timedelta(days=30))
    cipher = ActionPayloadCipher(AeadCipher.from_file(get_settings().app_master_key_file))
    async with clients.session_factory.begin() as session:
        session.add(
            OAuthConnectionModel(
                id=connection_id,
                user_id=clients.owner_id,
                provider="google",
                provider_account_id=f"task17-calendar-{connection_id}",
                provider_tenant_id="",
                account_type="google",
                account_email="task17-calendar@example.test",
                scopes=[],
                status="connected",
            )
        )
        session.add_all(
            ConnectionCapabilityModel(
                user_id=clients.owner_id,
                connection_id=connection_id,
                capability=capability,
                status="enabled",
                actual_scopes=[],
            )
            for capability in ("calendar.read", "calendar.write")
        )
        session.add(
            ProviderCalendarModel(
                user_id=clients.owner_id,
                connection_id=connection_id,
                provider_calendar_id="task17-calendar",
                name="Synthetic calendar",
                timezone="UTC",
                is_primary=True,
                access_role="owner" if calendar_can_write else "reader",
                can_write=calendar_can_write,
                provider_url="https://calendar.example.test/task17",
            )
        )
        # 先固定连接/目录外键；随后 repository 查询触发 autoflush 时不能让事件
        # 先于尚未写入的 connection 行发送到 PostgreSQL。
        await session.flush()
        session.add(
            CalendarEventModel(
                id=local_event_id,
                user_id=clients.owner_id,
                connection_id=connection_id,
                provider_event_id="task17-provider-event",
                calendar_id="task17-calendar",
                title="Current synthetic event",
                starts_at=datetime(2030, 1, 1, 9, tzinfo=UTC),
                ends_at=datetime(2030, 1, 1, 10, tzinfo=UTC),
                all_day=False,
                transparency="opaque",
                status="confirmed",
                timezone="UTC",
                recurring_event_id=recurring_event_id,
                etag=etag,
                organizer=None,
                attendees=[],
                access_role="owner",
                can_edit=True,
                provider_url="https://calendar.example.test/task17/event",
            )
        )
        repository = SqlAlchemyCalendarProposalRepository(session, cipher)
        content = {
            "operation_id": str(uuid4()),
            "title": "Synthetic historical event",
            "description": "Synthetic description",
            "location": "Synthetic room",
            "starts_at": "2030-01-01T08:00:00+00:00",
            "ends_at": "2030-01-01T09:00:00+00:00",
            "timezone": "UTC",
            "all_day": False,
            "attendees": [],
            "notification_policy": "all",
            "changed_fields": ["location"],
            "confirmed_fields": [],
            "source_event_ids": [str(local_event_id)],
            "notification_policy_user_set": False,
            "requires_explicit_confirmation": False,
            "required_confirmations": [],
            "availability": None,
        }
        await repository.create(
            proposal_id=proposal_id,
            snapshot_id=desired_id,
            user_id=clients.owner_id,
            connection_id=connection_id,
            creation_idempotency_key=f"task17-source-{proposal_id}",
            creation_payload_hash="a" * 64,
            calendar_id="task17-calendar",
            operation_kind="update",
            target_event_id="task17-provider-event",
            base_etag='W/"task17-old-etag"',
            retain_until=retention,
            desired_state=content,
        )
        await repository.save_snapshot(
            snapshot_id=source_snapshot_id,
            user_id=clients.owner_id,
            proposal_id=proposal_id,
            version=1,
            snapshot_kind=source_kind,
            content=content,
            retain_until=retention,
        )
        proposal = await session.get(CalendarChangeProposalModel, proposal_id)
        assert proposal is not None
        proposal.status = proposal_status.value
        proposal.operation_kind = proposal_operation
        await session.flush()
    return local_event_id, source_snapshot_id, proposal_id


async def _seed_writable_calendar(clients: AuthenticatedApiClients) -> UUID:
    """写入当前用户的 connected 连接、日历读写能力和可写目录。"""
    connection_id = uuid4()
    async with clients.session_factory.begin() as session:
        session.add(
            OAuthConnectionModel(
                id=connection_id,
                user_id=clients.owner_id,
                provider="google",
                provider_account_id=f"task17-create-{connection_id}",
                provider_tenant_id="",
                account_type="google",
                account_email="task17-create@example.test",
                scopes=[],
                status="connected",
            )
        )
        await session.flush()
        session.add_all(
            ConnectionCapabilityModel(
                user_id=clients.owner_id,
                connection_id=connection_id,
                capability=capability,
                status="enabled",
                actual_scopes=[],
            )
            for capability in ("calendar.read", "calendar.write")
        )
        session.add(
            ProviderCalendarModel(
                user_id=clients.owner_id,
                connection_id=connection_id,
                provider_calendar_id="task17-create-calendar",
                name="Synthetic create calendar",
                timezone="UTC",
                is_primary=True,
                access_role="owner",
                can_write=True,
                provider_url="https://calendar.example.test/task17-create",
            )
        )
    return connection_id


async def _create_calendar_proposal(
    clients: AuthenticatedApiClients,
    *,
    connection_id: UUID,
    idempotency_key: str,
    title: str = "Synthetic planning session",
) -> httpx.Response:
    """经真实 API 创建完整 create 提案，集中复用严格请求与 CSRF 头。"""
    return await clients.owner.post(
        "/api/v1/calendar/proposals",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": idempotency_key,
        },
        json={
            "operation_kind": "create",
            "connection_id": str(connection_id),
            "calendar_id": "task17-create-calendar",
            "title": title,
            "starts_at": "2030-01-02T09:00:00+00:00",
            "ends_at": "2030-01-02T10:00:00+00:00",
            "timezone": "UTC",
            "all_day": False,
            "attendees": [],
            "notification_policy": "none",
        },
    )


async def _create_update_proposal(
    clients: AuthenticatedApiClients,
    *,
    event_id: UUID,
    idempotency_key: str,
) -> httpx.Response:
    """经真实 API 从本地事件创建 update 提案，供供应商事实错误映射测试复用。"""
    return await clients.owner.post(
        "/api/v1/calendar/proposals",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": idempotency_key,
        },
        json={
            "operation_kind": "update",
            "event_id": str(event_id),
            "location": "Updated synthetic room",
        },
    )


@dataclass(slots=True)
class _TransactionProbe:
    """记录应用引擎当前打开事务数，证明 submit 不包裹另一个请求事务。"""

    active: int = 0

    def began(self, *_args: object) -> None:
        """记录一个 SQLAlchemy 根事务开始。"""
        self.active += 1

    def ended(self, *_args: object) -> None:
        """记录 commit/rollback；负数表示测试监听器或事务边界损坏。"""
        self.active -= 1
        assert self.active >= 0


@dataclass(slots=True)
class _NoOuterTransactionCalendarSubmission:
    """在路由调用提交边界时断言此前短读事务已经完全关闭。"""

    probe: _TransactionProbe
    task_id: UUID = field(default_factory=uuid4)

    async def execute(self, **_kwargs: object) -> object:
        """返回不含敏感内容的合成任务结果，并拒绝外层活动事务。"""
        assert self.probe.active == 0
        return _SyntheticSubmissionResult(task_id=self.task_id)


@dataclass(frozen=True, slots=True)
class _SyntheticSubmissionResult:
    """只表达日历 submit 路由映射所需的任务标识。"""

    task_id: UUID


@pytest.mark.asyncio
async def test_calendar_routes_are_registered() -> None:
    """组合根必须注册 M2 日历提案的八条路由。"""
    from ai_employee.main import create_app

    app = create_app()
    paths = set(app.openapi()["paths"])
    assert {
        "/api/v1/calendar/proposals",
        "/api/v1/calendar/proposals/{proposal_id}",
        "/api/v1/calendar/proposals/{proposal_id}/suggest-times",
        "/api/v1/calendar/proposals/{proposal_id}/submit",
        "/api/v1/calendar/events/{event_id}/restore-proposal",
    }.issubset(paths)


@pytest.mark.asyncio
async def test_calendar_mutations_all_require_csrf(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """创建、编辑、取消、候选、提交和恢复虽不直接外写，也都必须拒绝无 CSRF 请求。"""
    clients = authenticated_api_clients
    connection_id = await _seed_writable_calendar(clients)
    created = await _create_calendar_proposal(
        clients,
        connection_id=connection_id,
        idempotency_key="task17-csrf-source",
    )
    event_id, snapshot_id, _source_proposal_id = await _seed_restore_source(clients)
    assert created.status_code == 201
    proposal_id = created.json()["id"]
    version = created.json()["version"]

    responses = (
        await clients.owner.post(
            "/api/v1/calendar/proposals",
            headers={"Idempotency-Key": "task17-csrf-create"},
            json={
                "operation_kind": "create",
                "connection_id": str(connection_id),
                "calendar_id": "task17-create-calendar",
                "title": "CSRF rejected event",
                "starts_at": "2030-01-02T09:00:00Z",
                "ends_at": "2030-01-02T10:00:00Z",
                "timezone": "UTC",
            },
        ),
        await clients.owner.patch(
            f"/api/v1/calendar/proposals/{proposal_id}",
            json={"version": version, "title": "CSRF rejected patch"},
        ),
        await clients.owner.delete(f"/api/v1/calendar/proposals/{proposal_id}"),
        await clients.owner.post(
            f"/api/v1/calendar/proposals/{proposal_id}/suggest-times",
            json={"version": version, "search_start": "2030-01-07T09:00:00Z"},
        ),
        await clients.owner.post(
            f"/api/v1/calendar/proposals/{proposal_id}/submit",
            headers={"Idempotency-Key": "task17-csrf-submit"},
            json={"version": version},
        ),
        await clients.owner.post(
            f"/api/v1/calendar/events/{event_id}/restore-proposal",
            headers={"Idempotency-Key": "task17-csrf-restore"},
            json={"snapshot_id": str(snapshot_id)},
        ),
    )

    assert [response.status_code for response in responses] == [403] * len(responses)
    assert all(response.json()["error_code"] == "csrf_rejected" for response in responses)


@pytest.mark.asyncio
async def test_calendar_validation_errors_are_no_store(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """Pydantic 在路由 body 解析前失败时，统一 Problem Details 仍不得被缓存。"""
    clients = authenticated_api_clients
    connection_id = await _seed_writable_calendar(clients)

    response = await clients.owner.post(
        "/api/v1/calendar/proposals",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": "task17-invalid-time-no-store",
        },
        json={
            "operation_kind": "create",
            "connection_id": str(connection_id),
            "calendar_id": "task17-create-calendar",
            "title": "Invalid numeric time",
            "starts_at": 123,
            "ends_at": "2030-01-02T10:00:00Z",
            "timezone": "UTC",
        },
    )

    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["error_code"] == "request_validation_failed"


@pytest.mark.asyncio
async def test_calendar_crud_replay_suggestions_and_no_store_contract(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """真实 HTTP 覆盖同载荷重放、CRUD、候选、版本 CAS、跨用户隐藏与 no-store。"""
    clients = authenticated_api_clients
    connection_id = await _seed_writable_calendar(clients)
    key = "task17-calendar-crud"

    created = await _create_calendar_proposal(
        clients,
        connection_id=connection_id,
        idempotency_key=key,
    )
    replay = await _create_calendar_proposal(
        clients,
        connection_id=connection_id,
        idempotency_key=key,
    )
    assert created.status_code == replay.status_code == 201
    assert replay.json() == created.json()
    proposal_id = created.json()["id"]
    assert created.headers["Cache-Control"] == replay.headers["Cache-Control"] == "no-store"

    listed = await clients.owner.get("/api/v1/calendar/proposals")
    fetched = await clients.owner.get(f"/api/v1/calendar/proposals/{proposal_id}")
    other_fetched = await clients.other.get(f"/api/v1/calendar/proposals/{proposal_id}")
    assert listed.status_code == fetched.status_code == 200
    assert listed.headers["Cache-Control"] == fetched.headers["Cache-Control"] == "no-store"
    assert [item["id"] for item in listed.json()["items"]] == [proposal_id]
    # mutation 只返回持久化提案；GET 另在短读事务结束后计算编辑器事实。
    created_payload = created.json()
    fetched_payload = fetched.json()
    assert created_payload.pop("editor_facts") is None
    editor_facts = fetched_payload.pop("editor_facts")
    assert editor_facts["before_status"] == "not_applicable"
    assert editor_facts["before"] is None
    assert editor_facts["conflict_status"] == "checked"
    assert isinstance(editor_facts["conflicts"], list)
    assert fetched_payload == created_payload
    assert other_fetched.status_code == 404
    assert other_fetched.json()["error_code"] == "calendar_proposal_not_found"

    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    updated = await clients.owner.patch(
        f"/api/v1/calendar/proposals/{proposal_id}",
        headers={"X-CSRF-Token": csrf},
        json={"version": 1, "title": "Updated planning session"},
    )
    stale = await clients.owner.patch(
        f"/api/v1/calendar/proposals/{proposal_id}",
        headers={"X-CSRF-Token": csrf},
        json={"version": 1, "title": "Stale update"},
    )
    assert updated.status_code == 200
    assert updated.headers["Cache-Control"] == "no-store"
    assert updated.json()["version"] == 2
    assert updated.json()["title"] == "Updated planning session"
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "proposal_version_conflict"

    other_csrf = clients.other.cookies.get("ai_employee_csrf") or ""
    other_patch = await clients.other.patch(
        f"/api/v1/calendar/proposals/{proposal_id}",
        headers={"X-CSRF-Token": other_csrf},
        json={"version": 2, "title": "Cross-user update"},
    )
    other_delete = await clients.other.delete(
        f"/api/v1/calendar/proposals/{proposal_id}",
        headers={"X-CSRF-Token": other_csrf},
    )
    assert other_patch.status_code == other_delete.status_code == 404
    assert other_patch.json()["error_code"] == "calendar_proposal_not_found"
    assert other_delete.json()["error_code"] == "calendar_proposal_not_found"

    suggested = await clients.owner.post(
        f"/api/v1/calendar/proposals/{proposal_id}/suggest-times",
        headers={"X-CSRF-Token": csrf},
        json={"version": 2, "search_start": "2030-01-07T09:00:00Z"},
    )
    assert suggested.status_code == 200
    assert suggested.headers["Cache-Control"] == "no-store"
    assert suggested.json()["version"] == 3
    assert len(suggested.json()["candidates"]) <= 3
    assert suggested.json()["completeness"] == "partial"
    assert suggested.json()["missing_connections"] == [str(connection_id)]
    assert suggested.json()["attendee_availability_checked"] is False

    cancelled = await clients.owner.delete(
        f"/api/v1/calendar/proposals/{proposal_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert cancelled.status_code == 200
    assert cancelled.headers["Cache-Control"] == "no-store"
    assert cancelled.json()["status"] == "cancelled"

    async with clients.session_factory() as session:
        proposals = tuple((await session.scalars(select(CalendarChangeProposalModel))).all())
    assert len(proposals) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed_overrides", "expected_error_code"),
    (
        ({"recurring_event_id": "synthetic-series"}, "calendar_recurring_event_unsupported"),
        ({"calendar_can_write": False}, "calendar_read_only"),
        ({"etag": None}, "calendar_event_version_conflict"),
    ),
    ids=("recurrence", "read-only", "etag-missing"),
)
async def test_update_creation_maps_event_precondition_conflicts(
    authenticated_api_clients: AuthenticatedApiClients,
    seed_overrides: dict[str, object],
    expected_error_code: str,
) -> None:
    """重复日程、只读目录与缺失 ETag 都返回稳定、无内容的 409。"""
    clients = authenticated_api_clients
    event_id, _snapshot_id, _proposal_id = await _seed_restore_source(
        clients,
        **seed_overrides,  # type: ignore[arg-type]
    )

    response = await _create_update_proposal(
        clients,
        event_id=event_id,
        idempotency_key=f"task17-update-conflict-{expected_error_code}",
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == expected_error_code


@pytest.mark.asyncio
async def test_create_replay_mismatch_returns_actionable_conflict(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """同键异载荷拒绝重绑，并明确提示客户端更换幂等键。"""
    clients = authenticated_api_clients
    connection_id = await _seed_writable_calendar(clients)

    first = await _create_calendar_proposal(
        clients,
        connection_id=connection_id,
        idempotency_key="task17-create-mismatch",
    )
    mismatch = await _create_calendar_proposal(
        clients,
        connection_id=connection_id,
        idempotency_key="task17-create-mismatch",
        title="Different synthetic planning session",
    )

    assert first.status_code == 201
    assert first.headers["Cache-Control"] == "no-store"
    assert mismatch.status_code == 409
    assert mismatch.headers["Cache-Control"] == "no-store"
    assert mismatch.json()["error_code"] == "idempotency_key_payload_mismatch"
    assert mismatch.json()["detail"] == (
        "Use a new Idempotency-Key when calendar request content changes."
    )


@pytest.mark.asyncio
async def test_submit_returns_actionable_disabled_writes_conflict(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """默认关闭真实写入时 submit 返回可操作 409，且不创建可信任务。"""
    clients = authenticated_api_clients
    connection_id = await _seed_writable_calendar(clients)
    created = await _create_calendar_proposal(
        clients,
        connection_id=connection_id,
        idempotency_key="task17-submit-disabled-source",
    )
    assert created.status_code == 201

    response = await clients.owner.post(
        f"/api/v1/calendar/proposals/{created.json()['id']}/submit",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": "task17-submit-disabled",
        },
        json={"version": created.json()["version"]},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "external_writes_disabled"
    assert response.json()["detail"] == (
        "Enable external calendar writes for this provider and account, then retry."
    )
    async with clients.session_factory() as session:
        assert await session.scalar(select(TaskRunModel.id)) is None


@pytest.mark.asyncio
async def test_submit_success_has_no_store_and_no_outer_crud_transaction(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """submit 调用独立原子用例时不保留 CRUD 事务，202 敏感响应禁止缓存。"""
    clients = authenticated_api_clients
    _event_id, _snapshot_id, proposal_id = await _seed_restore_source(clients)
    transport = clients.owner._transport
    assert isinstance(transport, httpx.ASGITransport)
    app = transport.app
    assert isinstance(app, FastAPI)
    probe = _TransactionProbe()
    engine = app.state.auth_session_factory.engine.sync_engine
    event.listen(engine, "begin", probe.began)
    event.listen(engine, "commit", probe.ended)
    event.listen(engine, "rollback", probe.ended)
    submission = _NoOuterTransactionCalendarSubmission(probe)
    app.dependency_overrides[get_submit_calendar_proposal_use_case] = lambda: submission
    try:
        response = await clients.owner.post(
            f"/api/v1/calendar/proposals/{proposal_id}/submit",
            headers={
                "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
                "Idempotency-Key": "task17-submit-no-outer-transaction",
            },
            json={"version": 1},
        )
    finally:
        app.dependency_overrides.pop(get_submit_calendar_proposal_use_case, None)
        event.remove(engine, "begin", probe.began)
        event.remove(engine, "commit", probe.ended)
        event.remove(engine, "rollback", probe.ended)

    assert response.status_code == 202
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"task_id": str(submission.task_id), "status": "queued"}


@pytest.mark.asyncio
async def test_submit_hides_cross_user_proposal(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """提交事务自身按用户锁定提案，跨用户与缺失资源统一返回 404。"""
    clients = authenticated_api_clients
    connection_id = await _seed_writable_calendar(clients)
    created = await _create_calendar_proposal(
        clients,
        connection_id=connection_id,
        idempotency_key="task17-submit-cross-user-source",
    )
    assert created.status_code == 201

    response = await clients.other.post(
        f"/api/v1/calendar/proposals/{created.json()['id']}/submit",
        headers={
            "X-CSRF-Token": clients.other.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": "task17-submit-cross-user",
        },
        json={"version": created.json()["version"]},
    )

    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["error_code"] == "calendar_proposal_not_found"


@pytest.mark.asyncio
async def test_restore_api_atomically_creates_exact_task_and_outbox(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """有效 before source 经 API 只创建 TaskRun/Outbox，并持久化精确两键输入。"""
    clients = authenticated_api_clients
    event_id, snapshot_id, _proposal_id = await _seed_restore_source(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""

    response = await clients.owner.post(
        f"/api/v1/calendar/events/{event_id}/restore-proposal",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task17-restore-valid",
        },
        json={"snapshot_id": str(snapshot_id)},
    )

    assert response.status_code == 202
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["status"] == "queued"
    task_id = UUID(response.json()["task_id"])
    async with clients.session_factory() as session:
        task = await session.get(TaskRunModel, task_id)
        outbox = await session.scalar(
            select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
        )
        restore_proposal = await session.scalar(
            select(CalendarChangeProposalModel).where(
                CalendarChangeProposalModel.operation_kind == "restore"
            )
        )
    assert task is not None
    assert task.kind == "calendar.restore.prepare"
    assert set(task.input_payload) == {
        "source_snapshot_id",
        "creation_idempotency_key",
    }
    assert task.input_payload == {
        "source_snapshot_id": str(snapshot_id),
        "creation_idempotency_key": "task17-restore-valid",
    }
    assert outbox is not None
    assert restore_proposal is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_kind", "proposal_status", "proposal_operation", "expired"),
    (
        ("desired", CalendarProposalStatus.APPLIED, "update", False),
        ("before", CalendarProposalStatus.EDITING, "update", False),
        ("before", CalendarProposalStatus.APPLIED, "restore", False),
        ("before", CalendarProposalStatus.APPLIED, "update", True),
    ),
    ids=("wrong-kind", "not-applied", "wrong-operation", "expired"),
)
async def test_restore_api_rejects_ineligible_source_without_queue_facts(
    authenticated_api_clients: AuthenticatedApiClients,
    source_kind: Literal["before", "desired"],
    proposal_status: CalendarProposalStatus,
    proposal_operation: Literal["update", "restore"],
    expired: bool,
) -> None:
    """历史类别、生命周期或保留失效均返回 409，且没有部分 Task/Outbox。"""
    clients = authenticated_api_clients
    retain_until = (
        datetime.now(UTC) - timedelta(minutes=1)
        if expired
        else datetime.now(UTC) + timedelta(days=30)
    )
    event_id, snapshot_id, _proposal_id = await _seed_restore_source(
        clients,
        source_kind=source_kind,
        proposal_status=proposal_status,
        proposal_operation=proposal_operation,
        retain_until=retain_until,
    )
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""

    response = await clients.owner.post(
        f"/api/v1/calendar/events/{event_id}/restore-proposal",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": f"task17-restore-invalid-{source_kind}-{proposal_status}",
        },
        json={"snapshot_id": str(snapshot_id)},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "calendar_restore_source_conflict"
    async with clients.session_factory() as session:
        tasks = tuple((await session.scalars(select(TaskRunModel))).all())
        outbox = tuple((await session.scalars(select(OutboxEventModel))).all())
    assert tasks == ()
    assert outbox == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    ("path-event-mismatch", "ciphertext-cleared"),
)
async def test_restore_api_rejects_binding_or_cleared_ciphertext_without_queue_facts(
    authenticated_api_clients: AuthenticatedApiClients,
    variant: str,
) -> None:
    """path UUID 不匹配或 AEAD 三元组已清理时必须 409，且 Task/Outbox 均为零。"""
    clients = authenticated_api_clients
    event_id, snapshot_id, _proposal_id = await _seed_restore_source(clients)
    path_event_id = event_id
    if variant == "path-event-mismatch":
        path_event_id = uuid4()
    elif variant == "ciphertext-cleared":
        async with clients.session_factory.begin() as session:
            snapshot = await session.get(CalendarChangeSnapshotModel, snapshot_id)
            assert snapshot is not None
            snapshot.content_ciphertext = None
            snapshot.content_nonce = None
            snapshot.content_key_version = None
    else:  # pragma: no cover - 参数集合由本测试静态冻结。
        raise AssertionError("unknown restore invalidation variant")

    response = await clients.owner.post(
        f"/api/v1/calendar/events/{path_event_id}/restore-proposal",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": f"task17-restore-{variant}",
        },
        json={"snapshot_id": str(snapshot_id)},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "calendar_restore_source_conflict"
    async with clients.session_factory() as session:
        assert await session.scalar(select(TaskRunModel.id)) is None
        assert await session.scalar(select(OutboxEventModel.id)) is None


@pytest.mark.asyncio
async def test_restore_api_hides_cross_user_source_without_queue_facts(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """跨用户 source 与缺失 source 同为 404，不能泄露存在性或创建任务。"""
    clients = authenticated_api_clients
    event_id, snapshot_id, _proposal_id = await _seed_restore_source(clients)
    other_csrf = clients.other.cookies.get("ai_employee_csrf") or ""

    response = await clients.other.post(
        f"/api/v1/calendar/events/{event_id}/restore-proposal",
        headers={
            "X-CSRF-Token": other_csrf,
            "Idempotency-Key": "task17-restore-cross-user",
        },
        json={"snapshot_id": str(snapshot_id)},
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "calendar_restore_source_not_found"
    async with clients.session_factory() as session:
        assert await session.scalar(select(TaskRunModel.id)) is None
        assert await session.scalar(select(OutboxEventModel.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_invalidation",
    ("ciphertext-cleared", "expired", "event-deleted"),
)
async def test_restore_api_replay_returns_same_task_after_source_invalidation(
    authenticated_api_clients: AuthenticatedApiClients,
    source_invalidation: str,
) -> None:
    """任务已提交后，source 清理或失效不得破坏同一冻结输入的精确重放。"""
    clients = authenticated_api_clients
    event_id, snapshot_id, _proposal_id = await _seed_restore_source(clients)
    headers = {
        "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
        "Idempotency-Key": "task17-restore-replay",
    }
    path = f"/api/v1/calendar/events/{event_id}/restore-proposal"
    payload = {"snapshot_id": str(snapshot_id)}

    first = await clients.owner.post(path, headers=headers, json=payload)
    assert first.status_code == 202

    async with clients.session_factory.begin() as session:
        snapshot = await session.get(CalendarChangeSnapshotModel, snapshot_id)
        assert snapshot is not None
        if source_invalidation == "ciphertext-cleared":
            snapshot.content_ciphertext = None
            snapshot.content_nonce = None
            snapshot.content_key_version = None
        elif source_invalidation == "expired":
            snapshot.retain_until = datetime.now(UTC) - timedelta(minutes=1)
        elif source_invalidation == "event-deleted":
            event = await session.get(CalendarEventModel, event_id)
            assert event is not None
            await session.delete(event)
        else:  # pragma: no cover - 参数集合由本测试静态冻结。
            raise AssertionError("unknown source invalidation")

    replay = await clients.owner.post(path, headers=headers, json=payload)

    assert replay.status_code == 202
    assert replay.json()["task_id"] == first.json()["task_id"]
    async with clients.session_factory() as session:
        tasks = tuple((await session.scalars(select(TaskRunModel))).all())
        outbox = tuple((await session.scalars(select(OutboxEventModel))).all())
    assert len(tasks) == 1
    assert len(outbox) == 1


@pytest.mark.asyncio
async def test_restore_api_same_key_different_input_keeps_payload_mismatch_precedence(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """同键异 source 必须先按持久任务判定 409，即使新 source 的 AEAD 已被清理。"""
    clients = authenticated_api_clients
    first_event_id, first_snapshot_id, _first_proposal_id = await _seed_restore_source(clients)
    second_event_id, second_snapshot_id, _second_proposal_id = await _seed_restore_source(clients)
    headers = {
        "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
        "Idempotency-Key": "task17-restore-payload-mismatch",
    }

    first = await clients.owner.post(
        f"/api/v1/calendar/events/{first_event_id}/restore-proposal",
        headers=headers,
        json={"snapshot_id": str(first_snapshot_id)},
    )
    assert first.status_code == 202

    async with clients.session_factory.begin() as session:
        second_snapshot = await session.get(CalendarChangeSnapshotModel, second_snapshot_id)
        assert second_snapshot is not None
        second_snapshot.content_ciphertext = None
        second_snapshot.content_nonce = None
        second_snapshot.content_key_version = None

    mismatch = await clients.owner.post(
        f"/api/v1/calendar/events/{second_event_id}/restore-proposal",
        headers=headers,
        json={"snapshot_id": str(second_snapshot_id)},
    )

    assert mismatch.status_code == 409
    assert mismatch.json()["error_code"] == "idempotency_key_payload_mismatch"
    async with clients.session_factory() as session:
        tasks = tuple((await session.scalars(select(TaskRunModel))).all())
        outbox = tuple((await session.scalars(select(OutboxEventModel))).all())
    assert len(tasks) == 1
    assert len(outbox) == 1
