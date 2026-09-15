"""以 PostgreSQL CAS 和记录绑定 AEAD 持久化本地邮件草稿。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.use_cases.mail_drafts import (
    MailDraftCapabilitySnapshot,
    MailDraftConnectionSnapshot,
    MailDraftRecipient,
    MailDraftSnapshot,
    MailDraftStateSnapshot,
)
from ai_employee.domain.actions import MailDraftStatus, transition_mail_draft
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode
from ai_employee.infrastructure.db.models.actions import (
    MailDraftModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.repositories.historical_action_bindings import (
    preserve_historical_action_bindings,
)
from ai_employee.infrastructure.db.repositories.identity import lock_active_user
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher

MAIL_DRAFT_CONTENT_KIND = "mail_draft_body"
MAIL_DRAFT_ACTION = "mail.draft"
MAIL_DRAFT_SCHEMA_VERSION = "mail_draft_body.v1"


class SqlAlchemyMailDraftRepository:
    """在调用方拥有的异步事务内维护草稿头和不可变加密版本。

    Repository 从不提交。所有可能失败的时间、正文和 recipient 内容准备都在首个
    INSERT/CAS 前完成，调用方即使捕获准备异常并正常提交也不会留下半成品。创建幂等由
    数据库唯一约束裁决，版本更新再以父行 ``current_version`` CAS 认领不可变子行版本。
    """

    def __init__(self, session: AsyncSession, cipher: ActionPayloadCipher) -> None:
        """绑定当前事务会话和记录级 JSON 加密器。

        Args:
            session: 由应用用例开启并负责提交或回滚的异步会话。
            cipher: 使用版本行 ID 构造 AAD 的操作内容加密器。
        """
        self._session = session
        self._cipher = cipher

    async def get_existing_creation(
        self,
        *,
        user_id: UUID,
        creation_idempotency_key: str,
        creation_payload_hash: str,
    ) -> MailDraftSnapshot | None:
        """按用户与创建键读取草稿，并验证键精确绑定当前规范请求哈希。

        该方法供应用用例在当前连接能力、默认设置和来源事实校验前识别已提交的创建重放；
        ``create`` 的并发唯一约束输家也复用它，避免只按键返回另一请求创建的草稿。

        Args:
            user_id: 当前认证用户，查询必须显式隔离该值。
            creation_idempotency_key: 用户范围内的创建重放键。
            creation_payload_hash: 当前规范客户端请求的 SHA-256 摘要。

        Returns:
            键不存在时返回 ``None``；精确命中时返回当前解密草稿快照。

        Raises:
            StateConflictError: 键已绑定不同请求哈希，或既有正文已不可用。
            cryptography.exceptions.InvalidTag: 既有正文密文或 AAD 被篡改。
        """
        existing = await self._session.scalar(
            select(MailDraftModel).where(
                MailDraftModel.user_id == user_id,
                MailDraftModel.creation_idempotency_key == creation_idempotency_key,
            )
        )
        if existing is None:
            return None
        if not compare_digest(existing.creation_payload_hash, creation_payload_hash):
            raise _idempotency_payload_mismatch()
        return await self._snapshot(existing)

    async def create(
        self,
        *,
        draft_id: UUID,
        version_id: UUID,
        user_id: UUID,
        connection_id: UUID,
        creation_idempotency_key: str,
        creation_payload_hash: str,
        source_thread_id: str | None,
        source_message_id: str | None,
        mode: MailMode,
        retain_until: datetime,
        to_recipients: tuple[MailDraftRecipient, ...],
        cc_recipients: tuple[MailDraftRecipient, ...],
        bcc_recipients: tuple[MailDraftRecipient, ...],
        subject: str,
        body_text: str,
        prompt_version: str | None,
        model_name: str | None,
    ) -> MailDraftSnapshot:
        """幂等创建草稿头和版本一，并保证正文从未以明文进入 ORM。

        同一用户和创建键发生冲突时，只有规范请求哈希完全相同才返回既有草稿。
        哈希不同会抛固定冲突，错误消息不包含创建键、地址、主题或正文。

        Args:
            draft_id: 新草稿的调用方生成稳定 UUID；重放时可能被既有 ID 取代。
            version_id: 版本一的稳定 UUID，同时作为正文 AAD 的记录维度。
            user_id: 当前认证用户，所有查询和写入均显式绑定该值。
            connection_id: 草稿固定发送连接。
            creation_idempotency_key: 用户范围内的创建重放键。
            creation_payload_hash: 完整规范创建请求的 SHA-256 十六进制哈希。
            source_thread_id: 回复类草稿的供应商线程标识。
            source_message_id: 回复类草稿的供应商源邮件标识。
            mode: 新邮件、回复或全部回复领域枚举。
            retain_until: 正文 AEAD 三元组的保留截止时间；会规范为 UTC。
            to_recipients: 主送地址元数据。
            cc_recipients: 抄送地址元数据。
            bcc_recipients: 密送地址元数据。
            subject: 当前版本纯文本主题；不作为正文写入密文。
            body_text: 必须只进入版本行 AEAD 三元组的纯文本正文。
            prompt_version: 可选模型 Prompt 版本。
            model_name: 可选模型名称。

        Returns:
            新建或同哈希重放命中的当前草稿稳定快照。

        Raises:
            StateConflictError: 创建键已绑定到不同请求哈希，或既有正文已清除。
            ValueError: 时间无时区、模式类型错误或加密 JSON 非法。
        """
        # 认证与连接读取已经结束；必须在当前事务先同步用户删除屏障，再插入父/版本。
        if not await lock_active_user(self._session, user_id=user_id):
            raise StateConflictError(
                error_code="mail_draft_not_editable",
                message="mail draft is not editable",
            )
        if type(mode) is not MailMode:
            raise TypeError("mail draft mode must be MailMode")
        normalized_retain_until = _as_utc(retain_until)
        encrypted_body = self._encrypt_body(
            body_text=body_text,
            user_id=user_id,
            version_id=version_id,
        )
        version = MailDraftVersionModel(
            id=version_id,
            user_id=user_id,
            draft_id=draft_id,
            version=1,
            to_recipients=_recipients_to_json(to_recipients),
            cc_recipients=_recipients_to_json(cc_recipients),
            bcc_recipients=_recipients_to_json(bcc_recipients),
            subject=subject,
            body_ciphertext=encrypted_body.ciphertext,
            body_nonce=encrypted_body.nonce,
            body_key_version=encrypted_body.key_version,
            prompt_version=prompt_version,
            model_name=model_name,
        )
        inserted_id = await self._session.scalar(
            insert(MailDraftModel)
            .values(
                id=draft_id,
                user_id=user_id,
                connection_id=connection_id,
                creation_idempotency_key=creation_idempotency_key,
                creation_payload_hash=creation_payload_hash,
                source_thread_id=source_thread_id,
                source_message_id=source_message_id,
                mode=mode.value,
                current_version=1,
                status=MailDraftStatus.EDITING.value,
                retain_until=normalized_retain_until,
            )
            .on_conflict_do_nothing(constraint="uq_mail_drafts_user_creation_idempotency_key")
            .returning(MailDraftModel.id)
        )
        if inserted_id is None:
            existing = await self.get_existing_creation(
                user_id=user_id,
                creation_idempotency_key=creation_idempotency_key,
                creation_payload_hash=creation_payload_hash,
            )
            if existing is None:
                raise RuntimeError("mail draft idempotency winner is not visible")
            return existing

        self._session.add(version)
        await self._session.flush()
        created = await self._session.scalar(
            select(MailDraftModel).where(
                MailDraftModel.id == draft_id,
                MailDraftModel.user_id == user_id,
            )
        )
        if created is None:
            raise RuntimeError("inserted mail draft is not visible")
        return await self._snapshot(created)

    async def get_current(
        self,
        *,
        user_id: UUID,
        draft_id: UUID,
    ) -> MailDraftSnapshot | None:
        """按用户读取并解密精确当前版本，跨用户草稿表现为不存在。

        Args:
            user_id: 当前认证用户。
            draft_id: 待读取草稿 ID。

        Returns:
            当前草稿快照；不存在或不属于用户时返回 ``None``。

        Raises:
            StateConflictError: 当前版本正文已按保留策略清除或持久数据不完整。
            cryptography.exceptions.InvalidTag: 密文或任一 AAD 维度被替换。
        """
        draft = await self._session.scalar(
            select(MailDraftModel).where(
                MailDraftModel.id == draft_id,
                MailDraftModel.user_id == user_id,
            )
        )
        return None if draft is None else await self._snapshot(draft)

    async def list_current(
        self,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[MailDraftSnapshot, ...]:
        """按最近编辑时间倒序列出当前用户的有界草稿快照。

        Args:
            user_id: 当前认证用户，查询不得省略。
            limit: 本次最多返回一百条。
            offset: 非负分页偏移。

        Returns:
            已解密的当前版本 tuple；跨用户行不会进入候选集合。

        Raises:
            ValueError: 分页参数越界。
            StateConflictError: 任一候选当前正文已按保留策略清除。
        """
        if not 1 <= limit <= 100:
            raise ValueError("mail draft list limit must be between 1 and 100")
        if offset < 0:
            raise ValueError("mail draft list offset must be non-negative")
        rows = (
            await self._session.execute(
                select(MailDraftModel, MailDraftVersionModel)
                .join(
                    MailDraftVersionModel,
                    (MailDraftVersionModel.user_id == MailDraftModel.user_id)
                    & (MailDraftVersionModel.draft_id == MailDraftModel.id)
                    & (MailDraftVersionModel.version == MailDraftModel.current_version),
                )
                .where(MailDraftModel.user_id == user_id)
                .order_by(MailDraftModel.updated_at.desc(), MailDraftModel.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return tuple(
            self._snapshot_from_version(draft=draft, version=version) for draft, version in rows
        )

    async def get_default_mail_connection(
        self, *, user_id: UUID
    ) -> MailDraftConnectionSnapshot | None:
        """读取用户显式配置的默认邮件连接，不对其他连接做隐式回退。"""
        row = await self._session.scalar(
            select(OAuthConnectionModel)
            .join(
                UserModel,
                (UserModel.default_mail_connection_id == OAuthConnectionModel.id)
                & (UserModel.id == OAuthConnectionModel.user_id),
            )
            .where(UserModel.id == user_id, OAuthConnectionModel.user_id == user_id)
        )
        return None if row is None else _connection_snapshot(row)

    async def get_connection(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> MailDraftConnectionSnapshot | None:
        """按用户与主键读取显式发送连接，跨用户连接表现为不存在。"""
        row = await self._session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
        )
        return None if row is None else _connection_snapshot(row)

    async def list_connections(self, *, user_id: UUID) -> tuple[MailDraftConnectionSnapshot, ...]:
        """列出用户全部连接主账户地址，供 reply-all 与自动补全排除自身。"""
        rows = await self._session.scalars(
            select(OAuthConnectionModel)
            .where(OAuthConnectionModel.user_id == user_id)
            .order_by(OAuthConnectionModel.id)
        )
        return tuple(_connection_snapshot(row) for row in rows)

    async def get_enabled_capabilities(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> frozenset[ConnectionCapability] | None:
        """兼容返回精确连接的 enabled 能力集合，不丢失新投影实现的校验。"""
        states = await self.get_capability_states(
            user_id=user_id,
            connection_id=connection_id,
        )
        if states is None:
            return None
        return frozenset(
            state.capability for state in states if state.status is CapabilityStatus.ENABLED
        )

    async def get_capability_states(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> tuple[MailDraftCapabilitySnapshot, ...] | None:
        """返回发送校验所需的最小类型化能力状态，不读取 token 或完整 scope。

        Returns:
            连接属于当前用户且 connected 时返回稳定排序的能力投影；否则返回 ``None``。

        Raises:
            RuntimeError: 数据库含未知能力或状态，必须 fail closed 而不能猜测授权语义。
        """
        connection = await self._session.scalar(
            select(OAuthConnectionModel.id).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
            )
        )
        if connection is None:
            return None
        rows = tuple(
            (
                await self._session.execute(
                    select(
                        ConnectionCapabilityModel.capability,
                        ConnectionCapabilityModel.status,
                        ConnectionCapabilityModel.last_error_code,
                    )
                    .where(
                        ConnectionCapabilityModel.user_id == user_id,
                        ConnectionCapabilityModel.connection_id == connection_id,
                    )
                    .order_by(ConnectionCapabilityModel.capability)
                )
            ).all()
        )
        try:
            return tuple(
                MailDraftCapabilitySnapshot(
                    capability=ConnectionCapability(capability),
                    status=CapabilityStatus(status),
                    last_error_code=last_error_code,
                )
                for capability, status, last_error_code in rows
            )
        except ValueError as error:
            raise RuntimeError("connection contains an unknown capability state") from error

    async def get_mail_draft_retention_days(self, *, user_id: UUID) -> int | None:
        """读取用户邮件正文保留天数，缺失用户返回空且不使用宿主机配置。"""
        return await self._session.scalar(
            select(UserModel.email_body_retention_days).where(UserModel.id == user_id)
        )

    async def save_next_version(
        self,
        *,
        version_id: UUID,
        user_id: UUID,
        draft_id: UUID,
        expected_version: int,
        to_recipients: tuple[MailDraftRecipient, ...],
        cc_recipients: tuple[MailDraftRecipient, ...],
        bcc_recipients: tuple[MailDraftRecipient, ...],
        subject: str,
        body_text: str,
        prompt_version: str | None,
        model_name: str | None,
        retain_until: datetime,
        connection_id: UUID | None = None,
    ) -> MailDraftSnapshot | None:
        """以父行单语句 CAS 认领下一版本，再写入一条不可变加密内容事实。

        Args:
            version_id: 新版本行 ID，也是正文 AAD 的记录维度。
            user_id: 当前认证用户。
            draft_id: 待编辑草稿 ID。
            expected_version: 客户端读到的当前正整数版本。
            to_recipients: 新版本主送地址元数据。
            cc_recipients: 新版本抄送地址元数据。
            bcc_recipients: 新版本密送地址元数据。
            subject: 新版本主题。
            body_text: 新版本纯文本正文。
            prompt_version: 可选 Prompt 版本。
            model_name: 可选模型名称。
            retain_until: 从本次成功编辑重新计算的正文保留截止时间。
            connection_id: 新邮件的显式账户选择；同一 CAS 同时更新账户和版本。

        Returns:
            保存成功后的当前快照；草稿不存在或跨用户时返回 ``None``。

        Raises:
            StateConflictError: expected version 陈旧，或草稿不在可编辑状态。
        """
        # 新邮件重绑还会写历史步骤摘要，因此用户锁必须早于父行锁和所有CAS分支。
        if not await lock_active_user(self._session, user_id=user_id):
            return None
        if type(expected_version) is not int or expected_version <= 0:
            raise ValueError("expected_version must be a positive integer")
        original_connection_id = None
        if connection_id is not None:
            # 先锁父行，确保所有历史验证与下一条不可变版本属于同一次 CAS 事务。
            current = await self._session.scalar(
                select(MailDraftModel)
                .where(MailDraftModel.id == draft_id, MailDraftModel.user_id == user_id)
                .with_for_update()
            )
            if current is None:
                return None
            if current.current_version != expected_version:
                raise _draft_version_conflict()
            if (
                current.mode != MailMode.NEW.value
                or current.status != MailDraftStatus.EDITING.value
            ):
                raise StateConflictError(
                    error_code="mail_draft_not_editable",
                    message="mail draft binding is not editable",
                )
            original_connection_id = current.connection_id
            if original_connection_id != connection_id:
                await preserve_historical_action_bindings(
                    self._session,
                    self._cipher,
                    user_id=user_id,
                    proposal_kind="mail_draft",
                    proposal_id=draft_id,
                    original_connection_id=original_connection_id,
                    original_version=expected_version,
                )
        next_version = expected_version + 1
        normalized_retain_until = _as_utc(retain_until)
        encrypted_body = self._encrypt_body(
            body_text=body_text,
            user_id=user_id,
            version_id=version_id,
        )
        version = MailDraftVersionModel(
            id=version_id,
            user_id=user_id,
            draft_id=draft_id,
            version=next_version,
            to_recipients=_recipients_to_json(to_recipients),
            cc_recipients=_recipients_to_json(cc_recipients),
            bcc_recipients=_recipients_to_json(bcc_recipients),
            subject=subject,
            body_ciphertext=encrypted_body.ciphertext,
            body_nonce=encrypted_body.nonce,
            body_key_version=encrypted_body.key_version,
            prompt_version=prompt_version,
            model_name=model_name,
        )
        updated_id = await self._session.scalar(
            update(MailDraftModel)
            .where(
                MailDraftModel.id == draft_id,
                MailDraftModel.user_id == user_id,
                MailDraftModel.current_version == expected_version,
                MailDraftModel.status == MailDraftStatus.EDITING.value,
            )
            .values(
                current_version=next_version,
                retain_until=normalized_retain_until,
                **({"connection_id": connection_id} if connection_id is not None else {}),
            )
            .returning(MailDraftModel.id)
        )
        if updated_id is None:
            existing = await self._session.scalar(
                select(MailDraftModel).where(
                    MailDraftModel.id == draft_id,
                    MailDraftModel.user_id == user_id,
                )
            )
            if existing is None:
                return None
            if existing.current_version != expected_version:
                raise _draft_version_conflict()
            raise StateConflictError(
                error_code="mail_draft_not_editable",
                message="mail draft is not editable",
            )

        self._session.add(version)
        await self._session.flush()
        draft = await self._session.scalar(
            select(MailDraftModel).where(
                MailDraftModel.id == draft_id,
                MailDraftModel.user_id == user_id,
            )
        )
        if draft is None:
            raise RuntimeError("updated mail draft is not visible")
        return await self._snapshot(draft)

    async def lock_for_submit(
        self,
        *,
        user_id: UUID,
        draft_id: UUID,
    ) -> MailDraftSnapshot | None:
        """按用户锁定草稿头并返回锁内观察到的精确当前版本。

        Repository 不改变状态；后续提交用例可在同一外层事务中验证能力、冻结命令并
        更新状态。正文已清除时会 fail closed，不能提交伪造空正文。

        Args:
            user_id: 当前认证用户。
            draft_id: 待提交草稿 ID。

        Returns:
            行锁保护下的当前版本快照；跨用户或不存在时返回 ``None``。
        """
        draft = await self._session.scalar(
            select(MailDraftModel)
            .where(
                MailDraftModel.id == draft_id,
                MailDraftModel.user_id == user_id,
            )
            .with_for_update()
        )
        return None if draft is None else await self._snapshot(draft)

    async def cancel(
        self,
        *,
        user_id: UUID,
        draft_id: UUID,
    ) -> MailDraftStateSnapshot | None:
        """锁定并按领域状态机取消当前用户尚未执行的本地草稿。

        Args:
            user_id: 当前认证用户。
            draft_id: 待取消草稿 ID。

        Returns:
            取消后的无正文状态快照；跨用户或不存在时返回 ``None``。

        Raises:
            StateConflictError: 锁内状态不是精确 ``editing``；待审批与未知结果分别要求
                走可信任务撤回或人工结果确认，执行中和终态由状态机拒绝。
        """
        # 沿用不存在时的None语义，取消也不能在inactive屏障后改写本地状态。
        if not await lock_active_user(self._session, user_id=user_id):
            return None
        draft = await self._session.scalar(
            select(MailDraftModel)
            .where(
                MailDraftModel.id == draft_id,
                MailDraftModel.user_id == user_id,
            )
            .with_for_update()
        )
        if draft is None:
            return None
        current = MailDraftStatus(draft.status)
        if current is MailDraftStatus.AWAITING_APPROVAL:
            # DELETE 只取消纯本地 editing 草稿；审批失效必须由 Task 18 的任务取消事务完成。
            raise StateConflictError(
                error_code="mail_draft_approval_withdrawal_required",
                message="cancel the trusted task before editing this mail draft",
            )
        if current is MailDraftStatus.NEEDS_ATTENTION:
            raise StateConflictError(
                error_code="mail_draft_result_confirmation_required",
                message="confirm the prior execution did not occur before editing",
            )
        target = transition_mail_draft(
            current,
            MailDraftStatus.CANCELLED,
        )
        draft.status = target.value
        await self._session.flush()
        return MailDraftStateSnapshot(
            draft_id=draft.id,
            current_version=draft.current_version,
            status=target,
        )

    def _encrypt_body(
        self,
        *,
        body_text: str,
        user_id: UUID,
        version_id: UUID,
    ) -> EncryptedValue:
        """使用版本行自己的 ID 和固定内部协议加密唯一正文对象。"""
        return self._cipher.encrypt_json(
            {"body_text": body_text},
            user_id=user_id,
            record_id=version_id,
            content_kind=MAIL_DRAFT_CONTENT_KIND,
            action=MAIL_DRAFT_ACTION,
            schema_version=MAIL_DRAFT_SCHEMA_VERSION,
        )

    async def _snapshot(self, draft: MailDraftModel) -> MailDraftSnapshot:
        """读取父行指向的不可变版本并在完整 AEAD 三元组存在时解密。"""
        version = await self._session.scalar(
            select(MailDraftVersionModel).where(
                MailDraftVersionModel.user_id == draft.user_id,
                MailDraftVersionModel.draft_id == draft.id,
                MailDraftVersionModel.version == draft.current_version,
            )
        )
        if version is None:
            raise _mail_content_unavailable()
        return self._snapshot_from_version(draft=draft, version=version)

    def _snapshot_from_version(
        self,
        *,
        draft: MailDraftModel,
        version: MailDraftVersionModel,
    ) -> MailDraftSnapshot:
        """校验并解密已经与父草稿当前版本精确连接的 ORM 行。

        ``list_current`` 在一个 SQL 中批量取得父行和当前版本，精确读取则可先查询父行再复用
        本函数。两条路径共享同一 AEAD、JSONB 白名单和状态映射，避免为消除 N+1 引入第二套
        内容解释规则。
        """
        if (
            version.body_ciphertext is None
            or version.body_nonce is None
            or version.body_key_version is None
        ):
            # 保留清理后的空三元组是合法数据库骨架，但绝不能被解释成空正文。
            raise _mail_content_unavailable()
        payload = self._cipher.decrypt_json(
            EncryptedValue(
                version.body_ciphertext,
                version.body_nonce,
                version.body_key_version,
            ),
            user_id=draft.user_id,
            record_id=version.id,
            content_kind=MAIL_DRAFT_CONTENT_KIND,
            action=MAIL_DRAFT_ACTION,
            schema_version=MAIL_DRAFT_SCHEMA_VERSION,
        )
        if set(payload) != {"body_text"} or type(payload["body_text"]) is not str:
            raise _mail_content_unavailable()
        return MailDraftSnapshot(
            draft_id=draft.id,
            connection_id=draft.connection_id,
            source_thread_id=draft.source_thread_id,
            source_message_id=draft.source_message_id,
            mode=MailMode(draft.mode),
            current_version=draft.current_version,
            status=MailDraftStatus(draft.status),
            retain_until=draft.retain_until,
            version_id=version.id,
            version=version.version,
            to_recipients=_recipients_from_json(version.to_recipients),
            cc_recipients=_recipients_from_json(version.cc_recipients),
            bcc_recipients=_recipients_from_json(version.bcc_recipients),
            subject=version.subject,
            body_text=payload["body_text"],
            prompt_version=version.prompt_version,
            model_name=version.model_name,
            created_at=version.created_at,
        )


def _as_utc(value: datetime) -> datetime:
    """要求带时区时间并转换为 UTC，禁止依赖宿主机本地时区。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retain_until must be timezone-aware")
    return value.astimezone(UTC)


def _connection_snapshot(row: OAuthConnectionModel) -> MailDraftConnectionSnapshot:
    """复制 ORM 连接为不会在事务外触发隐式 I/O 的最小快照。"""
    return MailDraftConnectionSnapshot(
        id=row.id,
        user_id=row.user_id,
        provider=row.provider,
        account_email=row.account_email,
        status=row.status,
    )


def _recipients_to_json(
    recipients: tuple[MailDraftRecipient, ...],
) -> list[dict[str, str]]:
    """把不可变地址元数据复制为 ORM JSONB 白名单结构。"""
    result: list[dict[str, str]] = []
    for recipient in recipients:
        if type(recipient) is not MailDraftRecipient:
            raise TypeError("mail recipients must contain MailDraftRecipient values")
        value = {"address": recipient.address}
        if recipient.display_name is not None:
            value["display_name"] = recipient.display_name
        result.append(value)
    return result


def _recipients_from_json(
    values: list[dict[str, str]],
) -> tuple[MailDraftRecipient, ...]:
    """验证数据库 JSONB 地址白名单，阻止异常结构逃逸为应用快照。"""
    recipients: list[MailDraftRecipient] = []
    for value in values:
        if not isinstance(value, dict) or not set(value).issubset({"address", "display_name"}):
            raise _mail_content_unavailable()
        address = value.get("address")
        display_name = value.get("display_name")
        if type(address) is not str or (display_name is not None and type(display_name) is not str):
            raise _mail_content_unavailable()
        recipients.append(MailDraftRecipient(address=address, display_name=display_name))
    return tuple(recipients)


def _idempotency_payload_mismatch() -> StateConflictError:
    """构造不回显创建键或敏感载荷的稳定幂等冲突。"""
    return StateConflictError(
        error_code="idempotency_key_payload_mismatch",
        message="idempotency key is already bound to different content",
    )


def _draft_version_conflict() -> StateConflictError:
    """构造 API 可稳定映射的草稿版本冲突。"""
    return StateConflictError(
        error_code="draft_version_conflict",
        message="mail draft version changed",
    )


def _mail_content_unavailable() -> StateConflictError:
    """构造正文过期、缺列或结构损坏时的固定 fail-closed 错误。"""
    return StateConflictError(
        error_code="mail_draft_content_unavailable",
        message="mail draft content is unavailable",
    )


class SqlAlchemyMailDraftRepositoryFactory:
    """为每次草稿用例提供自动提交或回滚的短事务 Repository。"""

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        cipher: ActionPayloadCipher,
    ) -> None:
        """保存进程级会话工厂与记录绑定加密器，不提前占用数据库连接。"""
        self._session_factory = session_factory
        self._cipher = cipher

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlAlchemyMailDraftRepository]:
        """把一次草稿读写限制为同一提交/回滚边界。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyMailDraftRepository(session, self._cipher)


__all__ = [
    "MAIL_DRAFT_ACTION",
    "MAIL_DRAFT_CONTENT_KIND",
    "MAIL_DRAFT_SCHEMA_VERSION",
    "SqlAlchemyMailDraftRepository",
    "SqlAlchemyMailDraftRepositoryFactory",
]
