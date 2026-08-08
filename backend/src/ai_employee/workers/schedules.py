"""把 Task 8 五个固定 Taskiq label 入口连接到应用用例与 PostgreSQL 适配器。"""

import asyncio
import hashlib
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID

from taskiq import TaskiqEvents

from ai_employee.application.use_cases.approval_checkpoint_recovery import (
    RecoverApprovalCheckpointsUseCase,
)
from ai_employee.application.use_cases.approvals import ExpireApprovalsUseCase
from ai_employee.application.use_cases.diagnostics import DispatchOverdueBriefDiagnosticsUseCase
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
from ai_employee.infrastructure.db.repositories.approval_checkpoint_recovery import (
    SqlAlchemyApprovalCheckpointRecoveryStore,
)
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.calendar import (
    SqlAlchemyEnabledSyncScopeReader,
)
from ai_employee.infrastructure.db.repositories.diagnostics import SqlAlchemyOverdueBriefReader
from ai_employee.infrastructure.db.repositories.identity import (
    SqlAlchemyActiveUserScheduleReader,
    SqlAlchemySessionMaintenanceRepositoryFactory,
)
from ai_employee.infrastructure.db.repositories.task_retry_recovery import (
    SqlAlchemyTaskRetryRecoveryStore,
)
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepositoryFactory
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.events.publisher import TaskEventPublisher
from ai_employee.infrastructure.observability.metrics import Metrics, run_periodic_heartbeat
from ai_employee.infrastructure.queue.broker import broker
from ai_employee.workers.execute_task import execute_task
from ai_employee.workers.observability import initialize_process_observability
from ai_employee.workers.outbox import build_outbox_relay
from ai_employee.workers.retention import build_retention_cleanup_worker

__all__ = [
    "daily_brief_idempotency_key",
    "dispatch_due_briefs",
    "dispatch_google_incremental_syncs",
    "dispatch_overdue_brief_diagnostics",
    "expire_approvals",
    "expire_sessions",
    "is_daily_brief_due",
    "recover_approval_checkpoints",
    "recover_task_retries",
    "relay_outbox",
    "run_retention_cleanup",
    "scheduled_daily_brief_instant",
]

settings = get_settings()
session_factory = build_session_factory(
    settings.database_url,
    task_event_publisher=TaskEventPublisher(settings.redis_url),
)
_scheduler_metrics: Metrics | None = None
_scheduler_heartbeat_task: asyncio.Task[None] | None = None


@broker.on_event(TaskiqEvents.CLIENT_STARTUP)
async def initialize_scheduler_observability(_: object) -> None:
    """在 Scheduler broker 启动后初始化 tracing 和仅容器内部的指标 listener。

    Taskiq scheduler 使用 CLIENT 生命周期而非 WORKER 生命周期。该注册在 Worker 导入本模块时
    不会被调用，因此 9102 不会出现在 Worker 进程。
    """
    global _scheduler_metrics, _scheduler_heartbeat_task
    if not broker.is_scheduler_process:
        return
    _scheduler_metrics = initialize_process_observability(
        settings=settings, session_factory=session_factory, process="scheduler"
    )
    if _scheduler_metrics is not None:
        _scheduler_heartbeat_task = asyncio.create_task(
            run_periodic_heartbeat(metrics=_scheduler_metrics, process="scheduler")
        )


@broker.on_event(TaskiqEvents.CLIENT_SHUTDOWN)
async def shutdown_scheduler_observability(_: object) -> None:
    """Scheduler 退出时释放其模块级数据库引擎。"""
    global _scheduler_heartbeat_task
    heartbeat_task = _scheduler_heartbeat_task
    _scheduler_heartbeat_task = None
    if heartbeat_task is not None:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
    if broker.is_scheduler_process:
        await session_factory.dispose()


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
    if _scheduler_metrics is not None:
        _scheduler_metrics.record_heartbeat(process="scheduler", age_seconds=0)
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


@broker.task(schedule=[{"cron": "*/10 * * * *", "schedule_id": "google-incremental-sync"}])
async def dispatch_google_incremental_syncs() -> None:
    """每十分钟为 enabled read owner 创建耐久幂等同步任务。

    Task 12 将 Google Calendar 的普通周期收敛到连接级 ``directory`` owner：该任务在
    同一供应商中立用例内按稳定 calendar ID 串行推进每个事件 scope。这里仍保留对旧
    ``EnabledSyncScopeReader`` 输出的防线，避免过渡期 reader 同时返回 directory 与旧
    primary/team scope 时重复排队；显式单日历维修任务不经过本周期入口。
    """
    now = datetime.now(UTC)
    bucket = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
    creator = CreateTaskUseCase(
        SqlAlchemyTaskRepositoryFactory(session_factory), dispatcher=_build_outbox_relay()
    )
    calendar_owners: set[tuple[UUID, UUID, str]] = set()
    for scope in await SqlAlchemyEnabledSyncScopeReader(session_factory).enabled_scopes():
        # Reader 已在 SQL 层执行同一过滤；此处保留 fail-closed 防线，避免测试替身或未来
        # reader 误把 Microsoft folder cursor 当作第二个周期 owner。
        if (
            scope.provider == "microsoft"
            and scope.resource_kind == "mail"
            and scope.scope_key != "mailbox"
        ):
            continue
        kind = "sync_mail" if scope.resource_kind == "mail" else "sync_calendar"
        scope_key = scope.scope_key
        if scope.provider == "google" and scope.resource_kind == "calendar":
            # 目录是 Google Calendar 的周期 owner；事件 scope 只在目录用例内部或维修
            # 路径显式执行，不能因旧 reader 返回多个游标而重复访问供应商。
            owner_key = (scope.user_id, scope.connection_id, scope.provider)
            if owner_key in calendar_owners:
                continue
            calendar_owners.add(owner_key)
            scope_key = "directory"
        # scope 可能是包含帐号标识的 512 字符 opaque ID；任务载荷必须保留精确值，但
        # 幂等键只使用稳定摘要，既满足列长度上限，也避免在运维界面重复暴露该标识。
        scope_digest = hashlib.sha256(scope_key.encode("utf-8")).hexdigest()[:16]
        await creator.execute(
            user_id=scope.user_id,
            kind=kind,
            input_payload={
                "connection_id": str(scope.connection_id),
                "scope_key": scope_key,
            },
            idempotency_key=(
                f"sync:{scope.provider}:{scope.connection_id}:{scope.resource_kind}:"
                f"{scope_digest}:{bucket.isoformat()}"
            ),
        )


@broker.task(schedule=[{"cron": "0 * * * *", "schedule_id": "expire-sessions"}])
async def expire_sessions() -> None:
    """每小时调用应用用例，在短事务中真实删除已到期会话摘要。"""
    use_case = ExpireSessionsUseCase(SqlAlchemySessionMaintenanceRepositoryFactory(session_factory))
    await use_case.execute(now=datetime.now(UTC))


@broker.task(schedule=[{"cron": "* * * * *", "schedule_id": "expire-approvals"}])
async def expire_approvals() -> None:
    """每分钟锁定有界过期待审批并原子写入失败及无内容审计。"""
    use_case = ExpireApprovalsUseCase(SqlAlchemyApprovalStore(session_factory))
    await use_case.execute(now=datetime.now(UTC), limit=settings.outbox_relay_batch_size)


@broker.task(schedule=[{"cron": "* * * * *", "schedule_id": "brief-overdue-diagnostics"}])
async def dispatch_overdue_brief_diagnostics() -> None:
    """每分钟扫描真实逾期用户，并通过事务型创建用例写入诊断任务。"""
    creator = CreateTaskUseCase(
        SqlAlchemyTaskRepositoryFactory(session_factory), dispatcher=_build_outbox_relay()
    )
    await DispatchOverdueBriefDiagnosticsUseCase(
        reader=SqlAlchemyOverdueBriefReader(session_factory), task_creator=creator
    ).execute(now=datetime.now(UTC))


@broker.task(schedule=[{"cron": "30 2 * * *", "schedule_id": "retention-cleanup"}])
async def run_retention_cleanup() -> None:
    """每天 UTC 02:30 调用 retention 角色执行有界保留清理。"""
    await build_retention_cleanup_worker().execute(now=datetime.now(UTC))


@broker.task(schedule=[{"cron": "* * * * *", "schedule_id": "recover-approval-checkpoints"}])
async def recover_approval_checkpoints() -> None:
    """每分钟补投 Redis 丢失前尚未保存 interrupt checkpoint 的冻结审批。"""
    use_case = RecoverApprovalCheckpointsUseCase(
        store=SqlAlchemyApprovalCheckpointRecoveryStore(session_factory)
    )
    await use_case.execute(now=datetime.now(UTC), limit=settings.outbox_relay_batch_size)
