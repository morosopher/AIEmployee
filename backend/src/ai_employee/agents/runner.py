"""把任务线程标识绑定到 PostgreSQL LangGraph checkpoint 的运行器。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def checkpoint_database_url(database_url: str) -> str:
    """把 SQLAlchemy asyncpg URL 转为 AsyncPostgresSaver 所需的 psycopg URL。"""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def postgres_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """创建并初始化唯一的 PostgreSQL checkpoint 表连接。

    Args:
        database_url: 已验证的 SQLAlchemy 异步 PostgreSQL URL。

    Yields:
        已运行 ``setup`` 的 ``AsyncPostgresSaver``，由调用方传给 Graph 编译。
    """
    async with AsyncPostgresSaver.from_conn_string(checkpoint_database_url(database_url)) as saver:
        await saver.setup()
        yield saver
