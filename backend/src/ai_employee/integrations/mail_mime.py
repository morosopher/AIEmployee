"""构建 M2 邮件发送所需的确定性、纯文本 RFC 2822 MIME。

该模块只接收已经冻结的 ``MailSendCommand`` 和由连接事实提供的发件地址。它不读取
数据库、不访问供应商，也不接受任意用户 Header；这样审批前的能力预检与实际发送会
共享完全相同的可表达性边界，安全重试也能复用字节级相同的 MIME 载荷。
"""

from __future__ import annotations

from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from uuid import UUID

from ai_employee.domain.mail_actions import MailMode, MailSendCommand, normalize_mailbox_address

MESSAGE_ID_DOMAIN = "ai-employee.invalid"
"""应用生成稳定 Message-ID 使用的保留域名。"""


def message_id_for(operation_id: UUID) -> str:
    """根据可信操作 UUID 生成稳定且唯一的 RFC Message-ID。

    Args:
        operation_id: 已绑定审批、ToolExecution 与结果核对的操作 UUID。

    Returns:
        ``<uuid@ai-employee.invalid>`` 形式的规范 Message-ID。

    Raises:
        TypeError: ``operation_id`` 不是 UUID。
    """
    if type(operation_id) is not UUID:
        raise TypeError("operation_id must be a UUID")
    return f"<{operation_id}@{MESSAGE_ID_DOMAIN}>"


def build_mail_mime(command: MailSendCommand, *, from_address: str) -> bytes:
    """把冻结邮件命令渲染为 UTF-8、纯文本且可重复的 MIME 字节。

    Args:
        command: 已通过领域校验的不可变邮件发送命令。
        from_address: 当前 OAuth 连接的主账户地址；不能从命令中提供。

    Returns:
        使用 CRLF 换行的 RFC 2822 MIME 字节，可直接进行 Base64URL 编码。

    Raises:
        TypeError: ``command`` 类型不正确。
        ValueError: 发件地址、回复绑定或 MIME Header 不可安全表达。

    设计约束：
        - 只写入应用生成的 From/To/Cc/Bcc/Subject/Date/Message-ID 及回复引用头；
        - ``EmailMessage`` 生成的 MIME-Version、Content-Type 等结构头也由应用固定；
        - 不生成 HTML、附件、任意用户 Header 或供应商草稿。
    """
    if type(command) is not MailSendCommand:
        raise TypeError("command must be MailSendCommand")

    # 连接身份是唯一可信 From 来源。规范化同时拒绝 CR/LF、显示名和空白注入；不把
    # 用户在草稿中的收件人字段当作发件人，也不允许通过 Header 语法伪造代理发送。
    normalized_from = normalize_mailbox_address(from_address)
    _validate_header_text(command.subject, "subject")

    message = EmailMessage(policy=policy.SMTP)
    message["From"] = normalized_from
    if command.to:
        message["To"] = ", ".join(command.to)
    if command.cc:
        message["Cc"] = ", ".join(command.cc)
    if command.bcc:
        message["Bcc"] = ", ".join(command.bcc)
    message["Subject"] = command.subject
    message["Date"] = _format_date(command.message_date)
    message["Message-ID"] = message_id_for(command.operation_id)

    if command.mode is not MailMode.NEW:
        headers = command.thread_headers
        if headers is None:  # 领域命令理论上已阻止；这里再 fail closed 防止未来绕过。
            raise ValueError("reply mail requires strict thread_headers")
        message["In-Reply-To"] = headers.in_reply_to
        message["References"] = " ".join(headers.references)

    # set_content 只创建 text/plain 单一叶节点；EmailMessage 会为 Unicode 正文选择
    # 明确 charset/传输编码，且输出在固定 policy 下可重复。正文中的 CR/LF 属于正文
    # 内容而非 Header，不应被删改或重新解释。
    message.set_content(command.body_text, subtype="plain", charset="utf-8")
    return message.as_bytes(policy=policy.SMTP)


def _format_date(value: datetime) -> str:
    """把冻结 aware datetime 格式化为稳定 RFC 2822 Date Header。"""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("message_date must be timezone-aware")
    return format_datetime(value)


def _validate_header_text(value: str, field_name: str) -> None:
    """拒绝 Header 注入字符，同时保持错误消息不回显正文或主题。"""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field_name} must not contain CR or LF")


__all__ = ["MESSAGE_ID_DOMAIN", "build_mail_mime", "message_id_for"]
