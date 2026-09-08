"""验证 marker 精确 planner、ordinal 与 recovery-only durable execution。"""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import text

from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
from ai_employee.application.use_cases.sync_calendar import SyncCalendarUseCase
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.calendar import (
    SqlAlchemyCalendarSyncRepositoryFactory,
)
from ai_employee.integrations.registry import ProviderAdapterRegistry
from tests.integration.alembic_commands import run_alembic_upgrade
from tests.integration.m2.test_credential_rotation_repository import CONNECTION_ID, USER_ID
from tests.integration.operations.test_calendar_aad_0019_preflight import (
    BASENAME,
    IMAGE,
    NOW,
    SCOPE,
    FakeAdapters,
    RecordingReader,
    run_fixture,
    seed_pair,
)
from tests.integration.operations.test_calendar_aad_0019_preflight import (
    aad_oauth as aad_oauth,  # noqa: PLC0414 - 显式re-export让pytest发现本模块fixture。
)
from tests.integration.operations.test_calendar_aad_0019_preflight import (
    aad_source as aad_source,  # noqa: PLC0414 - 显式re-export让pytest发现本模块fixture。
)
from tests.integration.operations.test_calendar_aad_0019_preflight import (
    isolated_database as isolated_database,  # noqa: PLC0414 - 保持本模块生命周期fixture覆盖。
)
from tests.integration.operations.test_calendar_aad_0019_preflight import (
    migrated_database as migrated_database,  # noqa: PLC0414 - 保持本模块生命周期fixture覆盖。
)

TASK_ID = UUID("00000000-0000-0000-0000-000000000401")
PAIR_DIGEST = hashlib.sha256(f"20260809_0019\0{CONNECTION_ID}\0{SCOPE}".encode()).hexdigest()
PAYLOAD = {
    "connection_id": str(CONNECTION_ID),
    "scope_key": SCOPE,
    "recovery_revision": "20260809_0019",
    "pair_digest": PAIR_DIGEST,
    "recovery_attempt_ordinal": 1,
}
KEY = f"calendar-aad-0019:{PAIR_DIGEST}:attempt:1"


@pytest.fixture
async def aad_recovery(aad_oauth, tmp_path):
    """经真实 active preflight 与注入式 guard 完成0018→0019，不手工 stamp/清 marker。"""
    from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadBinding
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        CalendarAadArtifactFile,
        CalendarAadMigrationArtifactGuard,
        calendar_aad_rollout_lease,
    )

    sessions, _, _, config = aad_oauth
    await seed_pair(sessions)
    await seed_pair(sessions, scope="synthetic-unmarked-calendar", complete=False)
    artifact = await run_fixture(aad_oauth, tmp_path)

    def migrate():
        """继续沿已有 typed maintenance/grant lifecycle 使用同一个真实 artifact guard。"""
        with calendar_aad_rollout_lease(sessions.engine.url) as lease:
            config.attributes["calendar_aad_0019_guard"] = CalendarAadMigrationArtifactGuard(
                artifact_file=CalendarAadArtifactFile(
                    tmp_path, CalendarAadBinding(BASENAME, IMAGE)
                ),
                lease=lease,
                clock=lambda: NOW,
            )
            run_alembic_upgrade(config, "20260809_0019")

    await asyncio.to_thread(migrate)
    return aad_oauth, tmp_path, artifact


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["generation", "marker", "ordinal"])
async def test_calendar_aad_marked_final_cas_rejects_provider_window_drift(aad_recovery, drift):
    """现有普通 sync 无 marker/generation/ordinal CAS，实际 provider 窗口漂移必须回滚事件写入。

    直接进入公开 marked 路径；数据库事务、任务输入、field encryption 与真实 guard
    均未替换。已保存的 RED 记录证明旧 execute 会错误提交。
    """
    from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadBinding
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        CalendarAadArtifactFile,
        CalendarAadCurrentGuard,
        SqlAlchemyCalendarAadPreflightRepository,
        async_calendar_aad_rollout_lease,
    )

    state, directory, artifact = aad_recovery
    sessions, cipher, _, _ = state
    async with sessions.begin() as session:
        session.add(
            TaskRunModel(
                id=TASK_ID,
                user_id=USER_ID,
                kind="calendar.aad_0019.resync",
                status="running",
                input_payload=PAYLOAD,
                idempotency_key=KEY,
                started_at=NOW,
                attempt_count=1,
                lease_owner="synthetic-recovery",
                lease_expires_at=NOW + timedelta(minutes=10),
            )
        )
    async with sessions() as session:
        before = await session.scalar(
            text("SELECT description_ciphertext FROM calendar_events WHERE calendar_id=:scope"),
            {"scope": SCOPE},
        )

    async def mutate():
        """在 provider 响应之前持久化真实竞争事实，禁止改写或模拟仓储返回值。"""
        async with sessions.begin() as session:
            if drift == "generation":
                await session.execute(
                    text(
                        "UPDATE oauth_connections SET authorization_generation=authorization_generation+1"
                    )
                )
            elif drift == "marker":
                await session.execute(
                    text("UPDATE sync_cursors SET last_error_code=NULL WHERE scope_key=:scope"),
                    {"scope": SCOPE},
                )
            else:
                task = await session.get(TaskRunModel, TASK_ID)
                task.input_payload = {**PAYLOAD, "recovery_attempt_ordinal": 2}

    reader = RecordingReader(before_page=mutate)
    use_case = SyncCalendarUseCase(
        SqlAlchemyCalendarSyncRepositoryFactory(sessions),
        ProviderAdapterRegistry(google_calendar=reader),
        cipher,
    )
    async with async_calendar_aad_rollout_lease(sessions.engine.url) as lease:
        guard = CalendarAadCurrentGuard(
            repository=SqlAlchemyCalendarAadPreflightRepository(sessions),
            artifact_file=CalendarAadArtifactFile(directory, CalendarAadBinding(BASENAME, IMAGE)),
            artifact=artifact,
            lease=lease,
            clock=lambda: NOW,
            expected_revision="20260809_0019",
        )
        from ai_employee.application.use_cases.calendar_aad_recovery import CalendarAadTaskBinding
        from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError

        binding = CalendarAadTaskBinding.from_task(
            LeasedTask(
                task_id=TASK_ID,
                kind="calendar.aad_0019.resync",
                input_payload=PAYLOAD,
                user_id=USER_ID,
                started_at=NOW,
                lease_owner="synthetic-recovery",
            )
        )
        with pytest.raises(CalendarAadRolloutError) as failure:
            await use_case.execute_marked_scope(binding=binding, guard=guard, clock=lambda: NOW)
        assert failure.value.error_code == "calendar_aad_recovery_state_changed"
        assert reader.scopes == [SCOPE]
    async with sessions() as session:
        assert (
            await session.scalar(
                text("SELECT description_ciphertext FROM calendar_events WHERE calendar_id=:scope"),
                {"scope": SCOPE},
            )
            == before
        )
        assert (
            await session.scalar(
                text(
                    "SELECT description_aad_version FROM calendar_events WHERE calendar_id=:scope"
                ),
                {"scope": SCOPE},
            )
            == 1
        )


async def plan_fixture(aad_recovery):
    """在真实 revision lease 与 current guard 下仅运行一次 planner。"""
    from ai_employee.application.use_cases.calendar_aad_recovery import CalendarAadRecoveryUseCase
    from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadBinding
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        CalendarAadArtifactFile,
        CalendarAadCurrentGuard,
        SqlAlchemyCalendarAadPreflightRepository,
        async_calendar_aad_rollout_lease,
    )
    from ai_employee.infrastructure.db.repositories.calendar_aad_recovery import (
        SqlAlchemyCalendarAadRecoveryStoreFactory,
    )

    state, directory, artifact = aad_recovery
    sessions, _, _, _ = state
    async with async_calendar_aad_rollout_lease(sessions.engine.url) as lease:
        guard = CalendarAadCurrentGuard(
            repository=SqlAlchemyCalendarAadPreflightRepository(sessions),
            artifact_file=CalendarAadArtifactFile(directory, CalendarAadBinding(BASENAME, IMAGE)),
            artifact=artifact,
            lease=lease,
            clock=lambda: NOW,
            expected_revision="20260809_0019",
        )
        return await CalendarAadRecoveryUseCase(
            stores=SqlAlchemyCalendarAadRecoveryStoreFactory(sessions),
            guard=guard,
            artifact=artifact,
        ).plan()


@pytest.mark.asyncio
async def test_calendar_aad_planner_reuses_active_and_allocates_only_after_terminal_failure(
    aad_recovery,
):
    """首次三个事实原子创建；active 不追加，failed 才产生新 ordinal，旧终态逐字保留。"""
    state, _, _ = aad_recovery
    sessions, _, _, _ = state
    first = await plan_fixture(aad_recovery)
    again = await plan_fixture(aad_recovery)
    assert again == first
    assert len(first) == 1
    assert first[0].recovery_attempt_ordinal == 1
    async with sessions() as session:
        row = await session.get(TaskRunModel, first[0].task_id)
        assert row.input_payload == PAYLOAD
        assert row.idempotency_key == KEY
        assert (
            await session.scalar(
                text("SELECT count(*) FROM outbox_events WHERE topic='task.execute'")
            )
            == 1
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM audit_events WHERE event_type='task.created'")
            )
            == 1
        )
    async with sessions.begin() as session:
        row = await session.get(TaskRunModel, first[0].task_id)
        row.status, row.finished_at, row.error_code = "failed", NOW, "synthetic_read_failed"
    async with sessions() as session:
        before = await session.scalar(
            text("SELECT row_to_json(t)::text FROM task_runs t WHERE id=:id"),
            {"id": first[0].task_id},
        )
    second = await plan_fixture(aad_recovery)
    assert second[0].recovery_attempt_ordinal == 2
    assert second[0].task_id != first[0].task_id
    async with sessions() as session:
        assert (
            await session.scalar(
                text("SELECT row_to_json(t)::text FROM task_runs t WHERE id=:id"),
                {"id": first[0].task_id},
            )
            == before
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["succeeded", "waiting_approval", "reconciling", "needs_attention"]
)
async def test_calendar_aad_planner_impossible_status_keeps_ordinal_and_marker(
    aad_recovery, status
):
    """不可能的状态与 marker 并存必须 fail closed，不能复活旧 TaskRun 或创建下一次。"""
    state, _, _ = aad_recovery
    sessions, _, _, _ = state
    first = await plan_fixture(aad_recovery)
    async with sessions.begin() as session:
        row = await session.get(TaskRunModel, first[0].task_id)
        row.status = status
    with pytest.raises(CalendarAadRolloutError) as failure:
        await plan_fixture(aad_recovery)
    assert failure.value.error_code == "calendar_aad_recovery_invariant"
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM task_runs")) == 1


@pytest.mark.asyncio
async def test_calendar_aad_exact_task_runner_preserves_unrelated_outbox_and_writes_v2(
    aad_recovery,
):
    """真实 CLI exact dispatch + DurableTaskRunner 仅执行 planner 返回的 pair，重投终态零调用。"""
    from ai_employee.cli.calendar_aad_0019 import run_recovery
    from ai_employee.config import Settings
    from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepository

    state, directory, _ = aad_recovery
    sessions, cipher, coordinator, _ = state
    async with sessions.begin() as session:
        unrelated = await SqlAlchemyTaskRepository(session).create_with_outbox(
            user_id=USER_ID,
            kind="sync_calendar",
            input_payload={"connection_id": str(CONNECTION_ID), "scope_key": "directory"},
            idempotency_key="synthetic-unrelated-calendar",
        )
    adapters = FakeAdapters()
    kwargs = {
        "sessions": sessions,
        "cipher": cipher,
        "coordinator": coordinator,
        "adapters": adapters,
        "backup_directory": directory,
        "basename": BASENAME,
        "immutable_image_id": IMAGE,
        "clock": lambda: NOW,
        "settings": Settings(app_env="test", app_test_mode=True),
    }
    result = await run_recovery(**kwargs)
    assert result.remaining_markers == 0
    assert adapters.reader.scopes == [SCOPE]
    assert adapters.provider.calls == 0
    async with sessions() as session:
        row = await session.get(TaskRunModel, unrelated.task_id)
        assert row.status == "created"
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM outbox_events WHERE aggregate_id=:id AND published_at IS NOT NULL"
                ),
                {"id": unrelated.task_id},
            )
            == 0
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM calendar_events WHERE calendar_id=:scope AND description_aad_version=2 AND location_aad_version=2"
                ),
                {"scope": SCOPE},
            )
            == 1
        )
        assert await session.scalar(text("SELECT count(*) FROM approval_requests")) == 0
        assert await session.scalar(text("SELECT count(*) FROM tool_executions")) == 0
    await run_recovery(**kwargs)
    assert adapters.reader.scopes == [SCOPE]


async def run_recovery_fixture(aad_recovery, adapters, *, clock=lambda: NOW):
    """真实 CLI composition，仅注入时钟与 provider；任务存储、Outbox 和 Runner 均真实。"""
    from ai_employee.cli.calendar_aad_0019 import run_recovery
    from ai_employee.config import Settings

    state, directory, _ = aad_recovery
    sessions, cipher, coordinator, _ = state
    return await run_recovery(
        sessions=sessions,
        cipher=cipher,
        coordinator=coordinator,
        adapters=adapters,
        backup_directory=directory,
        basename=BASENAME,
        immutable_image_id=IMAGE,
        clock=clock,
        settings=Settings(app_env="test", app_test_mode=True),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["created", "queued", "running", "retry_scheduled"])
async def test_calendar_aad_planner_reuses_each_active_state(aad_recovery, status):
    """四种活动状态不论投递次数都复用同一 ordinal，不追加 Task/Audit/Outbox。"""
    state, _, _ = aad_recovery
    sessions, _, _, _ = state
    first = await plan_fixture(aad_recovery)
    async with sessions.begin() as session:
        row = await session.get(TaskRunModel, first[0].task_id)
        row.status = status
        row.attempt_count = 3
    assert await plan_fixture(aad_recovery) == first
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM task_runs")) == 1
        assert (
            await session.scalar(
                text("SELECT count(*) FROM outbox_events WHERE topic='task.execute'")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_calendar_aad_planner_multiple_active_ordinals_fail_closed(aad_recovery):
    """两个活动 ordinal 是损坏事实，不能任选一个赢家或继续追加。"""
    from ai_employee.application.use_cases.calendar_aad_recovery import CalendarAadRecoveryInput
    from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepository

    state, _, _ = aad_recovery
    sessions, _, _, _ = state
    await plan_fixture(aad_recovery)
    second = CalendarAadRecoveryInput(CONNECTION_ID, SCOPE, PAIR_DIGEST, 2)
    async with sessions.begin() as session:
        await SqlAlchemyTaskRepository(session).create_with_outbox(
            user_id=USER_ID,
            kind="calendar.aad_0019.resync",
            input_payload=second.payload(),
            idempotency_key=second.idempotency_key,
        )
    with pytest.raises(CalendarAadRolloutError) as failure:
        await plan_fixture(aad_recovery)
    assert failure.value.error_code == "calendar_aad_recovery_invariant"
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM task_runs")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["permanent", "transient"])
async def test_calendar_aad_provider_failure_preserves_marker_and_attempt_rules(
    aad_recovery, failure_kind
):
    """永久失败只在后续显式调用分配新 ordinal；临时重试尊重 Outbox 到期和原预算。"""
    from ai_employee.domain.errors import PermanentProviderError, TransientProviderError

    state, _, _ = aad_recovery
    sessions, _, _, _ = state
    adapters = FakeAdapters()
    clock = [NOW]

    async def fail():
        """网络端失败发生在实际 initial scope 进入后，不改变任何本地 provider 状态。"""
        if failure_kind == "permanent":
            raise PermanentProviderError(
                error_code="synthetic_calendar_failed", message="Synthetic calendar failure"
            )
        raise TransientProviderError(
            error_code="synthetic_calendar_unavailable",
            message="Synthetic calendar unavailable",
            retry_after=1,
        )

    adapters.reader.before_page = fail
    first = await run_recovery_fixture(aad_recovery, adapters, clock=lambda: clock[0])
    assert first.remaining_markers == 1
    async with sessions() as session:
        row = await session.get(TaskRunModel, first.planned[0].task_id)
        assert row.status == ("failed" if failure_kind == "permanent" else "retry_scheduled")
        assert (
            await session.scalar(
                text("SELECT last_error_code FROM sync_cursors WHERE scope_key=:scope"),
                {"scope": SCOPE},
            )
            == "calendar_event_resync_required"
        )
        assert (
            await session.scalar(
                text(
                    "SELECT description_aad_version FROM calendar_events WHERE calendar_id=:scope"
                ),
                {"scope": SCOPE},
            )
            == 1
        )
    adapters.reader.before_page = None
    if failure_kind == "transient":
        early = await run_recovery_fixture(aad_recovery, adapters, clock=lambda: clock[0])
        assert early.planned == first.planned
        assert early.remaining_markers == 1
        assert adapters.reader.scopes == [SCOPE]
        clock[0] += timedelta(seconds=10)
    second = await run_recovery_fixture(aad_recovery, adapters, clock=lambda: clock[0])
    assert second.remaining_markers == 0
    assert second.planned[0].recovery_attempt_ordinal == (2 if failure_kind == "permanent" else 1)
    assert adapters.provider.calls == 0
    assert adapters.reader.scopes == [SCOPE, SCOPE]


@pytest.mark.asyncio
async def test_calendar_aad_final_guard_rolls_back_events_cursor_and_audit(
    aad_recovery, monkeypatch
):
    """完成本地 upsert/marker flush 后才越界，最终 guard 必须使全部领域事实回滚。"""
    from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository

    state, _, _ = aad_recovery
    sessions, _, _, _ = state
    clock = [NOW]
    original = SqlAlchemyCalendarSyncRepository.finish_marked_scope

    async def finish_then_expire(self, **kwargs):
        """保留真实 SQL 和 flush，只推进注入时钟。"""
        await original(self, **kwargs)
        clock[0] = NOW + timedelta(minutes=45)

    monkeypatch.setattr(SqlAlchemyCalendarSyncRepository, "finish_marked_scope", finish_then_expire)
    adapters = FakeAdapters()
    with pytest.raises(CalendarAadRolloutError) as failure:
        await run_recovery_fixture(aad_recovery, adapters, clock=lambda: clock[0])
    assert failure.value.error_code == "calendar_aad_rollout_deadline_exceeded"
    assert adapters.reader.scopes == [SCOPE]
    async with sessions() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT description_aad_version FROM calendar_events WHERE calendar_id=:scope"
                ),
                {"scope": SCOPE},
            )
            == 1
        )
        assert (
            await session.scalar(
                text("SELECT last_error_code FROM sync_cursors WHERE scope_key=:scope"),
                {"scope": SCOPE},
            )
            == "calendar_event_resync_required"
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM audit_events WHERE event_type='source.calendar.aad_0019_resynced'"
                )
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "bool_ordinal",
        "directory",
        "revision",
        "padded_uuid",
        "foreign_user",
        "missing_cursor",
        "credential_aad",
    ],
)
async def test_calendar_aad_worker_rejects_invalid_input_or_state_before_provider(
    aad_recovery, mutation
):
    """不合法任务、跨用户、缺 cursor 和错误双凭据在 reader 解析前拒绝，不生成修复行。"""
    from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadBinding
    from ai_employee.domain.errors import DomainError
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        CalendarAadArtifactFile,
        CalendarAadCurrentGuard,
        SqlAlchemyCalendarAadPreflightRepository,
        async_calendar_aad_rollout_lease,
    )
    from ai_employee.workers.sync_calendar import CalendarAadRecoveryTaskStep
    from tests.integration.m2.test_credential_rotation_repository import OTHER_USER_ID

    state, directory, artifact = aad_recovery
    sessions, cipher, coordinator, _ = state
    first = await plan_fixture(aad_recovery)
    async with sessions.begin() as session:
        row = await session.get(TaskRunModel, first[0].task_id)
        row.status, row.lease_owner, row.lease_expires_at = (
            "running",
            "synthetic-recovery",
            NOW + timedelta(minutes=10),
        )
        if mutation == "extra":
            row.input_payload = {**PAYLOAD, "extra": True}
        elif mutation == "bool_ordinal":
            row.input_payload = {**PAYLOAD, "recovery_attempt_ordinal": True}
        elif mutation == "directory":
            row.input_payload = {**PAYLOAD, "scope_key": "directory"}
        elif mutation == "revision":
            row.input_payload = {**PAYLOAD, "recovery_revision": "20260809_0018"}
        elif mutation == "padded_uuid":
            row.input_payload = {**PAYLOAD, "connection_id": " " + str(CONNECTION_ID)}
        elif mutation == "foreign_user":
            # 保留数据库组合 FK 强约束，模拟外来调用方伪造 user 快照，不篡改不可变 owner。
            pass
        elif mutation == "missing_cursor":
            await session.execute(
                text("DELETE FROM sync_cursors WHERE scope_key=:scope"), {"scope": SCOPE}
            )
        else:
            await session.execute(
                text(
                    "UPDATE encrypted_credentials SET ciphertext=:cipher WHERE credential_kind='refresh_token'"
                ),
                {"cipher": b"synthetic-invalid-aead"},
            )
        task = LeasedTask(
            task_id=row.id,
            kind=row.kind,
            input_payload=row.input_payload,
            user_id=OTHER_USER_ID if mutation == "foreign_user" else row.user_id,
            lease_owner="synthetic-recovery",
            started_at=NOW,
        )
    adapters = FakeAdapters()
    async with async_calendar_aad_rollout_lease(sessions.engine.url) as lease:
        guard = CalendarAadCurrentGuard(
            repository=SqlAlchemyCalendarAadPreflightRepository(sessions),
            artifact_file=CalendarAadArtifactFile(directory, CalendarAadBinding(BASENAME, IMAGE)),
            artifact=artifact,
            lease=lease,
            clock=lambda: NOW,
            expected_revision="20260809_0019",
        )
        step = CalendarAadRecoveryTaskStep(
            sessions=sessions,
            cipher=cipher,
            coordinator=coordinator,
            adapters=adapters,
            guard=guard,
            clock=lambda: NOW,
        )
        with pytest.raises(DomainError) as failure:
            await step.execute(task)
    expected_code = {
        "foreign_user": "calendar_aad_recovery_state_changed",
        "missing_cursor": "calendar_aad_local_recoverability_failed",
        "credential_aad": "oauth_credential_state_conflict",
    }.get(mutation, "calendar_aad_recovery_input_invalid")
    assert failure.value.error_code == expected_code
    assert adapters.provider.calls == 0
    assert adapters.reader.scopes == []
    assert adapters.accesses == []


@asynccontextmanager
async def recovery_guard_fixture(aad_recovery, *, clock=lambda: NOW):
    """为直接 planner/Worker 边界提供真实 revision lease、artifact 和当前事实 guard。"""
    from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadBinding
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        CalendarAadArtifactFile,
        CalendarAadCurrentGuard,
        SqlAlchemyCalendarAadPreflightRepository,
        async_calendar_aad_rollout_lease,
    )

    state, directory, artifact = aad_recovery
    sessions, _, _, _ = state
    async with async_calendar_aad_rollout_lease(sessions.engine.url) as lease:
        yield CalendarAadCurrentGuard(
            repository=SqlAlchemyCalendarAadPreflightRepository(sessions),
            artifact_file=CalendarAadArtifactFile(directory, CalendarAadBinding(BASENAME, IMAGE)),
            artifact=artifact,
            lease=lease,
            clock=clock,
            expected_revision="20260809_0019",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", ["empty", "subset", "empty_fields"])
async def test_calendar_aad_recovery_preserves_unreturned_v1_and_writes_empty_fields_as_v2(
    aad_recovery, snapshot
):
    """完整分页可为空或不含历史项；只写v2，未返回v1字节保留给后续只读审计计数。

    空描述/地点也通过既有AEAD writer保存完整v2三元组，因此当前affected set不会因
    字段清空缩小；不添加删除、历史解密或重写rollout artifact 的例外。
    """
    from ai_employee.application.ports.calendar import CalendarSyncPage

    state, _, artifact = aad_recovery
    sessions, _, _, _ = state
    async with sessions() as session:
        before = (
            await session.execute(
                text(
                    "SELECT description_ciphertext,location_ciphertext FROM calendar_events WHERE calendar_id=:scope"
                ),
                {"scope": SCOPE},
            )
        ).one()

    class SnapshotReader(RecordingReader):
        """继续复用规范合成事件，只变化本次初始页的显式供应商集合。"""

        async def initial_pages(self, calendar_id):
            async for page in super().initial_pages(calendar_id):
                if snapshot == "empty":
                    events = ()
                elif snapshot == "subset":
                    events = (replace(page.events[0], event_id="synthetic-new-event"),)
                else:
                    events = (replace(page.events[0], description="", location=""),)
                yield CalendarSyncPage(events, None, page.next_cursor)

    adapters = FakeAdapters(reader=SnapshotReader())
    result = await run_recovery_fixture(aad_recovery, adapters)
    assert result.remaining_markers == 0 and adapters.reader.scopes == [SCOPE]
    async with sessions() as session:
        original = (
            await session.execute(
                text(
                    "SELECT description_ciphertext,location_ciphertext,description_aad_version,location_aad_version FROM calendar_events WHERE calendar_id=:scope AND provider_event_id='synthetic-event'"
                ),
                {"scope": SCOPE},
            )
        ).one()
        if snapshot == "empty_fields":
            assert original[:2] != before and original[2:] == (2, 2)
            assert all(value is not None for value in original[:2])
        else:
            assert original[:2] == before and original[2:] == (1, 1)
        if snapshot == "subset":
            assert (
                await session.scalar(
                    text(
                        "SELECT description_aad_version FROM calendar_events WHERE provider_event_id='synthetic-new-event'"
                    )
                )
                == 2
            )
        assert (
            await session.scalar(
                text("SELECT last_error_code FROM sync_cursors WHERE scope_key=:scope"),
                {"scope": SCOPE},
            )
            is None
        )
    async with recovery_guard_fixture(aad_recovery) as guard:
        assert await guard.verify() == artifact.rollout_deadline


@pytest.mark.asyncio
async def test_calendar_aad_planner_final_guard_rolls_back_all_three_facts(aad_recovery):
    """planner已经写入TaskRun/Audit/Outbox但尚未提交时到期，三事实和ordinal必须一同回滚。"""
    from ai_employee.application.use_cases.calendar_aad_recovery import CalendarAadRecoveryUseCase
    from ai_employee.infrastructure.db.repositories.calendar_aad_recovery import (
        SqlAlchemyCalendarAadRecoveryStoreFactory,
    )

    state, _, artifact = aad_recovery
    sessions, _, _, _ = state
    clock = [NOW]
    async with sessions() as session:
        before = await session.scalar(text("SELECT count(*) FROM audit_events"))
    async with recovery_guard_fixture(aad_recovery, clock=lambda: clock[0]) as guard:

        class FinalGuard:
            """只在第二个真实验证之前推进注入时钟；仓储、事务和guard实现保持实际路径。"""

            calls = 0

            async def verify(self):
                self.calls += 1
                if self.calls == 2:
                    clock[0] += timedelta(minutes=45)
                return await guard.verify()

        with pytest.raises(CalendarAadRolloutError) as failure:
            await CalendarAadRecoveryUseCase(
                stores=SqlAlchemyCalendarAadRecoveryStoreFactory(sessions),
                guard=FinalGuard(),
                artifact=artifact,
            ).plan()
        assert failure.value.error_code == "calendar_aad_rollout_deadline_exceeded"
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM task_runs")) == 0
        assert await session.scalar(text("SELECT count(*) FROM outbox_events")) == 0
        assert await session.scalar(text("SELECT count(*) FROM audit_events")) == before
    assert (await plan_fixture(aad_recovery))[0].recovery_attempt_ordinal == 1


@pytest.mark.asyncio
async def test_calendar_aad_concurrent_planners_serialize_the_same_cursor(
    aad_recovery, monkeypatch
):
    """两个真实事务同时规划同pair；cursor锁使第二个等待并复用同一ordinal及三个事实。"""
    from ai_employee.application.use_cases.calendar_aad_recovery import CalendarAadRecoveryUseCase
    from ai_employee.infrastructure.db.repositories.calendar_aad_recovery import (
        SqlAlchemyCalendarAadRecoveryStore,
        SqlAlchemyCalendarAadRecoveryStoreFactory,
    )

    state, _, artifact = aad_recovery
    sessions, _, _, _ = state
    second_entered = asyncio.Event()
    calls = 0
    original = SqlAlchemyCalendarAadRecoveryStore.lock_marked_pair

    async def interleave(self, pair):
        """第一事务实际持有cursor后等待第二事务到达锁入口，避免顺序执行冒充并发。"""
        nonlocal calls
        calls += 1
        if calls == 1:
            result = await original(self, pair)
            await asyncio.wait_for(second_entered.wait(), 3)
            return result
        second_entered.set()
        return await original(self, pair)

    monkeypatch.setattr(SqlAlchemyCalendarAadRecoveryStore, "lock_marked_pair", interleave)
    async with recovery_guard_fixture(aad_recovery) as guard:
        planners = [
            CalendarAadRecoveryUseCase(
                stores=SqlAlchemyCalendarAadRecoveryStoreFactory(sessions),
                guard=guard,
                artifact=artifact,
            )
            for _ in range(2)
        ]
        first, second = await asyncio.wait_for(
            asyncio.gather(*(planner.plan() for planner in planners)), 5
        )
    assert first == second and len(first) == 1 and calls == 2
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM task_runs")) == 1
        assert (
            await session.scalar(
                text("SELECT count(*) FROM outbox_events WHERE topic='task.execute'")
            )
            == 1
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM audit_events WHERE task_id=:id"),
                {"id": first[0].task_id},
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_replay", [False, True])
async def test_calendar_aad_marker_absence_worker_and_terminal_runner_are_provider_free(
    aad_recovery, terminal_replay
):
    """直接Worker的已清marker和真实runner的终态投递均no-op，不能仅靠空planner证明幂等。"""
    from ai_employee.config import Settings
    from ai_employee.workers.execute_task import build_task_runner_for_session
    from ai_employee.workers.sync_calendar import CalendarAadRecoveryTaskStep

    state, _, _ = aad_recovery
    sessions, cipher, coordinator, _ = state
    first = await run_recovery_fixture(aad_recovery, FakeAdapters()) if terminal_replay else None
    task_id = first.planned[0].task_id if first else (await plan_fixture(aad_recovery))[0].task_id
    async with sessions.begin() as session:
        row = await session.get(TaskRunModel, task_id)
        if not terminal_replay:
            row.status, row.lease_owner, row.lease_expires_at = (
                "running",
                "synthetic-recovery",
                NOW + timedelta(minutes=10),
            )
            await session.execute(
                text("UPDATE sync_cursors SET last_error_code=NULL WHERE scope_key=:scope"),
                {"scope": SCOPE},
            )
        task = LeasedTask(
            task_id=row.id,
            kind=row.kind,
            input_payload=row.input_payload,
            user_id=row.user_id,
            lease_owner="synthetic-recovery",
            started_at=NOW,
        )
    adapters = FakeAdapters()
    async with recovery_guard_fixture(aad_recovery) as guard:
        step = CalendarAadRecoveryTaskStep(
            sessions=sessions,
            cipher=cipher,
            coordinator=coordinator,
            adapters=adapters,
            guard=guard,
            clock=lambda: NOW,
        )
        if terminal_replay:
            runner = build_task_runner_for_session(
                sessions,
                settings=Settings(app_env="test", app_test_mode=True),
                calendar_aad_step=step,
                clock=lambda: NOW,
            )
            await runner.run(task_id)
            await runner.run(task_id)
        else:
            await step.execute(task)
    assert adapters.reader.scopes == [] and adapters.accesses == [] and adapters.provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["key", "input", "unexpected_marker"])
async def test_calendar_aad_planner_rejects_malformed_history_or_unbound_marker(
    aad_recovery, mutation
):
    """部分身份命中的损坏历史不能被跳过；artifact之外的新marker也不能获分配任务。"""
    state, _, _ = aad_recovery
    sessions, _, _, _ = state
    first = await plan_fixture(aad_recovery)
    async with sessions.begin() as session:
        row = await session.get(TaskRunModel, first[0].task_id)
        if mutation == "key":
            row.idempotency_key = row.idempotency_key.removesuffix("1") + "01"
        elif mutation == "input":
            row.input_payload = {**row.input_payload, "extra": True}
        else:
            await session.execute(
                text(
                    "UPDATE sync_cursors SET last_error_code='calendar_event_resync_required' WHERE scope_key='synthetic-unmarked-calendar'"
                )
            )
    with pytest.raises(CalendarAadRolloutError) as failure:
        await plan_fixture(aad_recovery)
    assert failure.value.error_code == (
        "calendar_aad_affected_set_changed"
        if mutation == "unexpected_marker"
        else "calendar_aad_recovery_invariant"
    )
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM task_runs")) == 1
