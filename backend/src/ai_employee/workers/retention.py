"""执行可配置数据保留清理，所有操作只处理数据库中的最小元数据。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, exists, select, update

from ai_employee.config import get_settings
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.briefs import (
    ConversationModel,
    DailyBriefModel,
    LLMInvocationModel,
    MessageModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    EmailAnalysisModel,
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory

RETENTION_BATCH_SIZE: Final[int] = 100


class RetentionCleanupWorker:
    """按每位活动用户的持久化保留设置做有界、可重试的数据库清理。

    清理将邮件正文与来源元数据区分处理：正文到期后只抹除加密三元组，元数据到期后才
    删除邮件、分析及已结束日程。这样一次中断最多重复执行幂等删除或空值写入，不会暴露
    或重建任何来源内容。
    """

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存由 retention 专用数据库角色创建的会话工厂。"""
        self._session_factory = session_factory

    async def execute(self, *, now: datetime, batch_size: int = RETENTION_BATCH_SIZE) -> int:
        """扫描活动用户并执行各自策略，返回处理的用户数量。

        Args:
            now: 带时区的 UTC 当前瞬间，测试必须显式注入以避免宿主机时区影响。
            batch_size: 每个 SQL 写事务可触及的最大行数。

        Raises:
            ValueError: 时间无时区或批量大小越界时抛出。
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not 1 <= batch_size <= 1_000:
            raise ValueError("retention batch_size must be between 1 and 1000")
        async with self._session_factory() as session:
            users = (await session.scalars(select(UserModel).where(UserModel.is_active.is_(True)))).all()
        for user in users:
            await self._clean_user(user, now=now.astimezone(UTC), batch_size=batch_size)
        return len(users)

    async def _clean_user(self, user: UserModel, *, now: datetime, batch_size: int) -> None:
        """在独立小事务中清理一名用户，避免长事务锁住其他用户的数据。

        Args:
            user: 已在活动用户扫描中读取的持久化偏好快照，仅使用主键和三个保留天数。
            now: 规范化后的 UTC 清理时刻。
            batch_size: 每次提交最多触及的记录数，贯穿所有依赖顺序阶段。
        """
        body_cutoff = now - timedelta(days=user.email_body_retention_days)
        metadata_cutoff = now - timedelta(days=user.source_metadata_retention_days)
        workspace_cutoff = now - timedelta(days=user.workspace_history_retention_days)
        await self._scrub_email_bodies(user.id, body_cutoff, batch_size)
        await self._delete_source_metadata(user.id, metadata_cutoff, batch_size)
        await self._delete_workspace_history(user.id, workspace_cutoff, batch_size)
        await self._clean_disconnected_credentials(user.id, batch_size)
        # 本轮审计在过期 audit 删除后才新增，故 crash/retry 最多多出可解释的运行事实，且不会
        # 被同一轮的历史清理误删。
        await self._append_cleanup_audit(user.id, now)

    async def _scrub_email_bodies(self, user_id: UUID, cutoff: datetime, batch_size: int) -> None:
        """把到期邮件正文的密文、nonce 和密钥版本同时置空，防止残留可解密片段。"""
        while True:
            async with self._session_factory.begin() as session:
                ids = (await session.scalars(select(EmailMessageModel.id).where(
                    EmailMessageModel.user_id == user_id,
                    EmailMessageModel.received_at < cutoff,
                    EmailMessageModel.body_ciphertext.is_not(None),
                ).limit(batch_size))).all()
                if not ids:
                    return
                await session.execute(update(EmailMessageModel).where(EmailMessageModel.id.in_(ids)).values(
                    body_ciphertext=None, body_nonce=None, body_key_version=None,
                ))

    async def _delete_source_metadata(self, user_id: UUID, cutoff: datetime, batch_size: int) -> None:
        """删除到期分析、邮件和已结束日程；未来日程由结束时间过滤明确保留。"""
        await self._delete_bounded(EmailAnalysisModel, user_id, EmailAnalysisModel.created_at, cutoff, batch_size)
        await self._delete_bounded(EmailMessageModel, user_id, EmailMessageModel.received_at, cutoff, batch_size)
        await self._delete_bounded(CalendarEventModel, user_id, CalendarEventModel.ends_at, cutoff, batch_size)
        await self._delete_empty_email_threads(user_id, batch_size)

    async def _delete_empty_email_threads(self, user_id: UUID, batch_size: int) -> None:
        """分批删除没有保留邮件的线程，避免一批删除整个用户的全部历史线程。"""
        while True:
            async with self._session_factory.begin() as session:
                message_exists = exists(
                    select(EmailMessageModel.id).where(
                        EmailMessageModel.thread_id == EmailThreadModel.id
                    )
                )
                ids = (
                    await session.scalars(
                        select(EmailThreadModel.id)
                        .where(
                            EmailThreadModel.user_id == user_id,
                            ~message_exists,
                        )
                        .order_by(EmailThreadModel.id)
                        .limit(batch_size)
                    )
                ).all()
                if not ids:
                    return
                await session.execute(delete(EmailThreadModel).where(EmailThreadModel.id.in_(ids)))

    async def _delete_workspace_history(
        self, user_id: UUID, cutoff: datetime, batch_size: int
    ) -> None:
        """按依赖顺序清理到期工作区历史，仅选择已终态且已完成足够久的任务。

        对话消息与简报先于 TaskRun 删除：二者分别以外键引用任务或由任务产生，先清理可避免
        ``RESTRICT`` 约束阻塞终态图回收。任务子图同时显式选择 LLM、步骤、审批与工具表，
        即使未来外键策略变化，也不会把清理正确性仅寄托在级联行为上。
        """
        await self._delete_bounded(MessageModel, user_id, MessageModel.created_at, cutoff, batch_size)
        await self._delete_empty_conversations(user_id, batch_size)
        await self._delete_bounded(DailyBriefModel, user_id, DailyBriefModel.created_at, cutoff, batch_size)
        await self._delete_terminal_task_graph(user_id, cutoff, batch_size)

    async def _delete_empty_conversations(self, user_id: UUID, batch_size: int) -> None:
        """分批回收没有任何消息的会话，避免级联删除尚在保留期内的新消息。

        旧消息先按自己的创建时间删除；会话行仅是容器，不应凭自身创建时间跨越子消息的
        保留边界。相关 ``NOT EXISTS`` 条件也让重复运行在前一批已删除消息后自然收敛。

        Args:
            user_id: 当前清理的用户，查询和删除始终显式限定其归属。
            batch_size: 每次短事务中最多删除的空会话数量。
        """
        while True:
            async with self._session_factory.begin() as session:
                message_exists = exists(
                    select(MessageModel.id).where(
                        MessageModel.conversation_id == ConversationModel.id
                    )
                )
                ids = (
                    await session.scalars(
                        select(ConversationModel.id)
                        .where(
                            ConversationModel.user_id == user_id,
                            ~message_exists,
                        )
                        .order_by(ConversationModel.id)
                        .limit(batch_size)
                    )
                ).all()
                if not ids:
                    return
                await session.execute(delete(ConversationModel).where(ConversationModel.id.in_(ids)))

    async def _delete_terminal_task_graph(
        self, user_id: UUID, cutoff: datetime, batch_size: int
    ) -> None:
        """分批删除过期终态 TaskRun 及其依赖图，绝不处理等待、重试或运行中任务。"""
        terminal_statuses = (
            TaskStatus.SUCCEEDED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        )
        while True:
            async with self._session_factory.begin() as session:
                task_ids = (
                    await session.scalars(
                        select(TaskRunModel.id)
                        .where(
                            TaskRunModel.user_id == user_id,
                            TaskRunModel.status.in_(terminal_statuses),
                            TaskRunModel.finished_at.is_not(None),
                            TaskRunModel.finished_at < cutoff,
                        )
                        .order_by(TaskRunModel.finished_at, TaskRunModel.id)
                        .limit(batch_size)
                    )
                ).all()
                if not task_ids:
                    return
                step_ids = select(TaskStepModel.id).where(TaskStepModel.task_id.in_(task_ids))
                await session.execute(delete(LLMInvocationModel).where(LLMInvocationModel.task_id.in_(task_ids)))
                await session.execute(delete(ApprovalRequestModel).where(ApprovalRequestModel.task_id.in_(task_ids)))
                await session.execute(delete(ToolExecutionModel).where(ToolExecutionModel.task_id.in_(task_ids)))
                await session.execute(delete(TaskStepModel).where(TaskStepModel.id.in_(step_ids)))
                # 未发布 Outbox 是可恢复的投递事实，不能被保留任务静默丢弃；仅回收已发布记录。
                await session.execute(
                    delete(OutboxEventModel).where(
                        OutboxEventModel.aggregate_id.in_(task_ids),
                        OutboxEventModel.published_at.is_not(None),
                    )
                )
                await session.execute(delete(TaskRunModel).where(TaskRunModel.id.in_(task_ids)))

    async def _clean_disconnected_credentials(self, user_id: UUID, batch_size: int) -> None:
        """删除已断开连接的凭据并重置同步游标，保留仍处于 connected 的凭据。"""
        while True:
            async with self._session_factory.begin() as session:
                connection_ids = (
                    await session.scalars(
                        select(OAuthConnectionModel.id)
                        .where(
                            OAuthConnectionModel.user_id == user_id,
                            OAuthConnectionModel.status == "disconnected",
                            (
                                exists(
                                    select(EncryptedCredentialModel.id).where(
                                        EncryptedCredentialModel.connection_id
                                        == OAuthConnectionModel.id
                                    )
                                )
                                |
                                exists(
                                    select(SyncCursorModel.id).where(
                                        SyncCursorModel.connection_id == OAuthConnectionModel.id,
                                        (
                                            SyncCursorModel.cursor.is_not(None)
                                            | SyncCursorModel.last_success_at.is_not(None)
                                            | SyncCursorModel.last_attempt_at.is_not(None)
                                            | SyncCursorModel.last_error_code.is_not(None)
                                        ),
                                    )
                                )
                            ),
                        )
                        .order_by(OAuthConnectionModel.id)
                        .limit(batch_size)
                    )
                ).all()
                if not connection_ids:
                    return
                await session.execute(
                    delete(EncryptedCredentialModel).where(
                        EncryptedCredentialModel.user_id == user_id,
                        EncryptedCredentialModel.connection_id.in_(connection_ids),
                    )
                )
                await session.execute(
                    update(SyncCursorModel)
                    .where(SyncCursorModel.connection_id.in_(connection_ids))
                    .values(
                        cursor=None,
                        last_success_at=None,
                        last_attempt_at=None,
                        last_error_code=None,
                    )
                )

    async def _append_cleanup_audit(self, user_id: UUID, now: datetime) -> None:
        """在用户历史清理后追加本轮无内容审计事实，供恢复与合规检查使用。"""
        async with self._session_factory.begin() as session:
            session.add(
                AuditEventModel(
                    user_id=user_id,
                    task_id=None,
                    event_type="retention.cleanup_completed",
                    actor_type="system",
                    actor_id=None,
                    event_metadata={"operation": "retention_cleanup"},
                    created_at=now,
                )
            )

    async def _delete_bounded(self, model: type[Any], user_id: UUID, timestamp: Any, cutoff: datetime, batch_size: int) -> None:
        """按主键先选后删，让每批提交可在崩溃后安全重放。"""
        # ORM 映射类型的动态列描述符是 SQLAlchemy 第三方边界，收窄到 ``Any`` 后立即组成 SQL。
        identifier: Any = model.id
        ownership: Any = model.user_id
        while True:
            async with self._session_factory.begin() as session:
                ids = (await session.scalars(select(identifier).where(ownership == user_id, timestamp.is_not(None), timestamp < cutoff).limit(batch_size))).all()
                if not ids:
                    return
                await session.execute(delete(model).where(identifier.in_(ids)))


@lru_cache
def build_retention_cleanup_worker() -> RetentionCleanupWorker:
    """构造 retention 专用会话；生产禁止因 Secret 缺失而降级为应用角色。"""
    settings = get_settings()
    if not settings.retention_database_url_file.is_file():
        if settings.app_env == "production":
            raise RuntimeError("RETENTION_DATABASE_URL_FILE is required in production")
        database_url = settings.database_url
    else:
        database_url = settings.read_secret_file(settings.retention_database_url_file).get_secret_value()
    return RetentionCleanupWorker(build_session_factory(database_url))
