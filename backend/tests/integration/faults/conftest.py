"""为故障演练提供受限 Redis 测试库，绝不回退到开发或共享实例。"""

import os

import pytest

from ai_employee.infrastructure.queue.redis_url import (
    InvalidTestRedisUrl,
    RedisTestUrl,
    validate_test_redis_url,
)


@pytest.fixture
def redis_url() -> RedisTestUrl:
    """返回已验证的 loopback Redis DB 15 连接供 Redis 丢失演练使用。

    故障测试需要真实 ``FLUSHDB``，因此必须将环境变量校验限制为独立容器的保留 DB，
    避免任何测试代码将清库操作扩散到开发或生产 Redis。
    """
    value = os.environ.get("TEST_REDIS_URL")
    if value is None:
        raise pytest.UsageError("Redis integration tests require TEST_REDIS_URL")
    try:
        return validate_test_redis_url(value).value
    except InvalidTestRedisUrl as error:
        raise pytest.UsageError(str(error)) from None
