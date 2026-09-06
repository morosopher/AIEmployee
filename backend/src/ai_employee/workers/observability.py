"""为 Worker 与 Scheduler 组合根启动内部指标和追踪。"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.application.ports.trusted_actions import TrustedActionAdapterRegistry
from ai_employee.config import Settings
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.observability.logging import configure_json_logging
from ai_employee.infrastructure.observability.metrics import (
    M2_ACTIONS,
    M2_PROVIDERS,
    Metrics,
    create_metrics,
)
from ai_employee.infrastructure.observability.tracing import initialize_tracing


def initialize_process_metrics(*, metrics: Metrics, process: str, port: int) -> None:
    """启动内部 Prometheus listener 并写入初始新鲜心跳。

    Args:
        metrics: 当前独立进程唯一的 metrics registry。
        process: 固定 ``worker`` 或 ``scheduler`` 维度。
        port: 仅容器网络监听的配置端口。

    不在这里创建 Caddy 路由或公网端口。调用方只在 Taskiq 生命周期真正启动后调用，防止
    单元测试和 CLI 的任务发现阶段意外占用端口。
    """
    metrics.start_internal_listener(port=port)
    metrics.record_heartbeat(process=process, age_seconds=0)


def initialize_process_observability(
    *, settings: Settings, session_factory: ManagedAsyncSessionMaker, process: str
) -> Metrics | None:
    """为非 ASGI 进程装配脱敏日志、SQL tracing 和可选 metrics。

    Args:
        settings: 已验证的进程配置。
        session_factory: 当前进程拥有的 SQLAlchemy engine 工厂。
        process: 固定进程名，决定指标监听端口与心跳标签。

    Returns:
        已启动的 metrics；当配置禁用时返回 ``None``。
    """
    configure_json_logging(tuple(settings.model_redaction_patterns))
    initialize_tracing(
        app=None,
        async_engine=session_factory.engine,
        enabled=settings.otel_enabled,
        service_name=settings.otel_service_name,
        endpoint=settings.otel_exporter_otlp_endpoint,
    )
    if not settings.metrics_enabled:
        return None
    metrics = create_metrics()
    port = settings.worker_metrics_port if process == "worker" else settings.scheduler_metrics_port
    initialize_process_metrics(metrics=metrics, process=process, port=port)
    return metrics


def build_process_session_factory(settings: Settings) -> ManagedAsyncSessionMaker:
    """构造只供进程启动追踪和健康探测复用的数据库资源。

    Worker 任务本身仍在每个消息中创建可释放工厂，保持既有事务与资源语义；这个工厂由
    生命周期 shutdown 统一释放，且不携带 Redis 事件发布器。
    """
    return build_session_factory(settings.database_url)


async def refresh_stuck_task_metrics(
    *, session_factory: ManagedAsyncSessionMaker, metrics: Metrics, now: datetime
) -> None:
    """以有界聚合查询更新租约过期的 RUNNING 任务数量。

    Args:
        session_factory: Worker 已持有的 PostgreSQL 会话工厂，仅执行只读聚合查询。
        metrics: 当前进程的独立 Prometheus registry。
        now: 调用方注入的 UTC 参考时刻，便于测试且不依赖宿主机时区。

    不返回任务 ID、用户 ID 或输入载荷。查询只检查持久状态机已写入的 ``RUNNING`` 与
    ``lease_expires_at``，并按受控任务 kind 聚合，故一次调用的工作量受数据库 group 数量
    而非卡住任务总数约束。
    """
    try:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(TaskRunModel.kind, func.count(TaskRunModel.id))
                    .where(
                        TaskRunModel.status == "running",
                        TaskRunModel.lease_expires_at.is_not(None),
                        TaskRunModel.lease_expires_at <= now,
                    )
                    .group_by(TaskRunModel.kind)
                )
            ).all()
    except SQLAlchemyError:
        # 指标 probe 不属于任务状态机；数据库短暂不可达时宁可缺一个样本，也不能阻断
        # 已经收到的至少一次消息或改变其错误分类。
        return
    active_kinds = {str(kind) for kind, _count in rows}
    metrics.clear_stuck_tasks(active_kinds=active_kinds)
    for kind, count in rows:
        metrics.record_stuck_tasks(kind=kind, count=int(count))


def observe_write_runtime(
    *, metrics: Metrics, settings: Settings, adapters: TrustedActionAdapterRegistry
) -> None:
    """比较进程有效开关与固定 registry 的可用性；只检查本地组合，不调用供应商。

    开关关闭时已注册 adapter 仍被应用门禁保护，属于正常就绪状态；开关开启但四个
    M2 动作未完整组装时报告 mismatch。不同进程的开关漂移由 Prometheus 聚合比较。
    """
    for provider in M2_PROVIDERS:
        enabled = settings.provider_writes_enabled(provider)
        metrics.record_write_kill_switch(provider=provider, enabled=enabled)
        ready = True
        if enabled:
            for action in M2_ACTIONS:
                try:
                    adapters.trusted_action_adapter(provider=provider, action=action)
                except StateConflictError:
                    ready = False
        metrics.record_dependency_health(
            dependency=f"{provider}_write_adapter_policy", healthy=ready
        )


async def refresh_trusted_action_metrics(
    *,
    session_factory: ManagedAsyncSessionMaker,
    metrics: Metrics,
    now: datetime,
) -> None:
    """从持久事实恢复 M2 状态 Gauge；只查询有限组数，不读取身份或内容。

    这是跨用户的进程级维护聚合，结果只有固定 provider/action/capability/state。查询
    不 materialize 用户、命令或供应商对象。数据库失败时保留旧样本，不谎报恢复；心跳
    的独立年龄继续增长，后续成功扫描会显式清零已经消失的分组。
    """
    try:
        async with session_factory() as session:
            entered = (
                select(
                    AuditEventModel.task_id,
                    func.max(AuditEventModel.created_at).label("entered_at"),
                )
                .where(
                    AuditEventModel.event_type == "tool.needs_attention",
                )
                .group_by(AuditEventModel.task_id)
                .subquery()
            )
            rows = (
                await session.execute(
                    select(
                        ToolExecutionModel.provider,
                        ToolExecutionModel.tool_name,
                        func.count().label("unresolved"),
                        func.count()
                        .filter(TaskRunModel.status == "needs_attention")
                        .label("attention"),
                        func.min(
                            func.coalesce(
                                ToolExecutionModel.request_started_at,
                                ToolExecutionModel.claimed_at,
                                TaskRunModel.updated_at,
                            )
                        ).label("oldest"),
                        func.count()
                        .filter(
                            (TaskRunModel.status == "needs_attention")
                            & (
                                func.coalesce(entered.c.entered_at, TaskRunModel.updated_at)
                                <= now - timedelta(minutes=15)
                            )
                        )
                        .label("overdue"),
                    )
                    .join(TaskRunModel, TaskRunModel.id == ToolExecutionModel.task_id)
                    .outerjoin(
                        entered,
                        entered.c.task_id == TaskRunModel.id,
                    )
                    .where(
                        TaskRunModel.kind == "trusted_action",
                        TaskRunModel.status.in_(("reconciling", "needs_attention")),
                        ToolExecutionModel.provider.in_(M2_PROVIDERS),
                        ToolExecutionModel.tool_name.in_(M2_ACTIONS),
                    )
                    .group_by(ToolExecutionModel.provider, ToolExecutionModel.tool_name)
                )
            ).all()
            capabilities = (
                await session.execute(
                    select(
                        OAuthConnectionModel.provider,
                        ConnectionCapabilityModel.capability,
                        ConnectionCapabilityModel.status,
                        func.count(),
                    )
                    .join(
                        OAuthConnectionModel,
                        (OAuthConnectionModel.id == ConnectionCapabilityModel.connection_id)
                        & (OAuthConnectionModel.user_id == ConnectionCapabilityModel.user_id),
                    )
                    .where(OAuthConnectionModel.provider.in_(M2_PROVIDERS))
                    .group_by(
                        OAuthConnectionModel.provider,
                        ConnectionCapabilityModel.capability,
                        ConnectionCapabilityModel.status,
                    )
                )
            ).all()
    except SQLAlchemyError:
        return
    groups = {(row.provider, row.tool_name): row for row in rows}
    for provider in M2_PROVIDERS:
        for action in M2_ACTIONS:
            row = groups.get((provider, action))
            metrics.record_action_state(
                provider=provider,
                action=action,
                needs_attention=0 if row is None else row.attention,
                age_seconds=0 if row is None else max((now - row.oldest).total_seconds(), 0),
                unresolved=row is not None,
            )
    counts = {
        (provider, capability, state): count for provider, capability, state, count in capabilities
    }
    for provider in M2_PROVIDERS:
        for capability in ConnectionCapability:
            for state in CapabilityStatus:
                metrics.record_capability_state(
                    provider=provider,
                    capability=capability.value,
                    state=state.value,
                    count=counts.get((provider, capability.value, state.value), 0),
                )
    # 此 maintenance kind 不加入普通 RUNNING 租约扫描的清零集合，避免周期交错抹掉告警。
    metrics.stuck_tasks.labels(kind="trusted_action_needs_attention_overdue").set(
        sum(row.overdue for row in rows)
    )
