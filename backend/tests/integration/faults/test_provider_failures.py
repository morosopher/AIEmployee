"""验证供应商和模型失败保持明确、可恢复且不伪造完整简报。"""

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from ai_employee.api.routers.test_support import TestScenarioStore
from ai_employee.application.use_cases.task_execution import DurableTaskRunner
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    EncryptedCredentialModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import OutboxEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyGmailSyncRepositoryFactory
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.queue.redis_url import RedisTestUrl
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.fake import (
    FakeCalendarReader,
    FakeGmailReader,
    FakeGoogleOAuthClient,
)
from ai_employee.integrations.google.gmail import GmailAdapter
from ai_employee.workers.sync_calendar import CalendarSyncTaskStep
from ai_employee.workers.sync_gmail import GmailSyncTaskStep


@pytest.mark.asyncio
async def test_gmail_429_honors_retry_after() -> None:
    """Gmail 429 的秒数提示必须变成内部临时错误的 retry_after。"""
    adapter = GmailAdapter(
        access_token="synthetic", refresh_access_token=_token, mark_expired=_none
    )
    response = httpx.Response(
        429, headers={"Retry-After": "17"}, request=httpx.Request("GET", "https://example.test")
    )
    assert adapter._retry_after(response) == 17


@pytest.mark.asyncio
async def test_google_revocation_is_persisted_as_degraded_connection(database_url: str) -> None:
    """Gmail 与 Calendar 共用凭据仓储时必须写出同一可见撤销状态。"""
    sessions = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="revoked-owner@example.test",
                    display_name="Revoked Owner",
                    password_hash=None,
                    timezone="UTC",
                    brief_time=time(8),
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id="synthetic-revoked",
                    account_email="revoked-owner@example.test",
                    scopes=[],
                    status="connected",
                    last_error_code=None,
                )
            )
        async with SqlAlchemyGmailSyncRepositoryFactory(sessions)() as store:
            await store.mark_expired(user_id=user_id, connection_id=connection_id)
        async with sessions() as session:
            connection = await session.scalar(
                select(OAuthConnectionModel).where(OAuthConnectionModel.id == connection_id)
            )
        assert connection is not None
        assert connection.status == "degraded"
        assert connection.last_error_code == "oauth_revoked"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_gmail_429_persists_retry_scheduled_outbox_through_fake_reader(
    database_url: str, redis_url: RedisTestUrl
) -> None:
    """一次性 Gmail 429 必须经真实 Worker 写入可恢复的耐久重试事实。"""
    sessions = build_session_factory(database_url)
    now = datetime(2030, 1, 2, tzinfo=UTC)
    user_id, connection_id, task_id = uuid4(), uuid4(), uuid4()
    cipher = AeadCipher(b"g" * 32)
    redis = Redis.from_url(str(redis_url), decode_responses=False)
    scenario_store = TestScenarioStore(redis)
    fixture = Path(__file__).parents[2] / "contract" / "fixtures" / "gmail_initial.json"

    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="gmail-retry@example.test",
                    display_name="Gmail Retry",
                    password_hash=None,
                    timezone="UTC",
                    brief_time=time(8),
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id="synthetic-gmail",
                    account_email="gmail-retry@example.test",
                    scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                    status="connected",
                    last_error_code=None,
                )
            )
            await session.flush()
            access = cipher.encrypt(
                b"synthetic-access", f"{user_id}:{connection_id}:access_token".encode("ascii")
            )
            refresh = cipher.encrypt(
                b"synthetic-refresh", f"{user_id}:{connection_id}:refresh_token".encode("ascii")
            )
            session.add_all(
                (
                    EncryptedCredentialModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        credential_kind="access_token",
                        ciphertext=access.ciphertext,
                        nonce=access.nonce,
                        key_version=access.key_version,
                        token_expires_at=now + timedelta(hours=1),
                    ),
                    EncryptedCredentialModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        credential_kind="refresh_token",
                        ciphertext=refresh.ciphertext,
                        nonce=refresh.nonce,
                        key_version=refresh.key_version,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id, resource_kind="gmail", cursor=None
                    ),
                    TaskRunModel(
                        id=task_id,
                        user_id=user_id,
                        kind="sync_gmail",
                        status=TaskStatus.QUEUED.value,
                        idempotency_key=f"gmail-429:{task_id}",
                        input_payload={"connection_id": str(connection_id)},
                    ),
                )
            )
        await scenario_store.set(user_id=user_id, scenario="gmail_429")
        step = GmailSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=FakeGoogleOAuthClient(),
            reader=FakeGmailReader(
                fixture,
                scenario_consumer=lambda current_user_id: scenario_store.consume(
                    user_id=current_user_id
                ),
            ),
        )
        runner = DurableTaskRunner(
            store=SqlAlchemyTaskExecutionStore(sessions),
            clock=lambda: now,
            lease_duration=timedelta(seconds=30),
            task_timeout_seconds=60,
            task_step_timeout_seconds=10,
            max_transient_retries=3,
            resolve_steps=lambda _: (step,),
            retry_backoff_cap=timedelta(seconds=20),
        )

        assert await runner.run(task_id, lease_owner="gmail-worker", retry_delay=timedelta(seconds=5))

        async with sessions() as session:
            task = await session.get(TaskRunModel, task_id)
            outbox = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
            )
        assert task is not None
        assert task.status == TaskStatus.RETRY_SCHEDULED.value
        assert task.error_code == "google_rate_limited"
        assert task.attempt_count == 1
        assert outbox is not None
        assert outbox.topic == "task.execute"
        assert outbox.aggregate_id == task_id
        # Fake 返回 30 秒 Retry-After，Runner 必须将其封顶到二十秒。
        assert outbox.available_at == now + timedelta(seconds=20)
        assert outbox.deduplication_key == f"task.execute:{task_id}:retry:1"
    finally:
        await redis.delete(TestScenarioStore.key(user_id=user_id))
        await redis.aclose()
        await sessions.dispose()


@pytest.mark.asyncio
async def test_calendar_5xx_persists_capped_retry_scheduled_outbox(
    database_url: str, redis_url: RedisTestUrl
) -> None:
    """Calendar fake 5xx 经真实 Worker 保存封顶 retry Outbox，且场景只消费一次。"""
    sessions = build_session_factory(database_url)
    now = datetime(2030, 1, 2, tzinfo=UTC)
    user_id, connection_id, task_id = uuid4(), uuid4(), uuid4()
    cipher = AeadCipher(b"c" * 32)
    redis = Redis.from_url(str(redis_url), decode_responses=False)
    scenario_store = TestScenarioStore(redis)
    fixture = Path(__file__).parents[2] / "contract" / "fixtures" / "calendar_initial.json"

    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="calendar-retry@example.test",
                    display_name="Calendar Retry",
                    password_hash=None,
                    timezone="UTC",
                    brief_time=time(8),
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id="synthetic-calendar",
                    account_email="calendar-retry@example.test",
                    scopes=["https://www.googleapis.com/auth/calendar.readonly"],
                    status="connected",
                    last_error_code=None,
                )
            )
            # 显式 UUID 外键没有 ORM relationship 辅助排序，先写入父事实再写凭据。
            await session.flush()
            access = cipher.encrypt(
                b"synthetic-access", f"{user_id}:{connection_id}:access_token".encode("ascii")
            )
            refresh = cipher.encrypt(
                b"synthetic-refresh", f"{user_id}:{connection_id}:refresh_token".encode("ascii")
            )
            session.add_all(
                (
                    EncryptedCredentialModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        credential_kind="access_token",
                        ciphertext=access.ciphertext,
                        nonce=access.nonce,
                        key_version=access.key_version,
                        token_expires_at=now + timedelta(hours=1),
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
                    SyncCursorModel(
                        connection_id=connection_id, resource_kind="calendar", cursor=None
                    ),
                    TaskRunModel(
                        id=task_id,
                        user_id=user_id,
                        kind="sync_calendar",
                        status=TaskStatus.QUEUED.value,
                        idempotency_key=f"calendar-5xx:{task_id}",
                        input_payload={"connection_id": str(connection_id)},
                    ),
                )
            )
        await scenario_store.set(user_id=user_id, scenario="calendar_5xx")
        step = CalendarSyncTaskStep(
            session_factory=sessions,
            cipher=cipher,
            oauth=FakeGoogleOAuthClient(),
            reader=FakeCalendarReader(
                fixture,
                scenario_consumer=lambda current_user_id: scenario_store.consume(
                    user_id=current_user_id
                ),
            ),
        )
        runner = DurableTaskRunner(
            store=SqlAlchemyTaskExecutionStore(sessions),
            clock=lambda: now,
            lease_duration=timedelta(seconds=30),
            task_timeout_seconds=60,
            task_step_timeout_seconds=10,
            max_transient_retries=3,
            resolve_steps=lambda _: (step,),
            retry_backoff_cap=timedelta(seconds=30),
        )

        assert await runner.run(task_id, lease_owner="calendar-worker", retry_delay=timedelta(seconds=60))

        async with sessions() as session:
            task = await session.get(TaskRunModel, task_id)
            outbox = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
            )
        assert task is not None
        assert task.status == TaskStatus.RETRY_SCHEDULED.value
        assert task.error_code == "google_service_unavailable"
        assert task.attempt_count == 1
        assert outbox is not None
        assert outbox.topic == "task.execute"
        assert outbox.aggregate_id == task_id
        assert outbox.available_at == now + timedelta(seconds=30)
        assert outbox.deduplication_key == f"task.execute:{task_id}:retry:1"

        # 模拟 relay 已交接耐久重试，再次进入真实 step 时 GETDEL 已清空故障场景。
        async with sessions.begin() as session:
            retry_outbox = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
            )
            assert retry_outbox is not None
            retry_outbox.published_at = now
        assert await runner.run(
            task_id, lease_owner="calendar-worker-retry", retry_delay=timedelta(seconds=60)
        )
        async with sessions() as session:
            retried_task = await session.get(TaskRunModel, task_id)
        assert retried_task is not None
        assert retried_task.status == TaskStatus.SUCCEEDED.value
        assert retried_task.attempt_count == 2
        assert retried_task.error_code is None
    finally:
        await redis.delete(TestScenarioStore.key(user_id=user_id))
        await redis.aclose()
        await sessions.dispose()


async def _token() -> str:
    return "synthetic"


async def _none() -> None:
    return None
