"""配置不保存业务结果的 Taskiq Redis Streams broker。"""

from taskiq_redis import RedisStreamBroker

from ai_employee.config import get_settings

settings = get_settings()
DEFAULT_RETRY_COUNT = 3

# Redis Stream 只运输可从 PostgreSQL 重建的 task_id；所有延迟重试都由 PostgreSQL Outbox
# 的 available_at 表达，不能交给 Taskiq SmartRetry 另行写 Redis 调度事实。
broker = RedisStreamBroker(
    url=settings.redis_url,
    queue_name="ai_employee_tasks",
    consumer_group_name="ai_employee_workers",
    # taskiq-redis 只在首次 ``XGROUP CREATE`` 时使用 consumer_id。显式从 ``0-0`` 创建
    # 可让首个 Worker 消费 group 建立前已写入的 backlog；若 group 已存在，Redis 会保留
    # 既有 last-delivered-id 与 pending 状态，本配置不会重置 offset 或重复接管已确认消息。
    consumer_id="0-0",
)
