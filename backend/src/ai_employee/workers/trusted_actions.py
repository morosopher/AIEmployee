"""在 DurableTaskRunner 租约内驱动 identifier-only 可信动作 Graph。"""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from langgraph.types import Command

from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.agents.trusted_actions.graph import TrustedActionGraph
from ai_employee.application.ports.trusted_actions import TrustedActionAdapterRegistry
from ai_employee.application.use_cases.approvals import ApprovalProposalStore
from ai_employee.application.use_cases.task_execution import (
    LeasedTask,
    TaskWaitingApproval,
)
from ai_employee.application.use_cases.trusted_actions import (
    TrustedActionAttemptAbandoned,
    TrustedActionExecutionUseCase,
    TrustedActionUserInactive,
)
from ai_employee.config import Settings
from ai_employee.domain.tasks import JsonValue
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionReconciliationRecoveryStore,
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.integrations.registry import (
    build_oauth_security_services,
    build_trusted_action_registry,
)


class TrustedActionTaskStep:
    """运行或恢复一项已冻结 M2 可信动作，并保持 lease owner 不进入 checkpoint。"""

    name = "trusted_action_graph"

    def __init__(
        self,
        *,
        workflow: TrustedActionExecutionUseCase,
        approval_store: ApprovalProposalStore,
        resume: str | None,
        checkpoint_database_url: str,
        max_transient_retries: int,
    ) -> None:
        """保存一次消息共享的执行用例、审批 checkpoint 端口和恢复决定。

        Args:
            workflow: 负责 claim、解密验证、request-start 与结果持久化的应用用例。
            approval_store: 只用于确认 ``__interrupt__`` 已持久化并暂停任务。
            resume: Outbox 中内容无关的唤醒值；授权决定仍由 Graph 重读 PostgreSQL。
            checkpoint_database_url: AsyncPostgresSaver 使用的 PostgreSQL URL。
            max_transient_retries: 首次尝试后允许的耐久安全写重试次数。

        Raises:
            ValueError: resume 不是空、approved 或 rejected。
        """
        if resume is not None and resume not in {"approved", "rejected"}:
            raise ValueError("resume must be approved, rejected, or None")
        if max_transient_retries < 0:
            raise ValueError("max_transient_retries must not be negative")
        self._workflow = workflow
        self._approval_store = approval_store
        self._resume = resume
        self._checkpoint_database_url = checkpoint_database_url
        self._max_transient_retries = max_transient_retries

    async def execute(self, task: LeasedTask) -> None:
        """使用当前租约 owner 调用 Graph，并把持久中断转为 Runner 控制流。

        初次输入只从 TaskRun 的两个 UUID 标识构造；command、密文与用户内容绝不进入
        Graph state。恢复值仅唤醒包含 ``interrupt`` 的节点，节点会从数据库重读决定。

        Args:
            task: DurableTaskRunner 已获得的当前可信任务租约。

        Raises:
            TaskWaitingApproval: 首次调用已持久化审批中断。
            ValueError: TaskRun 输入不是 Task 18 冻结的精确两个 UUID 字段。
        """
        approval_id, operation_id = _trusted_action_identifiers(task.input_payload)
        lease_owner = task.lease_owner or ""
        # 与 DurableTaskRunner 使用同一 PostgreSQL attempt_count 语义；第二次及后续
        # 明确未应用结果只有在剩余预算内才可再次形成 retry_scheduled 写授权。
        may_retry_write = task.attempt_count <= self._max_transient_retries
        expected_payload_hash = ""
        try:
            # 提前读取的哈希仍是内容无关事实，仅用于 identifier-only Graph 输入；
            # provider 边界的 abandon 由应用用例持有未 checkpoint 化的 dispatch 投影。
            facts = await self._workflow.load_graph_facts(
                task_id=task.task_id,
                approval_id=approval_id,
                operation_id=operation_id,
                expected_payload_hash="",
            )
            expected_payload_hash = facts.payload_hash
            graph = TrustedActionGraph(
                workflow=self._workflow,
                lease_owner=lease_owner,
                may_retry_write=may_retry_write,
            )
            async with postgres_checkpointer(self._checkpoint_database_url) as saver:
                compiled = graph.compile(checkpointer=saver)
                config = {"configurable": {"thread_id": str(task.task_id)}}
                if self._resume is None:
                    result = await compiled.ainvoke(
                        {
                            "task_id": str(task.task_id),
                            "approval_id": str(approval_id),
                            "operation_id": str(operation_id),
                            "payload_hash": expected_payload_hash,
                            "decision": None,
                            "messages": [],
                        },
                        config=config,
                    )
                else:
                    result = await compiled.ainvoke(Command(resume=self._resume), config=config)
        except (TrustedActionAttemptAbandoned, TrustedActionUserInactive):
            # inactive 屏障、request-start CAS loser、UNKNOWN 或另一调用的 executing 事实都不是
            # 成功节点；应用用例已用未 checkpoint 化的 dispatch 投影处理需要释放的
            # 精确尝试，这里只映射为 Runner no-finish，不能让 loser 释放赢家租约。
            raise TaskWaitingApproval from None
        except BaseException:
            # checkpointer 打开、Graph checkpoint 写入或其它节点外基础设施故障可能发生在
            # 已有 request-start 的恢复投递上。应用用例会从 identifier-only 输入重载并
            # 认证 AEAD 命令后再释放，绝不把额外绑定或明文塞入 checkpoint。
            try:
                await self._preserve_started_attempt(
                    task=task,
                    approval_id=approval_id,
                    operation_id=operation_id,
                    expected_payload_hash=expected_payload_hash,
                    lease_owner=lease_owner,
                )
            except Exception:  # noqa: BLE001 - 保护边界失败时禁止退回通用任务终态。
                current_task = asyncio.current_task()
                if current_task is None or not current_task.cancelling():
                    raise TaskWaitingApproval from None
            raise
        if isinstance(result, Mapping) and "__interrupt__" in result:
            await self._approval_store.confirm_approval_checkpoint(
                task_id=task.task_id,
                lease_owner=lease_owner,
            )
            raise TaskWaitingApproval

    async def _preserve_started_attempt(
        self,
        *,
        task: LeasedTask,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        lease_owner: str,
    ) -> bool:
        """屏蔽取消并让应用层重载认证精确绑定后提交幂等 abandon CAS。"""
        return await asyncio.shield(
            self._workflow.abandon_started_attempt(
                task_id=task.task_id,
                approval_id=approval_id,
                operation_id=operation_id,
                expected_payload_hash=expected_payload_hash,
                lease_owner=lease_owner,
            )
        )


async def converge_revoked_pre_request_action(
    *, task_id: UUID, session_factory: ManagedAsyncSessionMaker
) -> bool:
    """先消费撤权后零请求的核对事实，避免不必要的密钥、Token 或 registry 装配。

    这是普通 Taskiq 路由和专用核对 Worker 共用的持久边界。终态与损坏绑定返回 False，
    已开始请求永远不会在此方法中被判定为未应用。
    """
    return await SqlAlchemyTrustedActionReconciliationRecoveryStore(
        session_factory
    ).converge_pre_request_reconciliation(task_id=task_id)


def build_worker_trusted_action_registry(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings,
) -> TrustedActionAdapterRegistry:
    """构造 Worker 进程使用的固定可信动作 registry 组合根。

    Args:
        session_factory: 当前消息拥有的数据库工厂；registry 按显式用户和冻结连接在短
            session 内读取凭据及来源事实，不得把连接或令牌放入 Taskiq 载荷。
        settings: 当前进程已验证配置；真实写入开关仍由应用层 request-start 再次检查。

    Returns:
        两家供应商四种固定动作的共享惰性 registry。构造和 slot 检查不加载 Secret
        或访问供应商；实际执行仍须通过调用方门禁和 connection-bound 凭据解析。

    Notes:
        这是显式的静态组合钩子，不提供运行时注册或队列级 adapter 注入。调用方按消息
        生命周期构造它，并在同一消息 finally 中释放数据库工厂；registry 本身不得持有
        未关闭的长生命周期资源。
    """
    return build_trusted_action_registry(session_factory=session_factory, settings=settings)


def build_trusted_action_task_step(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings,
    approval_store: ApprovalProposalStore,
    resume: str | None,
    max_transient_retries: int,
    adapters: TrustedActionAdapterRegistry | None = None,
    metrics: Metrics | None = None,
) -> TrustedActionTaskStep:
    """用消息级数据库工厂和主密钥构造可信动作 Graph step。

    Args:
        session_factory: 当前 Taskiq 消息拥有并在 finally 中释放的 SQLAlchemy 工厂。
        settings: 已验证进程配置，提供三层写门禁、checkpoint URL 与主密钥文件。
        approval_store: 与分类查询共享的审批 checkpoint 端口。
        resume: 内容无关的审批唤醒值；授权决定仍由 Graph 重读 PostgreSQL。
        max_transient_retries: 与外层 DurableTaskRunner 相同的耐久临时重试上限。
        adapters: 测试显式注入的固定动作注册表。省略时使用 API/Worker 共享的固定
            惰性 registry；按原用户和冻结连接解析凭据，缺少凭据时在 claim 前安全失败。

    Returns:
        复用调用方连接池、只在受控内存解密命令的可信动作步骤。

    Raises:
        OSError: 主密钥 Secret 不可读取；由 DurableTaskRunner 收敛为持久安全失败。
        ValueError: 主密钥、checkpoint URL 或 resume 不符合冻结配置协议。

    Notes:
        构造发生在 Runner 已取得租约后的节点解析阶段，因此密钥读取或组合失败仍由
        当前 owner 的持久错误边界处理；本函数不会创建第二个 SQLAlchemy Engine。
    """
    registry = (
        adapters
        if adapters is not None
        else build_worker_trusted_action_registry(
            session_factory=session_factory, settings=settings
        )
    )
    security = build_oauth_security_services(
        session_factory=session_factory, master_key_file=settings.app_master_key_file
    )
    command_cipher = ActionPayloadCipher(security.cipher)
    workflow = TrustedActionExecutionUseCase(
        transactions=SqlAlchemyTrustedActionRepositoryFactory(session_factory, command_cipher),
        adapters=registry,
        write_policy=settings,
        clock=lambda: datetime.now(UTC),
        observer=metrics,
    )
    return TrustedActionTaskStep(
        workflow=workflow,
        approval_store=approval_store,
        resume=resume,
        checkpoint_database_url=settings.checkpoint_database_url,
        max_transient_retries=max_transient_retries,
    )


def _trusted_action_identifiers(payload: Mapping[str, JsonValue]) -> tuple[UUID, UUID]:
    """严格解析 Task 18 冻结的 identifier-only TaskRun 输入。

    Args:
        payload: 预期仅含 ``approval_id`` 与 ``operation_id`` 的内部 JSON object。

    Returns:
        精确审批与操作 UUID。

    Raises:
        ValueError: 字段集合、类型或 UUID 表示不符合冻结协议。
    """
    if set(payload) != {"approval_id", "operation_id"}:
        raise ValueError("trusted action task input is invalid")
    approval_id = payload.get("approval_id")
    operation_id = payload.get("operation_id")
    if type(approval_id) is not str or type(operation_id) is not str:
        raise ValueError("trusted action task input is invalid")
    try:
        return UUID(approval_id), UUID(operation_id)
    except ValueError:
        raise ValueError("trusted action task input is invalid") from None


__all__ = [
    "TrustedActionTaskStep",
    "build_trusted_action_task_step",
    "build_worker_trusted_action_registry",
    "converge_revoked_pre_request_action",
]
