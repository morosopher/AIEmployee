"""验证 Redis 集成测试 URL 的 fail-closed 安全边界。"""

import pytest

from ai_employee.infrastructure.queue.redis_url import (
    InvalidTestRedisUrl,
    validate_test_redis_url,
)


def test_accepts_explicit_loopback_test_database() -> None:
    """有效 URL 必须保留给 fixture 使用，同时调试表示不得泄露连接目标。"""
    validated = validate_test_redis_url("redis://127.0.0.1:6380/15")

    assert str(validated.value) == "redis://127.0.0.1:6380/15"
    assert validated.database_number == 15
    assert repr(validated) == "<validated test redis target>"
    assert repr(validated.value) == "<validated test redis URL>"


def test_accepts_localhost_and_ipv6_loopback_with_reserved_database() -> None:
    """明确的本地主机名与 IPv6 loopback 可使用保留测试 DB 15。"""
    assert validate_test_redis_url("redis://localhost:6380/15").database_number == 15
    assert validate_test_redis_url("redis://[::1]:6380/15").database_number == 15


@pytest.mark.parametrize(
    "value",
    [
        "",
        " redis://127.0.0.1:6380/15",
        "redis://cache.internal:6379/15",
        "redis://127.0.0.1/15",
        "redis://127.0.0.1:6380/0",
        "redis://127.0.0.1:6380/1",
        "redis://127.0.0.1:6380/16",
        "redis://127.0.0.1:6380/15?db=0",
        "redis://127.0.0.1:6380/15#override",
        "rediss://127.0.0.1:6380/15",
        "redis://127.0.0.1:6380/15/extra",
        "redis://127.0.0.1:6380/15\n",
    ],
)
def test_rejects_nonlocal_or_ambiguous_redis_targets(value: str) -> None:
    """fixture 必须拒绝共享 DB、远端主机及可能覆盖连接目标的 URL 变体。"""
    with pytest.raises(InvalidTestRedisUrl):
        validate_test_redis_url(value)
