"""定义本地邮件草稿的类型化创建、读取、编辑与取消用例。

应用层只处理连接能力、源线程绑定、地址规范化、幂等哈希和状态边界。正文加密、
PostgreSQL CAS 与 ORM 映射由基础设施适配器负责；本模块不会创建审批、工具执行或
供应商草稿，也不会调用任何外部邮件接口。
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_employee.domain.actions import MailDraftStatus
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import (
    MailMode,
    normalize_mail_recipients,
    normalize_mailbox_address,
)

MAX_MAIL_RECIPIENTS = 50
MAX_RECIPIENT_SUGGESTIONS = 20
DEFAULT_MAIL_BODY_RETENTION_DAYS = 30


@dataclass(frozen=True, slots=True)
class MailDraftRecipient:
    """表示应用层可持久化的规范邮箱地址元数据。

    Attributes:
        address: 已通过 :func:`normalize_mailbox_address` 的 addr-spec。
        display_name: 可选本地历史显示名；当前用例不会由模型生成该字段。
    """

    address: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class MailDraftSourceMessage:
    """表示用户范围内一封可用于回复的本地同步消息。

    ``thread_id`` 与 ``message_id`` 是调用方可验证的稳定来源引用；Repository 必须在
    返回前证明它们和 ``connection_id`` 属于同一用户、同一线程且仍可读取。正文仅供
    模型上下文使用，创建回复草稿不会把它复制进新草稿。
    """

    connection_id: UUID
    thread_id: str
    message_id: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    received_at: datetime
    body_text: str = ""
    labels: tuple[str, ...] = ()
    thread_summary: str = ""


@dataclass(frozen=True, slots=True)
class MailRecipientHistoryEntry:
    """表示本地同步历史中一个地址的最近出现事实。"""

    address: str
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class MailDraftView:
    """应用/API/Worker 共用的当前草稿不可变视图，不暴露 ORM 对象。"""

    draft_id: UUID
    connection_id: UUID
    mode: MailMode
    source_thread_id: str | None
    source_message_id: str | None
    current_version: int
    status: MailDraftStatus
    to_recipients: tuple[str, ...]
    cc_recipients: tuple[str, ...]
    bcc_recipients: tuple[str, ...]
    subject: str
    body_text: str
    prompt_version: str | None = None
    model_name: str | None = None
    retain_until: datetime | None = None
    created_at: datetime | None = None
    recipient_suggestions: tuple[str, ...] = ()

    @property
    def id(self) -> UUID:
        """返回 REST 资源常用的 ``id`` 兼容属性。"""
        return self.draft_id

    @property
    def version(self) -> int:
        """返回当前不可变版本号。"""
        return self.current_version

    @property
    def to(self) -> tuple[str, ...]:
        """返回主送地址的短字段名兼容视图。"""
        return self.to_recipients

    @property
    def cc(self) -> tuple[str, ...]:
        """返回抄送地址的短字段名兼容视图。"""
        return self.cc_recipients

    @property
    def bcc(self) -> tuple[str, ...]:
        """返回密送地址的短字段名兼容视图。"""
        return self.bcc_recipients


class CreateMailDraftInput(BaseModel):
    """创建一封空白新邮件或绑定本地来源的回复草稿。"""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    user_id: UUID
    mode: MailMode
    idempotency_key: str = Field(min_length=1, max_length=255)
    connection_id: UUID | None = None
    source_thread_id: str | None = Field(default=None, max_length=255)
    source_message_id: str | None = Field(default=None, max_length=255)
    to_recipients: tuple[str, ...] = Field(
        default=(), validation_alias=AliasChoices("to_recipients", "to")
    )
    cc_recipients: tuple[str, ...] = Field(
        default=(), validation_alias=AliasChoices("cc_recipients", "cc")
    )
    bcc_recipients: tuple[str, ...] = Field(
        default=(), validation_alias=AliasChoices("bcc_recipients", "bcc")
    )
    subject: str = Field(default="", max_length=255)
    body_text: str = Field(default="", max_length=100_000)

    @field_validator("idempotency_key", "source_thread_id", "source_message_id")
    @classmethod
    def validate_unpadded_text(cls, value: str | None) -> str | None:
        """拒绝空白包裹和 CR/LF，避免 opaque 键与 Header 绑定被静默改写。"""
        if value is None:
            return None
        if value != value.strip() or not value or "\r" in value or "\n" in value:
            raise ValueError("identifier text must be nonempty, unpadded, and single-line")
        return value

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        """主题允许为空，但拒绝 Header 注入换行。"""
        if "\r" in value or "\n" in value:
            raise ValueError("mail subject must not contain CR or LF")
        return value

    @model_validator(mode="after")
    def validate_source_shape(self) -> "CreateMailDraftInput":
        """拒绝新邮件携带回复绑定，以及回复只携带半套显式来源。"""
        if self.mode is MailMode.NEW:
            if self.source_thread_id is not None or self.source_message_id is not None:
                raise ValueError("new mail must not contain source binding")
            return self
        if self.source_thread_id is None and self.source_message_id is None:
            raise ValueError("reply mail requires a source thread or message")
        return self


class UpdateMailDraftInput(BaseModel):
    """以当前版本号 PATCH 一封草稿；未出现字段保持原值。"""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    user_id: UUID
    draft_id: UUID
    expected_version: int = Field(ge=1, validation_alias=AliasChoices("expected_version", "version"))
    to_recipients: tuple[str, ...] | None = Field(
        default=None, validation_alias=AliasChoices("to_recipients", "to")
    )
    cc_recipients: tuple[str, ...] | None = Field(
        default=None, validation_alias=AliasChoices("cc_recipients", "cc")
    )
    bcc_recipients: tuple[str, ...] | None = Field(
        default=None, validation_alias=AliasChoices("bcc_recipients", "bcc")
    )
    subject: str | None = Field(default=None, max_length=255)
    body_text: str | None = Field(default=None, max_length=100_000)

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str | None) -> str | None:
        """主题出现时拒绝 CR/LF；回复模式是否允许由用例根据当前草稿判断。"""
        if value is not None and ("\r" in value or "\n" in value):
            raise ValueError("mail subject must not contain CR or LF")
        return value


class MailDraftNotFoundError(Exception):
    """表示目标草稿不存在或不属于当前用户，避免跨用户资源探测。"""


class MailDraftRepository(Protocol):
    """定义草稿用例所需的用户隔离、AEAD 与 CAS 持久化端口。"""

    async def list_current(
        self, *, user_id: UUID, limit: int, offset: int
    ) -> tuple[object, ...]:
        """列出当前用户的解密草稿快照。"""
        ...

    async def get_current(self, *, user_id: UUID, draft_id: UUID) -> object | None:
        """读取当前用户的一封精确当前版本。"""
        ...

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
    ) -> object:
        """幂等创建版本一；具体适配器必须哈希绑定创建键。"""
        ...

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
    ) -> object | None:
        """以 ``expected_version`` CAS 保存下一不可变版本。"""
        ...

    async def cancel(self, *, user_id: UUID, draft_id: UUID) -> object | None:
        """取消尚未发送且状态机允许取消的本地草稿。"""
        ...


@dataclass(frozen=True, slots=True)
class MailDraftCapabilitySnapshot:
    """表示草稿发送校验所需的最小连接能力投影。

    该类型只携带稳定能力、状态和脱敏错误码，不暴露实际 scope、token 或供应商响应。
    """

    capability: ConnectionCapability
    status: CapabilityStatus
    last_error_code: str | None


class MailDraftConnectionReader(Protocol):
    """读取用户默认连接、显式连接、能力与保留设置的应用端口。"""

    async def get_default_mail_connection(self, *, user_id: UUID) -> object | None:
        """返回用户显式配置的默认发送连接；缺失时不得猜测回退。"""
        ...

    async def get_connection(self, *, user_id: UUID, connection_id: UUID) -> object | None:
        """按当前用户读取显式连接。"""
        ...

    async def list_connections(self, *, user_id: UUID) -> tuple[object, ...]:
        """列出用户全部连接，供排除所有主账户地址。"""
        ...

    async def get_capability_states(
        self, *, user_id: UUID, connection_id: UUID
    ) -> tuple[MailDraftCapabilitySnapshot, ...] | None:
        """返回精确连接的最小能力状态；不存在或断开时返回 ``None``。"""
        ...

    async def get_mail_draft_retention_days(self, *, user_id: UUID) -> int | None:
        """返回用户配置的邮件正文保留天数。"""
        ...


class MailDraftSourceReader(Protocol):
    """读取本地同步来源消息与历史参与者，绝不调用 Contacts 或模型。"""

    async def get_draft_source_message(
        self,
        *,
        user_id: UUID,
        source_thread_id: str | None,
        source_message_id: str | None,
        source_connection_id: UUID | None,
    ) -> MailDraftSourceMessage | None:
        """返回已经验证用户、线程、消息和连接一致性的本地来源。"""
        ...

    async def list_recipient_history(
        self, *, user_id: UUID
    ) -> tuple[MailRecipientHistoryEntry, ...]:
        """返回本地同步邮件 sender/recipient 的最近出现事实。"""
        ...


class MailDraftUseCase:
    """协调本地草稿全部短事务行为，不触发审批、发送或供应商网络调用。"""

    def __init__(
        self,
        *,
        drafts: MailDraftRepository,
        connections: MailDraftConnectionReader,
        sources: MailDraftSourceReader | None = None,
        clock: Callable[[], datetime],
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        """注入草稿、连接、来源、时钟和 UUID 端口。

        Args:
            drafts: 负责 AEAD、用户隔离和不可变版本 CAS 的草稿 Repository。
            connections: 负责默认连接、能力与自有账户地址的本地读取端口。
            sources: 可选本地同步邮件读取端口；回复创建必须提供。
            clock: 返回带时区当前瞬间的显式时钟。
            id_factory: 为草稿和版本生成稳定 UUID 的可替换工厂。
        """
        self._drafts = drafts
        self._connections = connections
        self._sources = sources
        self._clock = clock
        self._id_factory = id_factory

    async def list(
        self, *, user_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[MailDraftView, ...]:
        """按用户列出当前草稿并附加本地派生的收件人建议。

        Raises:
            ValueError: limit 或 offset 超出有界分页范围。
        """
        if not 1 <= limit <= 100:
            raise ValueError("mail draft list limit must be between 1 and 100")
        if offset < 0:
            raise ValueError("mail draft list offset must be non-negative")
        suggestions = await self.recipient_suggestions(user_id=user_id)
        snapshots = await self._drafts.list_current(user_id=user_id, limit=limit, offset=offset)
        return tuple(
            replace(_snapshot_to_view(snapshot), recipient_suggestions=suggestions)
            for snapshot in snapshots
        )

    async def get(self, *, user_id: UUID, draft_id: UUID) -> MailDraftView:
        """读取一封当前草稿；不存在与跨用户统一抛出不可探测错误。"""
        snapshot = await self._drafts.get_current(user_id=user_id, draft_id=draft_id)
        if snapshot is None:
            raise MailDraftNotFoundError
        return replace(
            _snapshot_to_view(snapshot),
            recipient_suggestions=await self.recipient_suggestions(user_id=user_id),
        )

    async def create(self, request: CreateMailDraftInput) -> MailDraftView:
        """校验连接、来源和地址后幂等创建纯本地版本一。

        同一用户的 ``idempotency_key`` 会绑定规范化请求哈希。Repository 负责并发唯一
        约束，哈希不一致必须抛 ``idempotency_key_payload_mismatch``，本用例不会为重放
        创建第二行、审批、ToolExecution 或供应商草稿。
        """
        connection, source = await self._resolve_connection_and_source(request)
        own_addresses = await self._own_addresses(user_id=request.user_id)
        to, cc, bcc = await self._resolve_recipients(
            request=request,
            source=source,
            own_addresses=own_addresses,
        )
        subject = _resolved_subject(request=request, source=source)
        creation_payload_hash = _creation_payload_hash(
            connection_id=_uuid_attr(connection, "id"),
            mode=request.mode,
            source=source,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body_text=request.body_text,
        )
        now = _utc_now(self._clock)
        retention_days = await self._connections.get_mail_draft_retention_days(
            user_id=request.user_id
        )
        retain_until = now + timedelta(
            days=(
                retention_days
                if retention_days is not None
                else DEFAULT_MAIL_BODY_RETENTION_DAYS
            )
        )
        snapshot = await self._drafts.create(
            draft_id=self._id_factory(),
            version_id=self._id_factory(),
            user_id=request.user_id,
            connection_id=_uuid_attr(connection, "id"),
            creation_idempotency_key=request.idempotency_key,
            creation_payload_hash=creation_payload_hash,
            source_thread_id=source.thread_id if source is not None else None,
            source_message_id=source.message_id if source is not None else None,
            mode=request.mode,
            retain_until=retain_until,
            to_recipients=_recipient_values(to),
            cc_recipients=_recipient_values(cc),
            bcc_recipients=_recipient_values(bcc),
            subject=subject,
            body_text=request.body_text,
            prompt_version=None,
            model_name=None,
        )
        return replace(
            _snapshot_to_view(snapshot),
            recipient_suggestions=await self.recipient_suggestions(user_id=request.user_id),
        )

    async def create_new(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
        connection_id: UUID | None = None,
        to: tuple[str, ...] = (),
        cc: tuple[str, ...] = (),
        bcc: tuple[str, ...] = (),
        subject: str = "",
        body_text: str = "",
    ) -> MailDraftView:
        """以短字段名创建新邮件草稿，供对话和后续 REST 组合层复用。"""
        return await self.create(
            CreateMailDraftInput(
                user_id=user_id,
                mode=MailMode.NEW,
                idempotency_key=idempotency_key,
                connection_id=connection_id,
                to_recipients=to,
                cc_recipients=cc,
                bcc_recipients=bcc,
                subject=subject,
                body_text=body_text,
            )
        )

    async def create_reply(
        self,
        *,
        user_id: UUID,
        source_message_id: str,
        idempotency_key: str,
        source_thread_id: str | None = None,
        source_connection_id: UUID | None = None,
        to: tuple[str, ...] = (),
        cc: tuple[str, ...] = (),
        bcc: tuple[str, ...] = (),
        body_text: str = "",
    ) -> MailDraftView:
        """按本地来源消息创建固定账户与主题的回复草稿。"""
        return await self.create(
            CreateMailDraftInput(
                user_id=user_id,
                mode=MailMode.REPLY,
                idempotency_key=idempotency_key,
                connection_id=source_connection_id,
                source_thread_id=source_thread_id,
                source_message_id=source_message_id,
                to_recipients=to,
                cc_recipients=cc,
                bcc_recipients=bcc,
                body_text=body_text,
            )
        )

    async def create_reply_all(
        self,
        *,
        user_id: UUID,
        source_message_id: str,
        idempotency_key: str,
        source_thread_id: str | None = None,
        source_connection_id: UUID | None = None,
        to: tuple[str, ...] = (),
        cc: tuple[str, ...] = (),
        bcc: tuple[str, ...] = (),
        body_text: str = "",
    ) -> MailDraftView:
        """按本地来源消息创建排除全部自有账户地址的全部回复草稿。"""
        return await self.create(
            CreateMailDraftInput(
                user_id=user_id,
                mode=MailMode.REPLY_ALL,
                idempotency_key=idempotency_key,
                connection_id=source_connection_id,
                source_thread_id=source_thread_id,
                source_message_id=source_message_id,
                to_recipients=to,
                cc_recipients=cc,
                bcc_recipients=bcc,
                body_text=body_text,
            )
        )

    async def update(
        self,
        request: UpdateMailDraftInput,
        *,
        prompt_version: str | None = None,
        model_name: str | None = None,
    ) -> MailDraftView:
        """以当前版本 CAS 保存下一不可变版本。

        人工 PATCH 传入的 ``prompt_version``/``model_name`` 默认为空；模型 Worker 仅在
        body-only 输出成功后显式设置两者。回复类主题永远不能由 PATCH 改动。
        """
        current_raw = await self._drafts.get_current(
            user_id=request.user_id, draft_id=request.draft_id
        )
        if current_raw is None:
            raise MailDraftNotFoundError
        current = _snapshot_to_view(current_raw)
        _ensure_editable(current.status)
        if current.mode is not MailMode.NEW and "subject" in request.model_fields_set:
            raise _binding_immutable()

        to, cc, bcc = _normalize_and_limit_recipients(
            request.to_recipients
            if request.to_recipients is not None
            else current.to_recipients,
            request.cc_recipients
            if request.cc_recipients is not None
            else current.cc_recipients,
            request.bcc_recipients
            if request.bcc_recipients is not None
            else current.bcc_recipients,
        )
        retention_days = await self._connections.get_mail_draft_retention_days(
            user_id=request.user_id
        )
        retain_until = _utc_now(self._clock) + timedelta(
            days=(
                retention_days
                if retention_days is not None
                else DEFAULT_MAIL_BODY_RETENTION_DAYS
            )
        )
        saved = await self._drafts.save_next_version(
            version_id=self._id_factory(),
            user_id=request.user_id,
            draft_id=request.draft_id,
            expected_version=request.expected_version,
            to_recipients=_recipient_values(to),
            cc_recipients=_recipient_values(cc),
            bcc_recipients=_recipient_values(bcc),
            subject=request.subject if request.subject is not None else current.subject,
            body_text=request.body_text if request.body_text is not None else current.body_text,
            prompt_version=prompt_version,
            model_name=model_name,
            retain_until=retain_until,
        )
        if saved is None:
            raise MailDraftNotFoundError
        return replace(
            _snapshot_to_view(saved),
            recipient_suggestions=await self.recipient_suggestions(user_id=request.user_id),
        )

    async def cancel(self, *, user_id: UUID, draft_id: UUID) -> MailDraftView:
        """取消一个未发送本地草稿，不创建任何外部副作用。"""
        current_raw = await self._drafts.get_current(user_id=user_id, draft_id=draft_id)
        if current_raw is None:
            raise MailDraftNotFoundError
        current = _snapshot_to_view(current_raw)
        # 待审批草稿必须先经既有可信任务取消路径使审批失效；DELETE 不能成为旁路撤回。
        _ensure_editable(current.status)
        cancelled = await self._drafts.cancel(user_id=user_id, draft_id=draft_id)
        if cancelled is None:
            raise MailDraftNotFoundError
        status = _status_attr(cancelled)
        return replace(
            current,
            status=status,
            recipient_suggestions=await self.recipient_suggestions(user_id=user_id),
        )

    async def recipient_suggestions(self, *, user_id: UUID) -> tuple[str, ...]:
        """从本地邮件历史派生最多二十个唯一非自有地址。

        排序先按最近出现时间降序，再按规范地址升序。来源端口为空时返回空 tuple；
        本函数从不调用 Contacts API 或模型。
        """
        if self._sources is None:
            return ()
        own_addresses = await self._own_addresses(user_id=user_id)
        newest_by_address: dict[str, datetime] = {}
        for entry in await self._sources.list_recipient_history(user_id=user_id):
            try:
                address = normalize_mailbox_address(entry.address)
                seen_at = _utc_datetime(entry.last_seen_at, field="last_seen_at")
            except (TypeError, ValueError):
                # 历史 JSONB 若损坏不能污染自动补全；具体同步/修复任务仍负责报告源错误。
                continue
            if address in own_addresses:
                continue
            previous = newest_by_address.get(address)
            if previous is None or seen_at > previous:
                newest_by_address[address] = seen_at
        ordered = sorted(
            newest_by_address,
            key=lambda address: (-newest_by_address[address].timestamp(), address),
        )
        return tuple(ordered[:MAX_RECIPIENT_SUGGESTIONS])

    async def _resolve_connection_and_source(
        self, request: CreateMailDraftInput
    ) -> tuple[object, MailDraftSourceMessage | None]:
        """解析新邮件默认连接或回复来源，并验证 ``mail.send`` 能力。"""
        if request.mode is MailMode.NEW:
            connection = (
                await self._connections.get_connection(
                    user_id=request.user_id, connection_id=request.connection_id
                )
                if request.connection_id is not None
                else await self._connections.get_default_mail_connection(user_id=request.user_id)
            )
            await self._require_send_connection(user_id=request.user_id, connection=connection)
            return connection, None

        if self._sources is None:
            raise _thread_binding_conflict()
        source = await self._sources.get_draft_source_message(
            user_id=request.user_id,
            source_thread_id=request.source_thread_id,
            source_message_id=request.source_message_id,
            source_connection_id=request.connection_id,
        )
        if source is None:
            raise _thread_binding_conflict()
        if request.connection_id is not None and request.connection_id != source.connection_id:
            raise _thread_binding_conflict()
        connection = await self._connections.get_connection(
            user_id=request.user_id, connection_id=source.connection_id
        )
        await self._require_send_connection(user_id=request.user_id, connection=connection)
        return connection, source

    async def _require_send_connection(self, *, user_id: UUID, connection: object | None) -> None:
        """要求连接属于用户、保持 connected 且读写能力均为 enabled。

        ``action_required``、``revoked`` 或显式 ``connection_scope_missing`` 表示用户
        必须重新授权；其余本地不可用状态返回 capability disabled。两类错误不能由前端
        猜测，也不能通过寻找另一个非默认连接静默回退。
        """
        if connection is None or _str_attr(connection, "status") != "connected":
            raise _connection_capability_disabled()
        connection_id = _uuid_attr(connection, "id")
        capabilities = await self._connections.get_capability_states(
            user_id=user_id, connection_id=connection_id
        )
        if capabilities is None:
            raise _connection_capability_disabled()
        by_capability = {snapshot.capability: snapshot for snapshot in capabilities}
        required = (
            ConnectionCapability.MAIL_SEND,
            ConnectionCapability.MAIL_READ,
        )
        required_states = tuple(
            by_capability.get(capability) for capability in required
        )
        if any(snapshot is None for snapshot in required_states):
            raise _connection_capability_disabled()
        if any(
            snapshot is not None
            and (
                snapshot.last_error_code == "connection_scope_missing"
                or snapshot.status
                in {CapabilityStatus.ACTION_REQUIRED, CapabilityStatus.REVOKED}
            )
            for snapshot in required_states
        ):
            raise _connection_scope_missing()
        if any(
            snapshot is None or snapshot.status is not CapabilityStatus.ENABLED
            for snapshot in required_states
        ):
            raise _connection_capability_disabled()

    async def _resolve_recipients(
        self,
        *,
        request: CreateMailDraftInput,
        source: MailDraftSourceMessage | None,
        own_addresses: frozenset[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """生成回复默认地址，再应用用户显式可编辑的 To/CC/BCC。"""
        if source is None:
            return _normalize_and_limit_recipients(
                request.to_recipients, request.cc_recipients, request.bcc_recipients
            )

        sender = normalize_mailbox_address(source.sender)
        if request.mode is MailMode.REPLY:
            default_to = () if sender in own_addresses else (sender,)
            default_cc: tuple[str, ...] = ()
        else:
            default_to = () if sender in own_addresses else (sender,)
            default_cc = tuple(
                address
                for address in (
                    normalize_mailbox_address(value) for value in source.recipients
                )
                if address not in own_addresses and address not in default_to
            )
        to = request.to_recipients or default_to
        cc = request.cc_recipients or default_cc
        return _normalize_and_limit_recipients(to, cc, request.bcc_recipients)

    async def _own_addresses(self, *, user_id: UUID) -> frozenset[str]:
        """规范化用户全部连接主账户地址，供 reply-all 和建议排除。"""
        addresses: set[str] = set()
        for connection in await self._connections.list_connections(user_id=user_id):
            try:
                addresses.add(normalize_mailbox_address(_str_attr(connection, "account_email")))
            except (TypeError, ValueError):
                # selected connection 的状态/能力会另行 fail closed；其他历史坏行不能让
                # 自动补全或 reply-all 泄漏异常原值。
                continue
        return frozenset(addresses)


def _snapshot_to_view(snapshot: object) -> MailDraftView:
    """把结构化 Repository 快照或测试 Mapping 复制为应用视图。"""
    return MailDraftView(
        draft_id=_uuid_attr(snapshot, "draft_id"),
        connection_id=_uuid_attr(snapshot, "connection_id"),
        mode=_mail_mode_attr(snapshot),
        source_thread_id=_optional_str_attr(snapshot, "source_thread_id"),
        source_message_id=_optional_str_attr(snapshot, "source_message_id"),
        current_version=_int_attr(snapshot, "current_version"),
        status=_status_attr(snapshot),
        to_recipients=_addresses_attr(snapshot, "to_recipients"),
        cc_recipients=_addresses_attr(snapshot, "cc_recipients"),
        bcc_recipients=_addresses_attr(snapshot, "bcc_recipients"),
        subject=_str_attr(snapshot, "subject"),
        body_text=_str_attr(snapshot, "body_text"),
        prompt_version=_optional_str_attr(snapshot, "prompt_version"),
        model_name=_optional_str_attr(snapshot, "model_name"),
        retain_until=_optional_datetime_attr(snapshot, "retain_until"),
        created_at=_optional_datetime_attr(snapshot, "created_at"),
    )


def _value(snapshot: object, field: str, default: object = None) -> object:
    """在应用边界读取结构化属性或测试 Mapping，不让 ORM 类型进入签名。"""
    if isinstance(snapshot, Mapping):
        return snapshot.get(field, default)
    return getattr(snapshot, field, default)


def _uuid_attr(snapshot: object, field: str) -> UUID:
    """读取必须为 UUID 的稳定标识。"""
    value = _value(snapshot, field)
    if not isinstance(value, UUID):
        raise TypeError(f"{field} must be a UUID")
    return value


def _str_attr(snapshot: object, field: str) -> str:
    """读取必须为字符串的安全字段。"""
    value = _value(snapshot, field)
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _optional_str_attr(snapshot: object, field: str) -> str | None:
    """读取可选字符串字段。"""
    value = _value(snapshot, field)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None")
    return value


def _int_attr(snapshot: object, field: str) -> int:
    """读取正整数版本字段并拒绝 bool。"""
    value = _value(snapshot, field)
    if type(value) is not int or value <= 0:
        raise TypeError(f"{field} must be a positive integer")
    return value


def _optional_datetime_attr(snapshot: object, field: str) -> datetime | None:
    """读取可选带时区时间。"""
    value = _value(snapshot, field)
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field} must be timezone-aware or None")
    return value.astimezone(UTC)


def _mail_mode_attr(snapshot: object) -> MailMode:
    """读取精确邮件模式枚举或稳定持久字符串。"""
    value = _value(snapshot, "mode")
    return value if type(value) is MailMode else MailMode(str(value))


def _status_attr(snapshot: object) -> MailDraftStatus:
    """读取精确草稿状态枚举或稳定持久字符串。"""
    value = _value(snapshot, "status")
    return value if type(value) is MailDraftStatus else MailDraftStatus(str(value))


def _addresses_attr(snapshot: object, field: str) -> tuple[str, ...]:
    """复制 Repository 地址元数据为不含显示名的应用 tuple。"""
    raw = _value(snapshot, field, ())
    if not isinstance(raw, (tuple, list)):
        raise TypeError(f"{field} must be a sequence")
    result: list[str] = []
    for item in raw:
        if isinstance(item, str):
            result.append(item)
            continue
        address = _value(item, "address")
        if not isinstance(address, str):
            raise TypeError(f"{field} entries must contain an address")
        result.append(address)
    return tuple(result)


def _normalize_and_limit_recipients(
    to: tuple[str, ...],
    cc: tuple[str, ...],
    bcc: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """复用领域规范化规则，并在去重后执行编辑态上限。"""
    normalized = normalize_mail_recipients(tuple(to), tuple(cc), tuple(bcc))
    if sum(len(field) for field in normalized) > MAX_MAIL_RECIPIENTS:
        raise StateConflictError(
            error_code="mail_recipient_limit_exceeded",
            message="mail draft exceeds the recipient limit",
        )
    return normalized


def _recipient_values(addresses: tuple[str, ...]) -> tuple[MailDraftRecipient, ...]:
    """把规范 addr-spec 包装为 Repository 可持久化的应用值对象。"""
    return tuple(MailDraftRecipient(address=address) for address in addresses)


def _resolved_subject(
    *, request: CreateMailDraftInput, source: MailDraftSourceMessage | None
) -> str:
    """新邮件保留用户主题，回复主题只由本地来源确定性生成。"""
    if source is None:
        return request.subject
    deterministic = _reply_subject(source.subject)
    if request.subject and request.subject != deterministic:
        raise _thread_binding_conflict()
    return deterministic


def _reply_subject(subject: str) -> str:
    """为回复生成稳定 ``Re:`` 主题，并保持总长度不超过 255 字符。"""
    normalized = subject if subject[:3].casefold() == "re:" else f"Re: {subject}"
    return normalized[:255]


def _creation_payload_hash(
    *,
    connection_id: UUID,
    mode: MailMode,
    source: MailDraftSourceMessage | None,
    to: tuple[str, ...],
    cc: tuple[str, ...],
    bcc: tuple[str, ...],
    subject: str,
    body_text: str,
) -> str:
    """哈希完整规范创建意图，使同键异载荷无法复用已有草稿。"""
    payload = {
        "connection_id": str(connection_id),
        "mode": mode.value,
        "source_thread_id": source.thread_id if source is not None else None,
        "source_message_id": source.message_id if source is not None else None,
        "to": list(to),
        "cc": list(cc),
        "bcc": list(bcc),
        "subject": subject,
        "body_text": body_text,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _ensure_editable(status: MailDraftStatus) -> None:
    """把审批、未知结果和终态分别收敛为清晰的编辑边界。"""
    if status is MailDraftStatus.EDITING:
        return
    if status is MailDraftStatus.AWAITING_APPROVAL:
        raise StateConflictError(
            error_code="mail_draft_approval_withdrawal_required",
            message="cancel the trusted task before editing this mail draft",
        )
    if status is MailDraftStatus.NEEDS_ATTENTION:
        raise StateConflictError(
            error_code="mail_draft_result_confirmation_required",
            message="confirm the prior execution did not occur before editing",
        )
    raise StateConflictError(
        error_code="mail_draft_not_editable",
        message="mail draft is not editable",
    )


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    """读取显式时钟并规范为 UTC。"""
    return _utc_datetime(clock(), field="mail draft clock")


def _utc_datetime(value: datetime, *, field: str) -> datetime:
    """要求带时区时间并转换为 UTC，禁止依赖宿主机时区。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _connection_capability_disabled() -> StateConflictError:
    """构造默认或显式发送连接不可用时的固定 fail-closed 错误。"""
    return StateConflictError(
        error_code="connection_capability_disabled",
        message="mail send capability is not enabled for the selected connection",
    )


def _connection_scope_missing() -> StateConflictError:
    """构造实际委托 scope 缺失或已撤销时的固定重新授权错误。"""
    return StateConflictError(
        error_code="connection_scope_missing",
        message="mail send requires reauthorization for the selected connection",
    )


def _thread_binding_conflict() -> StateConflictError:
    """构造不回显来源 ID、地址或主题的线程绑定冲突。"""
    return StateConflictError(
        error_code="mail_thread_binding_conflict",
        message="mail reply source binding is unavailable or inconsistent",
    )


def _binding_immutable() -> StateConflictError:
    """构造回复账户、线程或主题不可通过 PATCH 修改的稳定冲突。"""
    return StateConflictError(
        error_code="mail_draft_binding_immutable",
        message="reply account, thread, source message, and subject are immutable",
    )


# 兼容后续路由或调用方使用复数命名；两者指向同一窄用例，不建立第二套体系。
MailDraftsUseCase = MailDraftUseCase


__all__ = [
    "DEFAULT_MAIL_BODY_RETENTION_DAYS",
    "MAX_MAIL_RECIPIENTS",
    "MAX_RECIPIENT_SUGGESTIONS",
    "CreateMailDraftInput",
    "MailDraftCapabilitySnapshot",
    "MailDraftConnectionReader",
    "MailDraftNotFoundError",
    "MailDraftRecipient",
    "MailDraftRepository",
    "MailDraftSourceMessage",
    "MailDraftSourceReader",
    "MailDraftUseCase",
    "MailDraftView",
    "MailDraftsUseCase",
    "MailRecipientHistoryEntry",
    "UpdateMailDraftInput",
]
