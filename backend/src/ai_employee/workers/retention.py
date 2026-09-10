"""执行可配置数据保留清理，所有操作只处理数据库中的最小元数据。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import DBAPIError

from ai_employee.application.use_cases.privacy import (
    PRIVACY_DELETION_STARTED_EVENT_TYPE,
    ExpiredTaskCheckpointCleaner,
)
from ai_employee.config import get_settings
from ai_employee.infrastructure.db.models.briefs import (
    ConversationModel,
    DailyBriefModel,
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
    AuditEventModel,
)
from ai_employee.infrastructure.db.repositories.oauth_lifecycle import (
    OAuthLifecycleCleanup,
    lock_cleanup_refresh_events,
    lock_oauth_cleanup_identity,
    oauth_cleanup_lock_contended,
)
from ai_employee.infrastructure.db.repositories.privacy_checkpoints import (
    PostgresPrivacyCheckpointCleaner,
)
from ai_employee.infrastructure.db.repositories.task_history import TaskHistoryCleanup
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.workers.action_lifecycle import ActionCleanupMode, ActionLifecycleCleanup

RETENTION_BATCH_SIZE: Final[int] = 100


class RetentionCleanupWorker:
    """按每位活动用户的持久化保留设置做有界、可重试的数据库清理。

    清理将邮件正文与来源元数据区分处理：正文到期后只抹除加密三元组，元数据到期后才
    删除邮件、分析及已结束日程。这样一次中断最多重复执行幂等删除或空值写入，不会暴露
    或重建任何来源内容。
    """

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        *,
        checkpoint_cleaner: ExpiredTaskCheckpointCleaner | None = None,
    ) -> None:
        """保存 retention 专用工厂及只负责 checkpoint 三表的独立 app 窄端口。"""
        self._session_factory = session_factory
        self._checkpoint_cleaner = checkpoint_cleaner

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
        await ActionLifecycleCleanup(self._session_factory).clean_user(
            user_id=user.id,
            now=now,
            batch_size=batch_size,
            mode=ActionCleanupMode.EXPIRED,
            metadata_cutoff=metadata_cutoff,
        )
        await self._scrub_email_bodies(user.id, body_cutoff, batch_size)
        await self._scrub_calendar_content(user.id, now - timedelta(days=180), batch_size)
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

    async def _scrub_calendar_content(
        self,
        user_id: UUID,
        cutoff: datetime,
        batch_size: int,
    ) -> None:
        """结束后180天清空每个字段的四列 AEAD 组，保留期外元数据由后续阶段处理。"""
        while True:
            async with self._session_factory.begin() as session:
                ids = (
                    await session.scalars(
                        select(CalendarEventModel.id)
                        .where(
                            CalendarEventModel.user_id == user_id,
                            CalendarEventModel.ends_at < cutoff,
                            (
                                CalendarEventModel.description_ciphertext.is_not(None)
                                | CalendarEventModel.location_ciphertext.is_not(None)
                            ),
                        )
                        .order_by(CalendarEventModel.id)
                        .limit(batch_size)
                        .with_for_update()
                    )
                ).all()
                if not ids:
                    return
                await session.execute(
                    update(CalendarEventModel)
                    .where(
                        CalendarEventModel.user_id == user_id,
                        CalendarEventModel.id.in_(ids),
                        CalendarEventModel.ends_at < cutoff,
                    )
                    .values(
                        description_ciphertext=None,
                        description_nonce=None,
                        description_key_version=None,
                        description_aad_version=None,
                        location_ciphertext=None,
                        location_nonce=None,
                        location_key_version=None,
                        location_aad_version=None,
                    )
                )

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
        # 审计表对普通应用角色保持追加写；retention 专用角色只在此明确保留用例中按用户和
        # cutoff 删除历史内容。本轮 ``retention.cleanup_completed`` 在本方法返回后才追加。
        await self._delete_ordinary_audits(user_id, cutoff, batch_size)
        await OAuthLifecycleCleanup(self._session_factory).clean_user(
            user_id=user_id,
            cutoff=cutoff,
            batch_size=batch_size,
        )

    async def _delete_ordinary_audits(
        self,
        user_id: UUID,
        cutoff: datetime,
        batch_size: int,
    ) -> None:
        """普通365天审计只排除五种 OAuth 事实与全部删除 authority，不读取 restore catalog。

        malformed/conflicting started 也不能被清理成一个看似合法的赢家。OAuth 的关闭组
        由专用共享 parser 路径处理；database.restore.completed 和其他普通审计同等到期。
        """
        protected = (
            "oauth.refresh_started",
            "oauth.refresh_confirmed",
            "oauth.refresh_recovery_authorization_started",
            "oauth.refresh_recovery_unsatisfied",
            "oauth.refresh_credential_replaced",
            PRIVACY_DELETION_STARTED_EVENT_TYPE,
        )
        while True:
            async with self._session_factory.begin() as session:
                ids = (
                    await session.scalars(
                        select(AuditEventModel.id)
                        .where(
                            AuditEventModel.user_id == user_id,
                            AuditEventModel.created_at < cutoff,
                            AuditEventModel.event_type.not_in(protected),
                        )
                        .order_by(AuditEventModel.id)
                        .limit(batch_size)
                    )
                ).all()
                if not ids:
                    return
                await session.execute(
                    delete(AuditEventModel).where(
                        AuditEventModel.user_id == user_id,
                        AuditEventModel.id.in_(ids),
                        AuditEventModel.created_at < cutoff,
                        AuditEventModel.event_type.not_in(protected),
                    )
                )

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
        """连续持有 Task→user 锁，先通过 app 清 checkpoint，再显式回收业务依赖图。"""
        await TaskHistoryCleanup(self._session_factory, self._checkpoint_cleaner).clean_user(
            user_id=user_id,
            cutoff=cutoff,
            batch_size=batch_size,
        )

    async def _clean_disconnected_credentials(self, user_id: UUID, batch_size: int) -> None:
        """锁后重验 disconnected 再清凭据/游标，阻止旧扫描误删并发重连的新凭据。

        仅对确有本地残留的连接取得两表 EXCLUSIVE NOWAIT 和已有 refresh audit mutex。
        竞争时整笔回滚并有界结束；不调用 OAuth lease 方法、不读 lineage、不解密 token。
        """
        after: UUID | None = None
        while True:
            async with self._session_factory() as session:
                candidates = select(OAuthConnectionModel.id).where(
                    OAuthConnectionModel.user_id == user_id,
                    OAuthConnectionModel.status == "disconnected",
                    exists(
                        select(EncryptedCredentialModel.id).where(
                            EncryptedCredentialModel.user_id == user_id,
                            EncryptedCredentialModel.connection_id == OAuthConnectionModel.id,
                        )
                    )
                    | exists(
                        select(SyncCursorModel.id).where(
                            SyncCursorModel.connection_id == OAuthConnectionModel.id,
                            SyncCursorModel.cursor.is_not(None)
                            | SyncCursorModel.last_success_at.is_not(None)
                            | SyncCursorModel.last_attempt_at.is_not(None)
                            | SyncCursorModel.last_error_code.is_not(None),
                        )
                    ),
                )
                if after is not None:
                    candidates = candidates.where(OAuthConnectionModel.id > after)
                connection_ids = (
                    await session.scalars(
                        candidates.order_by(OAuthConnectionModel.id).limit(batch_size)
                    )
                ).all()
            if not connection_ids:
                return
            for connection_id in connection_ids:
                try:
                    async with self._session_factory.begin() as session:
                        identity = await lock_oauth_cleanup_identity(
                            session,
                            user_id=user_id,
                            connection_id=connection_id,
                        )
                        if identity is None:
                            continue
                        status = await session.scalar(
                            select(OAuthConnectionModel.status).where(
                                OAuthConnectionModel.user_id == user_id,
                                OAuthConnectionModel.id == connection_id,
                            )
                        )
                        if status != "disconnected":
                            continue
                        await lock_cleanup_refresh_events(
                            session, user_id=user_id, connection_id=connection_id
                        )
                        active = await session.scalar(
                            select(UserModel.is_active)
                            .where(
                                UserModel.id == user_id,
                            )
                            .with_for_update()
                        )
                        if active is not True:
                            return
                        await session.execute(
                            delete(EncryptedCredentialModel).where(
                                EncryptedCredentialModel.user_id == user_id,
                                EncryptedCredentialModel.connection_id == connection_id,
                            )
                        )
                        await session.execute(
                            update(SyncCursorModel)
                            .where(
                                SyncCursorModel.connection_id == connection_id,
                            )
                            .values(
                                cursor=None,
                                last_success_at=None,
                                last_attempt_at=None,
                                last_error_code=None,
                            )
                        )
                except DBAPIError as error:
                    if not oauth_cleanup_lock_contended(error):
                        raise
                    return
            after = connection_ids[-1]

    async def _append_cleanup_audit(self, user_id: UUID, now: datetime) -> None:
        """锁后确认用户仍活动再追加运行事实，不能在最终匿名化后重建普通审计。"""
        async with self._session_factory.begin() as session:
            active = await session.scalar(
                select(UserModel.is_active)
                .where(
                    UserModel.id == user_id,
                )
                .with_for_update()
            )
            if active is not True:
                return
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
    return RetentionCleanupWorker(
        build_session_factory(database_url),
        checkpoint_cleaner=PostgresPrivacyCheckpointCleaner(settings.checkpoint_database_url),
    )
