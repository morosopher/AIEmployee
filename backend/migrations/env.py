"""配置 Alembic 使用项目元数据与 SQLAlchemy 官方异步迁移流程。"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from ai_employee.infrastructure.db import models as db_models
from ai_employee.infrastructure.db.base import Base

config = context.config

database_url = os.environ.get("DATABASE_URL")
if database_url is not None and not config.get_main_option("sqlalchemy.url"):
    # 命令行迁移从环境接收部署配置，但不覆盖测试通过 Alembic Config 注入的隔离 URL。
    config.set_main_option("sqlalchemy.url", database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 导入模型模块是注册元数据所必需的显式副作用；保留引用可避免静态检查误判未使用导入。
_ = db_models
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """在无数据库连接时生成 SQL 迁移脚本。

    URL 仅由 Alembic Config 提供；迁移环境不会读取或记录额外凭据。
    """

    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在 Alembic 提供的同步桥接连接上执行迁移。

    Args:
        connection: 由异步连接通过 ``run_sync`` 暴露的同步 SQLAlchemy 连接。
    """

    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """创建一次性异步引擎并在线执行全部待应用迁移。

    引擎使用 ``NullPool``，避免短生命周期迁移命令在退出前保留连接。无论迁移成功或失败，
    引擎都会被释放，原始异常继续向调用方传播。
    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """从同步 Alembic 入口驱动异步在线迁移。"""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
