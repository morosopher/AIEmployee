"""定义邮件确定性分类与紧急度判断的纯领域规则。"""

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import StrEnum


class EmailCategory(StrEnum):
    """邮件分类的稳定持久化值，供规则和后续模型结果共同使用。"""

    WORK = "work"
    NOTIFICATION = "notification"
    SPAM = "spam"
    OTHER = "other"


class EmailUrgency(StrEnum):
    """邮件紧急度的稳定持久化值，独立于邮件分类。"""

    URGENT = "urgent"
    NORMAL = "normal"


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """供领域规则处理的最小、已规范化邮件元数据。

    邮件正文不属于确定性分类输入，避免将垃圾邮件等不可信内容意外传递到后续
    模型路径。适配器负责把供应商头字段和标签收窄为此值对象。

    Attributes:
        sender: 邮件发件人地址或已规范化的发送方标识。
        subject: 邮件主题，仅用于独立紧急度规则。
        labels: Gmail 标签的不可变集合。
        headers: 仅供规则使用的已规范化邮件头。
    """

    sender: str
    subject: str
    labels: frozenset[str] = field(default_factory=frozenset)
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EmailClassification:
    """一次确定性邮件分类及其不含敏感载荷的审计依据。"""

    category: EmailCategory
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EmailUrgencyClassification:
    """一次确定性紧急度判断及其不含邮件内容的审计依据。"""

    urgency: EmailUrgency
    reason_codes: tuple[str, ...]


def classify_by_rules(
    message: EmailMessage,
    *,
    known_work_sender_domains: AbstractSet[str],
) -> EmailClassification | None:
    """按固定优先级执行无需模型的邮件分类。

    垃圾邮件、批量通知和用户显式配置的工作域会直接返回结果及稳定原因码。
    其余情况返回 ``None``，由应用层决定是否在完成本地脱敏后调用模型；本函数
    绝不根据主题或类别猜测紧急度。

    Args:
        message: 已从供应商字段规范化的邮件元数据。
        known_work_sender_domains: 用户配置的工作发件人域集合。

    Returns:
        命中确定性规则时的分类；邮件语义歧义时为 ``None``。
    """
    normalized_labels = {label.casefold() for label in message.labels}
    if "spam" in normalized_labels:
        return EmailClassification(EmailCategory.SPAM, ("gmail_label_spam",))

    normalized_headers = {
        name.casefold(): value.casefold() for name, value in message.headers.items()
    }
    if "list-unsubscribe" in normalized_headers:
        return EmailClassification(
            EmailCategory.NOTIFICATION,
            ("header_list_unsubscribe",),
        )
    if "bulk" in normalized_headers.get("precedence", "").split(","):
        return EmailClassification(
            EmailCategory.NOTIFICATION,
            ("header_precedence_bulk",),
        )

    sender_domain = _sender_domain(message.sender)
    normalized_work_domains = {domain.casefold() for domain in known_work_sender_domains}
    if sender_domain is not None and sender_domain in normalized_work_domains:
        return EmailClassification(EmailCategory.WORK, ("known_work_sender_domain",))
    return None


def classify_urgency_by_rules(message: EmailMessage) -> EmailUrgencyClassification:
    """独立从主题中的显式紧急标记确定邮件紧急度。

    该判断特意不接收分类结果，确保任何类别都不会隐式改变紧急度，并为后续模型
    无法判断时提供稳定的 ``NORMAL`` 基线。

    Args:
        message: 已规范化的邮件元数据。

    Returns:
        包含 ``URGENT`` 或 ``NORMAL`` 及对应稳定原因码的判断结果。
    """
    if "urgent" in message.subject.casefold():
        return EmailUrgencyClassification(EmailUrgency.URGENT, ("subject_urgent_marker",))
    return EmailUrgencyClassification(EmailUrgency.NORMAL, ("no_urgent_marker",))


def _sender_domain(sender: str) -> str | None:
    """从简单地址标识中安全提取小写域名，无法提取时返回空值。"""
    local_part, separator, domain = sender.strip().rpartition("@")
    if not separator or not local_part or not domain:
        return None
    return domain.casefold()
