"""为 Redis Streams 专属集成测试提供 fail-closed 的隔离数据库 fixture。"""

import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from ai_employee.infrastructure.queue.redis_url import (
    InvalidTestRedisUrl,
    RedisTestUrl,
    validate_test_redis_url,
)


@pytest.fixture
def redis_url() -> RedisTestUrl:
    """读取并校验显式 ``TEST_REDIS_URL``，禁止回退到应用 ``REDIS_URL``。

    Returns:
        仅指向 loopback、显式端口和保留测试数据库 15 的 Redis URL。

    Raises:
        pytest.UsageError: 环境变量缺失或未通过安全校验。
    """
    value = os.environ.get("TEST_REDIS_URL")
    if value is None:
        raise pytest.UsageError("Redis integration tests require TEST_REDIS_URL")
    try:
        return validate_test_redis_url(value).value
    except InvalidTestRedisUrl as error:
        # 只传递静态拒绝原因，绝不把环境变量原值交给 pytest 输出。
        raise pytest.UsageError(str(error)) from None


@pytest.fixture
async def empty_redis(redis_url: RedisTestUrl) -> AsyncIterator[RedisTestUrl]:
    """只清空已验证的本地 Redis 保留测试 DB 15，并在用例后再次精确清理。

    Args:
        redis_url: 已通过 loopback、端口和测试 DB 选择校验的连接目标。

    Yields:
        可供干净子进程与断言客户端共用的受保护 URL。
    """
    client: Redis = Redis.from_url(str(redis_url), decode_responses=False)
    try:
        await client.ping()
        # FLUSHDB 的范围只覆盖 URL 明确选择的保留 DB 15，绝不操作默认或开发 DB。
        await client.flushdb()
        yield redis_url
    finally:
        await client.flushdb()
        await client.aclose()
