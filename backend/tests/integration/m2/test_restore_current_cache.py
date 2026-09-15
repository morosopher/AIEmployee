"""证明恢复的真实精确 GET 与本地版本一致，并阻止旧读取覆盖并发同步事实。"""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import event, func, select

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.calendar import CalendarEvent
from ai_employee.application.ports.trusted_actions import ApprovalPreflightResult
from ai_employee.application.use_cases.calendar_proposals import CalendarProposalUseCase
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.application.use_cases.trusted_actions import SubmitCalendarProposalUseCase
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.actions import CalendarChangeProposalModel
from ai_employee.infrastructure.db.models.sources import CalendarEventModel
from ai_employee.infrastructure.db.models.tasks import ApprovalRequestModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry
from ai_employee.workers.prepare_calendar_restore import PrepareCalendarRestoreTaskStep
from tests.integration.m2.test_calendar_proposal_versions import (
    ACTION_CIPHER,
    NOW,
    SOURCE_LOCAL_EVENT_ID,
    USER_ID,
    _current_provider_event,
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _ReaderResolver,
    _seed_restore_source,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]
SOURCE_CIPHER = AeadCipher(b"r" * 32)


class _CurrentReader:
    """供应商替身在返回精确 GET 前允许独立会话提交，模拟真实网络交错。"""

    def __init__(self, before_return: Callable[[], Awaitable[None]] | None = None, *, variant: str = "valid") -> None:
        self._before_return = before_return
        self._variant = variant

    async def get_current_event(self, calendar_id: str, provider_event_id: str) -> CalendarEvent | None:
        """只返回绑定目标的新 ETag 和规范字段，不写本地来源或审批结果。"""
        if self._before_return is not None:
            await self._before_return()
        current = replace(_current_provider_event(), calendar_id=calendar_id, event_id=provider_event_id)
        return {
            "valid": current, "missing": None, "cancelled": replace(current, status="cancelled"),
            "recurring": replace(current, recurring_event_id="synthetic-series"),
            "uneditable": replace(current, can_edit=False), "etag": replace(current, etag=""),
        }[self._variant]


class _Preflight:
    """冻结测试只允许原 Google 日历命令，不具备任何外部写方法。"""

    provider = "google"

    def validate_for_approval(self, command: TrustedCommand) -> ApprovalPreflightResult:
        """命令已经经过生产严格 Schema，本 Fake 只声明可表达性。"""
        assert command.action == "calendar.restore"
        return ApprovalPreflightResult()


class _RestoreWritePolicy:
    """只允许本文件的精确合成账户进入冻结；环境真实写开关不改变。"""

    def provider_writes_enabled(self, provider: str) -> bool:
        """仅测试中的 Google 预检可通过。"""
        return provider == "google"

    def write_account_allowed(self, provider_identity_key: str) -> bool:
        """精确绑定既有恢复fixture账户，不接受域名或供应商级宽泛匹配。"""
        return provider_identity_key == "google::calendar-restore-account"


async def _prepare(
    sessions: ManagedAsyncSessionMaker,
    *,
    before_return: Callable[[], Awaitable[None]] | None = None,
    variant: str = "valid",
) -> UUID:
    """真实 Prepare 两段事务使用合成租约；批准与外部执行留给独立测试。"""
    task_id, snapshot_id = await _seed_restore_source(sessions)
    await PrepareCalendarRestoreTaskStep(
        sessions, action_cipher=ACTION_CIPHER, source_cipher=SOURCE_CIPHER,
        reader_resolver=_ReaderResolver(_CurrentReader(before_return, variant=variant)), clock=lambda: NOW,
    ).execute(LeasedTask(
        task_id=task_id, user_id=USER_ID, kind="calendar.restore.prepare", started_at=NOW,
        lease_owner="calendar-restore-worker",
        input_payload={"source_snapshot_id": str(snapshot_id), "creation_idempotency_key": f"calendar-restore:{task_id}"},
    ))
    async with sessions() as session:
        task = await session.get(TaskRunModel, task_id)
        assert task is not None and task.result_payload is not None
        return UUID(str(task.result_payload["calendar_proposal_id"]))


async def _confirm(sessions: ManagedAsyncSessionMaker, proposal_id: UUID) -> int:
    """显式确认恢复的通知策略，返回真实不可变新版本。"""
    async with sessions.begin() as session:
        proposal = await CalendarProposalUseCase(
            proposals=SqlAlchemyCalendarProposalRepository(session, ACTION_CIPHER),
            calendar=SqlAlchemyCalendarSyncRepository(session, SOURCE_CIPHER), clock=lambda: NOW,
        ).confirm(user_id=USER_ID, proposal_id=proposal_id, expected_version=1, confirmation="notification_policy")
        return proposal.current_version


async def _submit(sessions: ManagedAsyncSessionMaker, proposal_id: UUID, version: int) -> None:
    """执行生产提交冻结，策略只允许本地合成身份；注册表没有网络动作。"""
    await SubmitCalendarProposalUseCase(
        transactions=SqlAlchemyTrustedActionRepositoryFactory(sessions, ACTION_CIPHER),
        preflights=ProviderAdapterRegistry(google_calendar_preflight=_Preflight()),
        write_policy=_RestoreWritePolicy(), command_cipher=ACTION_CIPHER,
    ).execute(user_id=USER_ID, proposal_id=proposal_id, expected_version=version,
              idempotency_key="synthetic-restore-current-cache", now=NOW)


async def test_restore_new_provider_etag_can_be_confirmed_and_frozen(database_url: str) -> None:
    """旧缓存与新 GET 的 ETag 不同，仍必须能以新 ETag 独立冻结恢复审批。"""
    sessions = build_session_factory(database_url)
    try:
        proposal_id = await _prepare(sessions)
        version = await _confirm(sessions, proposal_id)
        await _submit(sessions, proposal_id, version)
        async with sessions() as session:
            repository = SqlAlchemyCalendarSyncRepository(session, SOURCE_CIPHER)
            binding = await repository.get_proposal_event_binding(
                user_id=USER_ID, event_id=SOURCE_LOCAL_EVENT_ID,
            )
            assert binding is not None
            cached = await repository.get_proposal_event(user_id=USER_ID, binding=binding)
            proposal = await session.get(CalendarChangeProposalModel, proposal_id)
            assert await session.scalar(select(func.count()).select_from(ApprovalRequestModel)) == 1
        assert cached is not None and proposal is not None
        assert cached.etag == proposal.base_etag == _current_provider_event().etag
        assert cached.description == _current_provider_event().description
        assert cached.location == _current_provider_event().location
    finally:
        await sessions.dispose()


@pytest.mark.parametrize("drift", ["etag", "status", "recurring_event_id", "can_edit"])
async def test_restore_discards_get_when_precise_cache_changes_during_network(
    database_url: str, drift: str,
) -> None:
    """独立事务在 GET 期间提交新事实；旧读取不得覆盖，新增恢复提案与marker必须为零。"""
    sessions = build_session_factory(database_url)

    async def concurrent_sync() -> None:
        """使用第二个数据库会话模拟精确目标同步，绝不直接制造恢复结果。"""
        async with sessions.begin() as session:
            cached = await session.get(CalendarEventModel, SOURCE_LOCAL_EVENT_ID)
            assert cached is not None
            setattr(cached, drift, {
                "etag": 'W/"etag-newer"', "status": "cancelled",
                "recurring_event_id": "synthetic-series", "can_edit": False,
            }[drift])

    try:
        with pytest.raises(StateConflictError):
            await _prepare(sessions, before_return=concurrent_sync)
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(CalendarChangeProposalModel)) == 1
            task = await session.scalar(select(TaskRunModel))
            cached = await session.get(CalendarEventModel, SOURCE_LOCAL_EVENT_ID)
        assert task is not None and task.result_payload is None
        assert cached is not None and cached.title == "Synchronized source event"
        assert cached.description_ciphertext is None and cached.location_ciphertext is None
    finally:
        await sessions.dispose()


async def test_restore_later_sync_etag_still_blocks_frozen_submission(database_url: str) -> None:
    """准备完成后的同步版本变化继续由既有Submit规则拒绝，不绕过条件更新。"""
    sessions = build_session_factory(database_url)
    try:
        proposal_id = await _prepare(sessions)
        version = await _confirm(sessions, proposal_id)
        async with sessions.begin() as session:
            cached = await session.get(CalendarEventModel, SOURCE_LOCAL_EVENT_ID)
            assert cached is not None
            cached.etag = 'W/"etag-after-prepare"'
        with pytest.raises(StateConflictError) as failure:
            await _submit(sessions, proposal_id, version)
        assert failure.value.error_code == "calendar_event_version_conflict"
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ApprovalRequestModel)) == 0
    finally:
        await sessions.dispose()


async def test_restore_marker_failure_rolls_back_precise_cache_and_proposal(database_url: str) -> None:
    """最后marker写入失败时，实际GET缓存、before与提案必须和marker一起回滚。"""
    sessions = build_session_factory(database_url)

    def fail_marker(mapper: object, connection: object, row: TaskRunModel) -> None:
        """在新结果真实flush时失败，使此前同事务的upsert也必须撤销。"""
        del mapper, connection
        if row.result_payload is not None:
            raise RuntimeError("synthetic restore marker failure")

    event.listen(TaskRunModel, "before_update", fail_marker)
    try:
        with pytest.raises(RuntimeError, match="synthetic restore marker failure"):
            await _prepare(sessions)
        async with sessions() as session:
            cached = await session.get(CalendarEventModel, SOURCE_LOCAL_EVENT_ID)
            assert await session.scalar(select(func.count()).select_from(CalendarChangeProposalModel)) == 1
            task = await session.scalar(select(TaskRunModel))
        assert cached is not None and cached.etag == 'W/"etag-old"'
        assert cached.description_ciphertext is None and cached.location_ciphertext is None
        assert task is not None and task.result_payload is None
    finally:
        event.remove(TaskRunModel, "before_update", fail_marker)
        await sessions.dispose()


@pytest.mark.parametrize("variant,code", [
    ("missing", "calendar_event_deleted"), ("cancelled", "calendar_event_deleted"),
    ("recurring", "calendar_recurring_event_unsupported"),
    ("uneditable", "calendar_event_not_editable"), ("etag", "calendar_event_version_conflict"),
])
async def test_restore_unusable_provider_fact_keeps_stable_error_and_zero_cache_write(
    database_url: str, variant: str, code: str,
) -> None:
    """缺失/取消/重复/只读或缺版本的实际GET结果沿原领域错误拒绝，不能变成缓存事实。"""
    sessions = build_session_factory(database_url)
    try:
        with pytest.raises(StateConflictError) as failure:
            await _prepare(sessions, variant=variant)
        assert failure.value.error_code == code
        async with sessions() as session:
            cached = await session.get(CalendarEventModel, SOURCE_LOCAL_EVENT_ID)
            assert await session.scalar(select(func.count()).select_from(CalendarChangeProposalModel)) == 1
            task = await session.scalar(select(TaskRunModel))
        assert cached is not None and cached.etag == 'W/"etag-old"'
        assert cached.description_ciphertext is None and cached.location_ciphertext is None
        assert task is not None and task.result_payload is None
    finally:
        await sessions.dispose()


async def test_restore_marker_replay_does_not_read_or_overwrite_later_cache(database_url: str) -> None:
    """结果事务已提交但消息未ACK时，原task重复执行零GET且保留之后同步的新缓存。"""
    sessions = build_session_factory(database_url)

    async def forbid_read() -> None:
        """重复准备不能重新访问供应商，无论当前缓存是否已经变化。"""
        raise AssertionError("A committed restore preparation must not read again")

    try:
        proposal_id = await _prepare(sessions)
        async with sessions.begin() as session:
            task = await session.scalar(select(TaskRunModel))
            cached = await session.get(CalendarEventModel, SOURCE_LOCAL_EVENT_ID)
            assert task is not None and cached is not None
            cached.etag = 'W/"etag-after-marker"'
            replay = LeasedTask(task_id=task.id, user_id=USER_ID, kind=task.kind,
                input_payload=dict(task.input_payload), started_at=NOW, lease_owner=task.lease_owner)
        await PrepareCalendarRestoreTaskStep(
            sessions, action_cipher=ACTION_CIPHER, source_cipher=SOURCE_CIPHER,
            reader_resolver=_ReaderResolver(_CurrentReader(forbid_read)), clock=lambda: NOW,
        ).execute(replay)
        async with sessions() as session:
            cached = await session.get(CalendarEventModel, SOURCE_LOCAL_EVENT_ID)
            task = await session.get(TaskRunModel, replay.task_id)
        assert cached is not None and cached.etag == 'W/"etag-after-marker"'
        assert task is not None and task.result_payload == {"calendar_proposal_id": str(proposal_id)}
    finally:
        await sessions.dispose()
