"""把 Taskiq 执行入口组合到基础设施无关的持久任务执行用例。"""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID

from ai_employee.application.use_cases.task_execution import DurableTaskRunner, LeasedTask
from ai_employee.config import get_settings
from ai_employee.domain.errors import InternalInvariantError, TransientProviderError
from ai_employee.infrastructure.db.repositories.task_execution import (
    SqlAlchemyTaskExecutionStore,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.queue.broker import broker


class _MissingTaskHandlerStep:
    """在真实 Graph 尚未接入时以稳定内部错误终止，绝不伪造任务成功。"""

    name = "resolve_task_handler"

    async def execute(self, task: LeasedTask) -> None:
        """拒绝执行尚无 M1 handler 的任务种类。

        Args:
            task: 已获取租约的任务快照。

        Raises:
            InternalInvariantError: 当前 Task 7 尚未注册后续 LangGraph handler。
        """
        raise InternalInvariantError(
            error_code="task_handler_not_registered",
            message="task handler is not registered",
            metadata={"task_kind": task.kind},
        )


@lru_cache
def build_task_runner() -> DurableTaskRunner:
    """按进程配置构造并缓存共享数据库连接池与持久执行用例。

    Returns:
        使用 SQLAlchemy store、UTC 时钟和当前 Task 7 超时配置的执行用例。

    Task 7 只建立可靠执行底座；后续已批准任务会把 resolver 替换为具体 Graph 节点。
    在此之前使用显式失败节点，避免收到队列消息后把未实现业务错误标记为成功。
    """
    settings = get_settings()
    session_factory = build_session_factory(settings.database_url)
    return DurableTaskRunner(
        store=SqlAlchemyTaskExecutionStore(session_factory),
        clock=lambda: datetime.now(UTC),
        lease_duration=timedelta(seconds=settings.task_lease_seconds),
        task_timeout_seconds=settings.task_timeout_seconds,
        task_step_timeout_seconds=settings.task_step_timeout_seconds,
        resolve_steps=lambda task: (_MissingTaskHandlerStep(),),
    )


@broker.task(retry_on_error=True)
async def execute_task(task_id: str) -> None:
    """解析 task_id，并只允许已持久化的临时供应商错误触发 Taskiq 重试。

    Args:
        task_id: Outbox 发送的规范 UUID 字符串；消息不包含任务正文或结果。

    Raises:
        TransientProviderError: 执行用例已把状态写为 RETRY_SCHEDULED 后原样重新抛出，
            交由 SmartRetryMiddleware 按固定策略重新投递。
    """
    try:
        parsed_task_id = UUID(task_id)
    except ValueError:
        # 队列载荷由内部 enqueue adapter 生成；非法值不能关联持久任务，也不得反复重试。
        return

    try:
        await build_task_runner().run(parsed_task_id)
    except TransientProviderError:
        raise
    except Exception:  # noqa: BLE001 - 未知异常绝不能进入仅供应商错误允许的 SmartRetry。
        # 正常可写数据库路径已由应用用例持久化安全 FAILED/error_code。这里只处理主失败后
        # 数据库同样不可写或 composition 构建失败的最后防线，并且不记录可能敏感的原文。
        return
