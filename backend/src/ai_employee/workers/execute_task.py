"""把 Taskiq 执行入口组合到基础设施无关的持久任务执行用例。"""

import asyncio
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID

from langgraph.types import Command
from taskiq import Context, TaskiqDepends, TaskiqEvents

from ai_employee.agents.fake_write.graph import FakeWriteGraph
from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.application.ports.trusted_actions import TrustedActionAdapterRegistry
from ai_employee.application.use_cases.approvals import ApprovalProposalStore
from ai_employee.application.use_cases.task_execution import (
    DurableTaskRunner,
    LeasedTask,
    TaskWaitingApproval,
)
from ai_employee.config import Settings, get_settings
from ai_employee.domain.errors import InternalInvariantError
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.task_execution import (
    SqlAlchemyTaskExecutionStore,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionTaskExecutionStore,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.events.publisher import TaskEventPublisher
from ai_employee.infrastructure.observability.metrics import Metrics, run_periodic_heartbeat
from ai_employee.infrastructure.observability.sync import refresh_sync_age_metrics
from ai_employee.infrastructure.queue.broker import DEFAULT_RETRY_COUNT, broker
from ai_employee.workers.conversation import build_conversation_task_step
from ai_employee.workers.diagnostics import build_overdue_brief_diagnostic_task_step
from ai_employee.workers.generate_brief import build_generate_brief_task_step
from ai_employee.workers.generate_mail_draft import build_generate_mail_draft_task_step
from ai_employee.workers.observability import (
    build_process_session_factory,
    initialize_process_observability,
    refresh_stuck_task_metrics,
)
from ai_employee.workers.prepare_calendar_restore import (
    build_prepare_calendar_restore_task_step,
)
from ai_employee.workers.privacy import AllDataDeletionCompleted, build_privacy_deletion_worker
from ai_employee.workers.reconcile_actions import execute_reconciliation_task
from ai_employee.workers.sync_calendar import build_calendar_sync_task_step
from ai_employee.workers.sync_mail import build_mail_sync_task_step
from ai_employee.workers.trusted_actions import (
    build_trusted_action_task_step,
    build_worker_trusted_action_registry,
    converge_revoked_pre_request_action,
)

RETRY_DELAY_SECONDS = 5
_MAX_RETRY_JITTER_MICROSECONDS = 1_000_000

# Taskiq 以默认值实例识别依赖注入；保持为模块级单例既符合该框架约定，又避免每次模块检查
# 误把函数调用默认值标记为副作用。该对象只提供当前消息 Context，不跨越 Worker 边界。
taskiq_context_dependency: Context = TaskiqDepends()
_worker_metrics: Metrics | None = None
_worker_observability_factory = None
_worker_heartbeat_task: asyncio.Task[None] | None = None


def _bounded_retry_jitter(_: int) -> timedelta:
    """在 Worker 基础设施边界生成最多一秒的正随机抖动。

    Args:
        _: 当前持久尝试次数；随机策略目前不依赖它，但保留应用层注入协议。

    Returns:
        一至一百万微秒的正延迟；应用层仍负责同指数退避和全局上限合并。

    随机性只在组合根产生，领域与应用测试可继续注入确定实现，避免不受控随机值
    污染业务规则或测试结果。
    """
    return timedelta(microseconds=secrets.randbelow(_MAX_RETRY_JITTER_MICROSECONDS) + 1)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def initialize_worker_observability(_: object) -> None:
    """在 Taskiq 真正启动后开启 Worker tracing、日志与内部指标监听。

    任务发现和单元测试只会导入此模块，不会触发 lifecycle 回调，因而不会占用 9101。连接
    工厂由 shutdown 回调释放；任务执行路径不复用它，避免改变既有每消息资源生命周期。
    """
    global _worker_metrics, _worker_observability_factory, _worker_heartbeat_task
    settings = get_settings()
    factory = build_process_session_factory(settings)
    _worker_observability_factory = factory
    _worker_metrics = initialize_process_observability(
        settings=settings, session_factory=factory, process="worker"
    )
    if _worker_metrics is not None:

        async def refresh_worker_health() -> None:
            """在空闲时继续扫描过期租约，保持 Gauge 与数据库事实一致。"""
            await refresh_stuck_task_metrics(
                session_factory=factory, metrics=_worker_metrics, now=datetime.now(UTC)
            )
            await refresh_sync_age_metrics(
                session_factory=factory, metrics=_worker_metrics, now=datetime.now(UTC)
            )

        _worker_heartbeat_task = asyncio.create_task(
            run_periodic_heartbeat(
                metrics=_worker_metrics, process="worker", on_tick=refresh_worker_health
            )
        )


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def shutdown_worker_observability(_: object) -> None:
    """释放仅供 Worker 启动观测使用的连接池，避免优雅退出泄漏连接。"""
    global _worker_observability_factory, _worker_heartbeat_task
    heartbeat_task = _worker_heartbeat_task
    _worker_heartbeat_task = None
    if heartbeat_task is not None:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
    factory = _worker_observability_factory
    _worker_observability_factory = None
    if factory is not None:
        await factory.dispose()


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
    """在任务分类不可确定时暂停执行，避免通用 Runner 伪造终态。"""

    name = "load_task_kind"

    async def execute(self, task: LeasedTask) -> None:
        """保留当前租约并等待后续恢复扫描重新读取权威任务类型。

        Args:
            task: 已获取租约的任务快照，仅用于保持统一节点接口。

        Raises:
            TaskWaitingApproval: 复用 Runner 的 no-finish 控制流，不能写通用失败审计。
        """
        del task
        raise TaskWaitingApproval


class _ClassificationDeferredTaskExecutionStore(SqlAlchemyTrustedActionTaskExecutionStore):
    """在任务类型未知时只允许租约等待，不允许任何任务终态写入。

    首次分类查询和权威 ``TaskRun.kind`` 重查都失败时，调用方无法证明该任务属于
    trusted action、fake-write 还是 M1 任务。此时仍使用持久租约 acquisition，使成功
    获取的消息在租约到期后可由恢复扫描器重新投递；但覆盖 ``finish`` 与
    ``fail_internal``，避免 Runner 在超时、获取异常或节点异常边界把潜在可信动作写成
    通用 ``task.failed``。下一次投递会重新读取权威类型，再选择正确的专用 Runner。
    """

    async def finish(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        status: TaskStatus,
        finished_at: datetime,
        error_code: str | None,
        retry_recovery_at: datetime | None = None,
    ) -> bool:
        """拒绝未知分类路径的一切终态或重试完成写入。"""
        del task_id, lease_owner, status, finished_at, error_code, retry_recovery_at
        return False

    async def fail_internal(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        failed_at: datetime,
        error_code: str,
    ) -> bool:
        """拒绝前置数据库异常的通用失败写入，等待后续恢复重读分类。"""
        del task_id, lease_owner, failed_at, error_code
        return False


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
    return build_task_runner_for_session(session_factory, settings=settings)


def build_task_runner_for_session(
    session_factory: ManagedAsyncSessionMaker,
    *,
    settings: Settings,
) -> DurableTaskRunner:
    """以调用方拥有的会话工厂构造真实任务 Runner，不创建无法关闭的额外连接池。

    Args:
        session_factory: API 或 Worker 生命周期负责释放的数据库会话工厂。
        settings: 当前进程已验证的配置；测试双开关会据此注入离线 fake 适配器。

    Returns:
        使用给定会话工厂的 DurableTaskRunner。
    """
    return DurableTaskRunner(
        store=SqlAlchemyTaskExecutionStore(session_factory),
        clock=lambda: datetime.now(UTC),
        lease_duration=timedelta(seconds=settings.task_lease_seconds),
        task_timeout_seconds=settings.task_timeout_seconds,
        task_step_timeout_seconds=settings.task_step_timeout_seconds,
        max_transient_retries=DEFAULT_RETRY_COUNT,
        metrics=_worker_metrics,
        retry_jitter=_bounded_retry_jitter,
        resolve_steps=lambda task: (
            (
                build_mail_sync_task_step(
                    session_factory=session_factory, settings=settings, metrics=_worker_metrics
                ),
            )
            if task.kind in {"sync_mail", "sync_gmail"}
            else (
                build_calendar_sync_task_step(
                    session_factory=session_factory, settings=settings, metrics=_worker_metrics
                ),
            )
            if task.kind == "sync_calendar"
            else (
                build_generate_brief_task_step(
                    session_factory=session_factory, settings=settings, metrics=_worker_metrics
                ),
            )
            if task.kind == "daily_brief"
            else (
                build_generate_mail_draft_task_step(
                    session_factory=session_factory, settings=settings, metrics=_worker_metrics
                ),
            )
            if task.kind == "mail_draft.generate"
            else (
                build_conversation_task_step(
                    session_factory=session_factory, settings=settings, metrics=_worker_metrics
                ),
            )
            if task.kind == "conversation.respond"
            else (
                build_prepare_calendar_restore_task_step(
                    session_factory=session_factory,
                    settings=settings,
                ),
            )
            if task.kind == "calendar.restore.prepare"
            else (build_overdue_brief_diagnostic_task_step(session_factory=session_factory),)
            if task.kind == "brief.overdue_diagnostic"
            else (build_privacy_deletion_worker(),)
            if task.kind in {"privacy.clear_source_cache", "privacy.delete_all_data"}
            else (_MissingTaskHandlerStep(),)
        ),
    )


def _build_trusted_action_task_runner(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings,
    approval_store: ApprovalProposalStore,
    resume: str | None,
    adapters: TrustedActionAdapterRegistry,
) -> DurableTaskRunner:
    """构造仅使用可信动作 Store 与 checkpoint step 的 Worker Runner。

    Args:
        session_factory: 当前消息持有并负责释放的数据库工厂。
        settings: 当前 Worker 的租约、超时与 checkpoint 配置。
        approval_store: 共享的审批分类/中断确认端口。
        resume: 内容无关的审批唤醒值。
        adapters: 当前 Worker 组合根固定的可信动作 registry；不会从 Taskiq 载荷读取。

    Returns:
        绑定 ``SqlAlchemyTrustedActionTaskExecutionStore`` 的持久执行用例。

    该组合函数被正常分类与异常后权威重查两条路径共用，确保 trusted action 永远不会
    因一次分类读取异常退回通用失败 Store。
    """
    return DurableTaskRunner(
        store=SqlAlchemyTrustedActionTaskExecutionStore(session_factory),
        clock=lambda: datetime.now(UTC),
        lease_duration=timedelta(seconds=settings.task_lease_seconds),
        task_timeout_seconds=settings.task_timeout_seconds,
        task_step_timeout_seconds=settings.task_step_timeout_seconds,
        max_transient_retries=DEFAULT_RETRY_COUNT,
        metrics=_worker_metrics,
        retry_jitter=_bounded_retry_jitter,
        resolve_steps=lambda _task: (
            build_trusted_action_task_step(
                session_factory=session_factory,
                settings=settings,
                approval_store=approval_store,
                resume=resume,
                max_transient_retries=DEFAULT_RETRY_COUNT,
                adapters=adapters,
            ),
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
        if _worker_metrics is not None:
            _worker_metrics.record_heartbeat(process="worker", age_seconds=0)
        session_factory = build_session_factory(
            settings.database_url,
            task_event_publisher=TaskEventPublisher(settings.redis_url),
        )
        try:
            approval_store: ApprovalProposalStore = SqlAlchemyApprovalStore(session_factory)
            classification_deferred = False
            try:
                task = await approval_store.get_fake_write_task(task_id=parsed_task_id)
            except Exception:  # noqa: BLE001 - 由耐久 Runner 而非 Missing handler 收敛。
                # 首次分类查询可能在提交/响应交界丢失；必须用新事务锁住 TaskRun
                # 重读不可变 kind。若连权威重查也失败，只暂停本次 lease，不能让
                # 通用 Runner 把潜在 trusted action 终结为孤立 task.failed。
                try:
                    task_kind = await SqlAlchemyTrustedActionTaskExecutionStore(
                        session_factory
                    ).get_authoritative_task_kind(task_id=parsed_task_id)
                except Exception:  # noqa: BLE001 - 分类事实不可读时只允许延期。
                    classification_deferred = True
                    task_kind = None
                task = None
            else:
                task_kind = getattr(task, "kind", None) if task is not None else None

            # RECONCILING/NEEDS_ATTENTION 不能进入 DurableTaskRunner：通用 acquisition 会
            # 把任务改成 running，并在节点返回后尝试写入 generic 终态。M2 专用入口只在
            # PostgreSQL 已确认的 RECONCILING 状态取得只读 lease；needs_attention 则等待
            # 用户手工确认或显式重开，不产生任何 provider 调用。
            authoritative_status = None
            if task_kind == "trusted_action":
                status_store = SqlAlchemyTrustedActionTaskExecutionStore(session_factory)
                status_reader = getattr(status_store, "get_authoritative_task_status", None)
                if not callable(status_reader):
                    # 状态端口缺失本身就是无法证明权威状态的协议错误；分类快照来自
                    # 另一个非锁定事务，不能作为 fallback，否则可能把 reconciling
                    # 任务误交给可写 Runner。
                    return
                else:
                    try:
                        authoritative_status = await status_reader(task_id=parsed_task_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - 权威状态不可读时必须 fail closed。
                        # 状态读取失败不能落入 generic Runner：它可能正是一个
                        # reconciling/needs_attention trusted action，继续 acquisition
                        # 会把只读未决事实覆盖成普通 running/failed。
                        return
                if isinstance(authoritative_status, TaskStatus):
                    authoritative_status = authoritative_status.value
                elif type(authoritative_status) is not str:
                    authoritative_status = None
            if (
                task_kind == "trusted_action"
                and authoritative_status == TaskStatus.RECONCILING.value
            ):
                if await converge_revoked_pre_request_action(
                    task_id=parsed_task_id, session_factory=session_factory
                ):
                    return
                # registry 只由 Worker 组合根构造并显式传入；不能让专用核对入口自行创建
                # 空 registry，否则生产中的 reconciling 任务会永远看不到已组装 adapter。
                trusted_action_adapters = build_worker_trusted_action_registry(
                    session_factory=session_factory,
                    settings=settings,
                )
                await execute_reconciliation_task(
                    task_id=parsed_task_id,
                    session_factory=session_factory,
                    settings=settings,
                    adapters=trusted_action_adapters,
                )
                return
            if (
                task_kind == "trusted_action"
                and authoritative_status == TaskStatus.NEEDS_ATTENTION.value
            ):
                return
            if task_kind == "trusted_action" and authoritative_status is None:
                # 不能证明任务仍可进入普通 Runner；尤其不能把已删除/损坏的 trusted
                # action 当作 M1 任务继续 acquisition。下一次 Outbox/人工请求再尝试读取。
                return
            if task_kind == "trusted_action" and authoritative_status not in {
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.RETRY_SCHEDULED.value,
            }:
                # 其他状态（包括未知字符串、created、waiting_approval 及旧终态）都不
                # 能证明当前消息拥有可写的 trusted-action lease；让通用 Runner 处理它
                # 会产生错误状态迁移或覆盖持久核对事实，因此统一 no-op。
                return

            if classification_deferred:
                runner = DurableTaskRunner(
                    store=_ClassificationDeferredTaskExecutionStore(session_factory),
                    clock=lambda: datetime.now(UTC),
                    lease_duration=timedelta(seconds=settings.task_lease_seconds),
                    task_timeout_seconds=settings.task_timeout_seconds,
                    task_step_timeout_seconds=settings.task_step_timeout_seconds,
                    max_transient_retries=DEFAULT_RETRY_COUNT,
                    metrics=_worker_metrics,
                    retry_jitter=_bounded_retry_jitter,
                    resolve_steps=lambda _task: (_TaskKindLookupFailureStep(),),
                )
            elif task_kind == "fake_write":
                runner = DurableTaskRunner(
                    store=SqlAlchemyTaskExecutionStore(session_factory),
                    clock=lambda: datetime.now(UTC),
                    lease_duration=timedelta(seconds=settings.task_lease_seconds),
                    task_timeout_seconds=settings.task_timeout_seconds,
                    task_step_timeout_seconds=settings.task_step_timeout_seconds,
                    max_transient_retries=DEFAULT_RETRY_COUNT,
                    metrics=_worker_metrics,
                    retry_jitter=_bounded_retry_jitter,
                    resolve_steps=lambda _task: (
                        _FakeWriteStep(
                            approval_store=approval_store,
                            resume=resume,
                            checkpoint_database_url=settings.checkpoint_database_url,
                        ),
                    ),
                )
            elif task_kind == "trusted_action":
                trusted_action_adapters = build_worker_trusted_action_registry(
                    session_factory=session_factory,
                    settings=settings,
                )
                runner = _build_trusted_action_task_runner(
                    session_factory=session_factory,
                    settings=settings,
                    approval_store=approval_store,
                    resume=resume,
                    adapters=trusted_action_adapters,
                )
            else:
                # 明确读到非 trusted kind 时保留既有 M1 通用路由；None 也只会让
                # acquire 无命中，不会凭空创建一个任务终态。
                runner = build_task_runner()
            if _worker_metrics is not None:
                await refresh_stuck_task_metrics(
                    session_factory=session_factory,
                    metrics=_worker_metrics,
                    now=datetime.now(UTC),
                )
            try:
                await runner.run(
                    parsed_task_id,
                    may_retry_transient=True,
                    retry_delay=timedelta(seconds=RETRY_DELAY_SECONDS),
                    recover_waiting_approval=recover_approval_checkpoint,
                )
            except AllDataDeletionCompleted:
                # 当前 TaskRun 已由全量删除事务移除；不能再经 Runner 写入终态。
                return
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
