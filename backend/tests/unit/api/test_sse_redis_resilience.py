"""验证 SSE 的 Redis 快路径始终受限且可退回 PostgreSQL 轮询。"""

import asyncio
from uuid import uuid4

import pytest

from ai_employee.api import sse
from ai_employee.api.sse import RedisTaskEventSubscription, TaskEventStream


class _HangingPubSub:
    """模拟 Redis 连接成功后永久不返回订阅确认的异常边界。"""

    def __init__(self) -> None:
        """记录关闭调用，证明超时失败路径释放了专用 Pub/Sub 资源。"""
        self.closed = False

    async def subscribe(self, *_channels: str) -> None:
        """模拟永不完成的 Redis 订阅握手。"""
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        """记录 Pub/Sub 清理，不访问真实 Redis。"""
        self.closed = True


class _HangingRedisClient:
    """返回可观察清理状态的伪 Redis 客户端。"""

    def __init__(self, pubsub: _HangingPubSub) -> None:
        """保存唯一 Pub/Sub 实例。"""
        self._pubsub = pubsub
        self.closed = False

    def pubsub(self) -> _HangingPubSub:
        """返回用于本测试的阻塞订阅对象。"""
        return self._pubsub

    async def aclose(self) -> None:
        """记录客户端连接关闭。"""
        self.closed = True


class _SubscribedHangingPubSub:
    """模拟已确认订阅但读取通知永久阻塞的 Redis 连接。"""

    def __init__(self) -> None:
        """初始化订阅确认和资源关闭观察标记。"""
        self.subscribed = False
        self.closed = False

    async def subscribe(self, *_channels: str) -> None:
        """立即确认订阅，确保后续超时来自消息读取而非握手。"""
        self.subscribed = True

    async def get_message(self, **_kwargs: object) -> None:
        """模拟已订阅连接在读取 Redis 消息时永久无响应。"""
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        """记录 Pub/Sub 清理。"""
        self.closed = True


class _TimedOutSubscription:
    """模拟连接 Redis 时超时的订阅，要求流降级为持久轮询。"""

    def __init__(self) -> None:
        """初始化清理观察标记。"""
        self.closed = False

    async def open(self) -> None:
        """明确模拟受限 Redis 操作超时。"""
        raise TimeoutError

    async def wait(self, timeout_seconds: float) -> bool:
        """该订阅无法成功打开，因此不应被调用。"""
        del timeout_seconds
        raise AssertionError("timed-out subscription must be replaced")

    async def aclose(self) -> None:
        """记录流在降级前关闭旧订阅。"""
        self.closed = True


class _EmptyEventStore:
    """提供空 PostgreSQL 事实集合，使断言聚焦 Redis 降级语义。"""

    async def oldest_event_id(self, **_kwargs: object) -> int:
        """声明任务存在历史范围，跳过快照分支。"""
        return 0

    async def events_after(self, **_kwargs: object) -> tuple[object, ...]:
        """没有新持久事件，流应产生临时心跳。"""
        return ()


@pytest.mark.asyncio
async def test_subscription_open_timeout_is_bounded_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis 订阅握手超时必须在小预算内返回并释放已创建资源。"""
    pubsub = _HangingPubSub()
    client = _HangingRedisClient(pubsub)
    monkeypatch.setattr(sse, "REDIS_OPERATION_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda *_args, **_kwargs: client)
    subscription = RedisTaskEventSubscription("redis://unused/15", uuid4())

    started_at = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.open(), timeout=0.1)
    elapsed = asyncio.get_running_loop().time() - started_at

    assert elapsed < 0.05
    assert pubsub.closed is True
    assert client.closed is True


@pytest.mark.asyncio
async def test_subscription_open_timeout_falls_back_to_postgres_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis 连接超时不终止 SSE，持久事实流仍以轮询心跳继续。"""
    timed_out = _TimedOutSubscription()
    monkeypatch.setattr(sse, "HEARTBEAT_SECONDS", 0)
    stream = TaskEventStream(
        _EmptyEventStore(),  # type: ignore[arg-type]
        subscription_factory=lambda _: timed_out,
    )
    events = stream.events(task_id=uuid4(), user_id=uuid4(), last_event_id=None)

    event = await asyncio.wait_for(anext(events), timeout=0.1)
    await events.aclose()

    assert event.event == "heartbeat"
    assert timed_out.closed is True


@pytest.mark.asyncio
async def test_subscribed_message_read_timeout_closes_resources_and_falls_back_to_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已订阅 Redis 的消息读取超时后，流必须释放资源并持续 PostgreSQL 心跳兜底。"""
    pubsub = _SubscribedHangingPubSub()
    client = _HangingRedisClient(pubsub)  # type: ignore[arg-type]
    monkeypatch.setattr(sse, "HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(sse, "REDIS_OPERATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda *_args, **_kwargs: client)
    stream = TaskEventStream(_EmptyEventStore(), redis_url="redis://unused/15")  # type: ignore[arg-type]
    events = stream.events(task_id=uuid4(), user_id=uuid4(), last_event_id=None)

    first = await asyncio.wait_for(anext(events), timeout=0.1)
    second = await asyncio.wait_for(anext(events), timeout=0.1)
    await events.aclose()

    assert pubsub.subscribed is True
    assert pubsub.closed is True
    assert client.closed is True
    assert first.event == second.event == "heartbeat"
