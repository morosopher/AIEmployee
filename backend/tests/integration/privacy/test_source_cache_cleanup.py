"""验证 source-cache 隐私删除不会越过可恢复缓存边界。"""

from datetime import UTC, date, datetime, time

import pytest
from sqlalchemy import func, select

from ai_employee.infrastructure.db.models.briefs import (
    ConversationModel,
    DailyBriefItemModel,
    DailyBriefModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    EmailAnalysisModel,
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.workers.privacy import PrivacyDeletionWorker


@pytest.mark.asyncio
async def test_source_cache_cleanup_removes_only_source_rows_and_resets_cursors(database_url: str) -> None:
    """来源缓存删除清空邮件并复位 Gmail/Calendar 游标，不删连接或对话。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="privacy-source@example.test", display_name="Synthetic owner", password_hash=None,
                timezone="UTC", locale="zh-CN", brief_time=time(8, 0), is_active=True,
            )
            other = UserModel(email="privacy-source-other@example.test", display_name="Other", password_hash=None,
                timezone="UTC", locale="zh-CN", brief_time=time(8, 0), is_active=True)
            session.add_all((user, other))
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id, provider="google", provider_account_id="source-cleanup",
                account_email="synthetic@example.test", scopes=["gmail.readonly"], status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()
            session.add_all([
                SyncCursorModel(connection_id=connection.id, resource_kind="gmail", cursor="100", last_success_at=datetime.now(UTC)),
                SyncCursorModel(connection_id=connection.id, resource_kind="calendar", cursor="200", last_success_at=datetime.now(UTC)),
                ConversationModel(user_id=user.id, title="Keep this workspace"),
            ])
            thread = EmailThreadModel(
                user_id=user.id, connection_id=connection.id, provider_thread_id="thread-source-cleanup",
                subject="Synthetic source", participants=[], latest_message_at=datetime.now(UTC),
                provider_url="https://example.test/thread", provider_updated_at=None,
            )
            session.add(thread)
            await session.flush()
            session.add(EmailMessageModel(
                user_id=user.id, thread_id=thread.id, provider_message_id="message-source-cleanup",
                received_at=datetime.now(UTC), sender={}, recipients=[], subject="Synthetic source",
                snippet="Synthetic", body_ciphertext=b"synthetic", body_nonce=b"123456789012",
                body_key_version=1, labels=[], headers={}, provider_url="https://example.test/message",
            ))
            task = TaskRunModel(user_id=user.id, kind="daily_brief", status="succeeded", idempotency_key="source-brief", input_payload={})
            session.add(task)
            await session.flush()
            brief = DailyBriefModel(user_id=user.id, local_date=date(2030, 1, 1), version=1, task_id=task.id,
                completeness="complete", source_cutoff=datetime.now(UTC), headline="Synthetic", structured_content={}, markdown="Synthetic", warnings=[], created_at=datetime.now(UTC))
            session.add_all((brief, EmailAnalysisModel(user_id=user.id, thread_id=thread.id, category="other", urgency="low", needs_reply=False, deadline_at=None, confidence=1.0, reason_codes=[], model_name=None, prompt_version=None, input_hash="a" * 64, created_at=datetime.now(UTC)), CalendarEventModel(user_id=user.id, connection_id=connection.id, provider_event_id="event-source", calendar_id="primary", title="Synthetic", description_ciphertext=None, description_nonce=None, description_key_version=None, location_ciphertext=None, location_nonce=None, location_key_version=None, starts_at=datetime.now(UTC), ends_at=datetime.now(UTC), all_day=False, transparency="opaque", status="confirmed", timezone="UTC", recurring_event_id=None, etag=None, provider_url="https://example.test/event", provider_updated_at=None)))
            await session.flush()
            session.add(DailyBriefItemModel(brief_id=brief.id, position=1, section="email", priority="low", title="Synthetic", body_markdown="Synthetic", source_refs=[], suggested_action_kind=None))
            session.add(EncryptedCredentialModel(user_id=user.id, connection_id=connection.id, credential_kind="access_token", ciphertext=b"synthetic", nonce=b"123456789012", key_version=1, token_expires_at=None))
            other_connection = OAuthConnectionModel(user_id=other.id, provider="google", provider_account_id="source-other", account_email="other@example.test", scopes=[], status="connected", last_error_code=None)
            session.add(other_connection)
            await session.flush()
            other_thread = EmailThreadModel(user_id=other.id, connection_id=other_connection.id, provider_thread_id="thread-other", subject="Other", participants=[], latest_message_at=datetime.now(UTC), provider_url="https://example.test/other", provider_updated_at=None)
            session.add(other_thread)
            await session.flush()
            session.add(EmailMessageModel(user_id=other.id, thread_id=other_thread.id, provider_message_id="message-other", received_at=datetime.now(UTC), sender={}, recipients=[], subject="Other", snippet="Other", body_ciphertext=None, body_nonce=None, body_key_version=None, labels=[], headers={}, provider_url="https://example.test/other"))
            user_id = user.id

        await PrivacyDeletionWorker(session_factory).clear_source_cache(user_id=user_id, batch_size=10)

        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(EmailMessageModel).where(EmailMessageModel.user_id == user_id)) == 0
            assert await session.scalar(select(func.count()).select_from(EmailAnalysisModel).where(EmailAnalysisModel.user_id == user_id)) == 0
            assert await session.scalar(select(func.count()).select_from(CalendarEventModel).where(CalendarEventModel.user_id == user_id)) == 0
            assert await session.scalar(select(func.count()).select_from(DailyBriefModel).where(DailyBriefModel.user_id == user_id)) == 0
            assert await session.scalar(select(func.count()).select_from(OAuthConnectionModel).where(OAuthConnectionModel.user_id == user_id)) == 1
            assert await session.scalar(select(func.count()).select_from(EncryptedCredentialModel).where(EncryptedCredentialModel.user_id == user_id)) == 1
            assert await session.scalar(select(func.count()).select_from(ConversationModel).where(ConversationModel.user_id == user_id)) == 1
            cursors = (await session.scalars(select(SyncCursorModel).where(SyncCursorModel.connection_id == connection.id))).all()
            assert {cursor.resource_kind: (cursor.cursor, cursor.last_success_at) for cursor in cursors} == {
                "gmail": (None, None), "calendar": (None, None)
            }
            assert await session.scalar(select(func.count()).select_from(EmailMessageModel).where(EmailMessageModel.user_id == other.id)) == 1
    finally:
        await session_factory.dispose()
