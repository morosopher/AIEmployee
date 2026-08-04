"""把 Taskiq 执行入口组合到基础设施无关的持久任务执行用例。"""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID

from langgraph.types import Command
from taskiq import Context, TaskiqDepends

from ai_employee.agents.fake_write.graph import FakeWriteGraph
from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.application.use_cases.approvals import ApprovalProposalStore
from ai_employee.application.use_cases.task_execution import (
    DurableTaskRunner,
    LeasedTask,
    TaskWaitingApproval,
)
from ai_employee.config import get_settings
from ai_employee.domain.errors import InternalInvariantError
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.task_execution import (
    SqlAlchemyTaskExecutionStore,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.events.publisher import TaskEventPublisher
from ai_employee.infrastructure.queue.broker import DEFAULT_RETRY_COUNT, broker
from ai_employee.workers.conversation import build_conversation_task_step
from ai_employee.workers.generate_brief import build_generate_brief_task_step
from ai_employee.workers.sync_calendar import build_calendar_sync_task_step
from ai_employee.workers.sync_gmail import build_gmail_sync_task_step

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


class _TaskKindLookupFailureStep:
    """把任务分类读取异常交给耐久执行边界收敛为安全失败。"""

    name = "load_task_kind"

    async def execute(self, task: LeasedTask) -> None:
        """拒绝把数据库异常伪装成未注册 handler。

        Args:
            task: 已获取租约的任务快照，仅用于保持统一节点接口。

        Raises:
            InternalInvariantError: 分类读取边界发生未知持久化异常。
        """
        del task
        raise InternalInvariantError(
            error_code="task_kind_lookup_failed",
            message="task kind lookup failed",
        )


class _FakeWriteStep:
    """在 DurableTaskRunner 租约内驱动一次 fake-write Graph。"""

    name = "fake_write_graph"

    def __init__(
        self,
        *,
        approval_store: ApprovalProposalStore,
        resume: str | None,
        checkpoint_database_url: str,
    ) -> None:
        """保存一次 Worker 尝试所需的恢复决定和两类数据库连接配置。

        Args:
            approval_store: 由消息组合层创建并负责释放连接池的审批事实端口；Graph 必须
                复用它，避免每次 fake-write 再创建一个无法释放的 SQLAlchemy Engine。
            resume: 已由审批 Outbox 冻结的 ``approved`` 或 ``rejected`` 决定；首次执行为空。
            checkpoint_database_url: LangGraph checkpointer 专用 psycopg URL。
        """
        self._approval_store = approval_store
        self._resume = resume
        self._checkpoint_database_url = checkpoint_database_url

    async def execute(self, task: LeasedTask) -> None:
        """在当前租约内运行或恢复同一 Graph checkpoint。

        首次中断后，审批存储已将任务原子置为 ``WAITING_APPROVAL`` 并释放租约；本方法把
        LangGraph 返回的 interrupt 转换为 Runner 控制流，避免把暂停误记为成功。恢复时用
        新 owner 覆盖 checkpoint 中旧 owner，确保批准后的副作用认领继续受当前租约保护。

        Args:
            task: 已由 ``DurableTaskRunner`` 获取且包含当前 owner 的任务快照。

        Raises:
            TaskWaitingApproval: Graph 返回人工审批中断，调用方必须停止本次消息。
        """
        graph = FakeWriteGraph(
            approval_store=self._approval_store,
            clock=lambda: datetime.now(UTC),
            approval_ttl=timedelta(minutes=5),
            fake_tool=_fake_write_tool,
        )
        async with postgres_checkpointer(self._checkpoint_database_url) as saver:
            compiled = graph.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": str(task.task_id)}}
            if self._resume is None:
                result = await compiled.ainvoke(
                    {
                        "task_id": str(task.task_id),
                        "lease_owner": task.lease_owner or "",
                        "proposal_payload": task.input_payload,
                        "approval_decision": None,
                        "tool_called": False,
                        "messages": [],
                    },
                    config=config,
                )
            else:
                result = await compiled.ainvoke(
                    Command(resume=self._resume, update={"lease_owner": task.lease_owner or ""}),
                    config=config,
                )
        if isinstance(result, dict) and "__interrupt__" in result:
            await self._approval_store.confirm_approval_checkpoint(
                task_id=task.task_id,
                lease_owner=task.lease_owner or "",
            )
            raise TaskWaitingApproval


@lru_cache
def build_task_runner() -> DurableTaskRunner:
    """按进程配置构造并缓存共享数据库连接池与持久执行用例。

    Returns:
        使用 SQLAlchemy store、UTC 时钟和当前 Task 7 超时配置的执行用例。

    Task 7 只建立可靠执行底座；后续已批准任务会把 resolver 替换为具体 Graph 节点。
    在此之前使用显式失败节点，避免收到队列消息后把未实现业务错误标记为成功。
    """
    settings = get_settings()
    session_factory = build_session_factory(
        settings.database_url,
        task_event_publisher=TaskEventPublisher(settings.redis_url),
    )
    return DurableTaskRunner(
        store=SqlAlchemyTaskExecutionStore(session_factory),
        clock=lambda: datetime.now(UTC),
        lease_duration=timedelta(seconds=settings.task_lease_seconds),
        task_timeout_seconds=settings.task_timeout_seconds,
        task_step_timeout_seconds=settings.task_step_timeout_seconds,
        max_transient_retries=DEFAULT_RETRY_COUNT,
        resolve_steps=lambda task: (
            (build_gmail_sync_task_step(session_factory=session_factory, settings=settings),)
            if task.kind == "sync_gmail"
            else (
                build_calendar_sync_task_step(session_factory=session_factory, settings=settings),
            )
            if task.kind == "sync_calendar"
            else (
                build_generate_brief_task_step(session_factory=session_factory, settings=settings),
            )
            if task.kind == "daily_brief"
            else (
                build_conversation_task_step(session_factory=session_factory, settings=settings),
            )
            if task.kind == "conversation.respond"
            else (_MissingTaskHandlerStep(),)
        ),
    )


@broker.task(retry_on_error=False)
async def execute_task(
    task_id: str,
    context: Context = taskiq_context_dependency,
    resume: str | None = None,
    recover_approval_checkpoint: bool = False,
) -> None:
    """解析 task_id，并把临时供应商错误写为耐久延迟 Outbox 重试。

    Args:
        task_id: Outbox 发送的规范 UUID 字符串；消息不包含任务正文或结果。
        context: Taskiq 注入的当前消息上下文；只保留装饰任务的依赖兼容性，不传入
            application 用例，也不作为重试状态来源。

    Taskiq 只消费 Outbox relay 发送的 task_id。临时错误已由 Runner 在 PostgreSQL 事务内
    写入延迟 Outbox，故本入口不能重新抛出它再让 SmartRetryMiddleware 绕过 Outbox 写 Redis。

    Raises:
        RuntimeError: Runner 未能将异常持久化时上抛稳定错误，阻止 Worker 静默确认消息。
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
        session_factory = build_session_factory(
            settings.database_url,
            task_event_publisher=TaskEventPublisher(settings.redis_url),
        )
        try:
            approval_store: ApprovalProposalStore = SqlAlchemyApprovalStore(session_factory)
            try:
                task = await approval_store.get_fake_write_task(task_id=parsed_task_id)
            except Exception:  # noqa: BLE001 - 由耐久 Runner 而非 Missing handler 收敛。
                runner = DurableTaskRunner(
                    store=SqlAlchemyTaskExecutionStore(session_factory),
                    clock=lambda: datetime.now(UTC),
                    lease_duration=timedelta(seconds=settings.task_lease_seconds),
                    task_timeout_seconds=settings.task_timeout_seconds,
                    task_step_timeout_seconds=settings.task_step_timeout_seconds,
                    max_transient_retries=DEFAULT_RETRY_COUNT,
                    resolve_steps=lambda _task: (_TaskKindLookupFailureStep(),),
                )
            else:
                runner = (
                    DurableTaskRunner(
                        store=SqlAlchemyTaskExecutionStore(session_factory),
                        clock=lambda: datetime.now(UTC),
                        lease_duration=timedelta(seconds=settings.task_lease_seconds),
                        task_timeout_seconds=settings.task_timeout_seconds,
                        task_step_timeout_seconds=settings.task_step_timeout_seconds,
                        max_transient_retries=DEFAULT_RETRY_COUNT,
                        resolve_steps=lambda _task: (
                            _FakeWriteStep(
                                approval_store=approval_store,
                                resume=resume,
                                checkpoint_database_url=settings.checkpoint_database_url,
                            ),
                        ),
                    )
                    if task is not None and task.kind == "fake_write"
                    else build_task_runner()
                )
            await runner.run(
                parsed_task_id,
                may_retry_transient=True,
                retry_delay=timedelta(seconds=RETRY_DELAY_SECONDS),
                recover_waiting_approval=recover_approval_checkpoint,
            )
        finally:
            await session_factory.dispose()
    except (OSError, RuntimeError, ValueError):
        # 正常可写数据库路径已由 Runner 收敛为安全 FAILED/error_code。若该收敛本身也失败，
        # 不能伪装成已处理并 ACK 消息；这些进程组合边界异常改为稳定错误并隐藏可能敏感原文。
        # 其余编程错误直接上抛，避免宽泛捕获再次把调用契约错误静默转换为确认。
        raise RuntimeError("task execution persistence boundary unavailable") from None


async def _fake_write_tool(payload: dict[str, object]) -> None:
    """执行不访问外部系统的假写，并故意不记录输入正文。"""
    del payload
