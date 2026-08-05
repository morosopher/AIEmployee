"""为不触碰数据库的故障语义测试屏蔽全局迁移 fixture。"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """这些测试使用内存端口验证恢复语义，不创建或清理任何数据库。"""
    return


@pytest.fixture(autouse=True)
async def isolated_database() -> None:
    """内存端口测试不需要 PostgreSQL 清理，也不应读取 TEST_DATABASE_URL。"""
    yield
