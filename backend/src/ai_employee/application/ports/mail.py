"""定义供应商中立的邮件目录、增量页面与规范化消息读取边界。"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)


@dataclass(frozen=True, slots=True)
class MailScope:
    """表示一个可独立同步并保存游标的 opaque mailbox/folder scope。

    ``scope_key`` 只能由对应供应商适配器解释；应用层与数据库仅把它作为稳定键。展示名称
    必须已经由适配器清洗，``well_known_name`` 只保存供应商无关的规范化类别。
    """

    scope_key: str
    display_name: str
    well_known_name: str | None = None


class MailMessageUpsertResult(StrEnum):
    """表示规范消息是否成为当前连接内最新的持久 projection。"""

    APPLIED = "applied"
    STALE_SKIPPED = "stale_skipped"


@dataclass(frozen=True, slots=True, init=False)
class MailMessage:
    """表示已去除供应商 JSON、原始 MIME 与危险正文的一封规范化邮件。

    规范字段使用 provider-neutral 名称；构造器暂时接受 M1 Gmail 的旧关键字，确保迁移期间
    既有 Google 适配器与脱敏 fixture 不需要复制第二套消息模型。旧名称只作为只读属性暴露，
    新 Repository 与后续 Microsoft 适配器必须使用规范字段。
    """

    provider_message_id: str
    provider_thread_id: str
    provider_conversation_id: str | None
    internet_message_id: str | None
    mailbox_scope_key: str
    sender: Mapping[str, str]
    recipients: tuple[Mapping[str, str], ...]
    subject: str
    sanitized_body: str
    received_at: datetime
    sent_at: datetime | None
    provider_updated_at: datetime | None
    labels: tuple[str, ...]
    normalized_reply_headers: Mapping[str, str]
    provider_url: str
    snippet: str
    _legacy_history_id: str

    def __init__(
        self,
        *,
        provider_message_id: str | None = None,
        provider_thread_id: str | None = None,
        provider_conversation_id: str | None = None,
        internet_message_id: str | None = None,
        mailbox_scope_key: str = "mailbox",
        sender: Mapping[str, str] | None = None,
        recipients: Sequence[Mapping[str, str]] = (),
        subject: str = "",
        sanitized_body: str | None = None,
        received_at: datetime | None = None,
        sent_at: datetime | None = None,
        provider_updated_at: datetime | None = None,
        labels: Sequence[str] = (),
        normalized_reply_headers: Mapping[str, str] | None = None,
        provider_url: str = "",
        snippet: str = "",
        message_id: str | None = None,
        thread_id: str | None = None,
        history_id: str = "",
        normalized_body: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """复制并冻结规范字段，同时收窄 M1 Gmail 兼容输入。

        Args:
            provider_message_id: 连接内稳定的供应商消息 ID。
            provider_thread_id: 连接内稳定的供应商线程 ID。
            provider_conversation_id: 可选供应商会话 ID，不得由线程 ID 猜测。
            internet_message_id: 可选 RFC Message-ID。
            mailbox_scope_key: 消息所属的精确 mailbox/folder scope。
            sender: 已规范化发件人。
            recipients: 已规范化收件人集合。
            subject: 主题明文元数据。
            sanitized_body: 已在适配器边界清洗的正文。
            received_at: 带时区的接收时间。
            sent_at: 可选带时区发送时间。
            provider_updated_at: 可选供应商版本时间；Microsoft 必须提供，Google 可为空。
            labels: 供应商标签或类别的规范字符串。
            normalized_reply_headers: 回复所需的规范化 Header 映射。
            provider_url: 供应商界面的只读链接。
            snippet: M1 兼容摘录；Repository 不持久化其明文。
            message_id: M1 ``provider_message_id`` 兼容名。
            thread_id: M1 ``provider_thread_id`` 兼容名。
            history_id: M1 Gmail history 兼容事实，仅供旧适配器计算页游标。
            normalized_body: M1 ``sanitized_body`` 兼容名。
            headers: M1 ``normalized_reply_headers`` 兼容名。

        Raises:
            ValueError: 规范名与兼容名冲突，或缺少稳定 ID、scope、接收时间。
        """
        resolved_message_id = self._resolve_alias(
            canonical=provider_message_id,
            legacy=message_id,
            field_name="provider_message_id",
        )
        resolved_thread_id = self._resolve_alias(
            canonical=provider_thread_id,
            legacy=thread_id,
            field_name="provider_thread_id",
        )
        if mailbox_scope_key == "":
            raise ValueError("mailbox_scope_key must not be empty")
        if received_at is None:
            raise ValueError("received_at is required")
        if (
            sanitized_body is not None
            and normalized_body is not None
            and sanitized_body != normalized_body
        ):
            raise ValueError("sanitized_body conflicts with normalized_body")
        resolved_body = sanitized_body if sanitized_body is not None else normalized_body or ""
        if (
            normalized_reply_headers is not None
            and headers is not None
            and dict(normalized_reply_headers) != dict(headers)
        ):
            raise ValueError("normalized_reply_headers conflicts with headers")
        resolved_headers = normalized_reply_headers or headers or {}
        resolved_internet_id = internet_message_id or resolved_headers.get("message-id")
        if provider_updated_at is not None:
            if provider_updated_at.tzinfo is None or provider_updated_at.utcoffset() is None:
                raise ValueError("provider_updated_at must be timezone-aware")
            provider_updated_at = provider_updated_at.astimezone(UTC)

        object.__setattr__(self, "provider_message_id", resolved_message_id)
        object.__setattr__(self, "provider_thread_id", resolved_thread_id)
        object.__setattr__(self, "provider_conversation_id", provider_conversation_id)
        object.__setattr__(self, "internet_message_id", resolved_internet_id)
        object.__setattr__(self, "mailbox_scope_key", mailbox_scope_key)
        object.__setattr__(self, "sender", MappingProxyType(dict(sender or {})))
        object.__setattr__(
            self,
            "recipients",
            tuple(MappingProxyType(dict(recipient)) for recipient in recipients),
        )
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "sanitized_body", resolved_body)
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "sent_at", sent_at)
        object.__setattr__(self, "provider_updated_at", provider_updated_at)
        object.__setattr__(self, "labels", tuple(labels))
        object.__setattr__(
            self,
            "normalized_reply_headers",
            MappingProxyType(dict(resolved_headers)),
        )
        object.__setattr__(self, "provider_url", provider_url)
        object.__setattr__(self, "snippet", snippet)
        object.__setattr__(self, "_legacy_history_id", history_id)

    @staticmethod
    def _resolve_alias(*, canonical: str | None, legacy: str | None, field_name: str) -> str:
        """合并规范名与 M1 兼容名，并拒绝互相矛盾的双重输入。"""
        if canonical is not None and legacy is not None and canonical != legacy:
            raise ValueError(f"{field_name} conflicts with its legacy alias")
        resolved = canonical if canonical is not None else legacy
        if resolved is None or resolved == "":
            raise ValueError(f"{field_name} is required")
        return resolved

    @property
    def message_id(self) -> str:
        """返回 M1 Gmail 使用的只读消息 ID 兼容属性。"""
        return self.provider_message_id

    @property
    def thread_id(self) -> str:
        """返回 M1 Gmail 使用的只读线程 ID 兼容属性。"""
        return self.provider_thread_id

    @property
    def history_id(self) -> str:
        """返回旧 Google 适配器计算页游标所需的 history ID。"""
        return self._legacy_history_id

    @property
    def normalized_body(self) -> str:
        """返回 M1 Gmail 使用的已清洗正文兼容属性。"""
        return self.sanitized_body

    @property
    def headers(self) -> Mapping[str, str]:
        """返回 M1 Gmail 使用的冻结 Header 兼容属性。"""
        return self.normalized_reply_headers


@dataclass(frozen=True, slots=True, init=False)
class MailSyncPage:
    """表示一个已规范化邮件页及仅在供应商确认时存在的下一游标。"""

    messages: tuple[MailMessage, ...]
    next_page_token: str | None
    next_cursor: str | None
    removals: tuple["MailRemoval", ...]

    def __init__(
        self,
        messages: Sequence[MailMessage],
        next_page_token: str | None,
        next_cursor: str | None = None,
        *,
        latest_history_id: str | None = None,
        removals: Sequence["MailRemoval"] = (),
    ) -> None:
        """冻结页面并兼容 M1 ``latest_history_id`` 关键字。

        Raises:
            ValueError: 新旧游标名称同时给出且值不同。
        """
        if (
            next_cursor is not None
            and latest_history_id is not None
            and next_cursor != latest_history_id
        ):
            raise ValueError("next_cursor conflicts with latest_history_id")
        object.__setattr__(self, "messages", tuple(messages))
        object.__setattr__(self, "next_page_token", next_page_token)
        object.__setattr__(
            self,
            "next_cursor",
            next_cursor if next_cursor is not None else latest_history_id,
        )
        object.__setattr__(self, "removals", tuple(removals))

    @property
    def latest_history_id(self) -> str:
        """返回旧 Gmail 页面使用的 history 游标兼容属性。"""
        return self.next_cursor or ""


@dataclass(frozen=True, slots=True)
class MailConnectionState:
    """表示已验证连接 provider 与一个精确邮件 scope 的持久游标。"""

    cursor: str | None
    provider: str = "google"
    scope_key: str = "mailbox"


@dataclass(frozen=True, slots=True)
class MailRemoval:
    """表示一个 Graph ``@removed`` 墓碑，而不伪造不存在的邮件字段。

    删除响应通常只携带 provider message ID 和可选 ``reason``，没有 thread、接收时间或
    正文。独立值对象让 integrations 在边界完成字段校验，application/repository 只能按
    精确连接与 folder scope 删除，避免把缺失字段填成看似真实的邮件。
    """

    provider_message_id: str
    mailbox_scope_key: str
    reason: str | None = None

    def __post_init__(self) -> None:
        """拒绝空 ID/scope，防止墓碑删除扩大到连接或用户范围。"""
        if self.provider_message_id == "":
            raise ValueError("provider_message_id must not be empty")
        if self.mailbox_scope_key == "":
            raise ValueError("mailbox_scope_key must not be empty")


class MailCursorExpiredError(PermanentProviderError):
    """表示单个邮件 scope 的增量游标失效，需要受限初始回退。"""

    def __init__(self, provider: str = "google", scope_key: str = "mailbox") -> None:
        """构造不含 cursor 或 Delta URL 的供应商中立稳定错误。"""
        self.provider = provider
        self.scope_key = scope_key
        super().__init__(
            error_code="mail_cursor_expired",
            message="Mail sync cursor expired",
            metadata={"provider": provider, "scope_key": scope_key},
        )


class MailReader(Protocol):
    """定义邮件目录与按 scope 增量读取的供应商中立端口。"""

    async def list_sync_scopes(self) -> tuple[MailScope, ...]:
        """返回可同步且已经过安全显示清洗的 mailbox/folder 目录。"""
        ...

    def initial_pages(self, scope_key: str, *, since: datetime) -> AsyncIterator[MailSyncPage]:
        """从显式 UTC 下界读取一个 scope 的受限初始页。"""
        ...

    def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """从一个 scope 的 opaque 游标读取增量页。"""
        ...


__all__ = [
    "MailConnectionState",
    "MailCursorExpiredError",
    "MailMessage",
    "MailMessageUpsertResult",
    "MailReader",
    "MailRemoval",
    "MailScope",
    "MailSyncPage",
    "TransientProviderError",
    "UserActionRequiredError",
]
