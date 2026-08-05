"""覆盖丢失租约后禁止提交及过期租约接管。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_employee.application.use_cases.task_execution import DurableTaskRunner, LeasedTask


@pytest.mark.asyncio
async def test_worker_losing_lease_cannot_commit_terminal_result() -> None:
    """节点之间续租 CAS 失败后 runner 返回 false，且不调用 finish。"""
    now = datetime(2026, 8, 5, tzinfo=UTC)
    store = _LostLeaseStore(uuid4(), now)
    runner = DurableTaskRunner(store=store, clock=lambda: now, lease_duration=timedelta(seconds=30), task_timeout_seconds=60, task_step_timeout_seconds=10, max_transient_retries=0, resolve_steps=lambda _: (_Noop(), _Noop()))
    assert not await runner.run(store.task_id, lease_owner="expired-owner")
    assert store.finished is False


class _Noop:
    """不产生副作用的检查点节点。"""
    name = "checkpoint"
    async def execute(self, task: LeasedTask) -> None: pass


class _LostLeaseStore:
    """让首个节点后的 owner 续租失败。"""
    def __init__(self, task_id, now): self.task_id, self.now, self.finished = task_id, now, False
    async def prepare_retry(self, **kwargs): pass
    async def acquire(self, *, task_id, lease_owner, **kwargs): return LeasedTask(task_id=task_id, kind="fake", input_payload={}, started_at=self.now, lease_owner=lease_owner)
    async def renew(self, **kwargs): return False
    async def finish(self, **kwargs): self.finished = True; return True
    async def schedule_retry(self, **kwargs): return False
    async def fail_internal(self, **kwargs): return False
