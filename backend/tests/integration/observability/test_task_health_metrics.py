"""验证来自 PostgreSQL 任务与同步边界的真实可观测性采集。"""

from datetime import UTC, datetime, time, timedelta

import pytest

from ai_employee.domain.errors import PermanentProviderError
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel, SyncCursorModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.observability.metrics import create_metrics
from ai_employee.infrastructure.observability.sync import (
    observe_provider_sync,
    refresh_sync_age_metrics,
)
from ai_employee.workers.observability import refresh_stuck_task_metrics


@pytest.mark.asyncio
async def test_stuck_task_probe_groups_only_running_tasks_with_expired_lease(
    database_url: str,
) -> None:
    """探针必须按 kind 汇总租约过期的 RUNNING 任务，不能把排队或终态任务算入。"""
    sessions = build_session_factory(database_url)
    now = datetime(2030, 1, 2, tzinfo=UTC)
    try:
        async with sessions.begin() as session:
            user = UserModel(
                email="telemetry-owner@example.test",
                display_name="Telemetry Owner",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            session.add_all(
                (
                    TaskRunModel(
                        user_id=user.id,
                        kind="sync_mail",
                        status=TaskStatus.RUNNING.value,
                        idempotency_key="stuck-mail",
                        input_payload={},
                        lease_expires_at=now - timedelta(seconds=1),
                    ),
                    TaskRunModel(
                        user_id=user.id,
                        kind="daily_brief",
                        status=TaskStatus.RUNNING.value,
                        idempotency_key="stuck-brief",
                        input_payload={},
                        lease_expires_at=now - timedelta(seconds=1),
                    ),
                    TaskRunModel(
                        user_id=user.id,
                        kind="daily_brief",
                        status=TaskStatus.QUEUED.value,
                        idempotency_key="queued",
                        input_payload={},
                    ),
                )
            )
        metrics = create_metrics()

        await refresh_stuck_task_metrics(session_factory=sessions, metrics=metrics, now=now)

        rendered = metrics.render().body.decode()
        assert 'ai_employee_stuck_tasks{kind="daily_brief"} 1.0' in rendered
        assert 'ai_employee_stuck_tasks{kind="sync_mail"} 1.0' in rendered
        assert 'ai_employee_stuck_tasks{kind="queued"}' not in rendered
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_sync_age_probe_recovers_last_success_without_resetting_failed_attempts(
    database_url: str,
) -> None:
    """进程重启后只依据持久成功时刻恢复年龄，失败尝试不能伪造新鲜度。"""
    sessions = build_session_factory(database_url)
    now = datetime(2030, 1, 2, tzinfo=UTC)
    try:
        async with sessions.begin() as session:
            user = UserModel(
                email="sync-telemetry-owner@example.test",
                display_name="Sync Telemetry Owner",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="google",
                provider_account_id="sync-telemetry-account",
                account_email="sync-telemetry@example.test",
                scopes=[],
                status="connected",
                last_error_code=None,
            )
            microsoft_connection = OAuthConnectionModel(
                user_id=user.id,
                provider="microsoft",
                provider_account_id="sync-telemetry-microsoft-account",
                provider_tenant_id="sync-telemetry-tenant",
                account_type="work_school",
                account_email="sync-telemetry-microsoft@example.test",
                scopes=[],
                status="connected",
                last_error_code=None,
            )
            session.add_all((connection, microsoft_connection))
            await session.flush()
            session.add_all(
                (
                    SyncCursorModel(
                        connection_id=connection.id,
                        resource_kind="mail",
                        scope_key="mailbox",
                        cursor="unchanged-after-failure",
                        last_success_at=now - timedelta(minutes=20),
                        last_attempt_at=now - timedelta(minutes=1),
                        last_error_code="google_rate_limited",
                    ),
                    SyncCursorModel(
                        connection_id=microsoft_connection.id,
                        resource_kind="calendar",
                        scope_key="directory",
                        cursor="microsoft-directory-cursor",
                        last_success_at=now - timedelta(minutes=35),
                        last_attempt_at=now - timedelta(minutes=2),
                        last_error_code="microsoft_calendar_rate_limited",
                    ),
                )
            )
        metrics = create_metrics()

        await refresh_sync_age_metrics(session_factory=sessions, metrics=metrics, now=now)

        rendered = metrics.render().body.decode()
        assert 'ai_employee_sync_age_seconds{provider="google",resource="mail"} 1200.0' in rendered
        assert (
            'ai_employee_sync_age_seconds{provider="microsoft",resource="calendar"} 2100.0'
            in rendered
        )
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_provider_sync_observer_records_permanent_error_code() -> None:
    """固定供应商错误也必须进入脱敏指标，并保持原异常语义。"""
    metrics = create_metrics()
    expected = PermanentProviderError(
        error_code="microsoft_calendar_invalid_response",
        message="sensitive provider response must not become a metric label",
    )

    async def fail_permanently() -> None:
        """模拟适配器在规范化边界拒绝畸形供应商响应。"""
        raise expected

    with pytest.raises(PermanentProviderError) as raised:
        await observe_provider_sync(
            provider="microsoft",
            metrics=metrics,
            resource="calendar",
            operation=fail_permanently,
        )

    rendered = metrics.render().body.decode("utf-8")
    assert raised.value is expected
    assert (
        'ai_employee_provider_errors_total{error_code="microsoft_calendar_invalid_response",provider="microsoft"} 1.0'
        in rendered
    )
    assert "sensitive provider response" not in rendered
