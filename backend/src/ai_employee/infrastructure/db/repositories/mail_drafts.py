"""以 PostgreSQL CAS 和记录绑定 AEAD 持久化本地邮件草稿。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.domain.actions import MailDraftStatus, transition_mail_draft
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode
from ai_employee.infrastructure.db.models.actions import (
    MailDraftModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher

MAIL_DRAFT_CONTENT_KIND = "mail_draft_body"
MAIL_DRAFT_ACTION = "mail.draft"
MAIL_DRAFT_SCHEMA_VERSION = "mail_draft_body.v1"


@dataclass(frozen=True, slots=True)
class MailRecipient:
    """表示草稿版本中的单个地址元数据，不承载邮件正文。

    Attributes:
        address: 调用方已完成语法处理的邮箱地址。
        display_name: 可选显示名；缺失时不会在 JSONB 中写入伪造空字符串。
    """

    address: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class MailDraftStateSnapshot:
    """返回不含正文的草稿头状态，供取消等状态操作使用。"""

    draft_id: UUID
    current_version: int
    status: MailDraftStatus


@dataclass(frozen=True, slots=True)
class MailDraftSnapshot:
    """把 ORM 草稿头和解密后的精确当前版本映射为稳定基础设施快照。"""

    draft_id: UUID
    connection_id: UUID
    source_thread_id: str | None
    source_message_id: str | None
    mode: MailMode
    current_version: int
    status: MailDraftStatus
    retain_until: datetime
    version_id: UUID
    version: int
    to_recipients: tuple[MailRecipient, ...]
    cc_recipients: tuple[MailRecipient, ...]
    bcc_recipients: tuple[MailRecipient, ...]
    subject: str
    body_text: str
    prompt_version: str | None
    model_name: str | None
    created_at: datetime


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
        to_recipients: tuple[MailRecipient, ...],
        cc_recipients: tuple[MailRecipient, ...],
        bcc_recipients: tuple[MailRecipient, ...],
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
            existing = await self._session.scalar(
                select(MailDraftModel).where(
                    MailDraftModel.user_id == user_id,
                    MailDraftModel.creation_idempotency_key == creation_idempotency_key,
                )
            )
            if existing is None:
                raise RuntimeError("mail draft idempotency winner is not visible")
            if not compare_digest(existing.creation_payload_hash, creation_payload_hash):
                raise _idempotency_payload_mismatch()
            return await self._snapshot(existing)

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

    async def save_next_version(
        self,
        *,
        version_id: UUID,
        user_id: UUID,
        draft_id: UUID,
        expected_version: int,
        to_recipients: tuple[MailRecipient, ...],
        cc_recipients: tuple[MailRecipient, ...],
        bcc_recipients: tuple[MailRecipient, ...],
        subject: str,
        body_text: str,
        prompt_version: str | None,
        model_name: str | None,
        retain_until: datetime,
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

        Returns:
            保存成功后的当前快照；草稿不存在或跨用户时返回 ``None``。

        Raises:
            StateConflictError: expected version 陈旧，或草稿不在可编辑状态。
        """
        if type(expected_version) is not int or expected_version <= 0:
            raise ValueError("expected_version must be a positive integer")
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
            StateConflictError: 当前状态为执行中或任一终态，状态机拒绝取消。
        """
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
        target = transition_mail_draft(
            MailDraftStatus(draft.status),
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


def _recipients_to_json(recipients: tuple[MailRecipient, ...]) -> list[dict[str, str]]:
    """把不可变地址元数据复制为 ORM JSONB 白名单结构。"""
    result: list[dict[str, str]] = []
    for recipient in recipients:
        if type(recipient) is not MailRecipient:
            raise TypeError("mail recipients must contain MailRecipient values")
        value = {"address": recipient.address}
        if recipient.display_name is not None:
            value["display_name"] = recipient.display_name
        result.append(value)
    return result


def _recipients_from_json(values: list[dict[str, str]]) -> tuple[MailRecipient, ...]:
    """验证数据库 JSONB 地址白名单，阻止异常结构逃逸为应用快照。"""
    recipients: list[MailRecipient] = []
    for value in values:
        if not isinstance(value, dict) or not set(value).issubset({"address", "display_name"}):
            raise _mail_content_unavailable()
        address = value.get("address")
        display_name = value.get("display_name")
        if type(address) is not str or (display_name is not None and type(display_name) is not str):
            raise _mail_content_unavailable()
        recipients.append(MailRecipient(address=address, display_name=display_name))
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


__all__ = [
    "MAIL_DRAFT_ACTION",
    "MAIL_DRAFT_CONTENT_KIND",
    "MAIL_DRAFT_SCHEMA_VERSION",
    "MailDraftSnapshot",
    "MailDraftStateSnapshot",
    "MailRecipient",
    "SqlAlchemyMailDraftRepository",
]
