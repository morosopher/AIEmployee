"""提供供应商中立邮件线程、消息、分 scope 游标与凭据旋转事务仓储。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, case, cast, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import JSONPATH, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement, SQLColumnExpression

from ai_employee.application.ports.encryption import EncryptedValue as ApplicationEncryptedValue
from ai_employee.application.ports.mail import (
    MailConnectionState,
    MailMessage,
    MailMessageUpsertResult,
    MailRemoval,
)
from ai_employee.application.use_cases.mail_drafts import (
    MailDraftSourceMessage,
    MailRecipientHistoryEntry,
)
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
from ai_employee.infrastructure.security.encryption import AeadCipher, EncryptedValue


@dataclass(frozen=True, slots=True)
class MailConnectionCredentials:
    """表示已验证归属、仍保持 AEAD 密文且携带规范 provider 的 OAuth 凭据。"""

    provider: str
    access_token: EncryptedValue
    refresh_token: EncryptedValue | None


class _MailMessageConflictTarget(StrEnum):
    """表示当前事务可安全使用的邮件消息 identity conflict target。"""

    NEW_CONSTRAINT = "new_constraint"
    NEW_INDEX = "new_index"
    LEGACY_CONSTRAINT = "legacy_constraint"


_NEW_MESSAGE_IDENTITY = "uq_email_messages_connection_provider_message"
_LEGACY_MESSAGE_IDENTITY = "uq_email_messages_thread_provider_message"
_MESSAGE_IDENTITY_CATALOG_ERROR = "mail message identity catalog is unsafe"


class SqlAlchemyMailSyncRepository:
    """在调用方事务内维护邮件可审计事实；所有方法均不自行提交。"""

    def __init__(self, session: AsyncSession, cipher: AeadCipher | None = None) -> None:
        """绑定由用例拥有的异步会话，禁止仓储跨边界提交。

        邮件 identity target 只允许在同一个数据库事务内缓存。0016/0017 online 部署期间
        不同事务可能看到 legacy constraint、已完成但未挂载的新索引或最终新约束；跨事务
        复用一次探测结果会把迁移窗口重新变成不可用窗口。

        Args:
            session: 调用方拥有的短事务会话。
            cipher: 可选源邮件正文 AEAD；同步写入不需要解密，草稿生成读取时必须注入。
        """
        self._session = session
        self._cipher = cipher
        self._message_conflict_transaction: object | None = None
        self._message_conflict_target: _MailMessageConflictTarget | None = None

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

    async def mark_directory_success(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        completed_at: datetime,
        folder_scope_keys: tuple[str, ...],
    ) -> None:
        """记录 Microsoft mailbox 目录成功，并保存所有已验证 folder 的存在事实。

        mailbox placeholder 是周期 owner 的协调事实；真实 folder 的增量恢复位置仍由各自
        ``finish_sync`` 事务维护。这里仅为首次发现且尚无行的 folder 建立 ``cursor=NULL``
        placeholder，不写 last_attempt/error/success；因此后续首次同步失败仍会被 Brief 识别为
        partial，而不会把 discovery 冒充 folder 尝试。已有 cursor 与时间全部保留，本次目录
        未返回的历史 scope 也不删除。供应商 I/O 已在进入本短事务前结束。

        Raises:
            StateConflictError: 连接不可同步、mailbox 含伪造 Delta cursor，或 scope tuple 未经
                非空、去重和稳定排序验证。
        """
        if (
            any(scope_key == "" or scope_key == "mailbox" for scope_key in folder_scope_keys)
            or len(set(folder_scope_keys)) != len(folder_scope_keys)
            or tuple(sorted(folder_scope_keys)) != folder_scope_keys
        ):
            raise StateConflictError(
                error_code="mail_directory_scopes_invalid",
                message="Mail directory scopes are not validated and stably sorted",
            )
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
                message="Mail connection is no longer available for discovery",
            )
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "mail",
                SyncCursorModel.scope_key == "mailbox",
            )
            .with_for_update()
        )
        if cursor is None:
            cursor = SyncCursorModel(
                connection_id=connection_id,
                resource_kind="mail",
                scope_key="mailbox",
                cursor=None,
            )
            self._session.add(cursor)
        elif cursor.cursor is not None:
            # 旧数据若把 mailbox 当作 Delta scope，宁可阻断并人工修复，也不覆盖 opaque 状态。
            raise StateConflictError(
                error_code="mailbox_cursor_must_be_null",
                message="Microsoft mailbox discovery cursor must remain empty",
            )
        cursor.last_success_at = completed_at
        cursor.last_attempt_at = completed_at
        cursor.last_error_code = None
        if folder_scope_keys:
            existing_scope_keys = set(
                (
                    await self._session.scalars(
                        select(SyncCursorModel.scope_key)
                        .where(
                            SyncCursorModel.connection_id == connection_id,
                            SyncCursorModel.resource_kind == "mail",
                            SyncCursorModel.scope_key.in_(folder_scope_keys),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for folder_scope_key in folder_scope_keys:
                if folder_scope_key not in existing_scope_keys:
                    # placeholder 只证明目录已发现该 folder。真正的尝试、错误或成功时间只能
                    # 由独立 folder 同步事务写入，不能在 discovery 阶段提前制造完整度事实。
                    self._session.add(
                        SyncCursorModel(
                            connection_id=connection_id,
                            resource_kind="mail",
                            scope_key=folder_scope_key,
                            cursor=None,
                        )
                    )
        self._session.add(
            AuditEventModel(
                user_id=user_id,
                task_id=None,
                event_type="source.mail.directory_discovered",
                actor_type="system",
                actor_id=str(connection_id),
                event_metadata={"folder_count": len(folder_scope_keys)},
            )
        )

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

    async def get_draft_source_message(
        self,
        *,
        user_id: UUID,
        source_thread_id: str | None,
        source_message_id: str | None,
        source_connection_id: UUID | None,
    ) -> MailDraftSourceMessage | None:
        """读取一封可访问的本地来源消息，并验证线程/消息/连接三者一致。

        ``source_thread_id`` 与 ``source_message_id`` 同时兼容本地 UUID 和供应商 opaque ID，
        但 UUID 字符串只按本地主键解释，不能再与另一连接的 provider ID 做 OR 匹配。纯
        provider ID 未携带连接时必须先证明只有一个候选连接；否则 fail closed 返回 ``None``。
        返回值始终规范为供应商 thread/message ID 供草稿冻结。连接必须仍为 connected 且
        ``mail.read`` enabled；跨用户、错线程、错连接、歧义或能力撤销统一返回 ``None``。

        Args:
            user_id: 当前认证用户。
            source_thread_id: 可选本地线程 UUID 或供应商线程 ID。
            source_message_id: 可选本地消息 UUID 或供应商消息 ID；缺失时选线程最新消息。
            source_connection_id: 可选调用方声称的精确来源连接。

        Returns:
            已验证的来源投影；无法证明完整绑定时返回 ``None``。
        """
        if source_thread_id is None and source_message_id is None:
            return None
        statement = (
            select(EmailMessageModel, EmailThreadModel)
            .join(
                EmailThreadModel,
                (EmailThreadModel.id == EmailMessageModel.thread_id)
                & (EmailThreadModel.user_id == EmailMessageModel.user_id)
                & (EmailThreadModel.connection_id == EmailMessageModel.connection_id),
            )
            .join(
                OAuthConnectionModel,
                (OAuthConnectionModel.id == EmailMessageModel.connection_id)
                & (OAuthConnectionModel.user_id == EmailMessageModel.user_id),
            )
            .join(
                ConnectionCapabilityModel,
                (ConnectionCapabilityModel.connection_id == OAuthConnectionModel.id)
                & (ConnectionCapabilityModel.user_id == OAuthConnectionModel.user_id),
            )
            .where(
                EmailMessageModel.user_id == user_id,
                EmailThreadModel.user_id == user_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
                ConnectionCapabilityModel.capability == "mail.read",
                ConnectionCapabilityModel.status == "enabled",
            )
            .order_by(EmailMessageModel.received_at.desc(), EmailMessageModel.id)
        )
        if source_connection_id is not None:
            statement = statement.where(EmailMessageModel.connection_id == source_connection_id)
        has_local_reference = False
        if source_thread_id is not None:
            thread_condition, thread_is_local = _draft_source_identifier_condition(
                source_thread_id,
                local_column=EmailThreadModel.id,
                provider_column=EmailThreadModel.provider_thread_id,
            )
            statement = statement.where(thread_condition)
            has_local_reference = has_local_reference or thread_is_local
        if source_message_id is not None:
            message_condition, message_is_local = _draft_source_identifier_condition(
                source_message_id,
                local_column=EmailMessageModel.id,
                provider_column=EmailMessageModel.provider_message_id,
            )
            statement = statement.where(message_condition)
            has_local_reference = has_local_reference or message_is_local
        if source_connection_id is None and not has_local_reference:
            candidate_connections = tuple(
                (
                    await self._session.scalars(
                        statement.with_only_columns(
                            EmailMessageModel.connection_id,
                            maintain_column_froms=True,
                        )
                        .order_by(None)
                        .distinct()
                        .limit(2)
                    )
                ).all()
            )
            if len(candidate_connections) != 1:
                # provider ID 只在连接内唯一；缺少连接且出现多个候选时不得按时间猜账户。
                return None
            statement = statement.where(
                EmailMessageModel.connection_id == candidate_connections[0]
            )
        row = (await self._session.execute(statement.limit(1))).one_or_none()
        if row is None:
            return None
        message, thread = row
        return self._draft_source_projection(message=message, thread=thread)

    async def list_draft_context_messages(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        source_thread_id: str,
    ) -> tuple[MailDraftSourceMessage, ...]:
        """返回同一可访问线程最近三封非垃圾本地消息。

        PostgreSQL 在任何正文解密前先验证 labels 为数组、大小写不敏感排除 ``spam``、
        按最新时间排序并限制三行。Python 仍重复执行 fail-closed spam 校验；Worker 的纯
        函数负责最终 12000 字符与第三道数量防线。正文密文已清除或未注入 cipher 的消息
        仅返回空正文，不伪造保留内容，也不会阻止其他可用消息进入上下文。
        """
        statement = (
            select(EmailMessageModel, EmailThreadModel)
            .join(
                EmailThreadModel,
                (EmailThreadModel.id == EmailMessageModel.thread_id)
                & (EmailThreadModel.user_id == EmailMessageModel.user_id)
                & (EmailThreadModel.connection_id == EmailMessageModel.connection_id),
            )
            .join(
                OAuthConnectionModel,
                (OAuthConnectionModel.id == EmailMessageModel.connection_id)
                & (OAuthConnectionModel.user_id == EmailMessageModel.user_id),
            )
            .join(
                ConnectionCapabilityModel,
                (ConnectionCapabilityModel.connection_id == OAuthConnectionModel.id)
                & (ConnectionCapabilityModel.user_id == OAuthConnectionModel.user_id),
            )
            .where(
                EmailMessageModel.user_id == user_id,
                EmailMessageModel.connection_id == connection_id,
                EmailThreadModel.user_id == user_id,
                EmailThreadModel.provider_thread_id == source_thread_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
                ConnectionCapabilityModel.capability == "mail.read",
                ConnectionCapabilityModel.status == "enabled",
                func.jsonb_typeof(EmailMessageModel.labels) == "array",
                ~EmailMessageModel.labels.op("@?")(
                    cast(
                        '$[*] ? (@ like_regex "^spam$" flag "i")',
                        JSONPATH,
                    )
                ),
            )
            .order_by(EmailMessageModel.received_at.desc(), EmailMessageModel.id)
            .limit(3)
        )
        rows = (await self._session.execute(statement)).all()
        result: list[MailDraftSourceMessage] = []
        for message, thread in rows:
            if _is_spam_labels(message.labels):
                continue
            result.append(self._draft_source_projection(message=message, thread=thread))
        return tuple(result)

    async def list_recipient_history(
        self, *, user_id: UUID
    ) -> tuple[MailRecipientHistoryEntry, ...]:
        """从本地同步 sender/recipient JSONB 派生地址最近出现事实。

        查询只使用当前用户行，并排除带 spam 标签的消息；返回值仍可能含历史 malformed
        地址，由应用层统一使用邮件领域规则规范化和去重。这里不访问 Contacts、供应商或模型。
        """
        rows = (
            await self._session.execute(
                select(
                    EmailMessageModel.sender,
                    EmailMessageModel.recipients,
                    EmailMessageModel.received_at,
                    EmailMessageModel.labels,
                )
                .where(EmailMessageModel.user_id == user_id)
                .order_by(EmailMessageModel.received_at.desc(), EmailMessageModel.id)
            )
        ).all()
        result: list[MailRecipientHistoryEntry] = []
        for sender, recipients, received_at, labels in rows:
            if _is_spam_labels(labels):
                continue
            for participant in (sender, *recipients):
                address = participant.get("email") if isinstance(participant, dict) else None
                if isinstance(address, str):
                    result.append(
                        MailRecipientHistoryEntry(
                            address=address,
                            last_seen_at=received_at,
                        )
                    )
        return tuple(result)

    def _draft_source_projection(
        self,
        *,
        message: EmailMessageModel,
        thread: EmailThreadModel,
    ) -> MailDraftSourceMessage:
        """复制来源 ORM 行并仅在完整 AEAD 三元组存在时解密正文。"""
        sender = message.sender.get("email") if isinstance(message.sender, dict) else None
        if not isinstance(sender, str):
            sender = ""
        recipients = tuple(
            address
            for value in message.recipients
            if isinstance(value, dict)
            for address in (value.get("email"),)
            if isinstance(address, str)
        )
        body_text = ""
        if (
            self._cipher is not None
            and message.body_ciphertext is not None
            and message.body_nonce is not None
            and message.body_key_version is not None
        ):
            body_text = self._cipher.decrypt(
                ApplicationEncryptedValue(
                    message.body_ciphertext,
                    message.body_nonce,
                    message.body_key_version,
                ),
                (
                    f"{message.user_id}:{message.connection_id}:"
                    f"{message.provider_message_id}:body"
                ).encode("ascii"),
            ).decode("utf-8")
        return MailDraftSourceMessage(
            connection_id=message.connection_id,
            thread_id=thread.provider_thread_id,
            message_id=message.provider_message_id,
            sender=sender,
            recipients=recipients,
            subject=message.subject,
            received_at=message.received_at,
            body_text=body_text,
            labels=tuple(message.labels),
            thread_summary="",
        )

    async def upsert_message(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        message: MailMessage,
        encrypted_body: EncryptedValue,
    ) -> MailMessageUpsertResult:
        """原子 upsert 一个规范化线程及其消息，保持幂等键和用户归属不变量。

        线程先按 ``connection_id + provider_thread_id`` 写入，消息随后以取得的本地主键按
        ``connection_id + provider_message_id`` 写入。Graph ImmutableId 在 folder move 或
        conversation 投影变化后仍代表同一消息，因此冲突更新必须同步切换 ``thread_id``、
        scope、规范元数据与新密文，不能制造第二个事实行或保留原始 MIME/附件。online
        迁移期间 conflict target 由当前事务的真实 PostgreSQL catalog 决定，不能假设 0017
        已经完成，也不能把供应商网络 I/O 带入这里。
        """
        conflict_target = await self._message_identity_conflict_target()
        if conflict_target == _MailMessageConflictTarget.LEGACY_CONSTRAINT:
            # 0016 尚无连接级唯一索引。锁定同一连接可串行化短暂部署窗口内的 folder
            # projection 写入，使下面的 legacy move 检查不会与另一个事务同时插入第二行。
            await self._lock_legacy_identity_connection(
                user_id=user_id,
                connection_id=connection_id,
            )
        participants = self._participants(message)
        thread_statement = insert(EmailThreadModel).values(
            user_id=user_id,
            connection_id=connection_id,
            provider_thread_id=message.provider_thread_id,
            subject=message.subject,
            participants=participants,
            latest_message_at=message.received_at,
            provider_url=message.provider_url,
            provider_updated_at=message.provider_updated_at,
        )
        thread_is_newer = self._incoming_projection_is_newer(
            stored=EmailThreadModel.provider_updated_at,
            incoming=thread_statement.excluded.provider_updated_at,
        )
        thread_id = await self._session.scalar(
            thread_statement.on_conflict_do_update(
                constraint="uq_email_threads_connection_provider_thread",
                set_={
                    "subject": case(
                        (thread_is_newer, thread_statement.excluded.subject),
                        else_=EmailThreadModel.subject,
                    ),
                    "participants": case(
                        (thread_is_newer, thread_statement.excluded.participants),
                        else_=EmailThreadModel.participants,
                    ),
                    "latest_message_at": func.greatest(
                        EmailThreadModel.latest_message_at,
                        thread_statement.excluded.latest_message_at,
                    ),
                    "provider_url": case(
                        (thread_is_newer, thread_statement.excluded.provider_url),
                        else_=EmailThreadModel.provider_url,
                    ),
                    "provider_updated_at": case(
                        (thread_is_newer, thread_statement.excluded.provider_updated_at),
                        else_=EmailThreadModel.provider_updated_at,
                    ),
                },
            ).returning(EmailThreadModel.id)
        )
        if thread_id is None:
            raise RuntimeError("Mail thread upsert did not return an ID")
        if conflict_target == _MailMessageConflictTarget.LEGACY_CONSTRAINT:
            legacy_result = await self._update_existing_legacy_message(
                user_id=user_id,
                connection_id=connection_id,
                thread_id=thread_id,
                message=message,
                encrypted_body=encrypted_body,
            )
            if legacy_result is not None:
                return legacy_result
        message_statement = insert(EmailMessageModel).values(
            user_id=user_id,
            connection_id=connection_id,
            thread_id=thread_id,
            provider_message_id=message.provider_message_id,
            internet_message_id=message.internet_message_id,
            provider_conversation_id=message.provider_conversation_id,
            received_at=message.received_at,
            sent_at=message.sent_at,
            provider_updated_at=message.provider_updated_at,
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
        message_is_newer = self._incoming_projection_is_newer(
            stored=EmailMessageModel.provider_updated_at,
            incoming=message_statement.excluded.provider_updated_at,
        )
        update_values = {
            # legacy 实例可能在 0016 nullable 窗口留下 NULL；即使本次命中旧 thread 约束，
            # 冲突更新也必须补写 direct connection，保证双写部署真正追赶旧列集合。
            "connection_id": message_statement.excluded.connection_id,
            "thread_id": message_statement.excluded.thread_id,
            "received_at": message_statement.excluded.received_at,
            "sent_at": message_statement.excluded.sent_at,
            "provider_updated_at": message_statement.excluded.provider_updated_at,
            "internet_message_id": message_statement.excluded.internet_message_id,
            "provider_conversation_id": message_statement.excluded.provider_conversation_id,
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
        }
        if conflict_target == _MailMessageConflictTarget.NEW_INDEX:
            conflict_statement = message_statement.on_conflict_do_update(
                index_elements=(
                    EmailMessageModel.connection_id,
                    EmailMessageModel.provider_message_id,
                ),
                set_=update_values,
                where=message_is_newer,
            )
        else:
            constraint_name = (
                _NEW_MESSAGE_IDENTITY
                if conflict_target == _MailMessageConflictTarget.NEW_CONSTRAINT
                else _LEGACY_MESSAGE_IDENTITY
            )
            conflict_statement = message_statement.on_conflict_do_update(
                constraint=constraint_name,
                set_=update_values,
                where=message_is_newer,
            )
        applied_id = await self._session.scalar(
            conflict_statement.returning(EmailMessageModel.id)
        )
        return (
            MailMessageUpsertResult.APPLIED
            if applied_id is not None
            else MailMessageUpsertResult.STALE_SKIPPED
        )

    async def _message_identity_conflict_target(self) -> _MailMessageConflictTarget:
        """按当前事务的真实 catalog 选择精确 conflict target。

        Returns:
            0016 legacy constraint、0017 standalone valid index 或最终新 constraint 对应模式。

        Raises:
            RuntimeError: 同名对象类型、归属表、唯一性、谓词或有序列不符合固定契约，或
                新旧 identity target 均不存在。错误文本固定且不包含 catalog 标识以外数据。
        """
        transaction = self._session.sync_session.get_transaction()
        if (
            transaction is not None
            and transaction is self._message_conflict_transaction
            and self._message_conflict_target is not None
        ):
            return self._message_conflict_target

        target = await self._resolve_message_identity_conflict_target()
        # catalog 查询本身会在未显式 begin 的兼容调用方中触发 autobegin；因此查询后再读取
        # 根事务对象，确保缓存最多存活到这一事务结束，绝不跨 0016/0017 部署阶段复用。
        self._message_conflict_transaction = self._session.sync_session.get_transaction()
        self._message_conflict_target = target
        return target

    async def _resolve_message_identity_conflict_target(self) -> _MailMessageConflictTarget:
        """按真实 catalog 选择当前事务可用且不会误删的邮件 identity target。

        0017 使用 ``CREATE UNIQUE INDEX CONCURRENTLY`` 时，精确新索引会短暂处于
        ``indisready=true``、``indisvalid=false``。只要它的完整键形状已被验证，且
        0016 legacy constraint 仍是有效的精确 ``(thread_id, provider_message_id)``，
        就必须回退到 legacy conflict target，保持 online expand/contract 窗口可写。
        错误表、表达式、INCLUDE、谓词、列数或列顺序永远不能触发 fallback。
        """
        new_constraints = await self._constraint_catalog_rows(_NEW_MESSAGE_IDENTITY)
        if new_constraints:
            if len(new_constraints) != 1 or not self._is_exact_identity_constraint(
                new_constraints[0],
                expected_columns=("connection_id", "provider_message_id"),
            ):
                raise RuntimeError(_MESSAGE_IDENTITY_CATALOG_ERROR)
            return _MailMessageConflictTarget.NEW_CONSTRAINT

        new_index = (
            await self._session.execute(
                text(
                    "SELECT index_class.relkind::text, table_class.relname, "
                    "index_info.indisready, index_info.indisvalid, "
                    "index_info.indisunique, index_info.indpred IS NULL, "
                    "index_info.indexprs IS NULL, index_info.indnkeyatts, "
                    "index_info.indnatts, ARRAY("
                    "SELECT CASE WHEN index_key.attnum = 0 THEN NULL "
                    "ELSE attribute.attname::text END "
                    "FROM unnest(index_info.indkey) WITH ORDINALITY "
                    "AS index_key(attnum, position) "
                    "LEFT JOIN pg_catalog.pg_attribute AS attribute "
                    "ON attribute.attrelid = index_info.indrelid "
                    "AND attribute.attnum = index_key.attnum "
                    "ORDER BY index_key.position) "
                    "FROM pg_catalog.pg_class AS index_class "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = index_class.relnamespace "
                    "LEFT JOIN pg_catalog.pg_index AS index_info "
                    "ON index_info.indexrelid = index_class.oid "
                    "LEFT JOIN pg_catalog.pg_class AS table_class "
                    "ON table_class.oid = index_info.indrelid "
                    "WHERE namespace.nspname = current_schema() "
                    "AND index_class.relname = :index_name"
                ),
                {"index_name": _NEW_MESSAGE_IDENTITY},
            )
        ).one_or_none()
        if new_index is not None:
            new_index_row = tuple(new_index)
            if not self._is_exact_identity_index(
                new_index_row,
                expected_columns=("connection_id", "provider_message_id"),
            ):
                # invalid CONCURRENTLY 索引只能由 0017 在明确迁移窗口内处理；应用继续写入
                # 会让 catalog 状态和冲突语义更难判定，因此必须立即 fail closed。
                raise RuntimeError(_MESSAGE_IDENTITY_CATALOG_ERROR)
            if not bool(new_index_row[3]):
                # 精确目标索引尚未 valid 时，0016 legacy constraint 仍是安全可用的
                # conflict target。先完整验证 legacy，拒绝把恶意同名约束当作降级入口。
                legacy_constraints = await self._constraint_catalog_rows(
                    _LEGACY_MESSAGE_IDENTITY
                )
                if len(legacy_constraints) != 1 or not self._is_exact_identity_constraint(
                    legacy_constraints[0],
                    expected_columns=("thread_id", "provider_message_id"),
                ):
                    raise RuntimeError(_MESSAGE_IDENTITY_CATALOG_ERROR)
                return _MailMessageConflictTarget.LEGACY_CONSTRAINT
            return _MailMessageConflictTarget.NEW_INDEX

        legacy_constraints = await self._constraint_catalog_rows(_LEGACY_MESSAGE_IDENTITY)
        if len(legacy_constraints) != 1 or not self._is_exact_identity_constraint(
            legacy_constraints[0],
            expected_columns=("thread_id", "provider_message_id"),
        ):
            raise RuntimeError(_MESSAGE_IDENTITY_CATALOG_ERROR)
        return _MailMessageConflictTarget.LEGACY_CONSTRAINT

    async def _constraint_catalog_rows(
        self,
        constraint_name: str,
    ) -> tuple[tuple[object, ...], ...]:
        """读取当前 schema 内同名 constraint 及其 backing index 的精确形状。"""
        rows = await self._session.execute(
            text(
                "SELECT table_class.relname, constraint_info.contype::text, "
                "ARRAY(SELECT attribute.attname::text "
                "FROM unnest(constraint_info.conkey) WITH ORDINALITY "
                "AS constraint_key(attnum, position) "
                "LEFT JOIN pg_catalog.pg_attribute AS attribute "
                "ON attribute.attrelid = constraint_info.conrelid "
                "AND attribute.attnum = constraint_key.attnum "
                "ORDER BY constraint_key.position), "
                "index_info.indisvalid, index_info.indisunique, "
                "index_info.indpred IS NULL, index_info.indexprs IS NULL, "
                "index_info.indisready, index_info.indnkeyatts, "
                "index_info.indnatts, ARRAY("
                "SELECT CASE WHEN index_key.attnum = 0 THEN NULL "
                "ELSE attribute.attname::text END "
                "FROM unnest(index_info.indkey) WITH ORDINALITY "
                "AS index_key(attnum, position) "
                "LEFT JOIN pg_catalog.pg_attribute AS attribute "
                "ON attribute.attrelid = index_info.indrelid "
                "AND attribute.attnum = index_key.attnum "
                "ORDER BY index_key.position) "
                "FROM pg_catalog.pg_constraint AS constraint_info "
                "JOIN pg_catalog.pg_class AS table_class "
                "ON table_class.oid = constraint_info.conrelid "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = table_class.relnamespace "
                "LEFT JOIN pg_catalog.pg_index AS index_info "
                "ON index_info.indexrelid = constraint_info.conindid "
                "WHERE namespace.nspname = current_schema() "
                "AND constraint_info.conname = :constraint_name"
            ),
            {"constraint_name": constraint_name},
        )
        return tuple(tuple(row) for row in rows)

    @staticmethod
    def _is_exact_identity_constraint(
        row: tuple[object, ...],
        *,
        expected_columns: tuple[str, ...],
    ) -> bool:
        """确认唯一 constraint 与其 backing index 的完整形状均精确匹配。"""
        if len(row) != 11:
            return False
        constraint_columns = SqlAlchemyMailSyncRepository._catalog_columns(row[2])
        index_columns = SqlAlchemyMailSyncRepository._catalog_columns(row[10])
        return (
            row[0] == "email_messages"
            and row[1] == "u"
            and constraint_columns == expected_columns
            and bool(row[3])
            and bool(row[4])
            and bool(row[5])
            and bool(row[6])
            and bool(row[7])
            and row[8] == len(expected_columns)
            and row[9] == len(expected_columns)
            and index_columns == expected_columns
        )

    @staticmethod
    def _is_exact_identity_index(
        row: tuple[object, ...],
        *,
        expected_columns: tuple[str, ...],
    ) -> bool:
        """确认 standalone index 的表、键类型、谓词和完整列形状均符合契约。"""
        if len(row) != 10:
            return False
        index_columns = SqlAlchemyMailSyncRepository._catalog_columns(row[9])
        return (
            row[0] == "i"
            and row[1] == "email_messages"
            and bool(row[4])
            and bool(row[5])
            and bool(row[6])
            and row[7] == len(expected_columns)
            and row[8] == len(expected_columns)
            and index_columns == expected_columns
        )

    @staticmethod
    def _catalog_columns(value: object) -> tuple[str, ...] | None:
        """把 asyncpg 的 PostgreSQL text[] 收窄为纯字符串 tuple，坏类型返回 ``None``。"""
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(column, str) for column in value
        ):
            return None
        return tuple(value)

    async def _lock_legacy_identity_connection(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> None:
        """在 0016 窗口锁定连接，串行化缺少连接级唯一索引的短事务。"""
        locked_id = await self._session.scalar(
            select(OAuthConnectionModel.id)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        if locked_id is None:
            raise StateConflictError(
                error_code="mail_connection_not_syncable",
                message="Mail connection is no longer available for message persistence",
            )

    async def _update_existing_legacy_message(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        thread_id: UUID,
        message: MailMessage,
        encrypted_body: EncryptedValue,
    ) -> MailMessageUpsertResult | None:
        """在 0016 legacy constraint 下原地移动已有 connection-level projection。

        查询通过 thread 的可信 connection 归属而不是 nullable direct 列，因此也能补齐旧
        实例在 expand 窗口写入的 NULL。连接行已由调用方锁定；若仍看到两行，说明历史数据
        在双写部署前已经含糊，必须留给迁移 preflight 和人工修复，不能任意选一行。
        """
        rows = tuple(
            (
                await self._session.scalars(
                    select(EmailMessageModel)
                    .join(EmailThreadModel, EmailThreadModel.id == EmailMessageModel.thread_id)
                    .where(
                        EmailMessageModel.user_id == user_id,
                        EmailThreadModel.user_id == user_id,
                        EmailThreadModel.connection_id == connection_id,
                        EmailMessageModel.provider_message_id == message.provider_message_id,
                    )
                    .order_by(EmailMessageModel.id)
                    .limit(2)
                    .with_for_update(of=EmailMessageModel)
                )
            ).all()
        )
        if len(rows) > 1:
            raise RuntimeError(
                "mail message connection-level duplicate requires manual repair"
            )
        if not rows:
            return None
        stored = rows[0]
        if not self._incoming_projection_value_is_newer(
            stored=stored.provider_updated_at,
            incoming=message.provider_updated_at,
        ):
            return MailMessageUpsertResult.STALE_SKIPPED
        self._apply_message_projection(
            stored=stored,
            connection_id=connection_id,
            thread_id=thread_id,
            message=message,
            encrypted_body=encrypted_body,
        )
        return MailMessageUpsertResult.APPLIED

    @staticmethod
    def _apply_message_projection(
        *,
        stored: EmailMessageModel,
        connection_id: UUID,
        thread_id: UUID,
        message: MailMessage,
        encrypted_body: EncryptedValue,
    ) -> None:
        """把经过版本判断的规范字段写回同一 ORM 行，并保持正文只存 AEAD 三元组。"""
        stored.connection_id = connection_id
        stored.thread_id = thread_id
        stored.received_at = message.received_at
        stored.sent_at = message.sent_at
        stored.provider_updated_at = message.provider_updated_at
        stored.internet_message_id = message.internet_message_id
        stored.provider_conversation_id = message.provider_conversation_id
        stored.mailbox_scope_key = message.mailbox_scope_key
        stored.sender = dict(message.sender)
        stored.recipients = [dict(recipient) for recipient in message.recipients]
        stored.subject = message.subject
        stored.snippet = ""
        stored.body_ciphertext = encrypted_body.ciphertext
        stored.body_nonce = encrypted_body.nonce
        stored.body_key_version = encrypted_body.key_version
        stored.labels = list(message.labels)
        stored.headers = dict(message.normalized_reply_headers)
        stored.provider_url = message.provider_url

    @staticmethod
    def _incoming_projection_value_is_newer(
        *,
        stored: datetime | None,
        incoming: datetime | None,
    ) -> bool:
        """以 Python 值复刻 SQL 版本顺序，供已锁定的 0016 legacy 行更新。"""
        if stored is None:
            return True
        return incoming is not None and incoming > stored

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

    @staticmethod
    def _incoming_projection_is_newer(
        *,
        stored: SQLColumnExpression[datetime | None],
        incoming: SQLColumnExpression[datetime | None],
    ) -> ColumnElement[bool]:
        """构造 provider 版本比较：Google 双 NULL 兼容，equal/旧值一律不覆盖。

        ``stored`` 与 ``incoming`` 是 SQLAlchemy 列表达式。返回表达式只用于数据库
        ``CASE``/``ON CONFLICT WHERE``，避免先读后写造成迟到页面竞态。
        """
        return or_(
            and_(stored.is_(None), incoming.is_(None)),
            and_(stored.is_(None), incoming.is_not(None)),
            and_(
                stored.is_not(None),
                incoming.is_not(None),
                incoming > stored,
            ),
        )


class SqlAlchemyMailSyncRepositoryFactory:
    """为邮件同步用例提供每次操作独立、自动提交或回滚的数据库事务。"""

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        cipher: AeadCipher | None = None,
    ) -> None:
        """保存 Worker 进程会话工厂及可选正文解密器，不持有跨任务 session。"""
        self._session_factory = session_factory
        self._cipher = cipher

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlAlchemyMailSyncRepository]:
        """在正常返回时提交，在异常时回滚所有邮件事实及 scope 游标推进。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyMailSyncRepository(session, self._cipher)


def _draft_source_identifier_condition(
    value: str,
    *,
    local_column: SQLColumnExpression[UUID],
    provider_column: SQLColumnExpression[str],
) -> tuple[ColumnElement[bool], bool]:
    """把来源引用解释为互斥的本地 UUID 或供应商 opaque ID。

    ``UUID`` 解析失败不会把输入拼入 SQL，而是只保留 provider 列的参数化等值条件。
    解析成功时只允许本地主键比较，避免同一 UUID 字符串同时命中另一连接 provider ID。

    Returns:
        SQL 等值条件，以及该值是否被解释为本地 UUID。
    """
    try:
        local_id = UUID(value)
    except ValueError:
        return provider_column == value, False
    return local_column == local_id, True


def _is_spam_labels(labels: object) -> bool:
    """只把字符串标签中的精确 ``spam`` 视为垃圾邮件，异常 JSON fail closed 跳过。"""
    if not isinstance(labels, list):
        return True
    return any(isinstance(label, str) and label.casefold() == "spam" for label in labels)


# M2 迁移期间保留旧类名，避免已持久化任务和现有测试导入立即失效；实现语义已经完全
# 使用 provider-neutral ``mail`` 资源和精确 scope。
GmailConnectionCredentials = MailConnectionCredentials
SqlAlchemyGmailSyncRepository = SqlAlchemyMailSyncRepository
SqlAlchemyGmailSyncRepositoryFactory = SqlAlchemyMailSyncRepositoryFactory
