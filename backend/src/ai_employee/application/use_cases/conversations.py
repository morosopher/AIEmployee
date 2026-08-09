"""定义受控对话意图、邮件草稿解析与原子消息创建应用端口。"""

import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ai_employee.application.use_cases.tasks import CreateTaskResult
from ai_employee.domain.mail_actions import MailMode, normalize_mailbox_address

UNSUPPORTED_RESPONSE = (
    "当前支持生成或查看每日简报，以及按明确请求准备可编辑的本地邮件草稿；"
    "不会直接发送邮件、执行其他外部写操作或进行通用规划。"
)

# 命令层只约束规范连字符文本，不臆造数据库 UUID 版本策略；实际值仍由 ``UUID`` 解析。
_UUID_TEXT = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ENGLISH_REPLY_COMMAND = re.compile(
    rf"(?i)^(?:(?:please|could you please|can you please)\s+)?"
    rf"prepare\s+(?:an?\s+)?(?:email|mail)\s+reply\s+to\s+thread\s+"
    rf"(?P<thread_id>{_UUID_TEXT})$"
)
_CHINESE_REPLY_COMMAND = re.compile(
    rf"^(?:(?:请|麻烦|请帮我|麻烦帮我)\s*)?"
    rf"准备\s*对线程\s*(?P<thread_id>{_UUID_TEXT})\s*的邮件回复$"
)
_ENGLISH_NEW_COMMAND = re.compile(
    r"(?i)^(?:(?:please|could you please|can you please)\s+)?"
    r"(?:prepare|draft|compose|create|write)\s+"
    r"(?:(?:an?|the)\s+)?(?:email|mail)(?:\s+draft)?"
    r"(?:\s+(?:to|for)\s+(?P<address>.+))?$"
)
_CHINESE_NEW_ADDRESS_FIRST_COMMAND = re.compile(
    r"^(?:(?:请|麻烦|请帮我|麻烦帮我)\s*)?"
    r"(?:给|为)\s*(?P<address>.+?)\s*"
    r"(?:起草|草拟|准备|创建)(?:一封)?邮件(?:草稿)?$"
)
_CHINESE_NEW_VERB_FIRST_COMMAND = re.compile(
    r"^(?:(?:请|麻烦|请帮我|麻烦帮我)\s*)?"
    r"(?:准备|创建|起草|草拟)(?:一封)?邮件(?:草稿)?"
    r"(?:(?:给|发给|为)\s*(?P<address>.+))?$"
)
_COMMAND_ENDINGS = frozenset(".!?。！？")


@dataclass(frozen=True, slots=True)
class MailDraftConversationRequest:
    """表示从明确整句命令确定性提取的新邮件或本地线程回复请求。

    ``to_recipients`` 只能来自新邮件命令中的一个完整合法 addr-spec；``source_thread_id``
    只能来自 reply 命令中的本地 UUID。模型不能补充地址、选择账户或绑定线程。
    """

    mode: MailMode
    to_recipients: tuple[str, ...] = ()
    source_thread_id: UUID | None = None


def parse_mail_draft_conversation_request(
    text: str,
) -> MailDraftConversationRequest | None:
    """仅把文档化的整句中英文命令解析为可编辑本地草稿意图。

    允许 ``please``/“请”等礼貌前缀，但拒绝否定、能力询问、说明性文本、直接发送和
    任意包含额外叙述的句子。新邮件地址作为完整 suffix 交给领域 addr-spec 解析器，
    因而 quoted local part 等合法语法不会被另一个更窄正则误删。回复语法固定为
    ``prepare an email reply to thread <uuid>`` 或“准备对线程 <uuid> 的邮件回复”。
    """
    if not isinstance(text, str):
        raise TypeError("conversation text must be a string")
    if "\r" in text or "\n" in text:
        return None
    command = text.strip()
    if not command:
        return None
    if command[-1] in _COMMAND_ENDINGS:
        command = command[:-1].rstrip()

    for pattern in (_ENGLISH_REPLY_COMMAND, _CHINESE_REPLY_COMMAND):
        match = pattern.fullmatch(command)
        if match is not None:
            return MailDraftConversationRequest(
                mode=MailMode.REPLY,
                source_thread_id=UUID(match.group("thread_id")),
            )

    for pattern in (
        _ENGLISH_NEW_COMMAND,
        _CHINESE_NEW_ADDRESS_FIRST_COMMAND,
        _CHINESE_NEW_VERB_FIRST_COMMAND,
    ):
        match = pattern.fullmatch(command)
        if match is None:
            continue
        candidate = match.groupdict().get("address")
        if candidate is None:
            return MailDraftConversationRequest(mode=MailMode.NEW)
        try:
            address = normalize_mailbox_address(candidate.strip())
        except ValueError:
            return None
        return MailDraftConversationRequest(
            mode=MailMode.NEW,
            to_recipients=(address,),
        )
    return None


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
