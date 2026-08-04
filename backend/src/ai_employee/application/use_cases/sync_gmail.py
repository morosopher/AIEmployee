"""协调 Gmail 只读分页、正文加密与单事务游标推进。"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from ai_employee.application.ports.encryption import EncryptedValue, Encryption
from ai_employee.application.ports.gmail import (
    GmailConnectionState,
    GmailMessage,
    GmailReader,
    GmailSyncPage,
    HistoryCursorExpiredError,
)


class GmailSyncStore(Protocol):
    """定义同步用例所需的最小事务存储，隔离 ORM 与测试 Fake。"""

    async def get_state(self, *, user_id: UUID, connection_id: UUID) -> GmailConnectionState | None: ...
    async def upsert_message(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        message: GmailMessage,
        encrypted_body: EncryptedValue,
    ) -> None: ...
    async def finish_sync(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        expected_cursor: str | None,
        latest_history_id: str,
        thread_count: int,
        message_count: int,
        used_full_resync: bool,
        completed_at: datetime,
    ) -> None: ...


class GmailSyncStoreFactory(Protocol):
    """为同步写入提供显式事务上下文。"""

    def __call__(self) -> AbstractAsyncContextManager[GmailSyncStore]: ...


class Clock(Protocol):
    """定义可替换 UTC 时钟，避免业务日期依赖宿主机本地时间。"""

    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class GmailSyncResult:
    """返回不含正文的同步结果计数，可用于 Worker 日志或任务结果。"""

    threads_upserted: int
    messages_upserted: int
    cursor: str
    used_full_resync: bool


class GmailConnectionNotFoundError(Exception):
    """表示连接不存在、不属于用户或不再处于可同步状态。"""


class SyncGmailUseCase:
    """在 HTTP 完成后将 Gmail 页一次性落库，保证游标和邮件事实共同提交。"""

    def __init__(
        self,
        stores: GmailSyncStoreFactory,
        cipher: Encryption,
        gmail: GmailReader,
        clock: Clock | None = None,
    ) -> None:
        """注入事务、加密器、只读端口与可控时间来源。"""
        self._stores = stores
        self._cipher = cipher
        self._gmail = gmail
        self._clock = clock

    async def execute(self, *, user_id: UUID, connection_id: UUID) -> GmailSyncResult:
        """同步有效 history，404 时回退七日初始页，再原子持久化全部页面。

        外部 Gmail I/O 被刻意置于数据库事务外，避免长事务占用锁或在回滚时重复网络调用；
        全部响应收集完才开启写事务，任何单条失败都会使邮件、审计和最终游标整体回滚。
        """
        async with self._stores() as store:
            state = await store.get_state(user_id=user_id, connection_id=connection_id)
        if state is None:
            raise GmailConnectionNotFoundError
        used_full_resync = state.cursor is None
        pages = await self._load_pages(state.cursor)
        if pages is None:
            used_full_resync = True
            pages = await self._collect_pages(self._gmail.initial_pages())
        latest_history_id = self._last_cursor(pages, state.cursor)
        thread_ids: set[str] = set()
        message_count = 0
        async with self._stores() as store:
            for page in pages:
                for message in page.messages:
                    encrypted = self._cipher.encrypt(
                        message.normalized_body.encode("utf-8"),
                        self._body_aad(user_id, connection_id, message.message_id),
                    )
                    await store.upsert_message(
                        user_id=user_id,
                        connection_id=connection_id,
                        message=message,
                        encrypted_body=encrypted,
                    )
                    thread_ids.add(message.thread_id)
                    message_count += 1
            await store.finish_sync(
                user_id=user_id,
                connection_id=connection_id,
                expected_cursor=state.cursor,
                latest_history_id=latest_history_id,
                thread_count=len(thread_ids),
                message_count=message_count,
                used_full_resync=used_full_resync,
                completed_at=self._now(),
            )
        return GmailSyncResult(len(thread_ids), message_count, latest_history_id, used_full_resync)

    async def _load_pages(self, cursor: str | None) -> tuple[GmailSyncPage, ...] | None:
        """读取 history 或首同步页；None 明确表示 Gmail 404 需要全量回退。"""
        if cursor is None:
            return await self._collect_pages(self._gmail.initial_pages())
        try:
            return await self._collect_pages(self._gmail.history_pages(cursor))
        except HistoryCursorExpiredError:
            return None

    @staticmethod
    async def _collect_pages(pages: AsyncIterator[GmailSyncPage]) -> tuple[GmailSyncPage, ...]:
        """在无数据库事务时收集有限供应商页，防止网络时间侵入持久化临界区。"""
        collected = [page async for page in pages]
        return tuple(collected)

    @staticmethod
    def _last_cursor(pages: tuple[GmailSyncPage, ...], fallback: str | None) -> str:
        """从最后一个有值页面取得最终历史游标，空同步保留既有游标。"""
        for page in reversed(pages):
            if page.latest_history_id:
                return page.latest_history_id
        return fallback or ""

    @staticmethod
    def _body_aad(user_id: UUID, connection_id: UUID, message_id: str) -> bytes:
        """生成 user、connection 和 Gmail message ID 绑定的正文 AAD。"""
        return f"{user_id}:{connection_id}:{message_id}:body".encode("ascii")

    def _now(self) -> datetime:
        """返回显式 UTC 完成时间，默认仅在组装层未注入时读取 UTC 时钟。"""
        now = self._clock.now() if self._clock is not None else datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("Gmail sync clock must return explicit UTC")
        return now.astimezone(UTC)
