"""验证 Redis 任务事件通知不会阻塞已提交的 PostgreSQL 业务事实。"""

import asyncio
from uuid import uuid4

import pytest

from ai_employee.infrastructure.events import publisher
from ai_employee.infrastructure.events.publisher import TaskEventPublisher


class _HangingRedisClient:
    """模拟发布调用永久阻塞的 Redis 客户端。"""

    def __init__(self) -> None:
        """初始化连接关闭观察标记。"""
        self.closed = False

    async def publish(self, *_args: object) -> None:
        """模拟不返回的网络发布操作。"""
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        """记录超时后的连接资源释放。"""
        self.closed = True


@pytest.mark.asyncio
async def test_publish_after_commit_times_out_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提交后通知受固定预算限制，超时不会传播至调用事务。"""
    client = _HangingRedisClient()
    monkeypatch.setattr(publisher, "REDIS_OPERATION_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda *_args, **_kwargs: client)

    await asyncio.wait_for(
        TaskEventPublisher("redis://unused/15").publish_after_commit(
            task_id=uuid4(), event_id=1
        ),
        timeout=0.1,
    )

    assert client.closed is True
