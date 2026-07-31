"""为真实 PostgreSQL 集成测试提供迁移与确定性数据隔离。"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from ai_employee.infrastructure.db.session import build_engine

# 清理范围只允许使用经过代码审查的应用表，顺序先子后父，且绝不包含 alembic_version。
APPLICATION_TABLES: tuple[str, ...] = ("user_sessions", "users")


@pytest.fixture(scope="session")
def database_url() -> str:
    """读取并校验隔离的 PostgreSQL 集成测试数据库 URL。

    只接受显式 ``TEST_DATABASE_URL``，避免意外回退到开发或生产配置。数据库名必须以
    ``_test`` 结尾，因为本测试套件会清空白名单内的应用表；凭据不会写入日志或仓库。

    Returns:
        供迁移和异步 SQLAlchemy 会话共用的测试数据库 URL。

    Raises:
        pytest.UsageError: 环境变量缺失、不是 PostgreSQL 异步 URL或数据库名不符合安全约束。
    """

    value = os.environ.get("TEST_DATABASE_URL")
    if value is None:
        raise pytest.UsageError("integration tests require TEST_DATABASE_URL")

    parsed = make_url(value)
    if parsed.drivername != "postgresql+asyncpg":
        raise pytest.UsageError("TEST_DATABASE_URL must use postgresql+asyncpg")
    if parsed.database is None or not parsed.database.endswith("_test"):
        raise pytest.UsageError("TEST_DATABASE_URL database name must end with _test")
    return value


@pytest.fixture(scope="session", autouse=True)
def migrated_database(database_url: str) -> Iterator[None]:
    """在本测试会话开始时以结构化 Alembic API 升级数据库。

    Args:
        database_url: 已通过测试专用数据库安全校验的异步 URL。

    Yields:
        迁移到最新版本且可供测试使用的会话级生命周期标记。
    """

    backend_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(backend_root / "alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    yield


async def _truncate_application_tables(database_url: str) -> None:
    """仅清空固定白名单内的应用表，并保留 Alembic 迁移版本。

    表名不是来自环境变量、数据库反射或测试输入，因此 SQL 标识符插值的范围是封闭且可审计
    的。显式子表到父表顺序也让后续改为逐表删除时仍保持外键安全。

    Args:
        database_url: 已通过测试专用数据库安全校验的异步 URL。
    """

    engine = build_engine(database_url)
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
    database_url: str,
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
