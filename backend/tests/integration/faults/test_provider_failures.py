"""验证供应商和模型失败保持明确、可恢复且不伪造完整简报。"""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from ai_employee.application.use_cases.task_execution import DurableTaskRunner, LeasedTask
from ai_employee.domain.errors import TransientProviderError
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel
from ai_employee.infrastructure.db.models.tasks import OutboxEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyGmailSyncRepositoryFactory
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.integrations.google.gmail import GmailAdapter


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
async def test_calendar_5xx_persists_capped_retry_scheduled_outbox(database_url: str) -> None:
    """Calendar 5xx 必须由真实租约事务保存延迟 Outbox，而非依赖 Redis 重试。"""
    sessions = build_session_factory(database_url)
    now = datetime(2030, 1, 2, tzinfo=UTC)
    user_id, task_id = uuid4(), uuid4()

    class CalendarFailureStep:
        """模拟经规范化后的 Calendar 5xx，不携带供应商原始响应。"""

        name = "sync_calendar"

        async def execute(self, task: LeasedTask) -> None:
            """在已获得 PostgreSQL 租约后抛出稳定临时错误。"""
            del task
            raise TransientProviderError(
                error_code="google_service_unavailable",
                message="Synthetic Calendar service unavailable",
                retry_after=999,
            )

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
                TaskRunModel(
                    id=task_id,
                    user_id=user_id,
                    kind="sync_calendar",
                    status=TaskStatus.QUEUED.value,
                    idempotency_key=f"calendar-5xx:{task_id}",
                    input_payload={"connection_id": str(uuid4())},
                )
            )
        runner = DurableTaskRunner(
            store=SqlAlchemyTaskExecutionStore(sessions),
            clock=lambda: now,
            lease_duration=timedelta(seconds=30),
            task_timeout_seconds=60,
            task_step_timeout_seconds=10,
            max_transient_retries=3,
            resolve_steps=lambda _: (CalendarFailureStep(),),
            retry_backoff_cap=timedelta(seconds=30),
        )

        assert await runner.run(task_id, lease_owner="calendar-worker", retry_delay=timedelta(seconds=5))

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
        assert outbox.available_at == now + timedelta(seconds=30)
        assert outbox.deduplication_key == f"task.execute:{task_id}:retry:1"
    finally:
        await sessions.dispose()


async def _token() -> str:
    return "synthetic"


async def _none() -> None:
    return None
