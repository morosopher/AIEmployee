"""验证 Alembic 能在独立空数据库中完成首个身份迁移并保持元数据一致。"""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ai_employee.infrastructure.db.alembic import set_alembic_database_url


def _public_table_names(database_url: URL) -> set[str]:
    """读取指定临时库的 public 表名，调用方负责在独立进程中运行异步查询。"""

    async def read_names() -> set[str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT tablename FROM pg_catalog.pg_tables "
                        "WHERE schemaname = 'public' ORDER BY tablename"
                    )
                )
                return {row[0] for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_names())


def _check_constraint_names(database_url: URL) -> set[str]:
    """读取会话摘要长度检查约束名称，确认初始迁移没有遗漏安全不变量。"""

    async def read_names() -> set[str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT conname FROM pg_catalog.pg_constraint "
                        "WHERE conrelid = 'user_sessions'::regclass AND contype = 'c'"
                    )
                )
                return {row[0] for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_names())


def test_head_migration_starts_from_empty_database_and_has_no_metadata_drift(
    empty_migration_database: URL,
) -> None:
    """升级唯一生成的临时库后，迁移头与 ORM 元数据必须完全一致。"""
    assert _public_table_names(empty_migration_database) == set()

    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "head")

    assert _public_table_names(empty_migration_database) == {
        "alembic_version",
        "users",
        "user_sessions",
    }
    assert _check_constraint_names(empty_migration_database) == {
        "ck_user_sessions_token_hash_octet_length_32",
        "ck_user_sessions_csrf_hash_octet_length_32",
    }
    command.check(alembic_config)
