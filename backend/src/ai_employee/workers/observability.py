"""为 Worker 与 Scheduler 组合根启动内部指标和追踪。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.config import Settings
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.observability.logging import configure_json_logging
from ai_employee.infrastructure.observability.metrics import Metrics, create_metrics
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
