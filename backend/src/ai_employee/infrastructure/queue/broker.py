"""配置不保存业务结果的 Taskiq Redis Streams broker。"""

from taskiq.middlewares import SmartRetryMiddleware
from taskiq_redis import ListRedisScheduleSource, RedisStreamBroker

from ai_employee.config import get_settings
from ai_employee.domain.errors import TransientProviderError

settings = get_settings()
DEFAULT_RETRY_COUNT = 3

# SmartRetry 通过调度源保存带秒级 target time 的重投任务。Redis Streams broker 只负责
# 立即可执行消息；若仅写 ``delay`` label，taskiq-redis 1.1.2 会直接 XADD 并忽略该延迟。
retry_schedule_source = ListRedisScheduleSource(
    url=settings.redis_url,
    prefix="ai_employee_task_retries",
)

# Redis Stream 只运输可从 PostgreSQL 重建的 task_id；DummyResultBackend 保持 Taskiq 默认值，
# 不把执行结果复制到 Redis。异常白名单是第二道防线，避免入口遗漏捕获时重试未知异常。
broker = RedisStreamBroker(
    url=settings.redis_url,
    queue_name="ai_employee_tasks",
    consumer_group_name="ai_employee_workers",
    # taskiq-redis 只在首次 ``XGROUP CREATE`` 时使用 consumer_id。显式从 ``0-0`` 创建
    # 可让首个 Worker 消费 group 建立前已写入的 backlog；若 group 已存在，Redis 会保留
    # 既有 last-delivered-id 与 pending 状态，本配置不会重置 offset 或重复接管已确认消息。
    consumer_id="0-0",
).with_middlewares(
    SmartRetryMiddleware(
        default_retry_count=DEFAULT_RETRY_COUNT,
        default_delay=5,
        use_jitter=True,
        use_delay_exponent=True,
        max_delay_exponent=300,
        schedule_source=retry_schedule_source,
        types_of_exceptions=(TransientProviderError,),
    )
)
