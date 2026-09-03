"""定义人工审批决定与到期清理的应用用例边界。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_employee.domain.tasks import ApprovalProposal, JsonValue


@dataclass(frozen=True, slots=True)
class FakeWriteTask:
    """提供 Worker 恢复审批图所需的非 ORM 任务快照。

    该快照只用于判断任务种类并向 LangGraph 提供已经持久化的输入载荷，避免 Worker
    直接读取 SQLAlchemy 模型而破坏进程组合层的依赖边界。
    """

    kind: str
    input_payload: dict[str, JsonValue]
    # M2 reconciliation 路由需要读取权威状态；保留可选值以兼容 M1 的最小 fake fixture。
    status: str | None = None


@dataclass(frozen=True, slots=True)
class FakeToolClaim:
    """副作用前持久幂等认领返回的冻结工具输入。"""

    payload: dict[str, JsonValue]
    should_call: bool


class PendingApproval:
    """图节点显示并中断时需要的已持久审批最小快照。"""

    def __init__(self, *, approval_id: UUID, version: int, status: str) -> None:
        """保存不含敏感载荷副本的稳定审批标识和版本。"""
        self.approval_id = approval_id
        self.version = version
        self.status = status


class ApprovalProposalStore(Protocol):
    """定义 Graph 把冻结提案写为等待审批状态的端口。"""

    async def create_or_get_pending(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        proposal: ApprovalProposal,
        preview_markdown: str,
        expires_at: datetime,
        checkpoint_recovery_at: datetime,
    ) -> PendingApproval:
        """以当前 ``RUNNING`` 租约原子创建审批、步骤、状态、审计与恢复事实。"""

    async def find_for_graph(self, *, task_id: UUID, payload_hash: str) -> PendingApproval | None:
        """返回同一冻结提案的现有审批，供 checkpoint 重放识别终态。"""

    async def finish_fake_write(
        self, *, task_id: UUID, lease_owner: str, decision: str, payload_hash: str, now: datetime
    ) -> None:
        """在 Graph 终点把已恢复的假写任务持久化为成功。"""

    async def get_fake_write_task(self, *, task_id: UUID) -> FakeWriteTask | None:
        """返回恢复假写审批图需要的任务快照，不暴露持久化实现类型。"""

    async def claim_fake_tool_execution(
        self, *, task_id: UUID, lease_owner: str, expected_payload_hash: str
    ) -> FakeToolClaim:
        """在批准与冻结哈希校验后原子认领假工具副作用。"""

    async def complete_fake_tool_execution(
        self, *, task_id: UUID, lease_owner: str, expected_payload_hash: str
    ) -> None:
        """把当前 owner 已调用完成的假工具执行记录为成功。"""

    async def confirm_approval_checkpoint(self, *, task_id: UUID, lease_owner: str) -> None:
        """确认 interrupt 已持久化后收敛审批恢复 anchor。"""


class ApprovalStore(Protocol):
    """定义审批用例需要的事务性持久化操作。"""

    async def expire_overdue(self, *, now: datetime, limit: int) -> int:
        """原子标记有界数量的过期待审批，并终止对应任务。"""


class ExpireApprovalsUseCase:
    """扫描并终止已经无法再由用户安全决定的审批。"""

    def __init__(self, store: ApprovalStore) -> None:
        """注入不暴露 ORM 的审批存储端口。

        Args:
            store: 负责锁定、状态迁移与审计追加的事务性存储。
        """
        self._store = store

    async def execute(self, *, now: datetime, limit: int) -> int:
        """使到期审批失效，并返回本轮实际终止的数量。

        Args:
            now: 带时区 UTC 的扫描瞬间。
            limit: 单次扫描最多处理的审批数量，必须为正数。

        Returns:
            本次由 PENDING 变更为 EXPIRED 的审批数量。

        Raises:
            ValueError: 批次上限不为正，或时间不是带时区 UTC 瞬间。
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return await self._store.expire_overdue(now=now, limit=limit)


class ApprovalDecisionStore(Protocol):
    """定义解决单一审批所需的锁定与原子写入能力。"""

    async def resolve(
        self,
        *,
        approval_id: UUID,
        user_id: UUID,
        decision: str,
        version: int,
        payload_hash: str,
        now: datetime,
    ) -> None:
        """验证并持久化一个审批决定，同时创建 Graph 恢复 Outbox。"""


class ApprovalDecisionUseCase:
    """验证冻结载荷后解决审批，并仅以 Outbox 请求恢复 Graph。"""

    def __init__(self, store: ApprovalDecisionStore) -> None:
        """注入审批决定存储端口。"""
        self._store = store

    async def execute(
        self,
        *,
        approval_id: UUID,
        user_id: UUID,
        decision: str,
        version: int,
        payload_hash: str,
        now: datetime,
    ) -> None:
        """解决待审批请求。

        决定只接受 ``approved`` 或 ``rejected``；精确载荷哈希与版本由存储层在
        ``FOR UPDATE`` 锁内比较，避免 API 读写之间的 TOCTOU 竞争。
        """
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        await self._store.resolve(
            approval_id=approval_id,
            user_id=user_id,
            decision=decision,
            version=version,
            payload_hash=payload_hash,
            now=now,
        )
