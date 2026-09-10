"""验证逾期简报告警在 PostgreSQL 持久化边界上的用户隔离。"""

from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from ai_employee.application.use_cases.diagnostics import GetDailyBriefOverdueAlertUseCase
from ai_employee.infrastructure.db.models.briefs import DailyBriefModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel, SyncCursorModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.diagnostics import (
    SqlAlchemyDiagnosticSnapshotStore,
    SqlAlchemyOverdueBriefReader,
)
from ai_employee.infrastructure.db.session import build_session_factory


@pytest.mark.asyncio
async def test_task27e_diagnostics_include_both_provider_scopes_without_content(
    database_url: str,
) -> None:
    """同用户 Google/Microsoft 游标均参与来源新鲜度，另一用户和 raw cursor 都不可见。"""
    from tests.integration.retention.test_m2_action_retention import NOW, seed_lifecycle_action

    sessions = build_session_factory(database_url)
    try:
        seed = await seed_lifecycle_action(sessions)
        other = await seed_lifecycle_action(sessions)
        microsoft_id = uuid4()
        async with sessions.begin() as session:
            session.add(
                OAuthConnectionModel(
                    id=microsoft_id,
                    user_id=seed.user_id,
                    provider="microsoft",
                    provider_account_id="synthetic-ms",
                    provider_tenant_id="synthetic-tenant",
                    account_type="work_school",
                    account_email="synthetic@example.test",
                    scopes=[],
                    status="connected",
                )
            )
            await session.flush()
            for connection_id, kind in (
                (seed.connection_id, "mail"),
                (microsoft_id, "calendar"),
                (other.connection_id, "other"),
            ):
                session.add(
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind=kind,
                        scope_key="synthetic-scope",
                        cursor="synthetic-sensitive-cursor",
                        last_success_at=NOW,
                    )
                )
        await SqlAlchemyDiagnosticSnapshotStore(sessions).save_snapshot(
            task_id=seed.task_id, user_id=seed.user_id, now=NOW
        )
        async with sessions() as session:
            payload = await session.scalar(
                select(TaskRunModel.result_payload).where(TaskRunModel.id == seed.task_id)
            )
            assert payload is not None
            assert payload["freshness"] == [
                {"resource": "calendar", "last_success_at": NOW.isoformat(), "error_code": None},
                {"resource": "mail", "last_success_at": NOW.isoformat(), "error_code": None},
            ]
            assert "synthetic-sensitive-cursor" not in str(payload)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_alert_query_is_user_scoped_and_failed_brief_does_not_suppress(
    database_url: str,
) -> None:
    """另一用户的 complete 简报不能抑制目标用户 failed 简报对应的逾期告警。

    本测试使用两个独立 ``user_id`` 的合成记录，直接穿过 PostgreSQL Repository 与应用用例，
    因而同时约束 SQL 的拥有者条件、失败完整度语义和诊断任务链接的可见性。
    """
    session_factory = build_session_factory(database_url)
    now = datetime(2026, 8, 1, 0, 16, tzinfo=UTC)
    local_date = date(2026, 8, 1)
    try:
        async with session_factory.begin() as session:
            overdue_user = UserModel(
                email="overdue-alert-owner@example.test",
                display_name="Overdue Alert Owner",
                password_hash=None,
                timezone="Asia/Shanghai",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            )
            complete_user = UserModel(
                email="overdue-alert-other@example.test",
                display_name="Other User",
                password_hash=None,
                timezone="Asia/Shanghai",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            )
            session.add_all((overdue_user, complete_user))
            await session.flush()
            failed_task = TaskRunModel(
                user_id=overdue_user.id,
                kind="daily_brief",
                status="failed",
                idempotency_key="failed-brief-owner",
                input_payload={},
                finished_at=now,
            )
            complete_task = TaskRunModel(
                user_id=complete_user.id,
                kind="daily_brief",
                status="succeeded",
                idempotency_key="complete-brief-other",
                input_payload={},
                finished_at=now,
            )
            diagnostic_task = TaskRunModel(
                user_id=overdue_user.id,
                kind="brief.overdue_diagnostic",
                status="queued",
                idempotency_key=(
                    f"diagnostic:daily-brief:{overdue_user.id}:{local_date.isoformat()}"
                ),
                input_payload={"local_date": local_date.isoformat()},
            )
            session.add_all((failed_task, complete_task, diagnostic_task))
            await session.flush()
            session.add_all(
                (
                    _brief(
                        user_id=overdue_user.id,
                        task_id=failed_task.id,
                        local_date=local_date,
                        completeness="failed",
                        now=now,
                    ),
                    _brief(
                        user_id=complete_user.id,
                        task_id=complete_task.id,
                        local_date=local_date,
                        completeness="complete",
                        now=now,
                    ),
                )
            )

        use_case = GetDailyBriefOverdueAlertUseCase(
            reader=SqlAlchemyOverdueBriefReader(session_factory)
        )
        owner_alert = await use_case.execute(user_id=overdue_user.id, now=now)
        other_alert = await use_case.execute(user_id=complete_user.id, now=now)

        assert owner_alert is not None
        assert owner_alert.diagnostic_task_id == diagnostic_task.id
        assert other_alert is None
    finally:
        await session_factory.dispose()


def _brief(
    *,
    user_id: UUID,
    task_id: UUID,
    local_date: date,
    completeness: str,
    now: datetime,
) -> DailyBriefModel:
    """构造不含真实来源内容的最小每日简报持久化记录。

    Args:
        user_id: 简报与其所属任务的同一用户标识。
        task_id: 生成该版本简报的耐久任务标识。
        local_date: 用户时区内用于唯一版本化的业务日期。
        completeness: 测试覆盖的 complete 或 failed 完整度。
        now: 合成的 UTC 来源截止与创建时间。

    Returns:
        可写入集成测试数据库的最小 ORM 实体。
    """
    return DailyBriefModel(
        user_id=user_id,
        local_date=local_date,
        version=1,
        task_id=task_id,
        completeness=completeness,
        source_cutoff=now,
        headline="Synthetic brief",
        structured_content={},
        markdown="Synthetic brief",
        warnings=[],
        created_at=now,
    )
