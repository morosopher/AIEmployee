"""定义受控对话意图、邮件草稿解析与原子消息创建应用端口。"""

import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ai_employee.application.use_cases.tasks import CreateTaskResult
from ai_employee.domain.mail_actions import normalize_mailbox_address

UNSUPPORTED_RESPONSE = (
    "当前支持生成或查看每日简报，以及按明确请求准备可编辑的本地邮件草稿；"
    "不会直接发送邮件、执行其他外部写操作或进行通用规划。"
)

_MAIL_DRAFT_MARKERS = (
    "draft an email",
    "draft a mail",
    "draft email to",
    "draft mail to",
    "prepare an email",
    "prepare a mail",
    "prepare email",
    "prepare mail",
    "create an email draft",
    "create a mail draft",
    "compose an email",
    "compose a mail",
    "write an email draft",
    "write email to",
    "write mail to",
    "起草邮件",
    "草拟邮件",
    "准备邮件草稿",
    "准备一封邮件",
    "创建邮件草稿",
)
_DIRECT_SEND_MARKERS = ("send email", "send mail", "发送邮件", "发邮件")
_MAILBOX_CANDIDATE_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_.!#$%&'*+/=?^`{|}~-])"
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    r"(?![a-z0-9_.!#$%&'*+/=?^`{|}~-])"
)


@dataclass(frozen=True, slots=True)
class MailDraftConversationRequest:
    """表示从明确对话文本确定性提取的本地新邮件草稿请求。

    ``to_recipients`` 只能来自用户原始文本中语法有效的显式地址；模型不能补充地址、
    选择账户或绑定线程。没有地址时仍可创建空白草稿供用户在编辑器中手工完成。
    """

    to_recipients: tuple[str, ...]


def parse_mail_draft_conversation_request(
    text: str,
) -> MailDraftConversationRequest | None:
    """仅把明确“准备/起草邮件草稿”文本解析为可编辑本地草稿意图。

    直接发送措辞、普通邮件闲聊和任意模型分类都不会通过本解析器。地址候选从用户
    原文确定性扫描，再复用领域 addr-spec 规范化；非法候选被忽略而不是交给模型修复。
    """
    if not isinstance(text, str):
        raise TypeError("conversation text must be a string")
    normalized = text.casefold()
    if any(marker in normalized for marker in _DIRECT_SEND_MARKERS):
        return None
    if not any(marker in normalized for marker in _MAIL_DRAFT_MARKERS):
        return None

    recipients: list[str] = []
    seen: set[str] = set()
    for match in _MAILBOX_CANDIDATE_PATTERN.finditer(text):
        candidate = match.group(0)
        try:
            address = normalize_mailbox_address(candidate)
        except ValueError:
            continue
        if address not in seen:
            seen.add(address)
            recipients.append(address)
    return MailDraftConversationRequest(to_recipients=tuple(recipients))


def unsupported_response() -> str:
    """返回不调用模型、工具或外部提供商的固定边界回复。"""
    return UNSUPPORTED_RESPONSE


class ConversationNotFoundError(Exception):
    """表示用户范围内不存在所请求会话。"""


class ConversationMessageStore(Protocol):
    """定义消息、任务和 Outbox 必须共同提交的原子持久化能力。"""

    async def create_message_and_task(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        content_markdown: str,
        idempotency_key: str,
    ) -> CreateTaskResult:
        """按用户范围和稳定键原子创建消息及回复任务。"""


class ConversationMessageStoreFactory(Protocol):
    """为一次消息创建提供提交或回滚受控的事务上下文。"""

    def __call__(self) -> AbstractAsyncContextManager[ConversationMessageStore]:
        """创建仅覆盖当前对话写入的事务上下文。"""


class CreateConversationMessageUseCase:
    """协调同一事务中的用户消息和 ``conversation.respond`` 任务创建。"""

    def __init__(self, stores: ConversationMessageStoreFactory) -> None:
        """注入不暴露 SQLAlchemy 或 ORM 的对话事务端口。"""
        self._stores = stores

    async def execute(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        content_markdown: str,
        client_request_id: str,
    ) -> CreateTaskResult:
        """用稳定客户端键幂等创建消息与 TaskRun，绝不留下半条消息。"""
        idempotency_key = f"conversation:{user_id}:{client_request_id}"
        async with self._stores() as store:
            return await store.create_message_and_task(
                user_id=user_id,
                conversation_id=conversation_id,
                content_markdown=content_markdown,
                idempotency_key=idempotency_key,
            )


__all__ = [
    "UNSUPPORTED_RESPONSE",
    "ConversationMessageStore",
    "ConversationMessageStoreFactory",
    "ConversationNotFoundError",
    "CreateConversationMessageUseCase",
    "MailDraftConversationRequest",
    "parse_mail_draft_conversation_request",
    "unsupported_response",
]
