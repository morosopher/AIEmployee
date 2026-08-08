"""提供供应商中立邮件线程、消息、分 scope 游标与凭据旋转事务仓储。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.mail import MailConnectionState, MailMessage, MailRemoval
from ai_employee.domain.errors import StateConflictError, TransientProviderError
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.encryption import EncryptedValue


@dataclass(frozen=True, slots=True)
class MailConnectionCredentials:
    """表示已验证归属、仍保持 AEAD 密文且携带规范 provider 的 OAuth 凭据。"""

    provider: str
    access_token: EncryptedValue
    refresh_token: EncryptedValue | None


class SqlAlchemyMailSyncRepository:
    """在调用方事务内维护邮件可审计事实；所有方法均不自行提交。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由用例拥有的异步会话，禁止仓储跨边界提交。"""
        self._session = session

    async def get_state(
        self, *, user_id: UUID, connection_id: UUID, scope_key: str
    ) -> MailConnectionState | None:
        """按用户、启用能力和精确 scope 锁定邮件游标。

        Returns:
            连接及 ``mail.read`` 能力有效时的 provider/scoped 游标；否则返回 ``None``。
        """
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .join(
                ConnectionCapabilityModel,
                (ConnectionCapabilityModel.connection_id == OAuthConnectionModel.id)
                & (ConnectionCapabilityModel.user_id == OAuthConnectionModel.user_id),
            )
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
                ConnectionCapabilityModel.capability == "mail.read",
                ConnectionCapabilityModel.status == "enabled",
            )
            .with_for_update()
        )
        if connection is None:
            return None
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "mail",
                SyncCursorModel.scope_key == scope_key,
            )
            .with_for_update()
        )
        # 初始同步尚无游标行是正常的未同步状态。这里必须保持纯读取，否则独立的状态
        # 事务会先提交占位行，后续供应商页缺最终游标时便无法随最终写事务一起回滚。
        # 只有 finish_sync 在验证有效最终游标后才能创建该精确 scope 的第一行。
        return MailConnectionState(
            provider=connection.provider,
            scope_key=scope_key,
            cursor=cursor.cursor if cursor is not None else None,
        )

    async def clear_cursor(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        expected_cursor: str,
    ) -> None:
        """在游标失效后只以 CAS 清除同一个邮件 scope 的恢复位置。"""
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .join(OAuthConnectionModel, OAuthConnectionModel.id == SyncCursorModel.connection_id)
            .join(
                ConnectionCapabilityModel,
                (ConnectionCapabilityModel.connection_id == OAuthConnectionModel.id)
                & (ConnectionCapabilityModel.user_id == OAuthConnectionModel.user_id),
            )
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "mail",
                SyncCursorModel.scope_key == scope_key,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
                ConnectionCapabilityModel.capability == "mail.read",
                ConnectionCapabilityModel.status == "enabled",
            )
            .with_for_update()
        )
        if cursor is None or cursor.cursor != expected_cursor:
            raise TransientProviderError(
                error_code="mail_sync_cursor_conflict",
                message="Mail sync cursor changed during provider read",
                retry_after=1,
            )
        cursor.cursor = None

    async def get_credentials(
        self, *, user_id: UUID, connection_id: UUID
    ) -> MailConnectionCredentials | None:
        """读取当前连接 provider 与密文 token，不让明文或 ORM 行离开基础设施边界。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
            )
        )
        if connection is None:
            return None
        credentials = tuple(
            (
                await self._session.scalars(
                    select(EncryptedCredentialModel).where(
                        EncryptedCredentialModel.connection_id == connection_id,
                        EncryptedCredentialModel.user_id == user_id,
                        EncryptedCredentialModel.credential_kind.in_(
                            ("access_token", "refresh_token")
                        ),
                    )
                )
            ).all()
        )
        by_kind = {credential.credential_kind: credential for credential in credentials}
        access = by_kind.get("access_token")
        if access is None:
            return None
        refresh = by_kind.get("refresh_token")
        return MailConnectionCredentials(
            provider=connection.provider,
            access_token=EncryptedValue(access.ciphertext, access.nonce, access.key_version),
            refresh_token=(
                EncryptedValue(refresh.ciphertext, refresh.nonce, refresh.key_version)
                if refresh is not None
                else None
            ),
        )

    async def get_user_timezone(self, *, user_id: UUID) -> str | None:
        """读取已验证用户 IANA 时区，Calendar 适配器不能退回宿主机或硬编码 UTC。"""
        return await self._session.scalar(select(UserModel.timezone).where(UserModel.id == user_id))

    async def upsert_message(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        message: MailMessage,
        encrypted_body: EncryptedValue,
    ) -> None:
        """原子 upsert 一个规范化线程及其消息，保持幂等键和用户归属不变量。

        线程先按 ``connection_id + provider_thread_id`` 写入，消息随后以取得的本地主键按
        ``connection_id + provider_message_id`` 写入。Graph ImmutableId 在 folder move 或
        conversation 投影变化后仍代表同一消息，因此冲突更新必须同步切换 ``thread_id``、
        scope、规范元数据与新密文，不能制造第二个事实行或保留原始 MIME/附件。
        """
        participants = self._participants(message)
        thread_statement = insert(EmailThreadModel).values(
            user_id=user_id,
            connection_id=connection_id,
            provider_thread_id=message.provider_thread_id,
            subject=message.subject,
            participants=participants,
            latest_message_at=message.received_at,
            provider_url=message.provider_url,
        )
        thread_id = await self._session.scalar(
            thread_statement.on_conflict_do_update(
                constraint="uq_email_threads_connection_provider_thread",
                set_={
                    "subject": thread_statement.excluded.subject,
                    "participants": thread_statement.excluded.participants,
                    "latest_message_at": func.greatest(
                        EmailThreadModel.latest_message_at,
                        thread_statement.excluded.latest_message_at,
                    ),
                    "provider_url": thread_statement.excluded.provider_url,
                },
            ).returning(EmailThreadModel.id)
        )
        if thread_id is None:
            raise RuntimeError("Mail thread upsert did not return an ID")
        message_statement = insert(EmailMessageModel).values(
            user_id=user_id,
            connection_id=connection_id,
            thread_id=thread_id,
            provider_message_id=message.provider_message_id,
            internet_message_id=message.internet_message_id,
            provider_conversation_id=message.provider_conversation_id,
            received_at=message.received_at,
            sent_at=message.sent_at,
            mailbox_scope_key=message.mailbox_scope_key,
            sender=dict(message.sender),
            recipients=[dict(recipient) for recipient in message.recipients],
            subject=message.subject,
            # 供应商摘录可能泄露正文；正文只能经 AAD 加密字段存储，明文列必须保持为空。
            snippet="",
            body_ciphertext=encrypted_body.ciphertext,
            body_nonce=encrypted_body.nonce,
            body_key_version=encrypted_body.key_version,
            labels=list(message.labels),
            headers=dict(message.normalized_reply_headers),
            provider_url=message.provider_url,
        )
        await self._session.execute(
            message_statement.on_conflict_do_update(
                constraint="uq_email_messages_connection_provider_message",
                set_={
                    "thread_id": message_statement.excluded.thread_id,
                    "received_at": message_statement.excluded.received_at,
                    "sent_at": message_statement.excluded.sent_at,
                    "internet_message_id": message_statement.excluded.internet_message_id,
                    "provider_conversation_id": (
                        message_statement.excluded.provider_conversation_id
                    ),
                    "mailbox_scope_key": message_statement.excluded.mailbox_scope_key,
                    "sender": message_statement.excluded.sender,
                    "recipients": message_statement.excluded.recipients,
                    "subject": message_statement.excluded.subject,
                    "snippet": message_statement.excluded.snippet,
                    "body_ciphertext": message_statement.excluded.body_ciphertext,
                    "body_nonce": message_statement.excluded.body_nonce,
                    "body_key_version": message_statement.excluded.body_key_version,
                    "labels": message_statement.excluded.labels,
                    "headers": message_statement.excluded.headers,
                    "provider_url": message_statement.excluded.provider_url,
                },
            )
        )

    async def remove_message(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        removal: MailRemoval,
    ) -> None:
        """按用户、连接、folder scope 和不可变消息 ID安全删除一个墓碑对象。

        direct ``connection_id`` 与 ``user_id``、scope、ImmutableId 共同形成完整删除谓词；
        即使供应商重复投递同一 tombstone，或另一连接拥有相同 ID，也不会扩大删除范围。
        空线程暂时保留其无敏感正文的索引元数据，避免墓碑与分析/审计外键形成级联副作用。
        """
        statement = delete(EmailMessageModel).where(
            EmailMessageModel.user_id == user_id,
            EmailMessageModel.connection_id == connection_id,
            EmailMessageModel.provider_message_id == removal.provider_message_id,
            EmailMessageModel.mailbox_scope_key == removal.mailbox_scope_key,
        )
        await self._session.execute(statement)

    async def finish_sync(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        expected_cursor: str | None,
        scope_key: str,
        next_cursor: str,
        thread_count: int,
        message_count: int,
        used_full_resync: bool,
        completed_at: datetime,
        removed_count: int = 0,
    ) -> None:
        """在同一事务中 CAS 推进精确 scope 游标并追加不含正文/游标的审计事实。

        游标只在所有页的 upsert 已成功排入当前事务后更新；提交失败会一起回滚，从而使下次
        至少一次执行从旧游标安全重放。最终锁定后必须仍等于网络读取前的 ``expected_cursor``；
        否则更晚同步已经提交，当前事务连同邮件写入一起回滚并交给 Durable Worker 重试。
        审计 metadata 只保存聚合计数和本地 scope key，不复制正文或 opaque 游标。
        """
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .join(
                ConnectionCapabilityModel,
                (ConnectionCapabilityModel.connection_id == OAuthConnectionModel.id)
                & (ConnectionCapabilityModel.user_id == OAuthConnectionModel.user_id),
            )
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
                ConnectionCapabilityModel.capability == "mail.read",
                ConnectionCapabilityModel.status == "enabled",
            )
            .with_for_update()
        )
        if connection is None:
            raise StateConflictError(
                error_code="mail_connection_not_syncable",
                message="Mail connection is no longer available for sync",
            )
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "mail",
                SyncCursorModel.scope_key == scope_key,
            )
            .with_for_update()
        )
        if cursor is None:
            cursor = SyncCursorModel(
                connection_id=connection_id,
                resource_kind="mail",
                scope_key=scope_key,
                cursor=None,
            )
            self._session.add(cursor)
        if cursor.cursor != expected_cursor:
            raise TransientProviderError(
                error_code="mail_sync_cursor_conflict",
                message="Mail sync cursor changed during provider read",
                retry_after=1,
            )
        cursor.cursor = next_cursor
        cursor.last_success_at = completed_at
        cursor.last_attempt_at = completed_at
        cursor.last_error_code = None
        self._session.add(
            AuditEventModel(
                user_id=user_id,
                task_id=None,
                event_type="source.mail.synced",
                actor_type="system",
                actor_id=str(connection_id),
                event_metadata={
                    "threads_upserted": thread_count,
                    "messages_upserted": message_count,
                    "messages_removed": removed_count,
                    "scope_key": scope_key,
                    "used_full_resync": used_full_resync,
                },
            )
        )

    async def rotate_access_token(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        access_token: EncryptedValue,
        expires_at: datetime,
        refresh_token: EncryptedValue | None,
    ) -> None:
        """原子写入新 access 密文和过期时间，仅在轮换时覆盖 refresh credential。"""
        await self._upsert_credential(
            user_id=user_id,
            connection_id=connection_id,
            kind="access_token",
            encrypted=access_token,
            expires_at=expires_at,
        )
        if refresh_token is not None:
            await self._upsert_credential(
                user_id=user_id,
                connection_id=connection_id,
                kind="refresh_token",
                encrypted=refresh_token,
                expires_at=None,
            )

    async def mark_expired(self, *, user_id: UUID, connection_id: UUID) -> None:
        """把撤销授权持久化为可见的降级状态，阻止后续 Worker 继续读取供应商。

        Mail 与 Calendar 暂时共用此凭据仓储；因此这里是两类只读资源发生永久授权失败时
        的唯一事实写入点，连接列表能够以稳定错误码提示用户重新授权。
        """
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id, OAuthConnectionModel.user_id == user_id
            )
            .with_for_update()
        )
        if connection is not None:
            connection.status = "degraded"
            connection.last_error_code = "oauth_revoked"

    async def mark_mail_capability_action_required(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        error_code: str = "microsoft_mail_permission_required",
    ) -> None:
        """持久化邮件读取权限撤销，只降级 ``mail.read`` 能力而不误断开连接。

        Graph 403 只证明当前 delegated mail scope 不可用，不能推断 OIDC 连接身份或日历
        scope 已失效。锁定同用户/连接的 capability 行并写入稳定错误码，Scheduler 会因
        ``status != enabled`` 停止该 folder 的新任务；原有 folder cursor 保留供重新授权后
        继续使用。
        """
        capability = await self._session.scalar(
            select(ConnectionCapabilityModel)
            .join(
                OAuthConnectionModel,
                (OAuthConnectionModel.id == ConnectionCapabilityModel.connection_id)
                & (OAuthConnectionModel.user_id == ConnectionCapabilityModel.user_id),
            )
            .where(
                ConnectionCapabilityModel.user_id == user_id,
                ConnectionCapabilityModel.connection_id == connection_id,
                ConnectionCapabilityModel.capability == "mail.read",
                OAuthConnectionModel.status == "connected",
            )
            .with_for_update()
        )
        if capability is not None:
            capability.status = "action_required"
            capability.last_error_code = error_code

    async def _upsert_credential(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        kind: str,
        encrypted: EncryptedValue,
        expires_at: datetime | None,
    ) -> None:
        """按连接和凭据种类覆写唯一行，不创建多份可用 token。"""
        statement = insert(EncryptedCredentialModel).values(
            user_id=user_id,
            connection_id=connection_id,
            credential_kind=kind,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            key_version=encrypted.key_version,
            token_expires_at=expires_at,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_encrypted_credentials_connection_kind",
                set_={
                    "ciphertext": statement.excluded.ciphertext,
                    "nonce": statement.excluded.nonce,
                    "key_version": statement.excluded.key_version,
                    "token_expires_at": statement.excluded.token_expires_at,
                },
            )
        )

    @staticmethod
    def _participants(message: MailMessage) -> list[dict[str, str]]:
        """以邮箱作为稳定键合并 sender/recipient，避免增量重放扩增参与者。"""
        participants: dict[str, dict[str, str]] = {}
        for address in (message.sender, *message.recipients):
            email = address.get("email")
            if email:
                participants[email.lower()] = {"name": address.get("name", ""), "email": email}
        return list(participants.values())


class SqlAlchemyMailSyncRepositoryFactory:
    """为邮件同步用例提供每次操作独立、自动提交或回滚的数据库事务。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存 Worker 进程拥有的 session factory，而不持有跨任务 session。"""
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlAlchemyMailSyncRepository]:
        """在正常返回时提交，在异常时回滚所有邮件事实及 scope 游标推进。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyMailSyncRepository(session)


# M2 迁移期间保留旧类名，避免已持久化任务和现有测试导入立即失效；实现语义已经完全
# 使用 provider-neutral ``mail`` 资源和精确 scope。
GmailConnectionCredentials = MailConnectionCredentials
SqlAlchemyGmailSyncRepository = SqlAlchemyMailSyncRepository
SqlAlchemyGmailSyncRepositoryFactory = SqlAlchemyMailSyncRepositoryFactory
