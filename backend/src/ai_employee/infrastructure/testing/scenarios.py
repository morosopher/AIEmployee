"""连接 test-support Redis 一次性场景与离线 fake 适配器。"""

from uuid import UUID

from redis.asyncio import Redis

TEST_SCENARIO_KEY_PREFIX = "ai_employee:test-scenario:"


async def consume_test_scenario(*, redis_url: str, user_id: UUID) -> str | None:
    """原子消费当前用户的测试故障并立即关闭短生命周期 Redis 客户端。

    该函数只由 ``APP_TEST_MODE`` 下的组合根传入 fake；生产适配器既不导入也不调用它。
    ``GETDEL`` 保证同一场景不会被重试或并发 fake 重复施加。
    """
    client = Redis.from_url(redis_url)
    try:
        value = await client.getdel(f"{TEST_SCENARIO_KEY_PREFIX}{user_id}")
    finally:
        await client.aclose()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value if isinstance(value, str) else None
