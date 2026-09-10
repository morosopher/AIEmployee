"""用已有 app/checkpointer 权限清理精确 TaskRun thread，不为 retention 提升权限。

原生 saver 的 delete 与准入共用一条 psycopg 连接和事务；不复制 LangGraph SQL/序列化，
不扫描 checkpoint 内容推断所有者，失败时保留赢家和目标 TaskRun 供同一请求恢复。
"""

from datetime import datetime
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection

from ai_employee.application.use_cases.auth import Clock
from ai_employee.application.use_cases.privacy import (
    PRIVACY_DELETION_STARTED_EVENT_TYPE,
    PrivacyDeletionBinding,
    PrivacyDeletionStartedFact,
    parse_privacy_deletion_started_authority,
)
from ai_employee.infrastructure.db.repositories.privacy_deletion import deletion_unavailable


class PostgresPrivacyCheckpointCleaner:
    """只提供有权删除 thread 的窄端口；DSN 来自共同 app checkpoint 配置。"""

    def __init__(self, database_url: str, *, clock: Clock | None = None) -> None:
        """保存组合根明确提供的 app DSN，不读取 retention URL 或动态 SET ROLE。"""
        self._database_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._clock = clock

    async def clear_thread(
        self,
        *,
        binding: PrivacyDeletionBinding,
        task_id: UUID,
        now: datetime,
    ) -> None:
        """排序锁目标/赢家，再锁 user 并调用严格 parser；原生三表删除与准入原子提交。

        Args:
            binding: 当前删除赢家的原始 request 与活租约身份。
            task_id: 同用户现存任务，其规范 UUID 是唯一允许的 thread。
            now: 删除 Worker 的显式 UTC Clock 瞬间。

        Raises:
            StateConflictError: 任一归属、任务状态、租约或 authority 事实不匹配。
            psycopg.Error: 角色无权限、锁超时或原生删除失败；调用者不能继续删除父任务。
        """
        async with AsyncPostgresSaver.from_conn_string(self._database_url) as saver:
            connection = saver.conn
            if not isinstance(connection, AsyncConnection):
                raise TypeError("privacy checkpoint cleaner requires a dedicated connection")
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute("SET LOCAL statement_timeout = '10s'")
                    expected = {binding.task_id, task_id}
                    await cursor.execute(
                        "SELECT id, graph_thread_id, kind, status, input_payload, started_at, "
                        "attempt_count, lease_owner, lease_expires_at FROM task_runs "
                        "WHERE user_id = %s AND id = ANY(%s) ORDER BY id FOR UPDATE",
                        (binding.user_id, sorted(expected)),
                    )
                    tasks = {row["id"]: row for row in await cursor.fetchall()}
                    if set(tasks) != expected or tasks[task_id]["graph_thread_id"] not in (
                        None,
                        str(task_id),
                    ):
                        raise deletion_unavailable()
                    await cursor.execute(
                        "SELECT is_active FROM users WHERE id = %s FOR UPDATE", (binding.user_id,)
                    )
                    user = await cursor.fetchone()
                    winner = tasks[binding.task_id]
                    # 等待另一 saver/Worker 的锁可能跨越租约截止，必须锁后从共同 Clock
                    # 取时。测试未提供 Clock 时使用其显式固定瞬间，不借宿主机时区猜测。
                    checked_at = self._clock.now() if self._clock is not None else now
                    if (
                        user is None
                        or user["is_active"] is not False
                        or winner["kind"] != "privacy.delete_all_data"
                        or winner["status"] != "running"
                        or winner["started_at"] is None
                        or winner["attempt_count"] <= 0
                        or not binding.request_id
                        or not binding.lease_owner
                        or winner["input_payload"] != {"deletion_request_id": binding.request_id}
                        or winner["lease_owner"] != binding.lease_owner
                        or winner["lease_expires_at"] is None
                        or winner["lease_expires_at"] <= checked_at
                    ):
                        raise deletion_unavailable()
                    await cursor.execute(
                        "SELECT id FROM tool_executions WHERE task_id = %s LIMIT 1",
                        (binding.task_id,),
                    )
                    if await cursor.fetchone() is not None:
                        raise deletion_unavailable()
                    # app 的审计表没有 UPDATE，不能借 FOR UPDATE 绕过冻结 ACL。winner/user
                    # 锁已经保护唯一屏障创建；此处仍读取完整集合，绝不按 request 筛出想要的行。
                    await cursor.execute(
                        "SELECT event_type, user_id, task_id, metadata FROM audit_events "
                        "WHERE user_id = %s AND event_type = %s ORDER BY id",
                        (binding.user_id, PRIVACY_DELETION_STARTED_EVENT_TYPE),
                    )
                    facts = [
                        PrivacyDeletionStartedFact(
                            event_type=row["event_type"],
                            user_id=row["user_id"],
                            task_id=row["task_id"],
                            event_metadata=row["metadata"],
                        )
                        for row in await cursor.fetchall()
                    ]
                    if (
                        parse_privacy_deletion_started_authority(
                            facts,
                            expected_user_id=binding.user_id,
                            expected_task_id=binding.task_id,
                            expected_request_id=binding.request_id,
                        )
                        is None
                    ):
                        raise deletion_unavailable()
                await saver.adelete_thread(str(task_id))

    async def clear_expired_thread(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        cutoff: datetime,
    ) -> None:
        """在 retention 连续持有 Task→user 锁期间，使用 app 只读重验并清理原生三表。

        Args:
            user_id: 外层已锁定且保持 active 的精确任务所有者。
            task_id: 外层锁定并将在本次事务删除的过期终态任务。
            cutoff: 当前用户的工作区历史截止，要求 finished_at 严格小于此值。

        Raises:
            StateConflictError: 归属、规范 thread、状态、截止或删除 authority 不匹配。
            psycopg.Error: 原生删除或超时失败；外层不能继续删除父行。

        这里不能 FOR UPDATE：行锁由尚未提交的 retention 连接持有，重复请求会自锁。
        TaskHistoryCleanup 在本调用前后保持同一个事务和 Task 行锁；旧 saver 的 Task 锁
        因此持续阻塞到父行删除提交。独立 app 提交只移除合法到期的数据，不提交业务父表。
        """
        async with AsyncPostgresSaver.from_conn_string(self._database_url) as saver:
            connection = saver.conn
            if not isinstance(connection, AsyncConnection):
                raise TypeError("privacy checkpoint cleaner requires a dedicated connection")
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute("SET LOCAL statement_timeout = '10s'")
                    await cursor.execute(
                        "SELECT t.graph_thread_id FROM task_runs AS t JOIN users AS u ON u.id=t.user_id "
                        "WHERE t.id=%s AND t.user_id=%s AND u.is_active IS TRUE "
                        "AND t.status IN ('succeeded','failed','cancelled') AND t.finished_at < %s "
                        "AND NOT EXISTS (SELECT 1 FROM audit_events AS a WHERE a.user_id=t.user_id "
                        "AND a.event_type=%s)",
                        (task_id, user_id, cutoff, PRIVACY_DELETION_STARTED_EVENT_TYPE),
                    )
                    task = await cursor.fetchone()
                    if task is None or task["graph_thread_id"] not in (None, str(task_id)):
                        raise deletion_unavailable()
                await saver.adelete_thread(str(task_id))
