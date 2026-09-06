"""执行可信动作的专用只读核对 Worker。"""

import asyncio
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ai_employee.application.ports.trusted_actions import TrustedActionAdapterRegistry
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.application.use_cases.trusted_actions import (
    TrustedActionAttemptAbandoned,
    TrustedActionExecutionUseCase,
)
from ai_employee.config import Settings
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepositoryFactory,
    SqlAlchemyTrustedActionTaskExecutionStore,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry
from ai_employee.workers.trusted_actions import converge_revoked_pre_request_action


def _reconciliation_owner() -> str:
    """生成不携带主机或用户资料的短期核对 owner。"""
    return f"reconcile:{secrets.token_hex(16)}"


def _identifiers(task: LeasedTask) -> tuple[UUID, UUID]:
    """严格解析 TaskRun 的两个 identifier-only 字段。"""
    payload = task.input_payload
    if set(payload) != {"approval_id", "operation_id"}:
        raise ValueError("trusted action task input is invalid")
    raw_approval = payload.get("approval_id")
    raw_operation = payload.get("operation_id")
    if type(raw_approval) is not str or type(raw_operation) is not str:
        raise ValueError("trusted action task input is invalid")
    try:
        return UUID(raw_approval), UUID(raw_operation)
    except ValueError:
        raise ValueError("trusted action task input is invalid") from None


class ReconcileActionsTaskStep:
    """在既有专用租约内执行一次 ``adapter.reconcile``，不提供写入口。

    该 Step 只接受注入的 ``TrustedActionExecutionUseCase.reconcile``。它先从数据库重读
    哈希，再让用例解密同一冻结命令并调用只读 adapter；因此 checkpoint、队列载荷或
    调用方都无法把一次核对升级为新的 ``execute`` 写请求。
    """

    name = "trusted_action_reconcile"

    def __init__(self, workflow: TrustedActionExecutionUseCase) -> None:
        """保存已组装的供应商中立可信动作用例。"""
        self._workflow = workflow

    async def execute(self, task: LeasedTask) -> None:
        """核对当前任务并把结果交给原子持久化边界。"""
        approval_id, operation_id = _identifiers(task)
        facts = await self._workflow.load_graph_facts(
            task_id=task.task_id,
            approval_id=approval_id,
            operation_id=operation_id,
            expected_payload_hash="",
        )
        await self._workflow.reconcile(
            task_id=task.task_id,
            approval_id=approval_id,
            operation_id=operation_id,
            expected_payload_hash=facts.payload_hash,
            lease_owner=task.lease_owner or "",
        )


async def execute_reconciliation_task(
    *,
    task_id: UUID,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings,
    adapters: TrustedActionAdapterRegistry | None = None,
    lease_owner: str | None = None,
    now: datetime | None = None,
    metrics: Metrics | None = None,
) -> bool:
    """认领并执行一轮持久只读核对。

    Args:
        task_id: Outbox 仅携带的可信 TaskRun UUID。
        session_factory: 当前 Worker 消息拥有的异步数据库工厂。
        settings: 提供主密钥、租约时长和已验证写策略；核对不会打开写开关。
        adapters: 测试或组合根注入的固定 adapter registry；省略时使用空 registry，
            以便尚未组装真实 provider adapter 的进程安全失败。
        lease_owner: 可替换的专用 owner，缺省生成随机短期值。
        now: 可替换的 UTC 当前时间，便于恢复测试确定调度边界。

    Returns:
        成功取得核对租约并完成（包括 UNKNOWN/终态 no-finish 控制流）时为 ``True``；
        任务不存在、未到期、已被接管或已终态时为 ``False``。

    Notes:
        专用 lease 只存在于 ``reconciling`` 状态，绝不调用通用
        ``DurableTaskRunner.acquire``，也不会把 TaskRun 改成 ``running``。适配器异常会
        清除当前 owner 并把任务重新压到可恢复的 ``scheduled_for``，随后交给 PostgreSQL
        恢复扫描；不会写通用成功/失败终态。
    """
    if not isinstance(task_id, UUID):
        raise TypeError("task_id must be UUID")
    owner = lease_owner or _reconciliation_owner()
    if type(owner) is not str or not owner or owner != owner.strip():
        raise ValueError("lease_owner is invalid")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(UTC)
    if await converge_revoked_pre_request_action(task_id=task_id, session_factory=session_factory):
        return True
    if (
        await SqlAlchemyTrustedActionTaskExecutionStore(
            session_factory
        ).get_authoritative_task_status(task_id=task_id)
        != TaskStatus.RECONCILING.value
    ):
        # 重复投递已收敛的任务无需 Secret，保留即使已清理内容仍可复用的终态。
        return False
    cipher = ActionPayloadCipher(AeadCipher.from_file(settings.app_master_key_file))
    transactions = SqlAlchemyTrustedActionRepositoryFactory(session_factory, cipher)
    async with transactions() as transaction:
        snapshot = await transaction.claim_reconciliation(
            task_id=task_id,
            lease_owner=owner,
            now=current,
            lease_expires_at=current + timedelta(seconds=settings.task_lease_seconds),
        )
    if snapshot is None:
        return False

    workflow = TrustedActionExecutionUseCase(
        transactions=transactions,
        adapters=adapters if adapters is not None else ProviderAdapterRegistry(),
        write_policy=settings,
        # 同一轮核对使用调用方注入的固定瞬间；持久结果时间仍由 repository 在锁内
        # 读取 PostgreSQL clock_timestamp() 决定。
        clock=lambda: current,
        observer=metrics,
    )
    step = ReconcileActionsTaskStep(workflow)
    leased = LeasedTask(
        task_id=snapshot.task_id,
        kind="trusted_action",
        input_payload={
            "approval_id": str(snapshot.approval_id),
            "operation_id": str(snapshot.operation_id),
        },
        started_at=current,
        user_id=snapshot.user_id,
        lease_owner=owner,
    )
    try:
        await step.execute(leased)
    except TrustedActionAttemptAbandoned:
        # UNKNOWN、已确认结果和并发 loser 都由持久边界决定 no-finish；不让通用 Runner
        # 或 Taskiq 把它们误写为成功。正常结果已自行清除 lease；若异常发生在结果提交
        # 前，CAS release 只会释放仍属于当前 owner 的租约。
        await _release_reconciliation_lease(
            transactions=transactions,
            task_id=task_id,
            lease_owner=owner,
            now=current,
        )
        return True
    except BaseException:
        # provider/network/commit-ACK 异常不能被解释成未应用；只释放当前 owner，保留
        # reconciling 与原 request-start 事实，待下一次有界只读恢复。
        await _release_reconciliation_lease(
            transactions=transactions,
            task_id=task_id,
            lease_owner=owner,
            now=current,
        )
        raise
    return True


async def _release_reconciliation_lease(
    *,
    transactions: SqlAlchemyTrustedActionRepositoryFactory,
    task_id: UUID,
    lease_owner: str,
    now: datetime,
) -> None:
    """在取消/超时边界 shield 一次 lease 释放，避免留下不可恢复的 owner。

    外层 Taskiq 消息可以在 provider 调用或数据库 ACK 等待期间收到取消。使用独立 task
    加 ``asyncio.shield`` 让短事务继续完成；repository 内部仍以 owner CAS 保护，不会
    清掉另一个 worker 已接管的租约。释放失败沿原异常上抛，交由上层保持消息未确认。
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    checked_now = now.astimezone(UTC)

    async def release() -> None:
        async with transactions() as transaction:
            await transaction.release_reconciliation_lease(
                task_id=task_id,
                lease_owner=lease_owner,
                now=checked_now,
            )

    release_task = asyncio.create_task(release())

    def consume_cleanup_result(done: asyncio.Task[None]) -> None:
        """消费取消后仍在后台完成的清理异常，避免 ``Task exception was never retrieved``。"""
        if not done.cancelled():
            with suppress(BaseException):
                done.exception()

    # 即使调用方在第二次取消时无法继续等待，内部短事务结束后也必须读取其结果；否则
    # 数据库异常会变成未处理任务并污染后续 Worker 日志。
    release_task.add_done_callback(consume_cleanup_result)
    try:
        await asyncio.shield(release_task)
    except asyncio.CancelledError:
        # shield 已保证 inner task 不会被取消；尽力等一次让连接归还/事务提交，再保留
        # 原始取消信号。若调用方连续取消，inner task 仍会由事件循环继续收尾。
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            # 第二次取消不能再阻塞调用方；done callback 会在内部事务结束后消费结果。
            pass
        except Exception:  # noqa: BLE001,S110 - preserve cancellation after consuming cleanup error.
            # 原始取消优先级高于清理错误，但清理异常已被此处读取，不会留下孤儿 Task。
            pass
        raise


def build_reconcile_actions_task_step(
    workflow: TrustedActionExecutionUseCase,
) -> ReconcileActionsTaskStep:
    """构造注入式核对 Step，供单元测试和消息组合根复用。"""
    return ReconcileActionsTaskStep(workflow)


# 两个拼写形式都保留为稳定组合入口，避免调用方把 ``reconcile`` 与动作名混写时自行
# 复制实现；它们指向同一只读 Step，不提供任何额外写能力。
build_reconciliation_task_step = build_reconcile_actions_task_step


__all__ = [
    "ReconcileActionsTaskStep",
    "build_reconcile_actions_task_step",
    "build_reconciliation_task_step",
    "execute_reconciliation_task",
]
