"""为不接触 PostgreSQL 的 tracing 集成测试隔离全局数据库 fixture。"""

from collections.abc import AsyncIterator, Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    """覆盖上级迁移 fixture，使仅进程内的 telemetry 测试不要求 TEST_DATABASE_URL。"""
    yield


@pytest.fixture(autouse=True)
async def isolated_database() -> AsyncIterator[None]:
    """覆盖上级表清理 fixture；本目录测试不得使用 PostgreSQL 事实。"""
    yield
