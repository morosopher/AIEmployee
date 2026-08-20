"""在 DurableTaskRunner 租约内驱动 identifier-only 可信动作 Graph。"""

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
from ai_employee.application.use_cases.trusted_actions import TrustedActionExecutionUseCase
from ai_employee.config import Settings
from ai_employee.domain.tasks import JsonValue
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry


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
    ) -> None:
        """保存一次消息共享的执行用例、审批 checkpoint 端口和恢复决定。

        Args:
            workflow: 负责 claim、解密验证、request-start 与结果持久化的应用用例。
            approval_store: 只用于确认 ``__interrupt__`` 已持久化并暂停任务。
            resume: Outbox 中内容无关的唤醒值；授权决定仍由 Graph 重读 PostgreSQL。
            checkpoint_database_url: AsyncPostgresSaver 使用的 PostgreSQL URL。

        Raises:
            ValueError: resume 不是空、approved 或 rejected。
        """
        if resume is not None and resume not in {"approved", "rejected"}:
            raise ValueError("resume must be approved, rejected, or None")
        self._workflow = workflow
        self._approval_store = approval_store
        self._resume = resume
        self._checkpoint_database_url = checkpoint_database_url

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
        graph = TrustedActionGraph(workflow=self._workflow, lease_owner=lease_owner)
        async with postgres_checkpointer(self._checkpoint_database_url) as saver:
            compiled = graph.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": str(task.task_id)}}
            if self._resume is None:
                result = await compiled.ainvoke(
                    {
                        "task_id": str(task.task_id),
                        "approval_id": str(approval_id),
                        "operation_id": str(operation_id),
                        "payload_hash": "",
                        "decision": None,
                        "messages": [],
                    },
                    config=config,
                )
            else:
                result = await compiled.ainvoke(Command(resume=self._resume), config=config)
        if isinstance(result, Mapping) and "__interrupt__" in result:
            await self._approval_store.confirm_approval_checkpoint(
                task_id=task.task_id,
                lease_owner=lease_owner,
            )
            raise TaskWaitingApproval


def build_trusted_action_task_step(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings,
    approval_store: ApprovalProposalStore,
    resume: str | None,
    adapters: TrustedActionAdapterRegistry | None = None,
) -> TrustedActionTaskStep:
    """用消息级数据库工厂和主密钥构造可信动作 Graph step。

    Args:
        session_factory: 当前 Taskiq 消息拥有并在 finally 中释放的 SQLAlchemy 工厂。
        settings: 已验证进程配置，提供三层写门禁、checkpoint URL 与主密钥文件。
        approval_store: 与分类查询共享的审批 checkpoint 端口。
        resume: 内容无关的审批唤醒值；授权决定仍由 Graph 重读 PostgreSQL。
        adapters: 测试或后续供应商任务显式注入的固定真实动作注册表。省略时使用
            空注册表，使尚未组装真实 adapter 的进程在 claim 前安全失败。

    Returns:
        复用调用方连接池、只在受控内存解密命令的可信动作步骤。

    Raises:
        OSError: 主密钥 Secret 不可读取；由 DurableTaskRunner 收敛为持久安全失败。
        ValueError: 主密钥、checkpoint URL 或 resume 不符合冻结配置协议。

    Notes:
        构造发生在 Runner 已取得租约后的节点解析阶段，因此密钥读取或组合失败仍由
        当前 owner 的持久错误边界处理；本函数不会创建第二个 SQLAlchemy Engine。
    """
    registry = adapters or ProviderAdapterRegistry()
    command_cipher = ActionPayloadCipher(AeadCipher.from_file(settings.app_master_key_file))
    workflow = TrustedActionExecutionUseCase(
        transactions=SqlAlchemyTrustedActionRepositoryFactory(session_factory, command_cipher),
        adapters=registry,
        write_policy=settings,
        clock=lambda: datetime.now(UTC),
    )
    return TrustedActionTaskStep(
        workflow=workflow,
        approval_store=approval_store,
        resume=resume,
        checkpoint_database_url=settings.checkpoint_database_url,
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


__all__ = ["TrustedActionTaskStep", "build_trusted_action_task_step"]
