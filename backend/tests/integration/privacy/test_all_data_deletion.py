"""验证全数据删除在撤销失败时仍完成本地不可逆清理。"""

from datetime import UTC, date, datetime, time
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from ai_employee.infrastructure.db.models.briefs import (
    ConversationModel,
    DailyBriefModel,
    MessageModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.models.sources import (
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.workers.privacy import PrivacyDeletionWorker


class FailingRevoker:
    """记录合成 token 后模拟供应商网络不可用，绝不建立真实连接。"""

    def __init__(self) -> None:
        """初始化撤销调用记录。"""
        self.tokens: list[str] = []

    async def revoke(self, token: str) -> None:
        """记录 token 并抛出可预期传输错误。"""
        self.tokens.append(token)
        raise httpx.ConnectError("synthetic provider outage")


class CrashAfterCredentialCleanupWorker(PrivacyDeletionWorker):
    """仅供测试在已提交的首个删除阶段后模拟进程崩溃。"""

    async def _after_user_row_batch(
        self,
        *,
        model: type[object],
        user_id: UUID,
        deleted_count: int,
    ) -> None:
        """在首个凭据批已提交、下一批尚未开始时精确模拟进程崩溃。"""
        del user_id, deleted_count
        if model is EncryptedCredentialModel:
            raise RuntimeError("synthetic mid-batch crash")


@pytest.mark.asyncio
async def test_all_data_deletion_attempts_revocation_once_without_blocking_local_cleanup(
    database_url: str,
) -> None:
    """Google 撤销失败不能阻止凭据删除和一次内容无关完成审计。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"p" * 32)
    revoker = FailingRevoker()
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="privacy-all-data@example.test",
                display_name="Synthetic owner",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            other = UserModel(email="privacy-all-data-other@example.test", display_name="Other", password_hash=None, timezone="UTC", locale="zh-CN", brief_time=time(8, 0), is_active=True)
            session.add(other)
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="google",
                provider_account_id="all-data-cleanup",
                account_email="synthetic@example.test",
                scopes=["gmail.readonly"],
                status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()
            encrypted = cipher.encrypt(
                b"synthetic-refresh-token",
                f"{user.id}:{connection.id}:refresh_token".encode("ascii"),
            )
            session.add(
                EncryptedCredentialModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    credential_kind="refresh_token",
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=datetime.now(UTC),
                )
            )
            thread = EmailThreadModel(user_id=user.id, connection_id=connection.id, provider_thread_id="all-data-email", subject="Synthetic subject", participants=[], latest_message_at=datetime.now(UTC), provider_url="https://example.test/email", provider_updated_at=None)
            session.add(thread)
            await session.flush()
            session.add(EmailMessageModel(user_id=user.id, thread_id=thread.id, provider_message_id="all-data-message", received_at=datetime.now(UTC), sender={}, recipients=[], subject="Synthetic subject", snippet="Synthetic body", body_ciphertext=None, body_nonce=None, body_key_version=None, labels=[], headers={}, provider_url="https://example.test/email"))
            task = TaskRunModel(user_id=user.id, kind="daily_brief", status="succeeded", idempotency_key="all-data-task", input_payload={})
            conversation = ConversationModel(user_id=user.id, title="Synthetic")
            session.add_all((task, conversation, UserSessionModel(user_id=user.id, token_hash=b"t" * 32, csrf_hash=b"c" * 32, created_at=datetime.now(UTC), expires_at=datetime(2031, 1, 1, tzinfo=UTC), last_seen_at=datetime.now(UTC))))
            await session.flush()
            session.add_all((MessageModel(user_id=user.id, conversation_id=conversation.id, role="user", content_markdown="Synthetic body", task_id=None, created_at=datetime.now(UTC)), DailyBriefModel(user_id=user.id, local_date=date(2030, 1, 1), version=1, task_id=task.id, completeness="complete", source_cutoff=datetime.now(UTC), headline="Synthetic", structured_content={}, markdown="Synthetic", warnings=[], created_at=datetime.now(UTC))))
            other_task = TaskRunModel(user_id=other.id, kind="daily_brief", status="succeeded", idempotency_key="other-task", input_payload={})
            other_conversation = ConversationModel(user_id=other.id, title="Other")
            session.add_all((other_task, other_conversation))
            await session.flush()
            other_id = other.id
            user_id: UUID = user.id

        await PrivacyDeletionWorker(
            session_factory,
            credential_cipher=cipher,
            oauth_revoker=revoker,
        ).delete_all_data(user_id=user_id, request_id="opaque-delete-request", batch_size=1)

        async with session_factory() as session:
            credentials = await session.scalar(
                select(func.count())
                .select_from(EncryptedCredentialModel)
                .where(EncryptedCredentialModel.user_id == user_id)
            )
            audit_events = (
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.user_id == user_id,
                        AuditEventModel.event_type == "privacy.deletion_completed",
                    )
                )
            ).all()
            owner_sessions = await session.scalar(select(func.count()).select_from(UserSessionModel).where(UserSessionModel.user_id == user_id))
            owner_tasks = await session.scalar(select(func.count()).select_from(TaskRunModel).where(TaskRunModel.user_id == user_id))
            owner_conversations = await session.scalar(select(func.count()).select_from(ConversationModel).where(ConversationModel.user_id == user_id))
            owner_messages = await session.scalar(select(func.count()).select_from(EmailMessageModel).where(EmailMessageModel.user_id == user_id))
            other_tasks = await session.scalar(select(func.count()).select_from(TaskRunModel).where(TaskRunModel.user_id == other_id))
            other_conversations = await session.scalar(select(func.count()).select_from(ConversationModel).where(ConversationModel.user_id == other_id))
            deleted_user = await session.get(UserModel, user_id)

        assert revoker.tokens == ["synthetic-refresh-token"]
        assert credentials == 0
        assert owner_sessions == owner_tasks == owner_conversations == owner_messages == 0
        assert other_tasks == other_conversations == 1
        assert deleted_user is not None and deleted_user.is_active is False
        assert deleted_user.email == f"deleted-{user_id}@invalid.local"
        assert len(audit_events) == 1
        assert set(audit_events[0].event_metadata) == {
            "operation",
            "request_id",
            "completed_at",
            "trace_id",
        }
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_all_data_deletion_retries_after_committed_batch_with_one_redacted_audit(
    database_url: str,
) -> None:
    """中途崩溃后重试必须删除构造的本地数据并只保留一条脱敏完成审计。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = UserModel(email="crash-owner@example.test", display_name="Crash", password_hash=None, timezone="UTC", locale="zh-CN", brief_time=time(8, 0), is_active=True)
            session.add(user)
            await session.flush()
            connections = [
                OAuthConnectionModel(user_id=user.id, provider="google", provider_account_id=f"crash-{index}", account_email=f"crash-{index}@example.test", scopes=[], status="connected", last_error_code=None)
                for index in range(3)
            ]
            session.add_all(connections)
            await session.flush()
            session.add_all(
                EncryptedCredentialModel(user_id=user.id, connection_id=connection.id, credential_kind="access_token", ciphertext=b"synthetic-token", nonce=b"123456789012", key_version=1, token_expires_at=None)
                for connection in connections
            )
            session.add(
                UserSessionModel(
                    user_id=user.id,
                    token_hash=b"s" * 32,
                    csrf_hash=b"f" * 32,
                    created_at=datetime.now(UTC),
                    expires_at=datetime(2031, 1, 1, tzinfo=UTC),
                    last_seen_at=datetime.now(UTC),
                )
            )
            user_id = user.id

        with pytest.raises(RuntimeError, match="synthetic mid-batch crash"):
            await CrashAfterCredentialCleanupWorker(session_factory).delete_all_data(user_id=user_id, request_id="crash-request", batch_size=1)
        async with session_factory() as session:
            remaining_after_crash = await session.scalar(
                select(func.count()).select_from(EncryptedCredentialModel).where(
                    EncryptedCredentialModel.user_id == user_id
                )
            )
        assert remaining_after_crash == 2
        await PrivacyDeletionWorker(session_factory).delete_all_data(user_id=user_id, request_id="crash-request", batch_size=1)

        async with session_factory() as session:
            credential_count = await session.scalar(
                select(func.count())
                .select_from(EncryptedCredentialModel)
                .where(EncryptedCredentialModel.user_id == user_id)
            )
            connection_count = await session.scalar(
                select(func.count())
                .select_from(OAuthConnectionModel)
                .where(OAuthConnectionModel.user_id == user_id)
            )
            session_count = await session.scalar(
                select(func.count())
                .select_from(UserSessionModel)
                .where(UserSessionModel.user_id == user_id)
            )
            deleted_user = await session.get(UserModel, user_id)
            events = (
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.user_id == user_id,
                        AuditEventModel.event_type == "privacy.deletion_completed",
                    )
                )
            ).all()

        assert credential_count == connection_count == session_count == 0
        assert deleted_user is not None
        assert deleted_user.email == f"deleted-{user_id}@invalid.local"
        assert deleted_user.display_name == "Deleted User"
        assert deleted_user.is_active is False
        assert len(events) == 1
        metadata = events[0].event_metadata
        assert set(metadata) == {"operation", "request_id", "completed_at", "trace_id"}
        assert not any(marker in str(metadata) for marker in ("crash@example.test", "Synthetic", "token", "prompt"))
    finally:
        await session_factory.dispose()
