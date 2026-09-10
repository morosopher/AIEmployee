"""把任务线程标识绑定到 PostgreSQL LangGraph checkpoint 的运行器。"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from psycopg import AsyncConnection, AsyncPipeline
from psycopg.rows import DictRow

from ai_employee.domain.errors import StateConflictError


class _GuardedPostgresSaver(AsyncPostgresSaver):
    """仅为 SDK 两个保存入口补 TaskRun→user 事务屏障，沿用原生序列化与 SQL。

    单连接上的外围事务另有锁，防止两个原生保存调用嵌套到对方事务；SDK 内部 cursor
    锁仍由 SDK 管理。模型/网络不在这里执行，inactive 与缺失 task 都不能保存来源内容。
    """

    def __init__(
        self,
        conn: AsyncConnection[DictRow],
        pipe: AsyncPipeline | None = None,
        serde: SerializerProtocol | None = None,
    ) -> None:
        """固定使用 from_conn_string 创建的独占连接，不引入新的池/持久化实现。"""
        super().__init__(conn=conn, pipe=pipe, serde=serde)
        self._guard_connection = conn
        self._write_gate = asyncio.Lock()

    async def _authorize_write(self, config: RunnableConfig) -> None:
        """规范 thread UUID 绑定现存 TaskRun；行锁覆盖随后的实际原生写入与提交。"""
        thread = config.get("configurable", {}).get("thread_id")
        try:
            task_id = UUID(thread) if isinstance(thread, str) else None
        except ValueError:
            task_id = None
        if task_id is None or str(task_id) != thread:
            raise self._unavailable()
        async with self._guard_connection.cursor() as cursor:
            await cursor.execute("SET LOCAL statement_timeout = '10s'")
            await cursor.execute(
                "SELECT user_id, graph_thread_id FROM task_runs WHERE id = %s FOR UPDATE",
                (task_id,),
            )
            task = await cursor.fetchone()
            if task is None or task["graph_thread_id"] not in (None, thread):
                raise self._unavailable()
            await cursor.execute(
                "SELECT is_active FROM users WHERE id = %s FOR UPDATE", (task["user_id"],)
            )
            user = await cursor.fetchone()
            if user is None or user["is_active"] is not True:
                raise self._unavailable()

    @staticmethod
    def _unavailable() -> StateConflictError:
        """不泄露 thread/user 或 checkpoint 输入的稳定拒绝。"""
        return StateConflictError(
            error_code="checkpoint_write_unavailable", message="Checkpoint write is unavailable"
        )

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """在同一锁/事务内验证 active 后调用原生完整 checkpoint 保存。"""
        async with self._write_gate, self._guard_connection.transaction():
            await self._authorize_write(config)
            return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """以相同屏障保护原生中间写；Any 只对应 SDK 无法收窄的序列化输入边界。"""
        async with self._write_gate, self._guard_connection.transaction():
            await self._authorize_write(config)
            await super().aput_writes(config, writes, task_id, task_path)


def checkpoint_database_url(database_url: str) -> str:
    """把 SQLAlchemy asyncpg URL 转为 AsyncPostgresSaver 所需的 psycopg URL。"""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def postgres_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """创建已由 Alembic 管理的 PostgreSQL checkpoint 连接。

    Args:
        database_url: 已验证的 SQLAlchemy 异步 PostgreSQL URL。

    Yields:
        可直接用于 Graph 编译的 ``AsyncPostgresSaver``。
    """
    async with _GuardedPostgresSaver.from_conn_string(
        checkpoint_database_url(database_url)
    ) as saver:
        yield saver
