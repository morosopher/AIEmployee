"""验证 Redis 丢失不影响 PostgreSQL 事实与 Outbox 恢复契约。"""

from datetime import UTC, datetime

import pytest

from ai_employee.application.use_cases.task_retry_recovery import RecoverScheduledTaskRetriesUseCase


@pytest.mark.asyncio
async def test_redis_flush_recovery_delegates_to_postgresql_outbox_store() -> None:
    """恢复用例只调用耐久 store，因此 Redis flush 不会删除业务事实。"""
    calls: list[tuple[datetime, int]] = []
    class Store:
        async def recover_due(self, *, now: datetime, limit: int) -> int:
            calls.append((now, limit)); return 1
    now = datetime(2026, 8, 5, tzinfo=UTC)
    assert await RecoverScheduledTaskRetriesUseCase(store=Store()).execute(now=now, limit=20) == 1
    assert calls == [(now, 20)]
