"""定义 Gmail 增量同步使用的不可变边界类型和已分类错误。"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)

__all__ = [
    "GmailConnectionState",
    "GmailMessage",
    "GmailReader",
    "GmailSyncPage",
    "HistoryCursorExpiredError",
    "TransientProviderError",
    "UserActionRequiredError",
]


@dataclass(frozen=True, slots=True)
class GmailMessage:
    """表示已去除供应商结构和危险内容的一封 Gmail 消息。

    正文已经过本地清洗但尚未持久化加密；调用方负责把它用消息记录绑定的 AAD 保护，
    适配器绝不接触数据库实体或原始 MIME。
    """

    message_id: str
    thread_id: str
    history_id: str
    received_at: datetime
    sender: Mapping[str, str]
    recipients: tuple[Mapping[str, str], ...]
    subject: str
    snippet: str
    normalized_body: str
    labels: tuple[str, ...]
    headers: Mapping[str, str]
    provider_url: str

    def __post_init__(self) -> None:
        """复制并冻结嵌套映射，避免适配器或测试调用方在消息创建后篡改同步事实。"""
        object.__setattr__(self, "sender", MappingProxyType(dict(self.sender)))
        object.__setattr__(
            self,
            "recipients",
            tuple(MappingProxyType(dict(recipient)) for recipient in self.recipients),
        )
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class GmailSyncPage:
    """表示一个已完成 message 获取的同步页及可恢复历史游标。"""

    messages: tuple[GmailMessage, ...]
    next_page_token: str | None
    latest_history_id: str


@dataclass(frozen=True, slots=True)
class GmailConnectionState:
    """表示已验证 Gmail 连接的上次成功 history cursor；``None`` 表示需要初始读取。"""

    cursor: str | None


class HistoryCursorExpiredError(PermanentProviderError):
    """表示 Gmail 已无法从指定 history cursor 提供增量变更，必须回退受限初始同步。"""

    def __init__(self) -> None:
        """构造不含供应商正文的稳定 history 过期错误码。"""
        super().__init__(
            error_code="google_history_cursor_expired",
            message="Google Gmail history cursor expired",
        )


class GmailReader(Protocol):
    """定义只读 Gmail 分页和刷新感知执行边界，供同步用例注入 Fake。"""

    def initial_pages(self) -> AsyncIterator[GmailSyncPage]:
        """产生初次同步的所有七日内消息页。"""
        ...

    def history_pages(self, cursor: str) -> AsyncIterator[GmailSyncPage]:
        """从给定 history cursor 产生增量变更页。"""
        ...

    async def execute_request(self, path: str, parameters: dict[str, str]) -> object:
        """执行会在首个 401 后刷新并最多重试一次的只读 Gmail 请求。"""
        ...
