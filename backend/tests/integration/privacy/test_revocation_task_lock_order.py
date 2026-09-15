"""用真实双任务撤权与PG等待图验证全部Task锁必须先于User锁和业务写入。"""

import asyncio
from typing import Literal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from ai_employee.application.use_cases.connections import ConnectionsUseCase
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.infrastructure.db.models.actions import MailDraftModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.connections import (
    SqlAlchemyConnectionStore,
    SqlAlchemyConnectionStoreFactory,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from tests.integration.m2.test_capability_revocation_races import (
    _DisconnectRevokeAdapter,
    _FixedClock,
)
from tests.integration.m2.test_encrypted_approval_submission import (
    GOOGLE_CONNECTION_ID,
    NOW,
    USER_ID,
    _create_additional_mail_draft,
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _seed_mail_draft,
    _submit_mail_draft,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]


@pytest.mark.parametrize("mode", ("disable", "disconnect"))
async def test_connection_revocation_locks_all_tasks_before_user(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["disable", "disconnect"],
) -> None:
    """撤权等待后一个Task时尚未持User，避免与Task→User的claim形成反向等待。

    两个草稿均经过真实创建及冻结用例，未批准且没有ToolExecution。独立事务只持按UUID
    排序较后的Task，PG确认撤权正在等待该锁后，再以savepoint/NOWAIT尝试User锁。
    旧逐项循环会先写第一个Task的Audit，外键取得User KEY SHARE，从而暴露55P03；
    全批Task先锁定时探针成功。随后释放holder并核实真实撤权的两组完整持久状态。
    """
    drafts = (
        await _seed_mail_draft(database_url),
        await _create_additional_mail_draft(database_url, idempotency_key="lock-order-second"),
    )
    submissions = [
        await _submit_mail_draft(
            database_url=database_url,
            draft_id=draft_id,
            expected_version=1,
            idempotency_key=f"lock-order-submit-{index}",
            now=NOW,
        )
        for index, draft_id in enumerate(drafts)
    ]
    last_task_id = max(task_id for task_id, _approval_id, _operation_id in submissions)
    sessions = build_session_factory(database_url)
    adapter = _DisconnectRevokeAdapter()
    use_case = ConnectionsUseCase(
        SqlAlchemyConnectionStoreFactory(sessions),
        AeadCipher(b"r" * 32),
        {"google": adapter},
        _FixedClock(),
    )
    entered = asyncio.Event()
    method = "disable_capability" if mode == "disable" else "disconnect"
    original = getattr(SqlAlchemyConnectionStore, method)
    writer_pid: int | None = None
    pending: asyncio.Task[None] | None = None
    user_lock_failure: tuple[str, str | None] | None = None

    async def observe_revocation(store: SqlAlchemyConnectionStore, **kwargs: object) -> object:
        """仅记录原事务会话ID，保留生产入口的候选选择、加锁、绑定及所有DML。"""
        nonlocal writer_pid
        writer_pid = await store._session.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        return await original(store, **kwargs)

    async def revoke() -> None:
        """从真实连接用例进入同一短事务，fixture无凭据，不会发起供应商请求。"""
        if mode == "disable":
            await use_case.disable_capability(
                user_id=USER_ID,
                connection_id=GOOGLE_CONNECTION_ID,
                capability=ConnectionCapability.MAIL_SEND,
            )
        else:
            await use_case.disconnect(user_id=USER_ID, connection_id=GOOGLE_CONNECTION_ID)

    monkeypatch.setattr(SqlAlchemyConnectionStore, method, observe_revocation)
    try:
        async with sessions.begin() as holder:
            assert (
                await holder.scalar(
                    select(TaskRunModel.id)
                    .where(
                        TaskRunModel.id == last_task_id,
                        TaskRunModel.user_id == USER_ID,
                    )
                    .with_for_update()
                )
                == last_task_id
            )
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(revoke())
            await asyncio.wait_for(entered.wait(), timeout=5)

            async def wait_for_task_block() -> None:
                """只读取两个已知合成事务的等待边，不用固定sleep猜测Task锁阶段。"""
                while True:
                    async with sessions() as observer:
                        if await observer.scalar(
                            text("SELECT :holder_pid = ANY(pg_blocking_pids(:writer_pid))"),
                            {"holder_pid": holder_pid, "writer_pid": writer_pid},
                        ):
                            return

            await asyncio.wait_for(wait_for_task_block(), timeout=5)
            try:
                async with holder.begin_nested():
                    assert (
                        await holder.scalar(
                            select(UserModel.id)
                            .where(
                                UserModel.id == USER_ID,
                            )
                            .with_for_update(nowait=True)
                        )
                        == USER_ID
                    )
            except DBAPIError as error:
                # 参数、SQL和用户内容不得进入失败输出；只保留类型和标准SQLSTATE。
                user_lock_failure = (type(error).__name__, getattr(error.orig, "sqlstate", None))

        await asyncio.wait_for(pending, timeout=5)
        async with sessions() as session:
            for draft_id, (task_id, approval_id, _operation_id) in zip(
                drafts, submissions, strict=True
            ):
                task = await session.get(TaskRunModel, task_id)
                approval = await session.get(ApprovalRequestModel, approval_id)
                draft = await session.get(MailDraftModel, draft_id)
                assert task is not None and approval is not None and draft is not None
                assert task.status == "cancelled" and task.finished_at is not None
                assert task.lease_owner is None and task.lease_expires_at is None
                assert approval.status == "invalidated" and draft.status == "editing"
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(OutboxEventModel)
                        .where(
                            OutboxEventModel.aggregate_id == task_id,
                            OutboxEventModel.topic == "task.execute",
                        )
                    )
                    == 0
                )
                for event_type in ("approval.invalidated", "task.cancelled"):
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(AuditEventModel)
                            .where(
                                AuditEventModel.task_id == task_id,
                                AuditEventModel.event_type == event_type,
                            )
                        )
                        == 1
                    )
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(OutboxEventModel)
                            .where(
                                OutboxEventModel.aggregate_id == task_id,
                                OutboxEventModel.topic == event_type,
                            )
                        )
                        == 1
                    )
            assert await session.scalar(select(func.count()).select_from(ToolExecutionModel)) == 0
            connection = await session.get(OAuthConnectionModel, GOOGLE_CONNECTION_ID)
            assert connection is not None
            assert connection.status == ("connected" if mode == "disable" else "disconnected")
            capability = await session.scalar(
                select(ConnectionCapabilityModel).where(
                    ConnectionCapabilityModel.user_id == USER_ID,
                    ConnectionCapabilityModel.connection_id == GOOGLE_CONNECTION_ID,
                    ConnectionCapabilityModel.capability == "mail.send",
                )
            )
            assert capability is not None
            assert capability.status == ("disabled" if mode == "disable" else "revoked")
        assert not adapter.revoked_tokens
        assert user_lock_failure is None, f"User locked before remaining Task: {user_lock_failure}"
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await sessions.dispose()
