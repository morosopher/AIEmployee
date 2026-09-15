"""验证新审批提交的Task创建屏障及其与连接撤权共享的user/local锁序。"""

import asyncio
from typing import Literal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from ai_employee.application.commands import trusted_command_hash
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.trusted_actions import TrustedActionSubmissionResult
from ai_employee.application.use_cases.trusted_actions import (
    CalendarProposalSubmissionNotFoundError,
)
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.actions import CalendarChangeProposalModel, MailDraftModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from tests.integration.m2.test_capability_revocation_races import (
    _ClaimAdapter,
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _PendingSubmissionSeed,
    _seed_pending_submission,
    _submit_pending_resource,
)
from tests.integration.m2.test_encrypted_approval_submission import ACTION_CIPHER
from tests.integration.privacy.inactive_barrier import (
    assert_facts_unchanged,
    commit_deletion_barrier,
    database_facts,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]


async def _assert_frozen_submission(
    sessions: ManagedAsyncSessionMaker,
    seed: _PendingSubmissionSeed,
    result: TrustedActionSubmissionResult,
) -> None:
    """从独立会话检查真实加密审批、哈希和Task/Step/Outbox，明文不进入失败输出。"""
    async with sessions() as session:
        task = await session.get(TaskRunModel, result.task_id)
        approval = await session.get(ApprovalRequestModel, result.approval_id)
        assert task is not None and approval is not None
        assert task.user_id == seed.user_id and task.status == "queued"
        assert task.input_payload == {
            "approval_id": str(result.approval_id),
            "operation_id": str(result.operation_id),
        }
        assert approval.status == "pending" and approval.proposal_id == seed.resource_id
        assert approval.payload_ciphertext is not None and approval.payload_nonce is not None
        assert approval.payload_key_version is not None and approval.schema_version is not None
        command = ACTION_CIPHER.decrypt_json(
            EncryptedValue(
                approval.payload_ciphertext, approval.payload_nonce, approval.payload_key_version
            ),
            user_id=seed.user_id,
            record_id=approval.id,
            content_kind="approval_command",
            action=approval.action,
            schema_version=approval.schema_version,
        )
        actual_hash = trusted_command_hash(command)
        assert actual_hash == approval.payload_hash
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TaskStepModel)
                .where(
                    TaskStepModel.task_id == result.task_id,
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_id == result.task_id,
                    OutboxEventModel.topic == "task.execute",
                )
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(ToolExecutionModel)) == 0


@pytest.mark.parametrize("kind", ("mail", "calendar"))
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_new_submission_respects_deletion_barrier(
    database_url: str,
    kind: Literal["mail", "calendar"],
    inactive: bool,
) -> None:
    """真实提交用例冻结完整当前版本；barrier先提交时所有普通事实保持不变。

    活跃用户仍获得可认证解密且哈希匹配的审批；重放同一键只返回原标识，不产生新的
    Task/Step/Approval/Audit/Outbox。Fake端口只预检，本测试从不批准或执行供应商写入。
    """
    seed = await _seed_pending_submission(database_url, kind)
    sessions = build_session_factory(database_url)
    adapter = _ClaimAdapter()
    try:
        if inactive:
            await commit_deletion_barrier(sessions, user_id=seed.user_id)
        before = await database_facts(sessions)
        first: TrustedActionSubmissionResult | None = None
        for _ in range(2):
            result = None
            try:
                result = await _submit_pending_resource(sessions, seed, adapter)
            except StateConflictError as error:
                assert inactive and kind == "mail" and error.error_code == "draft_version_conflict"
            except CalendarProposalSubmissionNotFoundError:
                assert inactive and kind == "calendar"
            if inactive:
                assert_facts_unchanged(before, await database_facts(sessions))
                assert result is None
            else:
                assert result is not None
                if first is None:
                    first = result
                    await _assert_frozen_submission(sessions, seed, result)
                    before = await database_facts(sessions)
                else:
                    assert (result.task_id, result.approval_id) == (
                        first.task_id,
                        first.approval_id,
                    )
                    assert_facts_unchanged(before, await database_facts(sessions))
        assert adapter.write_calls == 0
    finally:
        await sessions.dispose()


@pytest.mark.parametrize("kind", ("mail", "calendar"))
async def test_new_submission_locks_user_before_local_action(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["mail", "calendar"],
) -> None:
    """真实新提交等待User时不应持有local；旧FK隐式用户锁会暴露local→User反序。

    User持有者以NOWAIT探测同一local，只在PG确认另一提交正在等待它后执行。探针
    在savepoint失败会仅记录55P03，随后释放User让实际提交完成，不伪造死锁或写结果。
    """
    seed = await _seed_pending_submission(database_url, kind)
    sessions = build_session_factory(database_url)
    adapter = _ClaimAdapter()
    entered = asyncio.Event()
    method = "lock_mail_draft" if kind == "mail" else "lock_calendar_proposal"
    original = getattr(SqlAlchemyTrustedActionRepository, method)
    writer_pid: int | None = None
    pending: asyncio.Task[TrustedActionSubmissionResult] | None = None
    local_lock_failure: tuple[str, str | None] | None = None

    async def observe_submission(
        repository: SqlAlchemyTrustedActionRepository, **kwargs: object
    ) -> object:
        """仅取得真实提交会话ID，原方法仍决定全部授权、资源锁和快照内容。"""
        nonlocal writer_pid
        writer_pid = await repository._session.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        return await original(repository, **kwargs)

    monkeypatch.setattr(SqlAlchemyTrustedActionRepository, method, observe_submission)
    try:
        async with sessions.begin() as holder:
            assert (
                await holder.scalar(
                    select(UserModel.id)
                    .where(
                        UserModel.id == seed.user_id,
                    )
                    .with_for_update()
                )
                == seed.user_id
            )
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(_submit_pending_resource(sessions, seed, adapter))
            await asyncio.wait_for(entered.wait(), timeout=5)

            async def wait_for_user_block() -> None:
                """等到实际PG等待边成立，避免以固定sleep推断FK或显式用户锁阶段。"""
                while True:
                    async with sessions() as observer:
                        if await observer.scalar(
                            text("SELECT :holder_pid = ANY(pg_blocking_pids(:writer_pid))"),
                            {"holder_pid": holder_pid, "writer_pid": writer_pid},
                        ):
                            return

            await asyncio.wait_for(wait_for_user_block(), timeout=5)
            local_model = MailDraftModel if kind == "mail" else CalendarChangeProposalModel
            try:
                async with holder.begin_nested():
                    assert (
                        await holder.scalar(
                            select(local_model.id)
                            .where(
                                local_model.id == seed.resource_id,
                            )
                            .with_for_update(nowait=True)
                        )
                        == seed.resource_id
                    )
            except DBAPIError as error:
                local_lock_failure = (type(error).__name__, getattr(error.orig, "sqlstate", None))
        result = await asyncio.wait_for(pending, timeout=5)
        await _assert_frozen_submission(sessions, seed, result)
        assert local_lock_failure is None, (
            f"submission local lock precedes User: {local_lock_failure}"
        )
        assert adapter.write_calls == 0
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await sessions.dispose()
