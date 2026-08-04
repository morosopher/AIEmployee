"""为需要 PostgreSQL 客户端的 retention 角色测试控制 fixture 前置条件。"""

from __future__ import annotations

import os
import shutil

import pytest

from ai_employee.infrastructure.db.database_url import (
    InvalidTestDatabaseUrl,
    TestDatabaseUrl,
    validate_test_database_url,
)


@pytest.fixture(scope="session")
def database_url() -> TestDatabaseUrl:
    """仅在可调用生产角色初始化脚本时解析隔离测试数据库。

    上级 ``migrated_database`` 是 autouse fixture，会先请求本目录覆盖的 fixture。先检查
    ``psql`` 可避免普通单测环境在缺失 ``TEST_DATABASE_URL`` 时把应跳过的角色测试报为错误。
    当客户端可用时仍严格复用集成测试 URL 校验和迁移流程，不会降级为模拟权限测试。
    """
    if shutil.which("psql") is None:
        pytest.skip("database role permission integration test requires the PostgreSQL psql client")
    value = os.environ.get("TEST_DATABASE_URL")
    if value is None:
        raise pytest.UsageError("integration tests require TEST_DATABASE_URL")
    try:
        return validate_test_database_url(value).value
    except InvalidTestDatabaseUrl as error:
        raise pytest.UsageError(str(error)) from None
