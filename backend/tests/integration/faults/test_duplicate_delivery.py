"""验证至少一次 Taskiq 投递不会制造重复业务终态或工具执行。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_employee.application.use_cases.task_execution import DurableTaskRunner, LeasedTask


@pytest.mark.asyncio
async def test_duplicate_delivery_has_one_completion_and_one_tool_execution() -> None:
    """第二个 owner 未获得租约时必须完全无副作用。"""
    task_id = uuid4()
    now = datetime(2026, 8, 5, tzinfo=UTC)
    store = _Store(task_id, now)
    executions: list[str] = []
    runner = DurableTaskRunner(store=store, clock=lambda: now, lease_duration=timedelta(seconds=30), task_timeout_seconds=60, task_step_timeout_seconds=10, max_transient_retries=1, resolve_steps=lambda _: (_Step(executions),))
    assert await runner.run(task_id, lease_owner="first")
    assert not await runner.run(task_id, lease_owner="duplicate")
    assert executions == ["tool"]
    assert store.finish_count == 1


class _Step:
    """记录一次假工具执行。"""
    name = "tool"
    def __init__(self, calls: list[str]) -> None: self._calls = calls
    async def execute(self, task: LeasedTask) -> None: self._calls.append(self.name)


class _Store:
    """最小内存租约事实，模拟数据库 owner CAS。"""
    def __init__(self, task_id, now): self.task_id, self.now, self.status, self.owner, self.finish_count = task_id, now, "queued", None, 0
    async def prepare_retry(self, **kwargs): pass
    async def acquire(self, *, task_id, lease_owner, now, lease_expires_at, recover_waiting_approval=False):
        if self.status != "queued": return None
        self.status, self.owner = "running", lease_owner
        return LeasedTask(task_id=task_id, kind="fake", input_payload={}, started_at=self.now, lease_owner=lease_owner)
    async def renew(self, **kwargs): return True
    async def finish(self, *, lease_owner, **kwargs):
        if self.status != "running" or self.owner != lease_owner: return False
        self.status, self.owner, self.finish_count = "succeeded", None, self.finish_count + 1
        return True
    async def schedule_retry(self, **kwargs): return False
    async def fail_internal(self, **kwargs): return False
