"""经真实 app/retention 角色验证两轮内容到期与原生 Checkpoint 历史回收。

本模块只复用既有合成动作及原生 saver fixture，不创建角色或修改权限。观测端口
继续调用生产 cleaner，记录首次完成时间和三表删除边界，不能替代真实写入。
"""

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select, text, update

from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
    MailDraftModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.privacy_checkpoints import (
    PostgresPrivacyCheckpointCleaner,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.workers.retention import RetentionCleanupWorker
from tests.integration.privacy.test_deletion_checkpoints import seed_checkpoint_thread
from tests.integration.retention.checkpoint_cases import _checkpoint_counts
from tests.integration.retention.test_m2_action_retention import NOW, seed_lifecycle_action


class _ObservedHistoryCleaner(PostgresPrivacyCheckpointCleaner):
    """在真实 app 三表删除前后观测事实，保持原生产准入和原生删除事务。"""

    def __init__(self, app_url: str, app: ManagedAsyncSessionMaker) -> None:
        """保存 app 只读工厂；父类仍从同一已登记 app URL 打开原生连接。"""
        super().__init__(app_url)
        self._app = app
        self.cleared: list[UUID] = []

    async def clear_expired_thread(self, *, user_id: UUID, task_id: UUID, cutoff: datetime) -> None:
        """在 retention 连续持锁期间只读检查，不再次请求 Task/user 行锁。"""
        async with self._app() as session:
            task = await session.scalar(
                select(TaskRunModel).where(
                    TaskRunModel.user_id == user_id, TaskRunModel.id == task_id
                )
            )
            assert task is not None and task.finished_at == NOW
            approval = await session.scalar(
                select(ApprovalRequestModel).where(ApprovalRequestModel.task_id == task_id)
            )
            assert approval is not None
            assert all(
                value is None
                for value in (
                    approval.payload_ciphertext,
                    approval.payload_nonce,
                    approval.payload_key_version,
                )
            )
            if task.status == "cancelled":
                assert task.error_code == "action_content_expired"
                assert all(
                    value is None
                    for value in (
                        task.scheduled_for,
                        task.retry_recovery_at,
                        task.approval_checkpoint_recovery_at,
                        task.lease_owner,
                        task.lease_expires_at,
                    )
                )
            else:
                assert task.status == "succeeded" and task.error_code is None
                execution = await session.scalar(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == task_id)
                )
                assert execution is not None and execution.status == "succeeded"
                assert execution.write_attempt_count == 1
                assert execution.reconciliation_attempt_count == 3
        assert await _checkpoint_counts(self._app, task_id) == (1, 1, 1)
        await super().clear_expired_thread(user_id=user_id, task_id=task_id, cutoff=cutoff)
        assert await _checkpoint_counts(self._app, task_id) == (0, 0, 0)
        self.cleared.append(task_id)


async def assert_repeated_expiry_reaches_history(
    *, app_url: str, retention_url: str, kind: Literal["mail", "calendar"]
) -> None:
    """两轮真实 retention 跨越 365 天 cutoff，取消和终态执行的全部依赖最终回收。

    Args:
        app_url: 官方 disposable lifecycle 已核验的 app 登录 URL。
        retention_url: 同一数据库的冻结 retention 登录 URL。
        kind: 邮件版本或日程快照，两种聚合都必须遵守相同的完成事实语义。
    """
    app = build_session_factory(app_url)
    retention = build_session_factory(retention_url)
    try:
        unclaimed = await seed_lifecycle_action(app, kind=kind, metadata_days=180)
        terminal = await seed_lifecycle_action(
            app, kind=kind, execution_status="succeeded", user_id=unclaimed.user_id
        )
        for seed in (unclaimed, terminal):
            await seed_checkpoint_thread(app_url, seed.task_id)
        async with app() as session:
            migrations = (await session.execute(text("SELECT * FROM checkpoint_migrations"))).all()
        cleaner = _ObservedHistoryCleaner(app_url, app)
        worker = RetentionCleanupWorker(retention, checkpoint_cleaner=cleaner)

        await worker.execute(now=NOW, batch_size=1)

        assert cleaner.cleared == []
        async with app() as session:
            for seed in (unclaimed, terminal):
                task = await session.get(TaskRunModel, seed.task_id)
                approval = await session.get(ApprovalRequestModel, seed.approval_id)
                assert task is not None and task.finished_at == NOW
                assert task.status == ("cancelled" if seed is unclaimed else "succeeded")
                assert approval is not None
                assert all(
                    value is None
                    for value in (
                        approval.payload_ciphertext,
                        approval.payload_nonce,
                        approval.payload_key_version,
                    )
                )
                if kind == "mail":
                    content = await session.get(MailDraftVersionModel, seed.content_id)
                    assert content is not None
                    assert all(
                        value is None
                        for value in (
                            content.body_ciphertext,
                            content.body_nonce,
                            content.body_key_version,
                        )
                    )
                else:
                    snapshot = await session.get(CalendarChangeSnapshotModel, seed.content_id)
                    assert snapshot is not None
                    assert all(
                        value is None
                        for value in (
                            snapshot.content_ciphertext,
                            snapshot.content_nonce,
                            snapshot.content_key_version,
                        )
                    )
        repeated_at = NOW + timedelta(days=366)
        # 显式制造迟到调度残留，证明重复收敛仍清理可执行入口，而非简单跳过整项动作。
        async with app.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(
                    TaskRunModel.user_id == unclaimed.user_id, TaskRunModel.id == unclaimed.task_id
                )
                .values(
                    scheduled_for=repeated_at,
                    retry_recovery_at=repeated_at,
                    approval_checkpoint_recovery_at=repeated_at,
                    lease_owner=str(uuid4()),
                    lease_expires_at=repeated_at + timedelta(minutes=5),
                )
            )

        await worker.execute(now=repeated_at, batch_size=1)

        assert terminal.task_id in cleaner.cleared
        async with app() as session:
            for seed in (unclaimed, terminal):
                assert await session.get(TaskRunModel, seed.task_id) is None
                assert await session.get(ApprovalRequestModel, seed.approval_id) is None
                assert await session.get(ToolExecutionModel, seed.execution_id) is None
                assert (
                    await session.scalar(
                        select(TaskStepModel.id).where(TaskStepModel.task_id == seed.task_id)
                    )
                    is None
                )
                action_model = MailDraftModel if kind == "mail" else CalendarChangeProposalModel
                content_model = (
                    MailDraftVersionModel if kind == "mail" else CalendarChangeSnapshotModel
                )
                assert await session.get(action_model, seed.action_id) is None
                assert await session.get(content_model, seed.content_id) is None
            assert (
                await session.execute(text("SELECT * FROM checkpoint_migrations"))
            ).all() == migrations
        assert set(cleaner.cleared) == {unclaimed.task_id, terminal.task_id}
    finally:
        await retention.dispose()
        await app.dispose()
