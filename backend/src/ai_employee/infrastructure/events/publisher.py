"""Redis Pub/Sub 通知适配器：只提示重新读取 PostgreSQL 事实。"""

from uuid import UUID


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
