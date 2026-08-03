"""把 Taskiq 执行入口组合到基础设施无关的持久任务执行用例。"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from uuid import UUID

from taskiq import Context, TaskiqDepends

from ai_employee.application.use_cases.task_execution import DurableTaskRunner, LeasedTask
from ai_employee.config import get_settings
from ai_employee.domain.errors import InternalInvariantError, TransientProviderError
from ai_employee.infrastructure.db.repositories.task_execution import (
    SqlAlchemyTaskExecutionStore,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.queue.broker import DEFAULT_RETRY_COUNT, broker

# Taskiq 以默认值实例识别依赖注入；保持为模块级单例既符合该框架约定，又避免每次模块检查
# 误把函数调用默认值标记为副作用。该对象只提供当前消息 Context，不跨越 Worker 边界。
taskiq_context_dependency: Context = TaskiqDepends()


def has_remaining_transient_retry_budget(labels: Mapping[str, Any]) -> bool:
    """按 Taskiq 0.12.4 的标签语义判断当前临时错误是否还可重新投递。

    Args:
        labels: 由 Taskiq ``Context`` 提供的原始消息标签；该第三方边界允许 ``Any``，并与
            SmartRetryMiddleware 一样直接转换 ``_retries`` 与 ``max_retries``。

    Returns:
        当本次失败后仍满足 ``retries < max_retries`` 时返回 ``True``。默认上限复用 broker
        模块供 SmartRetryMiddleware 配置使用的同一常量，避免两处策略发生漂移。
    """
    try:
        retries = int(labels.get("_retries", 0)) + 1
        max_retries = int(labels.get("max_retries", DEFAULT_RETRY_COUNT))
    except (OverflowError, TypeError, ValueError):
        # 队列损坏时不能在获取 PostgreSQL 租约前静默 ACK。保守地禁用后续自动重投，
        # 使 Runner 能把任何临时错误收敛为 owner-safe 的 FAILED 终态。
        return False
    return retries < max_retries


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
async def execute_task(
    task_id: str,
    context: Context = taskiq_context_dependency,
) -> None:
    """解析 task_id，并只允许已持久化的临时供应商错误触发 Taskiq 重试。

    Args:
        task_id: Outbox 发送的规范 UUID 字符串；消息不包含任务正文或结果。
        context: Taskiq 注入的当前消息上下文；只在 Worker 层读取重试标签，不能传入
            application 用例。

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
        await build_task_runner().run(
            parsed_task_id,
            may_retry_transient=has_remaining_transient_retry_budget(context.message.labels),
        )
    except TransientProviderError:
        raise
    except Exception:  # noqa: BLE001 - 未知异常绝不能进入仅供应商错误允许的 SmartRetry。
        # 正常可写数据库路径已由应用用例持久化安全 FAILED/error_code。这里只处理主失败后
        # 数据库同样不可写或 composition 构建失败的最后防线，并且不记录可能敏感的原文。
        return
