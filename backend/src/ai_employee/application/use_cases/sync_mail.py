"""协调供应商中立邮件分页、正文加密与单 scope 游标推进。"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from ai_employee.application.ports.encryption import EncryptedValue, Encryption
from ai_employee.application.ports.mail import (
    MailConnectionState,
    MailCursorExpiredError,
    MailMessage,
    MailMessageUpsertResult,
    MailReader,
    MailRemoval,
    MailSyncPage,
)
from ai_employee.domain.errors import InternalInvariantError


class MailSyncStore(Protocol):
    """定义单个邮件 scope 同步所需的最小事务存储。"""

    async def mark_directory_success(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        completed_at: datetime,
        folder_count: int,
    ) -> None: ...

    async def get_state(
        self, *, user_id: UUID, connection_id: UUID, scope_key: str
    ) -> MailConnectionState | None: ...

    async def clear_cursor(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        expected_cursor: str,
    ) -> None: ...

    async def upsert_message(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        message: MailMessage,
        encrypted_body: EncryptedValue,
    ) -> MailMessageUpsertResult: ...

    async def remove_message(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        removal: MailRemoval,
    ) -> None: ...

    async def finish_sync(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        expected_cursor: str | None,
        next_cursor: str,
        thread_count: int,
        message_count: int,
        removed_count: int,
        used_full_resync: bool,
        completed_at: datetime,
    ) -> None: ...


class MailSyncStoreFactory(Protocol):
    """为邮件读取前后提供彼此独立的短事务。"""

    def __call__(self) -> AbstractAsyncContextManager[MailSyncStore]: ...


class ReadAdapterRegistry(Protocol):
    """按连接 provider 返回固定邮件读取端口，不允许应用层导入具体适配器。"""

    def mail_reader(self, *, provider: str, connection_id: UUID, scope_key: str) -> MailReader: ...


class Clock(Protocol):
    """定义可替换 UTC 时钟，确保初始七日窗口可确定测试。"""

    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class MailSyncResult:
    """返回不含正文与 opaque 游标内容之外供应商字段的同步结果。"""

    threads_upserted: int
    messages_upserted: int
    next_cursor: str
    used_full_resync: bool
    messages_removed: int = 0

    @property
    def cursor(self) -> str:
        """返回 M1 ``GmailSyncResult.cursor`` 的只读兼容属性。"""
        return self.next_cursor


class MailConnectionNotFoundError(Exception):
    """表示连接不存在、归属不符、能力关闭或 scope 不可同步。"""


class SyncMailUseCase:
    """按连接 provider 选择读取适配器，并原子提交一个邮件 scope 的全部事实。"""

    def __init__(
        self,
        stores: MailSyncStoreFactory,
        registry: ReadAdapterRegistry,
        cipher: Encryption | None = None,
        clock: Clock | None = None,
    ) -> None:
        """注入事务、固定适配器注册表、可选加密器与可控 UTC 时钟。"""
        self._stores = stores
        self._registry = registry
        self._cipher = cipher
        self._clock = clock

    async def execute(
        self, *, user_id: UUID, connection_id: UUID, scope_key: str
    ) -> MailSyncResult:
        """读取一个 scope；游标失效时只清除该 scope 并回退最近七天。

        供应商 I/O 始终位于数据库事务外。Repository 先验证用户归属、连接状态和
        ``mail.read`` 能力，再把持久 provider 交给固定注册表；因此 Microsoft 连接不会
        被错误发送到 Google 适配器。任何写入或最终 CAS 失败都会回滚消息、审计和游标。
        """
        if scope_key == "":
            raise ValueError("scope_key must not be empty")
        async with self._stores() as store:
            state = await store.get_state(
                user_id=user_id,
                connection_id=connection_id,
                scope_key=scope_key,
            )
        if state is None:
            raise MailConnectionNotFoundError
        self._validate_state_scope(state, scope_key)
        reader = self._registry.mail_reader(
            provider=state.provider,
            connection_id=connection_id,
            scope_key=scope_key,
        )

        now = self._now()
        used_full_resync = state.cursor is None
        try:
            pages = await self._collect_pages(
                reader.initial_pages(scope_key, since=now - timedelta(days=7))
                if state.cursor is None
                else reader.sync_pages(scope_key, state.cursor)
            )
        except MailCursorExpiredError as error:
            if state.cursor is None:
                raise InternalInvariantError(
                    error_code="mail_initial_cursor_expired",
                    message="Mail initial sync cannot have an expired cursor",
                ) from error
            self._validate_expired_scope(error, state, scope_key)
            async with self._stores() as store:
                await store.clear_cursor(
                    user_id=user_id,
                    connection_id=connection_id,
                    scope_key=scope_key,
                    expected_cursor=state.cursor,
                )
            used_full_resync = True
            pages = await self._collect_pages(
                reader.initial_pages(scope_key, since=now - timedelta(days=7))
            )
            state = MailConnectionState(
                provider=state.provider,
                scope_key=scope_key,
                cursor=None,
            )

        next_cursor = self._last_cursor(pages, state.cursor)
        thread_ids: set[str] = set()
        message_count = 0
        removed_count = 0
        async with self._stores() as store:
            for page in pages:
                for message in page.messages:
                    if message.mailbox_scope_key != scope_key:
                        raise InternalInvariantError(
                            error_code="mail_message_scope_mismatch",
                            message="Mail message scope does not match sync scope",
                        )
                    encrypted = self._encrypt_body(user_id, connection_id, message)
                    upsert_result = await store.upsert_message(
                        user_id=user_id,
                        connection_id=connection_id,
                        message=message,
                        encrypted_body=encrypted,
                    )
                    if upsert_result == MailMessageUpsertResult.APPLIED:
                        thread_ids.add(message.provider_thread_id)
                        message_count += 1
                for removal in page.removals:
                    if removal.mailbox_scope_key != scope_key:
                        raise InternalInvariantError(
                            error_code="mail_removal_scope_mismatch",
                            message="Mail removal scope does not match sync scope",
                        )
                    await store.remove_message(
                        user_id=user_id,
                        connection_id=connection_id,
                        removal=removal,
                    )
                    removed_count += 1
            await store.finish_sync(
                user_id=user_id,
                connection_id=connection_id,
                scope_key=scope_key,
                expected_cursor=state.cursor,
                next_cursor=next_cursor,
                thread_count=len(thread_ids),
                message_count=message_count,
                removed_count=removed_count,
                used_full_resync=used_full_resync,
                completed_at=now,
            )
        return MailSyncResult(
            threads_upserted=len(thread_ids),
            messages_upserted=message_count,
            next_cursor=next_cursor,
            used_full_resync=used_full_resync,
            messages_removed=removed_count,
        )

    @staticmethod
    async def _collect_pages(pages: AsyncIterator[MailSyncPage]) -> tuple[MailSyncPage, ...]:
        """在无数据库事务时收集有限供应商页，避免网络时间侵入持久化临界区。"""
        return tuple([page async for page in pages])

    @staticmethod
    def _last_cursor(pages: tuple[MailSyncPage, ...], fallback: str | None) -> str:
        """从最后一个有效页面取得最终 scope 游标，空增量保留既有值。

        Raises:
            InternalInvariantError: 初始同步或回退同步没有返回任何有效最终游标。
        """
        for page in reversed(pages):
            next_cursor = page.next_cursor
            if next_cursor is not None and next_cursor != "":
                return next_cursor
        if fallback is not None and fallback != "":
            return fallback
        # 游标决定后续至少一次投递从何处恢复。缺失时必须在进入持久化事务前失败，
        # 不能把空字符串写成成功事实并让下一次同步落入不可解释状态。
        raise InternalInvariantError(
            error_code="mail_final_cursor_missing",
            message="Mail sync pages are missing a final cursor",
        )

    @staticmethod
    def _validate_state_scope(state: MailConnectionState, scope_key: str) -> None:
        """拒绝 Repository 返回连接级或其他 scope 的游标状态。"""
        if state.scope_key != scope_key:
            raise InternalInvariantError(
                error_code="mail_cursor_scope_mismatch",
                message="Mail cursor scope does not match requested scope",
            )

    @staticmethod
    def _validate_expired_scope(
        error: MailCursorExpiredError,
        state: MailConnectionState,
        scope_key: str,
    ) -> None:
        """只接受同一 provider/scope 的失效信号，防止清除其他恢复位置。"""
        if error.provider != state.provider or error.scope_key != scope_key:
            raise InternalInvariantError(
                error_code="mail_cursor_expiry_scope_mismatch",
                message="Mail cursor expiry does not match requested scope",
            ) from error

    def _encrypt_body(
        self, user_id: UUID, connection_id: UUID, message: MailMessage
    ) -> EncryptedValue:
        """使用消息记录绑定 AAD 加密正文；空页面测试可以省略加密器。"""
        if self._cipher is None:
            raise InternalInvariantError(
                error_code="mail_body_encryption_unavailable",
                message="Mail body encryption is unavailable",
            )
        return self._cipher.encrypt(
            message.sanitized_body.encode("utf-8"),
            f"{user_id}:{connection_id}:{message.provider_message_id}:body".encode("ascii"),
        )

    def _now(self) -> datetime:
        """返回显式 UTC 时间，拒绝宿主机本地或无时区时间。"""
        now = self._clock.now() if self._clock is not None else datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("Mail sync clock must return explicit UTC")
        return now.astimezone(UTC)


__all__ = [
    "MailConnectionNotFoundError",
    "MailSyncResult",
    "MailSyncStore",
    "MailSyncStoreFactory",
    "SyncMailUseCase",
]
