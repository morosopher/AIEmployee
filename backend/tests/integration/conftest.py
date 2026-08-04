"""为真实 PostgreSQL 集成测试提供迁移与确定性数据隔离。"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ai_employee.infrastructure.db.alembic import set_alembic_database_url
from ai_employee.infrastructure.db.database_url import (
    InvalidTestDatabaseUrl,
    TestDatabaseUrl,
    validate_test_database_url,
)
from ai_employee.infrastructure.db.session import build_engine

# 清理范围只允许使用经过代码审查的应用表，顺序先子后父，且绝不包含 alembic_version。
APPLICATION_TABLES: tuple[str, ...] = (
    "email_analyses",
    "email_messages",
    "calendar_events",
    "email_threads",
    "sync_cursors",
    "encrypted_credentials",
    "oauth_connections",
    "oauth_attempts",
    "tool_executions",
    "approval_requests",
    "task_steps",
    "audit_events",
    "outbox_events",
    "task_runs",
    "user_sessions",
    "users",
)


@pytest.fixture(scope="session")
def database_url() -> TestDatabaseUrl:
    """读取并校验隔离的 PostgreSQL 集成测试数据库 URL。

    只接受显式 ``TEST_DATABASE_URL``，避免意外回退到开发或生产配置。数据库名必须以
    ``_test`` 结尾，因为本测试套件会清空白名单内的应用表；凭据不会写入日志或仓库。

    Returns:
        供迁移和异步 SQLAlchemy 会话共用的测试数据库 URL。

    Raises:
        pytest.UsageError: URL 缺失或不符合本地测试数据库安全约束。
    """

    value = os.environ.get("TEST_DATABASE_URL")
    if value is None:
        raise pytest.UsageError("integration tests require TEST_DATABASE_URL")
    try:
        return validate_test_database_url(value).value
    except InvalidTestDatabaseUrl as error:
        # 只传递静态拒绝原因，绝不把环境变量原值交给 pytest 输出。
        raise pytest.UsageError(str(error)) from None


@pytest.fixture(scope="session", autouse=True)
def migrated_database(database_url: TestDatabaseUrl) -> Iterator[None]:
    """在本测试会话开始时以结构化 Alembic API 升级数据库。

    Args:
        database_url: 已通过测试专用数据库安全校验的异步 URL。

    Yields:
        迁移到最新版本且可供测试使用的会话级生命周期标记。
    """

    validated = validate_test_database_url(database_url)
    backend_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(alembic_config, validated.value)
    command.upgrade(alembic_config, "head")
    yield


async def _create_temporary_database(maintenance_url: URL, database_name: str) -> bool:
    """检查 CREATEDB 权限并创建唯一的临时数据库。

    数据库名由本模块生成并先通过 URL 标识符校验；SQL 中唯一的标识符插值使用 PostgreSQL
    方言 quoting，所有环境值只作为参数绑定查询值。返回 ``False`` 表示角色缺少 CREATEDB，
    由 fixture 以 BLOCKED 方式报告，而不是退回共享数据库。
    """
    engine = create_async_engine(
        maintenance_url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    quoted_name = engine.sync_engine.dialect.identifier_preparer.quote(database_name)
    try:
        async with engine.connect() as connection:
            can_create = await connection.scalar(
                text("SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user")
            )
            if can_create is not True:
                return False
            already_exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                {"database_name": database_name},
            )
            if already_exists is not None:
                raise RuntimeError("generated temporary database name already exists")
            await connection.execute(text(f"CREATE DATABASE {quoted_name}"))
            return True
    finally:
        await engine.dispose()


async def _drop_temporary_database(maintenance_url: URL, database_name: str) -> None:
    """从维护连接精确删除本 fixture 创建的数据库。"""
    engine = create_async_engine(
        maintenance_url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    quoted_name = engine.sync_engine.dialect.identifier_preparer.quote(database_name)
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f"DROP DATABASE {quoted_name}"))
    finally:
        await engine.dispose()


@pytest.fixture
def empty_migration_database(database_url: TestDatabaseUrl) -> Iterator[URL]:
    """提供从零开始的唯一本地测试库，并在测试结束时删除同一精确目标。

    该 fixture 永远不操作共享 ``ai_employee_test``：先验证基础 URL，再生成并验证带 UUID
    的 ``_test`` 标识符，最后才创建维护引擎。若测试角色没有 CREATEDB 权限，明确报告
    BLOCKED，不能为了让套件“通过”而复用共享数据库或跳过隔离。
    """
    validated = validate_test_database_url(database_url)
    temporary_name = f"ai_employee_mig_{uuid4().hex}_test"
    temporary_url = validated.for_database(temporary_name)
    created = asyncio.run(_create_temporary_database(validated.maintenance_url(), temporary_name))
    if not created:
        pytest.fail("BLOCKED: configured test role lacks CREATEDB")

    try:
        yield temporary_url
    finally:
        asyncio.run(_drop_temporary_database(validated.maintenance_url(), temporary_name))


async def _truncate_application_tables(database_url: TestDatabaseUrl) -> None:
    """仅清空固定白名单内的应用表，并保留 Alembic 迁移版本。

    表名不是来自环境变量、数据库反射或测试输入，因此 SQL 标识符插值的范围是封闭且可审计
    的。显式子表到父表顺序也让后续改为逐表删除时仍保持外键安全。

    Args:
        database_url: 已通过测试专用数据库安全校验的异步 URL。
    """

    validated = validate_test_database_url(database_url)
    engine = build_engine(validated.value)
    quoted_tables = ", ".join(
        engine.dialect.identifier_preparer.quote(name) for name in APPLICATION_TABLES
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {quoted_tables}"))
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def isolated_database(
    migrated_database: None,
    database_url: TestDatabaseUrl,
) -> AsyncIterator[None]:
    """在每个集成测试前后清理应用表，阻断跨测试状态泄漏。

    Args:
        migrated_database: 保证 Schema 已升级到当前 Alembic head 的依赖标记。
        database_url: 已通过测试专用数据库安全校验的异步 URL。

    Yields:
        当前测试独占的空应用数据集合；``alembic_version`` 始终保留。
    """

    await _truncate_application_tables(database_url)
    try:
        yield
    finally:
        await _truncate_application_tables(database_url)
