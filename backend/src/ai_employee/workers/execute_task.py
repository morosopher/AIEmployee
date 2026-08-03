"""把 Taskiq 执行入口组合到基础设施无关的持久任务执行用例。"""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID

from langgraph.types import Command
from taskiq import Context, TaskiqDepends

from ai_employee.agents.fake_write.graph import FakeWriteGraph
from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.application.use_cases.approvals import ApprovalProposalStore
from ai_employee.application.use_cases.task_execution import DurableTaskRunner, LeasedTask
from ai_employee.config import get_settings
from ai_employee.domain.errors import InternalInvariantError
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.task_execution import (
    SqlAlchemyTaskExecutionStore,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.queue.broker import DEFAULT_RETRY_COUNT, broker

RETRY_DELAY_SECONDS = 5

# Taskiq 以默认值实例识别依赖注入；保持为模块级单例既符合该框架约定，又避免每次模块检查
# 误把函数调用默认值标记为副作用。该对象只提供当前消息 Context，不跨越 Worker 边界。
taskiq_context_dependency: Context = TaskiqDepends()


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
        max_transient_retries=DEFAULT_RETRY_COUNT,
        resolve_steps=lambda task: (_MissingTaskHandlerStep(),),
    )


@broker.task(retry_on_error=False)
async def execute_task(
    task_id: str,
    context: Context = taskiq_context_dependency,
    resume: str | None = None,
) -> None:
    """解析 task_id，并把临时供应商错误写为耐久延迟 Outbox 重试。

    Args:
        task_id: Outbox 发送的规范 UUID 字符串；消息不包含任务正文或结果。
        context: Taskiq 注入的当前消息上下文；只保留装饰任务的依赖兼容性，不传入
            application 用例，也不作为重试状态来源。

    Taskiq 只消费 Outbox relay 发送的 task_id。临时错误已由 Runner 在 PostgreSQL 事务内
    写入延迟 Outbox，故本入口不能重新抛出它再让 SmartRetryMiddleware 绕过 Outbox 写 Redis。
    """
    try:
        parsed_task_id = UUID(task_id)
    except ValueError:
        # 队列载荷由内部 enqueue adapter 生成；非法值不能关联持久任务，也不得反复重试。
        return

    try:
        del context
        if resume is not None and resume not in {"approved", "rejected"}:
            return
        settings = get_settings()
        session_factory = build_session_factory(settings.database_url)
        try:
            approval_store: ApprovalProposalStore = SqlAlchemyApprovalStore(session_factory)
            try:
                task = await approval_store.get_fake_write_task(task_id=parsed_task_id)
            except Exception:  # noqa: BLE001 - 非 fake 任务仍交给既有 DurableTaskRunner 路径。
                task = None
            if task is not None and task.kind == "fake_write":
                graph = FakeWriteGraph(
                    approval_store=approval_store,
                    clock=lambda: datetime.now(UTC),
                    approval_ttl=timedelta(minutes=5),
                    fake_tool=_fake_write_tool,
                )
                async with postgres_checkpointer(settings.database_url) as saver:
                    compiled = graph.compile(checkpointer=saver)
                    config = {"configurable": {"thread_id": str(parsed_task_id)}}
                    if resume is None:
                        await compiled.ainvoke(
                            {
                                "task_id": str(parsed_task_id),
                                "proposal_payload": task.input_payload,
                                "approval_decision": None,
                                "tool_called": False,
                                "messages": [],
                            },
                            config=config,
                        )
                    else:
                        await compiled.ainvoke(Command(resume=resume), config=config)
                return
        finally:
            await session_factory.dispose()
        runner = build_task_runner()
        await runner.run(
            parsed_task_id,
            may_retry_transient=True,
            retry_delay=timedelta(seconds=RETRY_DELAY_SECONDS),
        )
    except Exception:  # noqa: BLE001 - 未知异常绝不能绕过已持久化的失败边界。
        # 正常可写数据库路径已由应用用例持久化安全 FAILED/error_code。这里只处理主失败后
        # 数据库同样不可写或 composition 构建失败的最后防线，并且不记录可能敏感的原文。
        return


async def _fake_write_tool(payload: dict[str, object]) -> None:
    """执行不访问外部系统的假写，并故意不记录输入正文。"""
    del payload
