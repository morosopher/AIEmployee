"""组合 Outbox 应用用例、SQLAlchemy store 与 Taskiq enqueue adapter。"""

from datetime import UTC, datetime, timedelta

from ai_employee.application.use_cases.outbox import OutboxClock, OutboxRelay
from ai_employee.config import Settings
from ai_employee.infrastructure.db.repositories.outbox import SqlAlchemyOutboxStore
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.events.publisher import TaskEventPublisher
from ai_employee.infrastructure.queue.enqueue import TaskiqTaskEnqueuer, TaskSender


def _utc_now() -> datetime:
    """返回 composition 默认使用的当前 UTC 瞬间。"""
    return datetime.now(UTC)


def build_outbox_relay(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings,
    task_sender: TaskSender,
    clock: OutboxClock | None = None,
) -> OutboxRelay:
    """构造即时任务投递与分钟任务/生命周期扫描共享的 Outbox relay。

    Args:
        session_factory: 进程级异步数据库 Session factory。
        settings: 已验证的 claim 与退避配置。
        task_sender: Worker 注入的 Taskiq decorated task ``kiq`` 发送函数。
        clock: 可选确定性时钟；运行时省略以使用 UTC 当前时间。

    Returns:
        只依赖应用端口的 Outbox relay 用例。
    """
    return OutboxRelay(
        store=SqlAlchemyOutboxStore(
            session_factory,
            retry_recovery_delay=timedelta(seconds=settings.task_retry_recovery_seconds),
        ),
        enqueuer=TaskiqTaskEnqueuer(task_sender),
        event_publisher=TaskEventPublisher(settings.redis_url),
        clock=clock or _utc_now,
        claim_ttl=timedelta(seconds=settings.outbox_claim_seconds),
        retry_base=timedelta(seconds=settings.outbox_retry_base_seconds),
        retry_max=timedelta(seconds=settings.outbox_retry_max_seconds),
    )
