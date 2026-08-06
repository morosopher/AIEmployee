"""定义 M2 邮件发送命令的纯领域值、地址规范化与回复绑定不变量。"""

from dataclasses import dataclass
from datetime import datetime
from email.errors import HeaderParseError
from email.headerregistry import Address, HeaderRegistry
from enum import StrEnum
from uuid import UUID

_HEADER_REGISTRY = HeaderRegistry()
_EMAIL_PARSER_EXCEPTIONS = (
    AttributeError,
    HeaderParseError,
    IndexError,
    TypeError,
    ValueError,
)


class MailMode(StrEnum):
    """邮件发送的稳定模式，联合范围严格限定为 M2 已批准能力。"""

    NEW = "new"
    REPLY = "reply"
    REPLY_ALL = "reply_all"


def normalize_mailbox_address(address: str) -> str:
    """验证单个邮箱 addr-spec，并只对域名执行大小写归一。

    本函数不接受显示名或任意 MIME Header 文本，也不执行 Gmail 点号、加号或
    本地部分大小写归并。解析后使用 local part 的语义值重建规范 addr-spec，
    从而消除注释、CFWS 与不必要引号的表示差异，同时保留真正需要的引号和转义。

    Args:
        address: 未带显示名的单个邮箱 addr-spec。

    Returns:
        保留 local part 语义、将域名 casefold 并重建后的规范邮箱地址。

    Raises:
        ValueError: 地址为空、含首尾空白、CR/LF 或不符合邮箱 addr-spec 语法。
    """
    if not isinstance(address, str) or not address or address != address.strip():
        raise ValueError("mailbox address must be a nonempty unpadded string")
    if "\r" in address or "\n" in address:
        raise ValueError("mailbox address must not contain CR or LF")

    try:
        parsed = Address(addr_spec=address)
        username = parsed.username
        domain = parsed.domain
        display_name = parsed.display_name
        if not username or not domain or display_name:
            raise ValueError("mailbox address is incomplete")

        # 使用解析后的语义 username 重建；Address 只在语法确有需要时添加引号/转义。
        normalized = Address(
            username=username,
            domain=domain.casefold(),
        ).addr_spec
    except _EMAIL_PARSER_EXCEPTIONS:
        # Python email 对少数残缺 token 会在构造或属性访问阶段抛内部异常；
        # 离开 except 后统一抛固定领域错误，避免异常链保留原始地址。
        pass
    else:
        return normalized
    raise ValueError("mailbox address must be a valid addr-spec")


def normalize_mail_recipients(
    to: tuple[str, ...],
    cc: tuple[str, ...],
    bcc: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """按 To、CC、BCC 顺序规范化并跨字段确定性去重收件人。

    Args:
        to: 主送地址，原有顺序表示用户意图。
        cc: 抄送地址，重复地址由较早字段保留。
        bcc: 密送地址，重复地址由较早字段保留。

    Returns:
        三个保持首次出现顺序的规范化不可变地址 tuple。

    Raises:
        ValueError: 任一地址语法非法或包含 Header 注入字符。
    """
    normalized_fields: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for addresses in (to, cc, bcc):
        normalized: list[str] = []
        for address in addresses:
            canonical = normalize_mailbox_address(address)
            if canonical in seen:
                continue
            seen.add(canonical)
            normalized.append(canonical)
        normalized_fields.append(tuple(normalized))
    return normalized_fields[0], normalized_fields[1], normalized_fields[2]


def _validate_message_id(value: str) -> None:
    """校验应用生成的单个现代 RFC Message-ID 规范表示。

    可信命令只接受无需折叠或兼容 obsolete 语法的规范 Header 值。标准库解析器
    负责识别 dot-atom、domain literal 与语法缺陷；额外的逐字比较和 ASCII 检查
    则拒绝解析器可能保留的 CFWS、非打印字符、国际化文本或多个 Message-ID。

    Args:
        value: 单个 ``<id-left@id-right>`` 形式的应用生成值。

    Raises:
        ValueError: 值不是无缺陷、无空白且可打印 ASCII 的规范 Message-ID。
    """
    if not isinstance(value, str) or not value:
        raise ValueError("reply thread header must be a canonical Message-ID")
    if any(not 0x21 <= ord(character) <= 0x7E for character in value):
        raise ValueError("reply thread header must be a canonical Message-ID")

    try:
        parsed = _HEADER_REGISTRY("Message-ID", value)
        defects = parsed.defects
        rendered = str(parsed)
    except _EMAIL_PARSER_EXCEPTIONS:
        # 构造、defects 属性与规范渲染均属于同一不可信 stdlib 解析边界。
        pass
    else:
        # M2 明确只接受现代、无 defects 的应用生成形式；obsolete 引号不会放宽。
        # 逐字相等还确保解析器没有忽略尾随内容或重排不可信输入。
        if not defects and rendered == value:
            return
    raise ValueError("reply thread header must be a canonical Message-ID")


@dataclass(frozen=True, slots=True)
class ReplyThreadHeaders:
    """回复发送所需的最小内部引用头，不接受任意 MIME Header 名。

    Attributes:
        in_reply_to: 当前回复直接引用的规范 Message-ID。
        references: 按线程顺序排列且不重复的规范 Message-ID tuple。
    """

    in_reply_to: str
    references: tuple[str, ...]

    def __post_init__(self) -> None:
        """拒绝空、重复、错序或非规范引用，确保值只能由同步事实确定性构造。

        Raises:
            ValueError: ``in_reply_to`` 或 ``references`` 不满足严格引用约束。
        """
        _validate_message_id(self.in_reply_to)
        if not isinstance(self.references, tuple) or not self.references:
            raise ValueError("reply references must be a nonempty tuple")
        for reference in self.references:
            _validate_message_id(reference)
        if len(set(self.references)) != len(self.references):
            raise ValueError("reply references must not contain duplicates")
        if self.references[-1] != self.in_reply_to:
            raise ValueError("reply references last reference must equal in_reply_to")


@dataclass(frozen=True, slots=True)
class MailSendCommand:
    """一个已冻结、可哈希且不包含用户可控 From 的邮件发送命令。

    Attributes:
        schema_version: 固定协议版本 ``mail_send.v1``。
        action: 固定动作 ``mail.send``。
        operation_id: 审批、幂等执行和核对共用的稳定 UUID。
        connection_id: 精确发送连接 UUID，发件身份由该连接事实决定。
        draft_id: 已冻结本地草稿 UUID。
        draft_version: 已冻结草稿的正整数版本。
        message_date: 带时区的邮件生成时间。
        mode: 新邮件、回复或全部回复。
        source_thread_id: 回复类命令绑定的规范线程标识。
        source_message_id: 回复类命令绑定的规范源邮件标识。
        to: 规范化且跨字段去重的主送地址。
        cc: 规范化且跨字段去重的抄送地址。
        bcc: 规范化且跨字段去重的密送地址。
        subject: 最多 255 个 Unicode 字符且不含 CR/LF 的主题。
        body_text: 最多 100000 个 Unicode 字符的纯文本正文。
        thread_headers: 回复类命令必需的严格内部引用头。
    """

    schema_version: str
    action: str
    operation_id: UUID
    connection_id: UUID
    draft_id: UUID
    draft_version: int
    message_date: datetime
    mode: MailMode
    source_thread_id: str | None
    source_message_id: str | None
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body_text: str
    thread_headers: ReplyThreadHeaders | None

    def __post_init__(self) -> None:
        """校验协议常量、回复绑定和安全边界，并冻结规范化收件人。

        Raises:
            ValueError: 固定协议值、草稿版本、时间、收件人、文本或回复绑定非法。
            TypeError: UUID、枚举或 tuple 字段不是声明的领域类型。
        """
        if self.schema_version != "mail_send.v1":
            raise ValueError("mail schema_version must be mail_send.v1")
        if self.action != "mail.send":
            raise ValueError("mail action must be mail.send")
        if not all(
            isinstance(value, UUID)
            for value in (self.operation_id, self.connection_id, self.draft_id)
        ):
            raise TypeError("mail command identifiers must be UUID values")
        if type(self.draft_version) is not int or self.draft_version <= 0:
            raise ValueError("draft_version must be a positive integer")
        if (
            not isinstance(self.message_date, datetime)
            or self.message_date.tzinfo is None
            or self.message_date.utcoffset() is None
        ):
            raise ValueError("message_date must be timezone-aware")
        if not isinstance(self.mode, MailMode):
            raise TypeError("mail mode must be MailMode")

        self._validate_source_binding()
        if not all(isinstance(addresses, tuple) for addresses in (self.to, self.cc, self.bcc)):
            raise TypeError("mail recipient fields must be tuples")
        to, cc, bcc = normalize_mail_recipients(self.to, self.cc, self.bcc)
        recipient_count = len(to) + len(cc) + len(bcc)
        if not 1 <= recipient_count <= 50:
            raise ValueError("mail command must contain 1 to 50 unique recipients")
        object.__setattr__(self, "to", to)
        object.__setattr__(self, "cc", cc)
        object.__setattr__(self, "bcc", bcc)

        if not isinstance(self.subject, str) or len(self.subject) > 255:
            raise ValueError("mail subject must contain at most 255 Unicode characters")
        if "\r" in self.subject or "\n" in self.subject:
            raise ValueError("mail subject must not contain CR or LF")
        if not isinstance(self.body_text, str) or len(self.body_text) > 100_000:
            raise ValueError("mail body_text must contain at most 100000 Unicode characters")

    def _validate_source_binding(self) -> None:
        """确保新邮件与回复类命令使用互斥且完整的来源绑定。"""
        if self.mode is MailMode.NEW:
            if any(
                value is not None
                for value in (
                    self.source_thread_id,
                    self.source_message_id,
                    self.thread_headers,
                )
            ):
                raise ValueError("new mail must not contain reply source binding")
            return

        if not _is_safe_nonempty_identifier(self.source_thread_id):
            raise ValueError("reply mail requires source_thread_id")
        if not _is_safe_nonempty_identifier(self.source_message_id):
            raise ValueError("reply mail requires source_message_id")
        if self.thread_headers is None:
            raise ValueError("reply mail requires strict thread_headers")
        if not isinstance(self.thread_headers, ReplyThreadHeaders):
            raise TypeError("reply thread_headers must be ReplyThreadHeaders")


def _is_safe_nonempty_identifier(value: str | None) -> bool:
    """判断供应商来源标识是否非空且不含换行控制字符。"""
    return isinstance(value, str) and bool(value) and "\r" not in value and "\n" not in value


__all__ = [
    "MailMode",
    "MailSendCommand",
    "ReplyThreadHeaders",
    "normalize_mail_recipients",
    "normalize_mailbox_address",
]
