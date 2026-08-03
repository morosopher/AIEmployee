"""Redis Pub/Sub 通知适配器：只提示重新读取 PostgreSQL 事实。"""

import logging
from uuid import UUID

from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class TaskEventPublisher:
    """发布任务审计 ID 的短暂通知，绝不把事件正文作为 Redis 事实。"""

    def __init__(self, redis_url: str) -> None:
        """保存连接地址；客户端延迟创建以避免 API 启动时占用 Redis。"""
        self._redis_url = redis_url

    @staticmethod
    def channel(task_id: UUID) -> str:
        """返回仅由稳定任务 UUID 构成的通知频道。"""
        return f"ai_employee:task-events:{task_id}"

    async def publish(self, *, task_id: UUID, event_id: int) -> None:
        """发布可重建审计行 ID；Redis 故障交给调用方按通知失败处理。"""
        from redis.asyncio import Redis

        client = Redis.from_url(self._redis_url, decode_responses=True)
        try:
            await client.publish(self.channel(task_id), str(event_id))
        finally:
            await client.aclose()

    async def publish_after_commit(self, *, task_id: UUID, event_id: int) -> None:
        """在 PostgreSQL 提交成功后尽力发布瞬时唤醒，不影响已持久化的业务结果。

        Redis Pub/Sub 只缩短在线 SSE 客户端发现新审计行的时间，不能成为任务、审批或步骤
        状态提交的前提。调用方只可在持久事务提交完成后调用本方法；连接、发布或关闭时的
        Redis 故障被记录为无正文的运维信号，SSE 会在下一次 15 秒 PostgreSQL 轮询恢复。

        Args:
            task_id: 已提交审计事件所属的稳定任务标识。
            event_id: 已提交审计事件的 PostgreSQL 单调主键。
        """
        try:
            await self.publish(task_id=task_id, event_id=event_id)
        except (OSError, RedisError):
            # 业务事实已存在 PostgreSQL，禁止把 Redis 瞬时故障反向传播到请求或 Worker。
            logger.warning("task event notification unavailable", extra={"event_id": event_id})
