"""把 Task 8 五个固定 Taskiq label 入口连接到应用用例与 PostgreSQL 适配器。"""

import asyncio
import hashlib
import logging
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID

from taskiq import TaskiqEvents

from ai_employee.application.use_cases.approval_checkpoint_recovery import (
    RecoverApprovalCheckpointsUseCase,
)
from ai_employee.application.use_cases.approvals import ExpireApprovalsUseCase
from ai_employee.application.use_cases.connections import (
    OAuthRevokeBacklog,
    OAuthRevokeMaintenanceUseCase,
)
from ai_employee.application.use_cases.diagnostics import DispatchOverdueBriefDiagnosticsUseCase
from ai_employee.application.use_cases.maintenance import ExpireSessionsUseCase
from ai_employee.application.use_cases.outbox import OutboxRelay
from ai_employee.application.use_cases.schedules import (
    DispatchDueDailyBriefsUseCase,
    daily_brief_idempotency_key,
    is_daily_brief_due,
    scheduled_daily_brief_instant,
)
from ai_employee.application.use_cases.task_execution import utc_instant
from ai_employee.application.use_cases.task_retry_recovery import (
    RecoverScheduledTaskRetriesUseCase,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.application.use_cases.trusted_actions import (
    InvalidateDisabledTrustedActionsUseCase,
)
from ai_employee.config import get_settings
from ai_employee.infrastructure.db.repositories.approval_checkpoint_recovery import (
    SqlAlchemyApprovalCheckpointRecoveryStore,
)
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.calendar import (
    SqlAlchemyEnabledSyncScopeReader,
)
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStoreFactory
from ai_employee.infrastructure.db.repositories.diagnostics import SqlAlchemyOverdueBriefReader
from ai_employee.infrastructure.db.repositories.identity import (
    SqlAlchemyActiveUserScheduleReader,
    SqlAlchemySessionMaintenanceRepositoryFactory,
)
from ai_employee.infrastructure.db.repositories.task_retry_recovery import (
    SqlAlchemyTaskRetryRecoveryStore,
)
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepositoryFactory
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionReconciliationRecoveryStore,
    SqlAlchemyTrustedActionRevocationStore,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.events.publisher import TaskEventPublisher
from ai_employee.infrastructure.observability.metrics import (
    M2_PROVIDERS,
    Metrics,
    run_periodic_heartbeat,
)
from ai_employee.infrastructure.queue.broker import broker
from ai_employee.workers.execute_task import execute_task, get_worker_metrics
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
    "monitor_oauth_revoke_backlog",
    "recover_approval_checkpoints",
    "recover_due_reconciliations",
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
        for provider in M2_PROVIDERS:
            _scheduler_metrics.record_write_kill_switch(
                provider=provider, enabled=settings.provider_writes_enabled(provider)
            )
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


def _maintenance_metrics() -> Metrics | None:
    """Taskiq cron 消息实际由 Worker 执行；仅在 Scheduler 内运行时使用其 registry。"""
    return _scheduler_metrics if _scheduler_metrics is not None else get_worker_metrics()


@broker.task(schedule=[{"cron": "* * * * *", "schedule_id": "outbox-relay"}])
async def relay_outbox() -> None:
    """每分钟 claim 并投递一批到期未发布 Outbox 事件。"""
    await _build_outbox_relay().relay_once(limit=settings.outbox_relay_batch_size)
    metrics = _maintenance_metrics()
    if metrics is not None:
        metrics.record_heartbeat(process="outbox_relay", age_seconds=0)


@broker.task(schedule=[{"cron": "* * * * *", "schedule_id": "recover-task-retries"}])
async def recover_task_retries() -> None:
    """每分钟从 PostgreSQL 恢复 Redis 延迟调度丢失的到期重试，再由 relay 异步投递。"""
    use_case = RecoverScheduledTaskRetriesUseCase(
        store=SqlAlchemyTaskRetryRecoveryStore(session_factory)
    )
    await use_case.execute(now=datetime.now(UTC), limit=settings.outbox_relay_batch_size)
    await recover_due_reconciliations(now=datetime.now(UTC), limit=settings.outbox_relay_batch_size)


async def recover_due_reconciliations(*, now: datetime, limit: int) -> int:
    """补建到期可信动作只读核对 Outbox，不改变 ``reconciling`` 任务状态。

    扫描器与普通 retry recovery 共用既有 ``recover-task-retries`` label，避免增加第二个
    Scheduler job。数据库事务由本函数打开，repository 本身不提交；Redis 投递仍由下一个
    ``outbox-relay`` tick 负责。
    """
    now = utc_instant(now, field="now")
    # 该扫描只读 TaskRun/ToolExecution/Outbox 调度事实；它不能因为 Worker Secret 未挂载
    # 或正在轮换而失败，也绝不需要解密冻结命令。实际命令解密仅发生在核对 Worker。
    recovered = await SqlAlchemyTrustedActionReconciliationRecoveryStore(
        session_factory
    ).recover_due_reconciliations(now=now, limit=limit)
    metrics = _maintenance_metrics()
    if metrics is not None:
        metrics.record_heartbeat(process="reconciliation_scanner", age_seconds=0)
    return recovered


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
    """每十分钟为 Google/Microsoft enabled read owner 创建耐久幂等同步任务。

    Task 12/13 将所有支持 provider 的 Calendar 普通周期收敛到连接级 ``directory`` owner：
    该任务在同一供应商中立用例内按稳定 calendar ID 串行推进每个事件 scope。这里仍保留
    对旧 ``EnabledSyncScopeReader`` 输出的防线，避免过渡期 reader 同时返回 directory 与
    旧单日历 scope 时重复排队；显式单日历维修任务不经过本周期入口。
    """
    now = datetime.now(UTC)
    bucket = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
    creator = CreateTaskUseCase(
        SqlAlchemyTaskRepositoryFactory(session_factory), dispatcher=_build_outbox_relay()
    )
    calendar_owners: set[tuple[UUID, UUID]] = set()
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
        if scope.resource_kind == "calendar":
            # 目录是所有支持 provider 的 Calendar 周期 owner；事件 scope 只在目录用例
            # 内部或维修路径显式执行，不能因旧 reader 返回多个游标而重复访问供应商。
            owner_key = (scope.user_id, scope.connection_id)
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
    """每分钟先处理停机开关，再清理过期待审批并统计 token-free 撤销积压。"""
    await InvalidateDisabledTrustedActionsUseCase(
        store=SqlAlchemyTrustedActionRevocationStore(session_factory), write_policy=settings
    ).execute(limit=settings.outbox_relay_batch_size)
    use_case = ExpireApprovalsUseCase(
        SqlAlchemyApprovalStore(session_factory, metrics=_maintenance_metrics())
    )
    await use_case.execute(now=datetime.now(UTC), limit=settings.outbox_relay_batch_size)
    await monitor_oauth_revoke_backlog()


class _MaintenanceClock:
    """向维护用例提供 UTC 时间，不依赖宿主机本地业务日期。"""

    def now(self) -> datetime:
        """返回带时区的 UTC 当前时刻。"""
        return datetime.now(UTC)


async def monitor_oauth_revoke_backlog() -> tuple[OAuthRevokeBacklog, ...]:
    """按供应商计数并发出内容无关告警；绝不恢复 token 或安排远端 revoke 重试。

    本函数复用既有分钟维护入口，不新增携带连接/token 的队列消息。返回类型化聚合，
    供可观测性消费者读取；日志仅携带固定供应商、错误码和整数计数。
    """
    backlog = await OAuthRevokeMaintenanceUseCase(
        SqlAlchemyConnectionStoreFactory(session_factory), _MaintenanceClock()
    ).scan()
    metrics = _maintenance_metrics()
    if metrics is not None:
        counts = {item.provider: item.unresolved_count for item in backlog}
        for provider in M2_PROVIDERS:
            # 复用既有维护积压 Gauge；不加入普通 RUNNING 租约扫描的清零集合。
            metrics.stuck_tasks.labels(kind=f"oauth_revoke_{provider}").set(counts.get(provider, 0))
    for item in backlog:
        if item.unresolved_count > 0:
            logging.getLogger("ai_employee.oauth.revoke_backlog").warning(
                "oauth revocation requires operator remediation",
                extra={
                    "provider": item.provider,
                    "error_code": "oauth_revoke_unresolved",
                    "unresolved_count": item.unresolved_count,
                },
            )
    return backlog


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
