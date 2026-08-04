"""验证逾期简报诊断节点只保存允许的最小诊断快照。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.workers.diagnostics import OverdueBriefDiagnosticTaskStep


class _SnapshotStore:
    """记录诊断节点请求的边界参数，不接触 PostgreSQL 或来源数据。"""

    def __init__(self) -> None:
        """初始化空调用记录。"""
        self.calls: list[tuple[UUID, UUID, datetime]] = []

    async def save_snapshot(self, *, task_id: UUID, user_id: UUID, now: datetime) -> None:
        """记录节点传递的标识与时钟，模拟脱敏存储端口。"""
        self.calls.append((task_id, user_id, now))


def _leased_task(*, user_id: UUID | None) -> LeasedTask:
    """构造最小诊断任务租约，输入不含任何来源内容。"""
    return LeasedTask(
        task_id=uuid4(),
        kind="brief.overdue_diagnostic",
        input_payload={"local_date": "2026-08-04"},
        started_at=datetime(2026, 8, 4, tzinfo=UTC),
        user_id=user_id,
        lease_owner="worker-1",
    )


@pytest.mark.asyncio
async def test_diagnostic_step_saves_snapshot_for_the_leased_task_owner() -> None:
    """节点只能用租约内用户 UUID 保存快照，不能接受客户端指定的用户。"""
    store = _SnapshotStore()
    step = OverdueBriefDiagnosticTaskStep.__new__(OverdueBriefDiagnosticTaskStep)
    step._store = store  # type: ignore[assignment]  # 测试替身保持生产节点的窄存储协议。
    task = _leased_task(user_id=uuid4())

    await step.execute(task)

    assert len(store.calls) == 1
    task_id, user_id, saved_at = store.calls[0]
    assert (task_id, user_id) == (task.task_id, task.user_id)
    assert saved_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_diagnostic_step_rejects_a_task_without_user_ownership() -> None:
    """缺少用户归属的队列消息不得创建全局或未隔离的诊断结果。"""
    step = OverdueBriefDiagnosticTaskStep.__new__(OverdueBriefDiagnosticTaskStep)
    step._store = _SnapshotStore()  # type: ignore[assignment]  # 该分支必须在存储调用前失败。

    with pytest.raises(ValueError, match="requires user_id"):
        await step.execute(_leased_task(user_id=None))
