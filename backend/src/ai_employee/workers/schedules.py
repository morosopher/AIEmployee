"""把 Task 7 四个固定 Taskiq label 入口连接到应用用例与 PostgreSQL 适配器。"""

from datetime import UTC, datetime

from ai_employee.application.use_cases.maintenance import ExpireSessionsUseCase
from ai_employee.application.use_cases.outbox import OutboxRelay
from ai_employee.application.use_cases.schedules import (
    DispatchDueDailyBriefsUseCase,
    daily_brief_idempotency_key,
    is_daily_brief_due,
    scheduled_daily_brief_instant,
)
from ai_employee.application.use_cases.task_retry_recovery import (
    RecoverScheduledTaskRetriesUseCase,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.config import get_settings
from ai_employee.infrastructure.db.repositories.identity import (
    SqlAlchemyActiveUserScheduleReader,
    SqlAlchemySessionMaintenanceRepositoryFactory,
)
from ai_employee.infrastructure.db.repositories.task_retry_recovery import (
    SqlAlchemyTaskRetryRecoveryStore,
)
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepositoryFactory
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.queue.broker import broker
from ai_employee.workers.execute_task import execute_task
from ai_employee.workers.outbox import build_outbox_relay

__all__ = [
    "daily_brief_idempotency_key",
    "dispatch_due_briefs",
    "expire_sessions",
    "is_daily_brief_due",
    "recover_task_retries",
    "relay_outbox",
    "scheduled_daily_brief_instant",
]

settings = get_settings()
session_factory = build_session_factory(settings.database_url)


def _build_outbox_relay() -> OutboxRelay:
    """用同一数据库池与 Taskiq enqueue adapter 构造即时/分钟共享的 relay 路径。"""
    return build_outbox_relay(
        session_factory=session_factory,
        settings=settings,
        task_sender=execute_task.kiq,
    )


@broker.task(schedule=[{"cron": "* * * * *", "schedule_id": "outbox-relay"}])
async def relay_outbox() -> None:
    """每分钟 claim 并投递一批到期未发布 Outbox 事件。"""
    await _build_outbox_relay().relay_once(limit=settings.outbox_relay_batch_size)


@broker.task(schedule=[{"cron": "* * * * *", "schedule_id": "recover-task-retries"}])
async def recover_task_retries() -> None:
    """每分钟从 PostgreSQL 恢复 Redis 延迟调度丢失的到期重试，再由 relay 异步投递。"""
    use_case = RecoverScheduledTaskRetriesUseCase(
        store=SqlAlchemyTaskRetryRecoveryStore(session_factory)
    )
    await use_case.execute(now=datetime.now(UTC), limit=settings.outbox_relay_batch_size)


@broker.task(schedule=[{"cron": "* * * * *", "schedule_id": "due-daily-briefs"}])
async def dispatch_due_briefs() -> None:
    """每分钟按用户本地日期扫描到期简报，并在提交后立即走同一 Outbox relay。"""
    relay = _build_outbox_relay()
    creator = CreateTaskUseCase(
        SqlAlchemyTaskRepositoryFactory(session_factory),
        dispatcher=relay,
    )
    use_case = DispatchDueDailyBriefsUseCase(
        reader=SqlAlchemyActiveUserScheduleReader(session_factory),
        task_creator=creator,
    )
    await use_case.execute(now=datetime.now(UTC))


@broker.task(schedule=[{"cron": "0 * * * *", "schedule_id": "expire-sessions"}])
async def expire_sessions() -> None:
    """每小时调用应用用例，在短事务中真实删除已到期会话摘要。"""
    use_case = ExpireSessionsUseCase(SqlAlchemySessionMaintenanceRepositoryFactory(session_factory))
    await use_case.execute(now=datetime.now(UTC))
