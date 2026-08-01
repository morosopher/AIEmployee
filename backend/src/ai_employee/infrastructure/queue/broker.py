"""配置不保存业务结果的 Taskiq Redis Streams broker。"""

from taskiq.middlewares import SmartRetryMiddleware
from taskiq_redis import RedisStreamBroker

from ai_employee.config import get_settings
from ai_employee.domain.errors import TransientProviderError

settings = get_settings()

# Redis Stream 只运输可从 PostgreSQL 重建的 task_id；DummyResultBackend 保持 Taskiq 默认值，
# 不把执行结果复制到 Redis。异常白名单是第二道防线，避免入口遗漏捕获时重试未知异常。
broker = RedisStreamBroker(
    url=settings.redis_url,
    queue_name="ai_employee_tasks",
    consumer_group_name="ai_employee_workers",
).with_middlewares(
    SmartRetryMiddleware(
        default_retry_count=3,
        default_delay=5,
        use_jitter=True,
        use_delay_exponent=True,
        max_delay_exponent=300,
        types_of_exceptions=(TransientProviderError,),
    )
)
