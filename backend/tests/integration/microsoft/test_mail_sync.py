"""在隔离 PostgreSQL 上验证 Microsoft 邮件 Delta 的持久化、幂等与恢复边界。

测试只使用 ``respx`` 合成 Graph 响应和 ``.example.test`` 身份，覆盖用户/连接隔离、
folder 级游标 CAS、tombstone 精确删除、cursor expiry 回退以及 Worker 的一次刷新语义。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from types import ModuleType
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import func, select

from ai_employee.application.ports.mail import MailMessage, MailRemoval, MailScope, MailSyncPage
from ai_employee.application.use_cases.sync_mail import (
    MailConnectionNotFoundError,
    SyncMailUseCase,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyMailSyncRepository
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher, EncryptedValue
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter
from ai_employee.integrations.registry import ProviderAdapterRegistry
from ai_employee.workers import sync_mail as sync_mail_worker
from ai_employee.workers.sync_mail import MailSyncTaskStep, build_mail_sync_task_step

FIXTURE_DIR = Path(__file__).parents[2] / "contract" / "microsoft" / "fixtures"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MAIL_FOLDERS_URL = f"{GRAPH_BASE_URL}/me/mailFolders"
INBOX_DELTA_URL = f"{GRAPH_BASE_URL}/me/mailFolders/synthetic-folder-inbox/messages/delta"
INBOX_NEXT_URL = f"{INBOX_DELTA_URL}?$skiptoken=synthetic-next-1"
INBOX_DELTA_LINK = f"{INBOX_DELTA_URL}?$deltatoken=synthetic-delta-2"
MAIL_SELECT = (
    "id,conversationId,internetMessageId,from,toRecipients,ccRecipients,bccRecipients,"
    "subject,body,receivedDateTime,sentDateTime,lastModifiedDateTime,categories,webLink"
)
INITIAL_PARAMS = {
    "$filter": "receivedDateTime ge 2030-01-01T12:00:00Z",
    "$select": MAIL_SELECT,
}

USER_ONE = UUID("10000000-0000-0000-0000-000000000001")
USER_TWO = UUID("20000000-0000-0000-0000-000000000002")
CONNECTION_ONE = UUID("10000000-0000-0000-0000-000000000011")
CONNECTION_TWO = UUID("20000000-0000-0000-0000-000000000022")


def _mail_module() -> ModuleType:
    """延迟导入待实现 adapter，保持 RED 由测试调用触发。"""
    return importlib.import_module("ai_employee.integrations.microsoft.mail")


def _fixture(name: str) -> dict[str, object]:
    """读取并复制固定合成 Graph fixture。"""
    payload = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _message(
    message_id: str,
    *,
    scope_key: str,
    thread_id: str | None = None,
    subject: str = "Synthetic subject",
    body: str = "Synthetic body",
    received_at: datetime | None = None,
    provider_updated_at: datetime | None = None,
) -> MailMessage:
    """构造可改变 thread/scope/metadata 的合成规范消息。"""
    resolved_thread_id = thread_id or f"synthetic-thread-{message_id}"
    return MailMessage(
        provider_message_id=message_id,
        provider_thread_id=resolved_thread_id,
        provider_conversation_id=f"synthetic-conversation-{resolved_thread_id}",
        internet_message_id=f"<{message_id}@example.test>",
        mailbox_scope_key=scope_key,
        sender={"name": "Synthetic Sender", "email": "sender@example.test"},
        recipients=({"name": "Synthetic Recipient", "email": "recipient@example.test"},),
        subject=subject,
        sanitized_body=body,
        received_at=received_at or datetime(2030, 1, 8, tzinfo=UTC),
        sent_at=datetime(2030, 1, 8, tzinfo=UTC),
        provider_updated_at=provider_updated_at,
        labels=("Synthetic",),
        normalized_reply_headers={"from": "sender@example.test"},
        provider_url=f"https://outlook.office.example.test/mail/{resolved_thread_id}",
    )


@dataclass(frozen=True, slots=True)
class _FixedClock:
    """为七日回退注入确定 UTC 当前时刻。"""

    value: datetime

    def now(self) -> datetime:
        """返回已验证的 UTC 时间。"""
        return self.value


@dataclass(slots=True)
class _BlockingMailReader:
    """在旧 cursor 读取后暂停，构造并发新 cursor 提交竞态。"""

    loaded: asyncio.Event
    release: asyncio.Event
    page: MailSyncPage

    async def list_sync_scopes(self) -> tuple[object, ...]:
        """竞态测试不重复验证目录发现。"""
        return ()

    async def initial_pages(
        self, scope_key: str, *, since: datetime
    ) -> AsyncIterator[MailSyncPage]:
        """竞态路径不走初始同步。"""
        del scope_key, since
        if False:
            yield self.page

    async def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """确认旧 cursor 后等待放行，再返回旧 Worker 读到的页面。"""
        assert scope_key == "synthetic-folder-inbox"
        assert cursor == "synthetic-delta-old"
        self.loaded.set()
        await self.release.wait()
        yield self.page


@dataclass(slots=True)
class _SinglePageMailReader:
    """返回一页固定增量，用于验证 stale skip 与 cursor CAS 相互独立。"""

    page: MailSyncPage
    expected_cursor: str

    async def list_sync_scopes(self) -> tuple[object, ...]:
        """该场景已有精确 folder scope，不重复发现目录。"""
        return ()

    async def initial_pages(
        self, scope_key: str, *, since: datetime
    ) -> AsyncIterator[MailSyncPage]:
        """测试只走已有 cursor 的增量分支。"""
        del scope_key, since
        if False:
            yield self.page

    async def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """返回供应商已确认消费的一页迟到 projection。"""
        assert scope_key == "synthetic-folder-inbox"
        assert cursor == self.expected_cursor
        yield self.page


@dataclass(slots=True)
class _BudgetFailingMailReader:
    """先产生一页消息再以固定预算错误终止，验证收集与事务边界。"""

    first_page: MailSyncPage
    expected_cursor: str

    async def list_sync_scopes(self) -> tuple[object, ...]:
        """该场景已有 folder cursor，不重复读取目录。"""
        return ()

    async def initial_pages(
        self, scope_key: str, *, since: datetime
    ) -> AsyncIterator[MailSyncPage]:
        """测试只走已有 cursor 的增量路径。"""
        del scope_key, since
        if False:
            yield self.first_page

    async def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """暴露第一页后模拟后续响应超过链预算。"""
        assert scope_key == "synthetic-folder-inbox"
        assert cursor == self.expected_cursor
        yield self.first_page
        raise PermanentProviderError(
            error_code="microsoft_mail_sync_budget_exceeded",
            message="Microsoft mail sync budget was exceeded",
        )


@dataclass(slots=True)
class _DiscoveringMailReader:
    """模拟目录发现结果，验证 worker 不会把 OAuth 占位 scope 发给 Graph。"""

    calls: list[tuple[str, str]]

    async def list_sync_scopes(self) -> tuple[MailScope, ...]:
        """返回两个可独立恢复的合成 folder，并记录目录调用。"""
        self.calls.append(("discover", ""))
        return (
            MailScope("synthetic-folder-sent", "Synthetic Sent", "sentitems"),
            MailScope("synthetic-folder-inbox", "Synthetic Inbox", "inbox"),
        )

    async def initial_pages(
        self, scope_key: str, *, since: datetime
    ) -> AsyncIterator[MailSyncPage]:
        """每个 folder 返回独立消息和 cursor；占位 mailbox 若被调用则测试失败。"""
        del since
        self.calls.append(("initial", scope_key))
        if scope_key == "mailbox":
            raise AssertionError("Microsoft placeholder scope must not be sent to Graph")
        yield MailSyncPage(
            (_message(f"{scope_key}-message", scope_key=scope_key),),
            None,
            f"synthetic-cursor-{scope_key}",
        )

    async def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """重复投递使用各自 cursor，保持同一 folder 的幂等事实。"""
        self.calls.append(("sync", f"{scope_key}:{cursor}"))
        yield MailSyncPage((), None, cursor)


@dataclass(slots=True)
class _PartiallyFailingMailReader:
    """让一个 folder 暂态失败、另一个 folder 成功，验证 owner 的部分提交语义。"""

    calls: list[tuple[str, str]]

    async def list_sync_scopes(self) -> tuple[MailScope, ...]:
        """返回两个真实 folder；owner 应按稳定 key 顺序处理。"""
        self.calls.append(("discover", ""))
        return (
            MailScope("synthetic-folder-sent", "Synthetic Sent", "sentitems"),
            MailScope("synthetic-folder-inbox", "Synthetic Inbox", "inbox"),
        )

    async def initial_pages(
        self, scope_key: str, *, since: datetime
    ) -> AsyncIterator[MailSyncPage]:
        """inbox 抛出暂态错误，sent 返回可提交的合成页面。"""
        del since
        self.calls.append(("initial", scope_key))
        if scope_key == "synthetic-folder-inbox":
            raise TransientProviderError(
                error_code="synthetic_folder_failure",
                message="synthetic folder failure",
            )
        yield MailSyncPage(
            (_message("synthetic-partial-message", scope_key=scope_key),),
            None,
            "synthetic-partial-cursor",
        )

    async def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """该场景只覆盖初始目录同步；不应读取已有 Delta。"""
        del scope_key, cursor
        if False:
            yield MailSyncPage((), None, None)


@asynccontextmanager
async def _repository_factory(sessions):
    """把真实 SQLAlchemy 仓储放入用例拥有的事务上下文。"""
    from ai_employee.infrastructure.db.repositories.email import SqlAlchemyMailSyncRepository

    async with sessions.begin() as session:
        yield SqlAlchemyMailSyncRepository(session)


async def _upsert_repository_message(
    sessions: ManagedAsyncSessionMaker,
    cipher: AeadCipher,
    *,
    user_id: UUID,
    connection_id: UUID,
    message: MailMessage,
) -> None:
    """在独立事务中执行一次真实 repository upsert，供重复/并发投递测试。"""
    encrypted_body = cipher.encrypt(
        message.sanitized_body.encode("utf-8"),
        f"{user_id}:{connection_id}:{message.provider_message_id}:body".encode("ascii"),
    )
    async with sessions.begin() as session:
        await SqlAlchemyMailSyncRepository(session).upsert_message(
            user_id=user_id,
            connection_id=connection_id,
            message=message,
            encrypted_body=encrypted_body,
        )


async def _seed_connection(
    sessions,
    cipher: AeadCipher,
    *,
    user_id: UUID,
    connection_id: UUID,
    scope_cursors: dict[str, str | None],
) -> None:
    """写入 Microsoft 合成用户、连接、能力、凭据和精确 folder 游标。"""
    async with sessions.begin() as session:
        user = UserModel(
            id=user_id,
            email=f"owner-{user_id.hex[:8]}@example.test",
            display_name="Synthetic Microsoft Owner",
            password_hash=None,
            timezone="UTC",
            locale="zh-CN",
            brief_time=time(8, 0),
            is_active=True,
        )
        session.add(user)
        connection = OAuthConnectionModel(
            id=connection_id,
            user_id=user_id,
            provider="microsoft",
            provider_account_id="synthetic-tenant:synthetic-graph-user",
            provider_tenant_id="synthetic-tenant",
            account_type="work_school",
            account_email="owner@example.test",
            scopes=["Mail.Read", "User.Read", "offline_access"],
            status="connected",
            last_error_code=None,
        )
        session.add(connection)
        # 先把父行 flush，确保异步 PostgreSQL 在同一批量 INSERT 中不会先校验凭据外键。
        await session.flush()
        access = cipher.encrypt(
            b"synthetic-access", f"{user_id}:{connection_id}:access_token".encode("ascii")
        )
        refresh = cipher.encrypt(
            b"synthetic-refresh", f"{user_id}:{connection_id}:refresh_token".encode("ascii")
        )
        session.add_all(
            [
                EncryptedCredentialModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    credential_kind="access_token",
                    ciphertext=access.ciphertext,
                    nonce=access.nonce,
                    key_version=access.key_version,
                    token_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                ),
                EncryptedCredentialModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    credential_kind="refresh_token",
                    ciphertext=refresh.ciphertext,
                    nonce=refresh.nonce,
                    key_version=refresh.key_version,
                    token_expires_at=None,
                ),
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability="mail.read",
                    status="enabled",
                    actual_scopes=["Mail.Read"],
                ),
            ]
        )
        for scope_key, cursor in scope_cursors.items():
            session.add(
                SyncCursorModel(
                    connection_id=connection_id,
                    resource_kind="mail",
                    scope_key=scope_key,
                    cursor=cursor,
                )
            )


def _adapter(**kwargs: object) -> object:
    """构造真实 HTTP adapter；模块缺失时让 RED 在测试体内失败。"""
    return _mail_module().MicrosoftMailAdapter(  # type: ignore[attr-defined]
        access_token="synthetic-access-token",
        **kwargs,
    )


@pytest.mark.asyncio
@respx.mock
async def test_initial_delta_persists_messages_scope_cursor_and_idempotent_replay(
    database_url: str,
) -> None:
    """初始/增量重放只生成一份 message/thread，并保存最终 folder deltaLink。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"m" * 32)
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={"synthetic-folder-inbox": None},
        )
        first_payload = _fixture("mail_delta_initial.json")
        incremental_payload = _fixture("mail_delta_incremental.json")
        respx.get(INBOX_DELTA_URL, params=INITIAL_PARAMS).respond(200, json=first_payload)
        respx.get(INBOX_NEXT_URL).respond(200, json=incremental_payload)
        respx.get(INBOX_DELTA_LINK).respond(200, json=incremental_payload)
        adapter = _adapter()
        use_case = SyncMailUseCase(
            lambda: _repository_factory(sessions),
            ProviderAdapterRegistry(microsoft_mail=adapter),  # type: ignore[arg-type]
            cipher,
            clock=_FixedClock(datetime(2030, 1, 8, 12, tzinfo=UTC)),
        )

        first = await use_case.execute(
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_key="synthetic-folder-inbox",
        )
        second = await use_case.execute(
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_key="synthetic-folder-inbox",
        )

        assert first.used_full_resync
        assert first.next_cursor == INBOX_DELTA_LINK
        assert not second.used_full_resync
        assert second.next_cursor == INBOX_DELTA_LINK
        async with sessions() as session:
            message_count = await session.scalar(
                select(func.count()).select_from(EmailMessageModel)
            )
            thread_count = await session.scalar(select(func.count()).select_from(EmailThreadModel))
            cursor = await session.scalar(
                select(SyncCursorModel).where(
                    SyncCursorModel.connection_id == CONNECTION_ONE,
                    SyncCursorModel.scope_key == "synthetic-folder-inbox",
                )
            )
            stored = await session.scalar(
                select(EmailMessageModel).where(
                    EmailMessageModel.provider_message_id == "synthetic-message-2"
                )
            )
        assert message_count == 2
        assert thread_count == 1
        assert cursor is not None and cursor.cursor == INBOX_DELTA_LINK
        assert stored is not None
        assert stored.mailbox_scope_key == "synthetic-folder-inbox"
        assert stored.snippet == ""
        assert stored.body_ciphertext is not None
        assert b"Synthetic incremental body" not in stored.body_ciphertext
        assert (
            cipher.decrypt(
                EncryptedValue(stored.body_ciphertext, stored.body_nonce, stored.body_key_version),
                f"{USER_ONE}:{CONNECTION_ONE}:synthetic-message-2:body".encode("ascii"),
            )
            == b"Synthetic incremental body"
        )
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_same_connection_message_replay_moves_one_fact_to_latest_thread_and_scope(
    database_url: str,
) -> None:
    """同一 ImmutableId 改变 thread/scope 后只能更新一条连接级消息事实。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"s" * 32)
    message_id = "synthetic-immutable-message"
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={},
        )
        first = _message(
            message_id,
            thread_id="synthetic-thread-before-move",
            scope_key="synthetic-folder-inbox",
            subject="Synthetic subject before move",
            body="Synthetic body before move",
            received_at=datetime(2030, 1, 8, 9, tzinfo=UTC),
        )
        latest = _message(
            message_id,
            thread_id="synthetic-thread-after-move",
            scope_key="synthetic-folder-archive",
            subject="Synthetic subject after move",
            body="Synthetic body after move",
            received_at=datetime(2030, 1, 8, 10, tzinfo=UTC),
        )

        await _upsert_repository_message(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            message=first,
        )
        await _upsert_repository_message(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            message=latest,
        )

        async with sessions() as session:
            rows = tuple(
                (
                    await session.execute(
                        select(EmailMessageModel, EmailThreadModel.provider_thread_id)
                        .join(EmailThreadModel, EmailThreadModel.id == EmailMessageModel.thread_id)
                        .where(
                            EmailThreadModel.connection_id == CONNECTION_ONE,
                            EmailMessageModel.provider_message_id == message_id,
                        )
                    )
                ).all()
            )

        assert len(rows) == 1
        stored, provider_thread_id = rows[0]
        assert provider_thread_id == "synthetic-thread-after-move"
        assert stored.mailbox_scope_key == "synthetic-folder-archive"
        assert stored.subject == "Synthetic subject after move"
        assert stored.received_at == datetime(2030, 1, 8, 10, tzinfo=UTC)
        assert stored.body_ciphertext is not None
        assert stored.body_nonce is not None
        assert stored.body_key_version is not None
        assert (
            cipher.decrypt(
                EncryptedValue(
                    stored.body_ciphertext,
                    stored.body_nonce,
                    stored.body_key_version,
                ),
                f"{USER_ONE}:{CONNECTION_ONE}:{message_id}:body".encode("ascii"),
            )
            == b"Synthetic body after move"
        )
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_first", (False, True))
async def test_provider_version_keeps_newest_message_projection_for_both_commit_orders(
    database_url: str,
    newer_first: bool,
) -> None:
    """较旧 Graph projection 即使后提交，也不得覆盖较新 thread/scope/body。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"v" * 32)
    message_id = "synthetic-versioned-message"
    older = _message(
        message_id,
        thread_id="synthetic-versioned-thread",
        scope_key="synthetic-folder-inbox",
        subject="Synthetic older subject",
        body="Synthetic older body",
        received_at=datetime(2030, 1, 8, 9, tzinfo=UTC),
        provider_updated_at=datetime(2030, 1, 8, 9, 5, tzinfo=UTC),
    )
    newer = _message(
        message_id,
        thread_id="synthetic-versioned-thread",
        scope_key="synthetic-folder-archive",
        subject="Synthetic newer subject",
        body="Synthetic newer body",
        received_at=datetime(2030, 1, 8, 10, tzinfo=UTC),
        provider_updated_at=datetime(2030, 1, 8, 10, 5, tzinfo=UTC),
    )
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={},
        )
        for message in ((newer, older) if newer_first else (older, newer)):
            await _upsert_repository_message(
                sessions,
                cipher,
                user_id=USER_ONE,
                connection_id=CONNECTION_ONE,
                message=message,
            )

        async with sessions() as session:
            stored = await session.scalar(
                select(EmailMessageModel).where(
                    EmailMessageModel.connection_id == CONNECTION_ONE,
                    EmailMessageModel.provider_message_id == message_id,
                )
            )
            thread = await session.scalar(
                select(EmailThreadModel).where(
                    EmailThreadModel.connection_id == CONNECTION_ONE,
                    EmailThreadModel.provider_thread_id == "synthetic-versioned-thread",
                )
            )

        assert stored is not None and thread is not None
        assert stored.provider_updated_at == datetime(2030, 1, 8, 10, 5, tzinfo=UTC)
        assert stored.mailbox_scope_key == "synthetic-folder-archive"
        assert stored.subject == "Synthetic newer subject"
        assert thread.subject == "Synthetic newer subject"
        assert thread.latest_message_at == datetime(2030, 1, 8, 10, tzinfo=UTC)
        assert stored.body_ciphertext is not None
        assert (
            cipher.decrypt(
                EncryptedValue(
                    stored.body_ciphertext,
                    stored.body_nonce,
                    stored.body_key_version,
                ),
                f"{USER_ONE}:{CONNECTION_ONE}:{message_id}:body".encode("ascii"),
            )
            == b"Synthetic newer body"
        )
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_equal_provider_version_does_not_replace_existing_projection(
    database_url: str,
) -> None:
    """相同 Graph 版本视为幂等重放，冲突 projection 保留先前已提交事实。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"e" * 32)
    message_id = "synthetic-equal-version-message"
    version = datetime(2030, 1, 8, 11, 5, tzinfo=UTC)
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={},
        )
        first = _message(
            message_id,
            thread_id="synthetic-equal-thread",
            scope_key="synthetic-folder-inbox",
            subject="Synthetic first equal subject",
            body="Synthetic first equal body",
            provider_updated_at=version,
        )
        conflicting = _message(
            message_id,
            thread_id="synthetic-equal-thread",
            scope_key="synthetic-folder-archive",
            subject="Synthetic conflicting equal subject",
            body="Synthetic conflicting equal body",
            provider_updated_at=version,
        )
        for message in (first, conflicting):
            await _upsert_repository_message(
                sessions,
                cipher,
                user_id=USER_ONE,
                connection_id=CONNECTION_ONE,
                message=message,
            )

        async with sessions() as session:
            stored = await session.scalar(
                select(EmailMessageModel).where(
                    EmailMessageModel.connection_id == CONNECTION_ONE,
                    EmailMessageModel.provider_message_id == message_id,
                )
            )
        assert stored is not None
        assert stored.mailbox_scope_key == "synthetic-folder-inbox"
        assert stored.subject == "Synthetic first equal subject"
        assert stored.body_ciphertext is not None
        assert (
            cipher.decrypt(
                EncryptedValue(
                    stored.body_ciphertext,
                    stored.body_nonce,
                    stored.body_key_version,
                ),
                f"{USER_ONE}:{CONNECTION_ONE}:{message_id}:body".encode("ascii"),
            )
            == b"Synthetic first equal body"
        )
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_stale_projection_skip_still_advances_confirmed_folder_cursor(
    database_url: str,
) -> None:
    """迟到消息被跳过时仍推进供应商确认的 Delta cursor，避免永久重放同一页。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"k" * 32)
    message_id = "synthetic-stale-cursor-message"
    current_cursor = "synthetic-current-cursor"
    next_cursor = "synthetic-next-cursor"
    newer = _message(
        message_id,
        scope_key="synthetic-folder-archive",
        subject="Synthetic current projection",
        body="Synthetic current body",
        provider_updated_at=datetime(2030, 1, 8, 12, 5, tzinfo=UTC),
    )
    stale = _message(
        message_id,
        scope_key="synthetic-folder-inbox",
        subject="Synthetic stale projection",
        body="Synthetic stale body",
        provider_updated_at=datetime(2030, 1, 8, 11, 5, tzinfo=UTC),
    )
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={"synthetic-folder-inbox": current_cursor},
        )
        await _upsert_repository_message(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            message=newer,
        )
        reader = _SinglePageMailReader(
            MailSyncPage((stale,), None, next_cursor),
            current_cursor,
        )
        result = await SyncMailUseCase(
            lambda: _repository_factory(sessions),
            ProviderAdapterRegistry(microsoft_mail=reader),
            cipher,
            clock=_FixedClock(datetime(2030, 1, 8, 13, tzinfo=UTC)),
        ).execute(
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_key="synthetic-folder-inbox",
        )

        async with sessions() as session:
            stored = await session.scalar(
                select(EmailMessageModel).where(
                    EmailMessageModel.connection_id == CONNECTION_ONE,
                    EmailMessageModel.provider_message_id == message_id,
                )
            )
            cursor = await session.scalar(
                select(SyncCursorModel).where(
                    SyncCursorModel.connection_id == CONNECTION_ONE,
                    SyncCursorModel.scope_key == "synthetic-folder-inbox",
                )
            )
        assert result.messages_upserted == 0
        assert stored is not None and stored.subject == "Synthetic current projection"
        assert stored.mailbox_scope_key == "synthetic-folder-archive"
        assert cursor is not None and cursor.cursor == next_cursor
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_budget_failure_after_first_page_does_not_persist_or_advance_cursor(
    database_url: str,
) -> None:
    """后续页预算失败时，预先产生的消息与 folder cursor 都不得形成持久事实。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"b" * 32)
    current_cursor = "synthetic-budget-current-cursor"
    sensitive_body = "Synthetic budget body that must not persist"
    reader = _BudgetFailingMailReader(
        first_page=MailSyncPage(
            (
                _message(
                    "synthetic-budget-message",
                    scope_key="synthetic-folder-inbox",
                    body=sensitive_body,
                    provider_updated_at=datetime(2030, 1, 8, 12, 5, tzinfo=UTC),
                ),
            ),
            "synthetic-budget-next-page",
            None,
        ),
        expected_cursor=current_cursor,
    )
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={"synthetic-folder-inbox": current_cursor},
        )
        use_case = SyncMailUseCase(
            lambda: _repository_factory(sessions),
            ProviderAdapterRegistry(microsoft_mail=reader),
            cipher,
            clock=_FixedClock(datetime(2030, 1, 8, 13, tzinfo=UTC)),
        )

        with pytest.raises(PermanentProviderError) as raised:
            await use_case.execute(
                user_id=USER_ONE,
                connection_id=CONNECTION_ONE,
                scope_key="synthetic-folder-inbox",
            )

        async with sessions() as session:
            message_count = await session.scalar(
                select(func.count()).select_from(EmailMessageModel)
            )
            thread_count = await session.scalar(select(func.count()).select_from(EmailThreadModel))
            cursor = await session.scalar(
                select(SyncCursorModel).where(
                    SyncCursorModel.connection_id == CONNECTION_ONE,
                    SyncCursorModel.scope_key == "synthetic-folder-inbox",
                )
            )
        assert raised.value.error_code == "microsoft_mail_sync_budget_exceeded"
        assert sensitive_body not in str(raised.value)
        assert message_count == 0
        assert thread_count == 0
        assert cursor is not None and cursor.cursor == current_cursor
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_different_connections_may_keep_the_same_provider_message_id(
    database_url: str,
) -> None:
    """连接级唯一性不能错误扩成供应商 ID 的全局唯一性。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"t" * 32)
    message_id = "synthetic-shared-provider-message"
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={},
        )
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_TWO,
            connection_id=CONNECTION_TWO,
            scope_cursors={},
        )
        await _upsert_repository_message(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            message=_message(
                message_id,
                thread_id="synthetic-thread-connection-one",
                scope_key="synthetic-folder-inbox",
            ),
        )
        await _upsert_repository_message(
            sessions,
            cipher,
            user_id=USER_TWO,
            connection_id=CONNECTION_TWO,
            message=_message(
                message_id,
                thread_id="synthetic-thread-connection-two",
                scope_key="synthetic-folder-inbox",
            ),
        )

        async with sessions() as session:
            counts = tuple(
                (
                    await session.execute(
                        select(EmailThreadModel.connection_id, func.count(EmailMessageModel.id))
                        .join(EmailMessageModel, EmailMessageModel.thread_id == EmailThreadModel.id)
                        .where(EmailMessageModel.provider_message_id == message_id)
                        .group_by(EmailThreadModel.connection_id)
                        .order_by(EmailThreadModel.connection_id)
                    )
                ).all()
            )

        assert dict(counts) == {CONNECTION_ONE: 1, CONNECTION_TWO: 1}
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_connection_replays_are_serialized_by_database_identity(
    database_url: str,
) -> None:
    """两事务同时写不同 thread 投影时仍由数据库唯一约束收敛为一行。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"u" * 32)
    message_id = "synthetic-concurrent-immutable-message"
    start = asyncio.Event()
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={},
        )

        async def write_projection(*, thread_id: str, scope_key: str) -> None:
            """在共同起跑信号后用独立事务写入一个完整合成投影。"""
            await start.wait()
            await _upsert_repository_message(
                sessions,
                cipher,
                user_id=USER_ONE,
                connection_id=CONNECTION_ONE,
                message=_message(
                    message_id,
                    thread_id=thread_id,
                    scope_key=scope_key,
                    subject=f"Synthetic concurrent {thread_id}",
                ),
            )

        first = asyncio.create_task(
            write_projection(
                thread_id="synthetic-thread-concurrent-a",
                scope_key="synthetic-folder-inbox",
            )
        )
        second = asyncio.create_task(
            write_projection(
                thread_id="synthetic-thread-concurrent-b",
                scope_key="synthetic-folder-archive",
            )
        )
        start.set()
        await asyncio.gather(first, second)

        async with sessions() as session:
            rows = tuple(
                (
                    await session.execute(
                        select(EmailThreadModel.provider_thread_id)
                        .join(EmailMessageModel, EmailMessageModel.thread_id == EmailThreadModel.id)
                        .where(
                            EmailThreadModel.connection_id == CONNECTION_ONE,
                            EmailMessageModel.provider_message_id == message_id,
                        )
                    )
                ).scalars()
            )

        assert len(rows) == 1
        assert rows[0] in {
            "synthetic-thread-concurrent-a",
            "synthetic-thread-concurrent-b",
        }
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_tombstone_requires_exact_user_connection_scope_and_message_id(
    database_url: str,
) -> None:
    """错 scope tombstone 不得删除，精确删除也不能影响另一连接的同 ID。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"v" * 32)
    message_id = "synthetic-tombstone-identity"
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={},
        )
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_TWO,
            connection_id=CONNECTION_TWO,
            scope_cursors={},
        )
        await _upsert_repository_message(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            message=_message(
                message_id,
                thread_id="synthetic-thread-tombstone-one",
                scope_key="synthetic-folder-archive",
            ),
        )
        await _upsert_repository_message(
            sessions,
            cipher,
            user_id=USER_TWO,
            connection_id=CONNECTION_TWO,
            message=_message(
                message_id,
                thread_id="synthetic-thread-tombstone-two",
                scope_key="synthetic-folder-inbox",
            ),
        )

        async with sessions.begin() as session:
            repository = SqlAlchemyMailSyncRepository(session)
            await repository.remove_message(
                user_id=USER_ONE,
                connection_id=CONNECTION_ONE,
                removal=MailRemoval(message_id, "synthetic-folder-inbox"),
            )
        async with sessions() as session:
            after_wrong_scope = await session.scalar(
                select(func.count())
                .select_from(EmailMessageModel)
                .where(EmailMessageModel.provider_message_id == message_id)
            )
        assert after_wrong_scope == 2

        async with sessions.begin() as session:
            repository = SqlAlchemyMailSyncRepository(session)
            await repository.remove_message(
                user_id=USER_ONE,
                connection_id=CONNECTION_ONE,
                removal=MailRemoval(message_id, "synthetic-folder-archive"),
            )
        async with sessions() as session:
            remaining_connections = tuple(
                (
                    await session.execute(
                        select(EmailThreadModel.connection_id)
                        .join(EmailMessageModel, EmailMessageModel.thread_id == EmailThreadModel.id)
                        .where(EmailMessageModel.provider_message_id == message_id)
                    )
                ).scalars()
            )
        assert remaining_connections == (CONNECTION_TWO,)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_worker_discovers_microsoft_folders_and_keeps_cursors_independent(
    database_url: str,
) -> None:
    """Microsoft 初始占位任务先发现目录，再逐 folder 幂等同步。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"d" * 32)
    reader = _DiscoveringMailReader([])
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={"mailbox": None},
        )
        step = MailSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=object(),  # Google path is not selected for this Microsoft connection.
            microsoft_oauth=MicrosoftOAuthAdapter(
                client_id="synthetic-client",
                client_secret="synthetic-secret",
                redirect_uri="https://app.example.test/callback",
            ),
            microsoft_reader=reader,
        )
        task = LeasedTask(
            task_id=UUID("10000000-0000-0000-0000-000000000098"),
            kind="sync_mail",
            input_payload={
                "connection_id": str(CONNECTION_ONE),
                "scope_key": "mailbox",
            },
            started_at=datetime(2030, 1, 8, tzinfo=UTC),
            user_id=USER_ONE,
        )

        await step.execute(task)
        await step.execute(task)

        assert reader.calls == [
            ("discover", ""),
            ("initial", "synthetic-folder-inbox"),
            ("initial", "synthetic-folder-sent"),
            ("discover", ""),
            ("sync", "synthetic-folder-inbox:synthetic-cursor-synthetic-folder-inbox"),
            ("sync", "synthetic-folder-sent:synthetic-cursor-synthetic-folder-sent"),
        ]
        async with sessions() as session:
            cursors = tuple(
                (
                    await session.scalars(
                        select(SyncCursorModel)
                        .where(
                            SyncCursorModel.connection_id == CONNECTION_ONE,
                            SyncCursorModel.resource_kind == "mail",
                        )
                        .order_by(SyncCursorModel.scope_key)
                    )
                ).all()
            )
            message_count = await session.scalar(
                select(func.count()).select_from(EmailMessageModel)
            )
        assert {cursor.scope_key: cursor.cursor for cursor in cursors} == {
            # mailbox 永久保留为新增 folder 的周期 discovery trigger。
            "mailbox": None,
            "synthetic-folder-inbox": "synthetic-cursor-synthetic-folder-inbox",
            "synthetic-folder-sent": "synthetic-cursor-synthetic-folder-sent",
        }
        mailbox = next(cursor for cursor in cursors if cursor.scope_key == "mailbox")
        assert mailbox.last_success_at is not None
        assert message_count == 2
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_mailbox_owner_commits_successful_folder_when_another_folder_fails(
    database_url: str,
) -> None:
    """目录 owner 遇到单 folder 暂态失败仍完成其余 folder，失败 scope 可重试。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"p" * 32)
    reader = _PartiallyFailingMailReader([])
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={
                "mailbox": None,
                "synthetic-folder-inbox": None,
                "synthetic-folder-sent": None,
            },
        )
        step = MailSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=object(),
            microsoft_oauth=MicrosoftOAuthAdapter(
                client_id="synthetic-client",
                client_secret="synthetic-secret",
                redirect_uri="https://app.example.test/callback",
            ),
            microsoft_reader=reader,
        )

        with pytest.raises(TransientProviderError) as raised:
            await step.execute(
                LeasedTask(
                    task_id=UUID("10000000-0000-0000-0000-0000000000a1"),
                    kind="sync_mail",
                    input_payload={
                        "connection_id": str(CONNECTION_ONE),
                        "scope_key": "mailbox",
                    },
                    started_at=datetime(2030, 1, 8, tzinfo=UTC),
                    user_id=USER_ONE,
                )
            )

        assert raised.value.error_code == "synthetic_folder_failure"
        assert reader.calls == [
            ("discover", ""),
            ("initial", "synthetic-folder-inbox"),
            ("initial", "synthetic-folder-sent"),
        ]
        async with sessions() as session:
            cursors = {
                cursor.scope_key: cursor
                for cursor in (
                    await session.scalars(
                        select(SyncCursorModel).where(
                            SyncCursorModel.connection_id == CONNECTION_ONE,
                            SyncCursorModel.resource_kind == "mail",
                        )
                    )
                ).all()
            }
        assert cursors["mailbox"].cursor is None
        assert cursors["mailbox"].last_success_at is not None
        assert cursors["synthetic-folder-inbox"].cursor is None
        assert cursors["synthetic-folder-sent"].cursor == "synthetic-partial-cursor"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_folder_discovery_failure_does_not_advance_existing_folder_cursor(
    database_url: str,
) -> None:
    """目录读取失败发生在逐 folder 循环前，所有既有恢复位置必须保持原值。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"e" * 32)
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={
                "mailbox": None,
                "synthetic-folder-inbox": INBOX_DELTA_LINK,
            },
        )
        respx.get(MAIL_FOLDERS_URL).respond(503, text="synthetic-sensitive-directory-error")
        delta_route = respx.get(INBOX_DELTA_LINK).respond(
            200,
            json=_fixture("mail_delta_incremental.json"),
        )
        step = MailSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=object(),  # Google path is not selected for this Microsoft connection.
            microsoft_oauth=MicrosoftOAuthAdapter(
                client_id="synthetic-client",
                client_secret="synthetic-secret",
                redirect_uri="https://app.example.test/callback",
            ),
        )

        with pytest.raises(TransientProviderError) as raised:
            await step.execute(
                LeasedTask(
                    task_id=UUID("10000000-0000-0000-0000-000000000096"),
                    kind="sync_mail",
                    input_payload={
                        "connection_id": str(CONNECTION_ONE),
                        "scope_key": "mailbox",
                    },
                    started_at=datetime(2030, 1, 8, tzinfo=UTC),
                    user_id=USER_ONE,
                )
            )

        async with sessions() as session:
            cursor = await session.scalar(
                select(SyncCursorModel.cursor).where(
                    SyncCursorModel.connection_id == CONNECTION_ONE,
                    SyncCursorModel.resource_kind == "mail",
                    SyncCursorModel.scope_key == "synthetic-folder-inbox",
                )
            )
        assert raised.value.error_code == "microsoft_mail_service_unavailable"
        assert cursor == INBOX_DELTA_LINK
        assert not delta_route.called
        assert "synthetic-sensitive-directory-error" not in str(raised.value)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_disabled_mail_capability_blocks_discovery_before_provider_io(
    database_url: str,
) -> None:
    """已撤销 mail.read 的排队任务必须在目录读取前停止。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"f" * 32)
    reader = _DiscoveringMailReader([])
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={"mailbox": None},
        )
        async with sessions.begin() as session:
            capability = await session.scalar(select(ConnectionCapabilityModel))
            assert capability is not None
            capability.status = "disabled"
        step = MailSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=object(),  # Google path is not selected for this Microsoft connection.
            microsoft_oauth=MicrosoftOAuthAdapter(
                client_id="synthetic-client",
                client_secret="synthetic-secret",
                redirect_uri="https://app.example.test/callback",
            ),
            microsoft_reader=reader,
        )

        with pytest.raises(MailConnectionNotFoundError):
            await step.execute(
                LeasedTask(
                    task_id=UUID("10000000-0000-0000-0000-000000000095"),
                    kind="sync_mail",
                    input_payload={
                        "connection_id": str(CONNECTION_ONE),
                        "scope_key": "mailbox",
                    },
                    started_at=datetime(2030, 1, 8, tzinfo=UTC),
                    user_id=USER_ONE,
                )
            )

        assert reader.calls == []
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_app_test_mode_microsoft_mail_sync_never_constructs_graph_client(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """测试模式组合根固定注入离线 reader，执行时也不会回退真实 Graph。"""
    sessions = build_session_factory(database_url)
    master_key = tmp_path / "master-key"
    master_key.write_text(
        "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        encoding="utf-8",
    )
    cipher = AeadCipher.from_file(master_key)

    class NoRealGraphAdapter:
        """若组合根未注入 fake reader，默认 adapter 构造立即让测试失败。"""

        def __init__(self, **values: object) -> None:
            del values
            raise AssertionError("APP_TEST_MODE must not construct MicrosoftMailAdapter")

    class NoHttpClient:
        """即使后续代码尝试 HTTP，也在离开进程前确定性失败。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("APP_TEST_MODE must not construct an HTTP client")

    monkeypatch.setattr(sync_mail_worker, "MicrosoftMailAdapter", NoRealGraphAdapter)
    monkeypatch.setattr(httpx, "AsyncClient", NoHttpClient)
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={"mailbox": None},
        )
        step = build_mail_sync_task_step(
            session_factory=sessions,
            settings=Settings(
                app_env="test",
                app_test_mode=True,
                app_master_key_file=master_key,
            ),
        )

        await step.execute(
            LeasedTask(
                task_id=UUID("10000000-0000-0000-0000-000000000097"),
                kind="sync_mail",
                input_payload={
                    "connection_id": str(CONNECTION_ONE),
                    "scope_key": "mailbox",
                },
                started_at=datetime(2030, 1, 8, tzinfo=UTC),
                user_id=USER_ONE,
            )
        )

        async with sessions() as session:
            cursor = await session.scalar(
                select(SyncCursorModel.cursor).where(
                    SyncCursorModel.connection_id == CONNECTION_ONE,
                    SyncCursorModel.resource_kind == "mail",
                    SyncCursorModel.scope_key == "fake-microsoft-mailbox",
                )
            )
        assert cursor == "fake-microsoft-mail-delta-v1"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_tombstone_deletes_only_exact_connection_and_scope(
    database_url: str,
) -> None:
    """Graph tombstone 只能删除本用户、本连接、本 folder 的精确 provider message。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"n" * 32)
    archive_url = f"{GRAPH_BASE_URL}/me/mailFolders/synthetic-folder-archive/messages/delta"
    archive_next = f"{archive_url}?$skiptoken=synthetic-archive-next"
    archive_delta = f"{archive_url}?$deltatoken=synthetic-archive-delta"
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={"synthetic-folder-inbox": None},
        )
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_TWO,
            connection_id=CONNECTION_TWO,
            scope_cursors={"synthetic-folder-archive": None},
        )

        inbox_initial = _fixture("mail_delta_initial.json")
        archive_initial = deepcopy(inbox_initial)
        archive_initial["@odata.context"] = str(archive_initial["@odata.context"]).replace(
            "synthetic-folder-inbox", "synthetic-folder-archive"
        )
        archive_initial["@odata.nextLink"] = archive_next
        archive_next_payload = _fixture("mail_delta_incremental.json")
        archive_next_payload["@odata.deltaLink"] = archive_delta
        respx.get(INBOX_DELTA_URL, params=INITIAL_PARAMS).respond(200, json=inbox_initial)
        respx.get(INBOX_NEXT_URL).respond(200, json=_fixture("mail_delta_incremental.json"))
        respx.get(archive_url, params=INITIAL_PARAMS).respond(200, json=archive_initial)
        respx.get(archive_next).respond(200, json=archive_next_payload)
        inbox_adapter = _adapter()
        archive_adapter = _adapter()
        inbox_use_case = SyncMailUseCase(
            lambda: _repository_factory(sessions),
            ProviderAdapterRegistry(microsoft_mail=inbox_adapter),  # type: ignore[arg-type]
            cipher,
            clock=_FixedClock(datetime(2030, 1, 8, 12, tzinfo=UTC)),
        )
        archive_use_case = SyncMailUseCase(
            lambda: _repository_factory(sessions),
            ProviderAdapterRegistry(microsoft_mail=archive_adapter),  # type: ignore[arg-type]
            cipher,
            clock=_FixedClock(datetime(2030, 1, 8, 12, tzinfo=UTC)),
        )
        await inbox_use_case.execute(
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_key="synthetic-folder-inbox",
        )
        await archive_use_case.execute(
            user_id=USER_TWO,
            connection_id=CONNECTION_TWO,
            scope_key="synthetic-folder-archive",
        )

        # The next response is a tombstone for connection one only. Its sibling connection has
        # the same provider message ID to prove the delete predicate cannot cross user ownership.
        tombstone = {
            "@odata.deltaLink": INBOX_DELTA_LINK,
            "value": [{"id": "synthetic-message-1", "@removed": {"reason": "deleted"}}],
        }
        respx.get(INBOX_DELTA_LINK).respond(200, json=tombstone)
        await inbox_use_case.execute(
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_key="synthetic-folder-inbox",
        )

        async with sessions() as session:
            one_count = await session.scalar(
                select(func.count())
                .select_from(EmailMessageModel)
                .join(EmailThreadModel, EmailThreadModel.id == EmailMessageModel.thread_id)
                .where(EmailThreadModel.connection_id == CONNECTION_ONE)
            )
            two_count = await session.scalar(
                select(func.count())
                .select_from(EmailMessageModel)
                .join(EmailThreadModel, EmailThreadModel.id == EmailMessageModel.thread_id)
                .where(EmailThreadModel.connection_id == CONNECTION_TWO)
            )
        assert one_count == 1  # message-2 remains; message-1 is removed by tombstone
        assert two_count == 2
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_expired_inbox_cursor_falls_back_without_touching_sent_cursor(
    database_url: str,
) -> None:
    """单 folder Delta 失效时仅清 inbox 并执行七日回退，Sent cursor 保持不变。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"o" * 32)
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={
                "synthetic-folder-inbox": INBOX_DELTA_LINK,
                "synthetic-folder-sent": "https://graph.microsoft.com/v1.0/sent-delta",
            },
        )
        fallback = _fixture("mail_delta_initial.json")
        respx.get(INBOX_DELTA_LINK).respond(410, json={"error": {"code": "syncStateNotFound"}})
        respx.get(INBOX_DELTA_URL, params=INITIAL_PARAMS).respond(200, json=fallback)
        respx.get(INBOX_NEXT_URL).respond(200, json=_fixture("mail_delta_incremental.json"))

        result = await SyncMailUseCase(
            lambda: _repository_factory(sessions),
            ProviderAdapterRegistry(microsoft_mail=_adapter()),  # type: ignore[arg-type]
            cipher,
            clock=_FixedClock(datetime(2030, 1, 8, 12, tzinfo=UTC)),
        ).execute(
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_key="synthetic-folder-inbox",
        )

        assert result.used_full_resync
        async with sessions() as session:
            cursors = tuple(
                (
                    await session.scalars(
                        select(SyncCursorModel).where(
                            SyncCursorModel.connection_id == CONNECTION_ONE
                        )
                    )
                ).all()
            )
        by_scope = {cursor.scope_key: cursor.cursor for cursor in cursors}
        assert by_scope["synthetic-folder-inbox"] == INBOX_DELTA_LINK
        assert by_scope["synthetic-folder-sent"] == "https://graph.microsoft.com/v1.0/sent-delta"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_old_microsoft_cursor_cannot_rollback_newer_cursor(database_url: str) -> None:
    """两个 Worker 竞争同一 folder 时，旧页只能得到 CAS 冲突且不能覆盖新 cursor。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"p" * 32)
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={"synthetic-folder-inbox": "synthetic-delta-old"},
        )
        loaded = asyncio.Event()
        release = asyncio.Event()
        old_reader = _BlockingMailReader(
            loaded=loaded,
            release=release,
            page=MailSyncPage((), None, "synthetic-delta-old-result"),
        )
        new_reader = _BlockingMailReader(
            loaded=asyncio.Event(),
            release=asyncio.Event(),
            page=MailSyncPage((), None, "synthetic-delta-new-result"),
        )

        class _NewReader(_BlockingMailReader):
            """新同步不等待，直接提交更晚的合成 cursor。"""

            async def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
                assert scope_key == "synthetic-folder-inbox"
                assert cursor == "synthetic-delta-old"
                yield self.page

        new_reader = _NewReader(
            loaded=asyncio.Event(),
            release=asyncio.Event(),
            page=MailSyncPage((), None, "synthetic-delta-new-result"),
        )
        old_case = SyncMailUseCase(
            lambda: _repository_factory(sessions),
            ProviderAdapterRegistry(microsoft_mail=old_reader),  # type: ignore[arg-type]
            cipher,
        )
        new_case = SyncMailUseCase(
            lambda: _repository_factory(sessions),
            ProviderAdapterRegistry(microsoft_mail=new_reader),  # type: ignore[arg-type]
            cipher,
        )
        old_task = asyncio.create_task(
            old_case.execute(
                user_id=USER_ONE,
                connection_id=CONNECTION_ONE,
                scope_key="synthetic-folder-inbox",
            )
        )
        await loaded.wait()
        newer = await new_case.execute(
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_key="synthetic-folder-inbox",
        )
        release.set()
        with pytest.raises(TransientProviderError) as raised:
            await old_task

        async with sessions() as session:
            cursor = await session.scalar(
                select(SyncCursorModel.cursor).where(
                    SyncCursorModel.connection_id == CONNECTION_ONE,
                    SyncCursorModel.scope_key == "synthetic-folder-inbox",
                )
            )
        assert newer.next_cursor == "synthetic-delta-new-result"
        assert raised.value.error_code == "mail_sync_cursor_conflict"
        assert cursor == "synthetic-delta-new-result"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_worker_marks_mail_capability_action_required_on_graph_403(database_url: str) -> None:
    """Graph 403 必须持久降级 mail.read capability，而非伪装成可重试成功。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"q" * 32)
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={"synthetic-folder-inbox": INBOX_DELTA_LINK},
        )
        respx.get(INBOX_DELTA_LINK).respond(403, json={"error": {"code": "ErrorAccessDenied"}})
        step = MailSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=object(),  # Google path is not selected for this Microsoft connection.
            microsoft_oauth=MicrosoftOAuthAdapter(
                client_id="synthetic-client",
                client_secret="synthetic-secret",
                redirect_uri="https://app.example.test/callback",
            ),
        )
        with pytest.raises(UserActionRequiredError) as raised:
            await step.execute(
                LeasedTask(
                    task_id=UUID("10000000-0000-0000-0000-000000000099"),
                    kind="sync_mail",
                    input_payload={
                        "connection_id": str(CONNECTION_ONE),
                        "scope_key": "synthetic-folder-inbox",
                    },
                    started_at=datetime(2030, 1, 8, tzinfo=UTC),
                    user_id=USER_ONE,
                )
            )
        assert raised.value.error_code == "microsoft_mail_permission_required"
        async with sessions() as session:
            capability = await session.scalar(select(ConnectionCapabilityModel))
            connection = await session.scalar(select(OAuthConnectionModel))
        assert capability is not None and capability.status == "action_required"
        assert capability.last_error_code == "microsoft_mail_permission_required"
        assert connection is not None and connection.status == "connected"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_worker_refreshes_microsoft_token_once_and_persists_rotation(
    database_url: str,
) -> None:
    """Microsoft Worker 在只读 401 后只刷新一次，并原子保存新 access/refresh 密文。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"r" * 32)
    try:
        await _seed_connection(
            sessions,
            cipher,
            user_id=USER_ONE,
            connection_id=CONNECTION_ONE,
            scope_cursors={"synthetic-folder-inbox": None},
        )
        refreshed_page = _fixture("mail_delta_initial.json")
        refreshed_page.pop("@odata.nextLink")
        refreshed_page["@odata.deltaLink"] = INBOX_DELTA_LINK
        graph_route = respx.get(INBOX_DELTA_URL).mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json=refreshed_page),
            ]
        )
        refresh_route = respx.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        ).respond(
            200,
            json={
                "access_token": "synthetic-microsoft-refreshed-access",
                "refresh_token": "synthetic-microsoft-refreshed-refresh",
                "expires_in": 3600,
                "scope": "openid profile email User.Read offline_access Mail.Read",
            },
        )
        step = MailSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=object(),
            microsoft_oauth=MicrosoftOAuthAdapter(
                client_id="synthetic-client",
                client_secret="synthetic-secret",
                redirect_uri="https://app.example.test/callback",
            ),
        )
        await step.execute(
            LeasedTask(
                task_id=UUID("10000000-0000-0000-0000-000000000098"),
                kind="sync_mail",
                input_payload={
                    "connection_id": str(CONNECTION_ONE),
                    "scope_key": "synthetic-folder-inbox",
                },
                started_at=datetime(2030, 1, 8, tzinfo=UTC),
                user_id=USER_ONE,
            )
        )
        assert graph_route.call_count == 2
        assert refresh_route.called
        async with sessions() as session:
            credentials = tuple((await session.scalars(select(EncryptedCredentialModel))).all())
            connection = await session.scalar(select(OAuthConnectionModel))
        access = next(item for item in credentials if item.credential_kind == "access_token")
        refresh = next(item for item in credentials if item.credential_kind == "refresh_token")
        assert connection is not None and connection.status == "connected"
        assert (
            cipher.decrypt(
                EncryptedValue(access.ciphertext, access.nonce, access.key_version),
                f"{USER_ONE}:{CONNECTION_ONE}:access_token".encode("ascii"),
            )
            == b"synthetic-microsoft-refreshed-access"
        )
        assert (
            cipher.decrypt(
                EncryptedValue(refresh.ciphertext, refresh.nonce, refresh.key_version),
                f"{USER_ONE}:{CONNECTION_ONE}:refresh_token".encode("ascii"),
            )
            == b"synthetic-microsoft-refreshed-refresh"
        )
    finally:
        await sessions.dispose()
