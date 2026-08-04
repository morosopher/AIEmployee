"""提供 Gmail 规范化线程、邮件、游标和凭据旋转的事务仓储。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.gmail import GmailConnectionState, GmailMessage
from ai_employee.infrastructure.db.models.sources import (
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
class GmailConnectionCredentials:
    """表示已验证归属、但仍保持 AEAD 密文的 Gmail OAuth 凭据。"""

    access_token: EncryptedValue
    refresh_token: EncryptedValue | None


class SqlAlchemyGmailSyncRepository:
    """在调用方事务内维护 Gmail 可审计事实；所有方法均不自行提交。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由用例拥有的异步会话，禁止仓储跨边界提交。"""
        self._session = session

    async def get_state(
        self, *, user_id: UUID, connection_id: UUID
    ) -> GmailConnectionState | None:
        """按用户条件锁定连接及 Gmail 游标，阻止跨用户读取或游标竞争。

        Returns:
            连接有效时的游标状态；不存在、非 Google 或已断开连接均返回 ``None``。
        """
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.provider == "google",
                OAuthConnectionModel.status == "connected",
            )
            .with_for_update()
        )
        if connection is None:
            return None
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .where(SyncCursorModel.connection_id == connection_id, SyncCursorModel.resource_kind == "gmail")
            .with_for_update()
        )
        if cursor is None:
            cursor = SyncCursorModel(connection_id=connection_id, resource_kind="gmail", cursor=None)
            self._session.add(cursor)
            await self._session.flush()
        return GmailConnectionState(cursor.cursor)

    async def get_credentials(
        self, *, user_id: UUID, connection_id: UUID
    ) -> GmailConnectionCredentials | None:
        """读取当前连接的密文 token，不让明文或 ORM 行离开基础设施边界。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.provider == "google",
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
                        EncryptedCredentialModel.credential_kind.in_(("access_token", "refresh_token")),
                    )
                )
            ).all()
        )
        by_kind = {credential.credential_kind: credential for credential in credentials}
        access = by_kind.get("access_token")
        if access is None:
            return None
        refresh = by_kind.get("refresh_token")
        return GmailConnectionCredentials(
            access_token=EncryptedValue(access.ciphertext, access.nonce, access.key_version),
            refresh_token=(
                EncryptedValue(refresh.ciphertext, refresh.nonce, refresh.key_version)
                if refresh is not None
                else None
            ),
        )

    async def upsert_message(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        message: GmailMessage,
        encrypted_body: EncryptedValue,
    ) -> None:
        """原子 upsert 一个线程及其消息，保持幂等键和用户归属不变量。

        线程先按 ``connection_id + provider_thread_id`` 写入，消息随后以取得的本地主键按
        ``thread_id + provider_message_id`` 写入。重复投递只覆盖供应商可变元数据与新密文，
        不会制造第二个事实行，也不会保留原始 MIME 或附件。
        """
        participants = self._participants(message)
        thread_statement = insert(EmailThreadModel).values(
            user_id=user_id,
            connection_id=connection_id,
            provider_thread_id=message.thread_id,
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
            raise RuntimeError("Gmail thread upsert did not return an ID")
        message_statement = insert(EmailMessageModel).values(
            user_id=user_id,
            thread_id=thread_id,
            provider_message_id=message.message_id,
            received_at=message.received_at,
            sender=message.sender,
            recipients=message.recipients,
            subject=message.subject,
            snippet=message.snippet,
            body_ciphertext=encrypted_body.ciphertext,
            body_nonce=encrypted_body.nonce,
            body_key_version=encrypted_body.key_version,
            labels=list(message.labels),
            headers=message.headers,
            provider_url=message.provider_url,
        )
        await self._session.execute(
            message_statement.on_conflict_do_update(
                constraint="uq_email_messages_thread_provider_message",
                set_={
                    "received_at": message_statement.excluded.received_at,
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

    async def finish_sync(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        latest_history_id: str,
        thread_count: int,
        message_count: int,
        used_full_resync: bool,
        completed_at: datetime,
    ) -> None:
        """在同一事务中推进最终游标并追加不含正文的审计事实。

        游标只在所有页的 upsert 已成功排入当前事务后更新；提交失败会一起回滚，从而使下次
        至少一次执行从旧游标安全重放。审计 metadata 只保存聚合计数和游标，不复制正文。
        """
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .where(SyncCursorModel.connection_id == connection_id, SyncCursorModel.resource_kind == "gmail")
            .with_for_update()
        )
        if cursor is None:
            cursor = SyncCursorModel(connection_id=connection_id, resource_kind="gmail", cursor=None)
            self._session.add(cursor)
        cursor.cursor = latest_history_id
        cursor.last_success_at = completed_at
        cursor.last_attempt_at = completed_at
        cursor.last_error_code = None
        self._session.add(
            AuditEventModel(
                user_id=user_id,
                task_id=None,
                event_type="source.gmail.synced",
                actor_type="system",
                actor_id=str(connection_id),
                event_metadata={
                    "threads_upserted": thread_count,
                    "messages_upserted": message_count,
                    "cursor": latest_history_id,
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
        """把连续 401 的连接持久化为 expired，阻止后续 Worker 继续访问 Google。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(OAuthConnectionModel.id == connection_id, OAuthConnectionModel.user_id == user_id)
            .with_for_update()
        )
        if connection is not None:
            connection.status = "expired"
            connection.last_error_code = "google_unauthorized"

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
    def _participants(message: GmailMessage) -> list[dict[str, str]]:
        """以邮箱作为稳定键合并 sender/recipient，避免每次 history 重放扩增参与者。"""
        participants: dict[str, dict[str, str]] = {}
        for address in [message.sender, *message.recipients]:
            email = address.get("email")
            if email:
                participants[email.lower()] = {"name": address.get("name", ""), "email": email}
        return list(participants.values())


class SqlAlchemyGmailSyncRepositoryFactory:
    """为 Gmail 同步用例提供每次操作独立、自动提交或回滚的数据库事务。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存 Worker 进程拥有的 session factory，而不持有跨任务 session。"""
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlAlchemyGmailSyncRepository]:
        """在正常返回时提交，在异常时回滚所有 Gmail 事实及游标推进。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyGmailSyncRepository(session)
