"""在真实 PostgreSQL 上验证 Gmail 同步的幂等、游标和加密不变量。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import func, select

from ai_employee.application.ports.gmail import (
    GmailMessage,
    GmailSyncPage,
    HistoryCursorExpiredError,
    UserActionRequiredError,
)
from ai_employee.application.use_cases.sync_gmail import SyncGmailUseCase
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyGmailSyncRepository
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher, EncryptedValue
from ai_employee.integrations.google.oauth import GoogleOAuthClient
from ai_employee.workers.sync_gmail import GmailSyncTaskStep


@dataclass(slots=True)
class FakeGmailReader:
    """以受控分页或 history 过期信号替代真实 Gmail 网络。"""

    initial: tuple[GmailSyncPage, ...]
    history: tuple[GmailSyncPage, ...] = ()
    history_expired: bool = False

    async def initial_pages(self) -> AsyncIterator[GmailSyncPage]:
        """返回合成初始页，模拟适配器已在事务外完成 HTTP。"""
        for page in self.initial:
            yield page

    async def history_pages(self, cursor: str) -> AsyncIterator[GmailSyncPage]:
        """按开关模拟有效增量或 Gmail 404 的 cursor 失效。"""
        del cursor
        if self.history_expired:
            raise HistoryCursorExpiredError
        for page in self.history:
            yield page


def _message(message_id: str = "message-1") -> GmailMessage:
    """创建不含真实个人数据、正文可验证加密的规范化邮件。"""
    return GmailMessage(
        message_id=message_id,
        thread_id="thread-1",
        history_id="102",
        received_at=datetime(2030, 1, 2, tzinfo=UTC),
        sender={"name": "Ada", "email": "ada@example.test"},
        recipients=[{"name": "Grace", "email": "grace@example.test"}],
        subject="Synthetic status",
        snippet="Synthetic",
        normalized_body="Synthetic private body",
        labels=("INBOX", "UNREAD"),
        headers={"from": "Ada <ada@example.test>"},
        provider_url="https://mail.google.com/mail/u/0/#all/thread-1",
    )


class FailingSecondMessageRepository(SqlAlchemyGmailSyncRepository):
    """在第二个消息写入后模拟持久化故障，证明最终 cursor 不会提前提交。"""

    def __init__(self, session) -> None:
        """初始化真实仓储和本次事务内消息计数。"""
        super().__init__(session)
        self._message_writes = 0

    async def upsert_message(self, **kwargs: object) -> None:
        """第一条先走真实写入，第二条前抛错使外围事务完整回滚。"""
        self._message_writes += 1
        if self._message_writes == 2:
            raise RuntimeError("synthetic second message persistence failure")
        await super().upsert_message(**kwargs)


class DisconnectBeforeFinishRepository(SqlAlchemyGmailSyncRepository):
    """模拟网络读取期间用户断开连接，验证最终写事务会重新确认可同步状态。"""

    async def finish_sync(self, **kwargs: object) -> None:
        """先将当前连接标为断开，再委托真实游标推进以触发状态复核。"""
        connection_id = kwargs["connection_id"]
        assert isinstance(connection_id, UUID)
        connection = await self._session.scalar(
            select(OAuthConnectionModel).where(OAuthConnectionModel.id == connection_id)
        )
        assert connection is not None
        connection.status = "disconnected"
        await super().finish_sync(**kwargs)


@asynccontextmanager
async def _repository_factory(sessions):
    """为每次同步建立真实提交/回滚事务，仓储本身不负责 commit。"""
    async with sessions.begin() as session:
        yield SqlAlchemyGmailSyncRepository(session)


async def _seed_connection(
    sessions, cipher: AeadCipher, cursor_value: str | None = None
) -> tuple[UUID, UUID]:
    """写入合成用户、连接、游标和最小凭据，返回用户与连接标识。"""
    async with sessions.begin() as session:
        user = UserModel(
            email="gmail-owner@example.test",
            display_name="Gmail Owner",
            password_hash=None,
            timezone="UTC",
            locale="zh-CN",
            brief_time=time(8, 0),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        connection = OAuthConnectionModel(
            user_id=user.id,
            provider="google",
            provider_account_id="synthetic-subject",
            account_email="gmail-owner@example.test",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            status="connected",
            last_error_code=None,
        )
        session.add(connection)
        await session.flush()
        access = cipher.encrypt(b"synthetic-access", f"{user.id}:{connection.id}:access_token".encode())
        refresh = cipher.encrypt(b"synthetic-refresh", f"{user.id}:{connection.id}:refresh_token".encode())
        session.add_all(
            [
                EncryptedCredentialModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    credential_kind="access_token",
                    ciphertext=access.ciphertext,
                    nonce=access.nonce,
                    key_version=access.key_version,
                    token_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                ),
                EncryptedCredentialModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    credential_kind="refresh_token",
                    ciphertext=refresh.ciphertext,
                    nonce=refresh.nonce,
                    key_version=refresh.key_version,
                    token_expires_at=None,
                ),
                SyncCursorModel(connection_id=connection.id, resource_kind="gmail", cursor=cursor_value),
            ]
        )
        return user.id, connection.id


@pytest.mark.asyncio
async def test_sync_upserts_messages_and_advances_cursor_only_after_final_page(database_url: str) -> None:
    """首同步写入一条线程/邮件，重放不重复，全部成功后才将 cursor 提交到末页值。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"a" * 32)
    try:
        user_id, connection_id = await _seed_connection(sessions, cipher)
        pages = (
            GmailSyncPage((_message(),), "next", "102"),
            GmailSyncPage((_message("message-2"),), None, "103"),
        )
        use_case = SyncGmailUseCase(
            lambda: _repository_factory(sessions),
            cipher,
            FakeGmailReader(initial=pages, history=pages),
        )

        first = await use_case.execute(user_id=user_id, connection_id=connection_id)
        second = await use_case.execute(user_id=user_id, connection_id=connection_id)

        assert first.messages_upserted == 2
        assert second.messages_upserted == 2
        async with sessions() as session:
            thread_count = await session.scalar(select(func.count()).select_from(EmailThreadModel))
            message_count = await session.scalar(select(func.count()).select_from(EmailMessageModel))
            cursor = await session.scalar(select(SyncCursorModel.cursor))
            stored = await session.scalar(select(EmailMessageModel).where(EmailMessageModel.provider_message_id == "message-1"))
            audit = await session.scalar(select(AuditEventModel).where(AuditEventModel.event_type == "source.gmail.synced"))
        assert thread_count == 1
        assert message_count == 2
        assert cursor == "103"
        assert stored is not None
        assert b"Synthetic private body" not in stored.body_ciphertext
        assert stored.snippet == ""
        assert cipher.decrypt(
            EncryptedValue(stored.body_ciphertext, stored.body_nonce, stored.body_key_version),
            f"{user_id}:{connection_id}:message-1:body".encode(),
        ) == b"Synthetic private body"
        assert audit is not None
        assert "body" not in str(audit.event_metadata).lower()
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_final_write_rejects_connection_disconnected_during_network_read(database_url: str) -> None:
    """连接在页获取后断开时，消息、审计与游标必须全部回滚而不写入旧用户域。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"e" * 32)
    try:
        user_id, connection_id = await _seed_connection(sessions, cipher)

        @asynccontextmanager
        async def disconnecting_factory():
            """使用真实单事务仓储，在 finish 前模拟并发断开。"""
            async with sessions.begin() as session:
                yield DisconnectBeforeFinishRepository(session)

        with pytest.raises(StateConflictError, match="connection"):
            await SyncGmailUseCase(
                disconnecting_factory,
                cipher,
                FakeGmailReader(initial=(GmailSyncPage((_message(),), None, "102"),)),
            ).execute(user_id=user_id, connection_id=connection_id)
        async with sessions() as session:
            cursor = await session.scalar(select(SyncCursorModel.cursor))
            message_count = await session.scalar(select(func.count()).select_from(EmailMessageModel))
            connection = await session.scalar(select(OAuthConnectionModel.status))
        assert cursor is None
        assert message_count == 0
        assert connection == "connected"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_expired_history_cursor_falls_back_to_seven_day_initial_sync(database_url: str) -> None:
    """Gmail 404 history cursor 后只回退初始七日读取，并提交该回退结果游标。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"b" * 32)
    try:
        user_id, connection_id = await _seed_connection(sessions, cipher, cursor_value="101")
        reader = FakeGmailReader(
            initial=(GmailSyncPage((_message(),), None, "200"),), history_expired=True
        )
        result = await SyncGmailUseCase(lambda: _repository_factory(sessions), cipher, reader).execute(
            user_id=user_id, connection_id=connection_id
        )
        assert result.used_full_resync
        async with sessions() as session:
            cursor = await session.scalar(select(SyncCursorModel.cursor))
        assert cursor == "200"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_cursor_is_not_advanced_when_a_later_page_write_fails(database_url: str) -> None:
    """任一末页邮件写入失败时，之前页的线程/邮件和最终 history cursor 必须一起回滚。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"d" * 32)
    try:
        user_id, connection_id = await _seed_connection(sessions, cipher)
        pages = (
            GmailSyncPage((_message(),), "next", "102"),
            GmailSyncPage((_message("message-2"),), None, "103"),
        )

        @asynccontextmanager
        async def failing_factory():
            """构造单次真实事务，第二条写入异常由 context manager 回滚。"""
            async with sessions.begin() as session:
                yield FailingSecondMessageRepository(session)

        with pytest.raises(RuntimeError, match="second message persistence failure"):
            await SyncGmailUseCase(
                failing_factory, cipher, FakeGmailReader(initial=pages)
            ).execute(user_id=user_id, connection_id=connection_id)
        async with sessions() as session:
            cursor = await session.scalar(select(SyncCursorModel.cursor))
            message_count = await session.scalar(select(func.count()).select_from(EmailMessageModel))
        assert cursor is None
        assert message_count == 0
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_access_token_rotation_is_atomic_and_preserves_refresh_when_omitted(database_url: str) -> None:
    """刷新更新 access 密文和 expiry；Google 未轮换 refresh token 时仍保留既有密文。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"c" * 32)
    try:
        user_id, connection_id = await _seed_connection(sessions, cipher)
        rotated = cipher.encrypt(b"rotated-access", f"{user_id}:{connection_id}:access_token".encode())
        async with _repository_factory(sessions) as repository:
            await repository.rotate_access_token(
                user_id=user_id,
                connection_id=connection_id,
                access_token=rotated,
                expires_at=datetime(2030, 1, 2, tzinfo=UTC),
                refresh_token=None,
            )
        async with sessions() as session:
            credentials = tuple((await session.scalars(select(EncryptedCredentialModel))).all())
        access = next(item for item in credentials if item.credential_kind == "access_token")
        refresh = next(item for item in credentials if item.credential_kind == "refresh_token")
        assert cipher.decrypt(EncryptedValue(access.ciphertext, access.nonce, access.key_version), f"{user_id}:{connection_id}:access_token".encode()) == b"rotated-access"
        assert cipher.decrypt(EncryptedValue(refresh.ciphertext, refresh.nonce, refresh.key_version), f"{user_id}:{connection_id}:refresh_token".encode()) == b"synthetic-refresh"
        assert access.token_expires_at == datetime(2030, 1, 2, tzinfo=UTC)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_worker_refreshes_once_then_marks_connection_expired_after_second_401(
    database_url: str,
) -> None:
    """Worker 刷新闭包必须原子更新 access token，二次 401 后再持久化连接过期状态。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"f" * 32)
    try:
        user_id, connection_id = await _seed_connection(sessions, cipher)
        token_route = respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "rotated-access", "expires_in": 3600}
            )
        )
        gmail_route = respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
            side_effect=[httpx.Response(401), httpx.Response(401)]
        )
        step = GmailSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=GoogleOAuthClient("synthetic-client", "synthetic-secret", "https://example.test/callback"),
        )

        with pytest.raises(UserActionRequiredError):
            await step.execute(
                LeasedTask(
                    task_id=UUID("00000000-0000-0000-0000-000000000001"),
                    kind="sync_gmail",
                    input_payload={"connection_id": str(connection_id)},
                    started_at=datetime(2030, 1, 1, tzinfo=UTC),
                    user_id=user_id,
                )
            )
        async with sessions() as session:
            connection = await session.scalar(select(OAuthConnectionModel))
            access = await session.scalar(
                select(EncryptedCredentialModel).where(
                    EncryptedCredentialModel.credential_kind == "access_token"
                )
            )
        assert token_route.called
        assert gmail_route.call_count == 2
        assert connection is not None and connection.status == "expired"
        assert access is not None
        assert cipher.decrypt(
            EncryptedValue(access.ciphertext, access.nonce, access.key_version),
            f"{user_id}:{connection_id}:access_token".encode(),
        ) == b"rotated-access"
    finally:
        await sessions.dispose()
