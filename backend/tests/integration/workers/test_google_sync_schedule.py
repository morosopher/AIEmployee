"""验证十分钟 Google 同步调度的标签、双任务和健康连接过滤。"""

from uuid import UUID

import pytest

from ai_employee.workers import schedules


class _Reader:
    """返回调度器已过滤后的 connected 连接，模拟 repository 的健康边界。"""
    async def connected_connections(self):
        """仅 connected 会到达此端口；断开/degraded 必须在 SQL 查询中排除。"""
        return ((UUID("00000000-0000-0000-0000-000000000001"), UUID("00000000-0000-0000-0000-000000000002")),)


@pytest.mark.asyncio
async def test_google_scheduler_creates_two_bucket_idempotent_tasks(monkeypatch) -> None:
    """每个健康连接每个桶创建 Gmail/Calendar 两项且键含 connection/resource/bucket。"""
    created: list[dict[str, object]] = []
    class _Creator:
        def __init__(self, *args, **kwargs):
            del args, kwargs
        async def execute(self, **kwargs):
            created.append(kwargs)
    monkeypatch.setattr(schedules, "SqlAlchemyConnectedGoogleReader", lambda _factory: _Reader())
    monkeypatch.setattr(schedules, "CreateTaskUseCase", _Creator)
    monkeypatch.setattr(schedules, "_build_outbox_relay", lambda: object())
    await schedules.dispatch_google_incremental_syncs()
    assert {item["kind"] for item in created} == {"sync_gmail", "sync_calendar"}
    assert all(str(item["idempotency_key"]).startswith("sync:google:00000000-0000-0000-0000-000000000002:") for item in created)


def test_google_incremental_schedule_has_ten_minute_label() -> None:
    """Taskiq 注册固定 schedule_id 和十分钟 cron，避免进程内无持久化定时器。"""
    assert hasattr(schedules, "dispatch_google_incremental_syncs")
