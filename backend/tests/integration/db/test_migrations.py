"""验证 Alembic 能从空数据库升级到当前任务持久化 Schema 且无元数据漂移。"""

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ai_employee.agents.runner import postgres_checkpointer
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


def _task_unique_constraint_names(database_url: URL) -> set[str]:
    """读取 Task 6 明确要求的幂等与顺序唯一约束名称。"""

    async def read_names() -> set[str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT c.conname "
                        "FROM pg_catalog.pg_constraint AS c "
                        "JOIN pg_catalog.pg_class AS t ON t.oid = c.conrelid "
                        "WHERE t.relname IN ("
                        "'task_runs', 'task_steps', 'tool_executions', 'outbox_events'"
                        ") AND c.contype = 'u'"
                    )
                )
                return {row[0] for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_names())


def _audit_index_names(database_url: URL) -> set[str]:
    """读取审计表索引，确认按任务追加重放路径已经落到迁移。"""

    async def read_names() -> set[str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT indexname FROM pg_catalog.pg_indexes "
                        "WHERE schemaname = 'public' AND tablename = 'audit_events'"
                    )
                )
                return {row[0] for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_names())


def _outbox_relay_index_definition(
    database_url: URL,
) -> tuple[tuple[str, ...], str | None] | None:
    """读取 Outbox relay 部分索引的有序列与 PostgreSQL 谓词。"""

    async def read_definition() -> tuple[tuple[str, ...], str | None] | None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT ARRAY("
                            "SELECT attribute.attname "
                            "FROM unnest(index_info.indkey) WITH ORDINALITY "
                            "AS index_key(attnum, position) "
                            "JOIN pg_catalog.pg_attribute AS attribute "
                            "ON attribute.attrelid = index_info.indrelid "
                            "AND attribute.attnum = index_key.attnum "
                            "ORDER BY index_key.position"
                            ") AS column_names, "
                            "pg_catalog.pg_get_expr(index_info.indpred, index_info.indrelid) "
                            "AS predicate "
                            "FROM pg_catalog.pg_index AS index_info "
                            "JOIN pg_catalog.pg_class AS index_class "
                            "ON index_class.oid = index_info.indexrelid "
                            "JOIN pg_catalog.pg_class AS table_class "
                            "ON table_class.oid = index_info.indrelid "
                            "JOIN pg_catalog.pg_namespace AS namespace "
                            "ON namespace.oid = table_class.relnamespace "
                            "WHERE namespace.nspname = 'public' "
                            "AND table_class.relname = 'outbox_events' "
                            "AND index_class.relname = "
                            "'ix_outbox_events_unpublished_available_at_id'"
                        )
                    )
                ).one_or_none()
                if row is None:
                    return None
                column_names = tuple(str(name) for name in row[0])
                predicate = row[1] if isinstance(row[1], str) else None
                return column_names, predicate
        finally:
            await engine.dispose()

    return asyncio.run(read_definition())


def _task_ownership_guard_modes(database_url: URL) -> dict[str, tuple[bool, bool]]:
    """读取组合归属外键是否可延迟且默认延迟到事务结束。"""

    async def read_modes() -> dict[str, tuple[bool, bool]]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT conname, condeferrable, condeferred "
                        "FROM pg_catalog.pg_constraint "
                        "WHERE conname IN ("
                        "'fk_task_runs_retry_of_task_id_user_id', "
                        "'fk_audit_events_task_id_user_id', "
                        "'fk_approval_requests_step_id_task_id', "
                        "'fk_tool_executions_step_id_task_id'"
                        ")"
                    )
                )
                return {row[0]: (row[1], row[2]) for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_modes())


def _m2_constraint_columns(database_url: URL) -> dict[str, tuple[str, ...]]:
    """读取 M2 连接、游标与用户设置约束的精确有序列集合。"""

    async def read_columns() -> dict[str, tuple[str, ...]]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT constraint_info.conname, "
                        "ARRAY_AGG(attribute.attname ORDER BY key_info.position) "
                        "FROM pg_catalog.pg_constraint AS constraint_info "
                        "JOIN pg_catalog.pg_class AS table_info "
                        "ON table_info.oid = constraint_info.conrelid "
                        "JOIN unnest(constraint_info.conkey) WITH ORDINALITY "
                        "AS key_info(attnum, position) ON TRUE "
                        "JOIN pg_catalog.pg_attribute AS attribute "
                        "ON attribute.attrelid = table_info.oid "
                        "AND attribute.attnum = key_info.attnum "
                        "WHERE constraint_info.conname IN ("
                        "'uq_oauth_connections_id_user_id', "
                        "'uq_sync_cursors_connection_resource', "
                        "'uq_sync_cursors_connection_resource_scope', "
                        "'uq_connection_capabilities_user_connection_capability', "
                        "'uq_provider_calendars_connection_provider_calendar', "
                        "'fk_users_default_mail_connection_id_user_id', "
                        "'fk_users_default_calendar_connection_id_user_id'"
                        ") GROUP BY constraint_info.conname"
                    )
                )
                return {row[0]: tuple(str(column_name) for column_name in row[1]) for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_columns())


def _m2_ownership_guard_modes(database_url: URL) -> dict[str, tuple[bool, bool]]:
    """读取 M2 组合归属外键是否可延迟且默认延迟。"""

    async def read_modes() -> dict[str, tuple[bool, bool]]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT conname, condeferrable, condeferred "
                        "FROM pg_catalog.pg_constraint "
                        "WHERE conname IN ("
                        "'fk_connection_capabilities_connection_user', "
                        "'fk_provider_calendars_connection_user', "
                        "'fk_users_default_mail_connection_id_user_id', "
                        "'fk_users_default_calendar_connection_id_user_id'"
                        ")"
                    )
                )
                return {row[0]: (row[1], row[2]) for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_modes())


def _m2_owned_user_columns(database_url: URL) -> dict[str, str]:
    """读取两个新增用户域表 ``user_id`` 的数据库可空元数据。"""

    async def read_nullability() -> dict[str, str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT table_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND column_name = 'user_id' "
                        "AND table_name IN ('connection_capabilities', 'provider_calendars')"
                    )
                )
                return {row[0]: row[1] for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_nullability())


def _m2_user_check_constraint_names(database_url: URL) -> set[str]:
    """读取 M2 用户工作设置的有界数值检查约束。"""

    async def read_names() -> set[str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT conname FROM pg_catalog.pg_constraint "
                        "WHERE conrelid = 'users'::regclass AND contype = 'c'"
                    )
                )
                return {row[0] for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_names())


def _alembic_revisions(database_url: URL) -> set[str]:
    """读取临时库当前 Alembic revision，证明空库实际升级到 M2 head。"""

    async def read_revisions() -> set[str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT version_num FROM alembic_version"))
                return {row[0] for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_revisions())


@pytest.mark.filterwarnings("error:Cannot correctly sort tables")
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
        "approval_requests",
        "audit_events",
        "calendar_events",
        "checkpoint_blobs",
        "checkpoint_migrations",
        "checkpoint_writes",
        "checkpoints",
        "connection_capabilities",
        "conversations",
        "daily_brief_items",
        "daily_briefs",
        "email_analyses",
        "email_messages",
        "email_threads",
        "encrypted_credentials",
        "llm_invocations",
        "messages",
        "oauth_attempts",
        "oauth_connections",
        "outbox_events",
        "provider_calendars",
        "sync_cursors",
        "task_runs",
        "task_steps",
        "tool_executions",
        "users",
        "user_sessions",
    }
    assert _alembic_revisions(empty_migration_database) == {"20260806_0011"}
    assert _check_constraint_names(empty_migration_database) == {
        "ck_user_sessions_token_hash_octet_length_32",
        "ck_user_sessions_csrf_hash_octet_length_32",
    }
    assert _task_unique_constraint_names(empty_migration_database) == {
        "uq_outbox_events_deduplication_key",
        "uq_task_runs_id_user_id",
        "uq_task_runs_user_id_idempotency_key",
        "uq_task_steps_id_task_id",
        "uq_task_steps_task_id_sequence",
        "uq_tool_executions_idempotency_key",
    }
    assert "ix_audit_events_task_id_id" in _audit_index_names(empty_migration_database)
    assert _outbox_relay_index_definition(empty_migration_database) == (
        ("available_at", "id"),
        "(published_at IS NULL)",
    )
    assert _task_ownership_guard_modes(empty_migration_database) == {
        "fk_task_runs_retry_of_task_id_user_id": (True, True),
        "fk_audit_events_task_id_user_id": (True, True),
        "fk_approval_requests_step_id_task_id": (True, True),
        "fk_tool_executions_step_id_task_id": (True, True),
    }
    assert _m2_constraint_columns(empty_migration_database) == {
        "fk_users_default_calendar_connection_id_user_id": (
            "default_calendar_connection_id",
            "id",
        ),
        "fk_users_default_mail_connection_id_user_id": (
            "default_mail_connection_id",
            "id",
        ),
        "uq_connection_capabilities_user_connection_capability": (
            "user_id",
            "connection_id",
            "capability",
        ),
        "uq_oauth_connections_id_user_id": ("id", "user_id"),
        "uq_provider_calendars_connection_provider_calendar": (
            "connection_id",
            "provider_calendar_id",
        ),
        # 旧约束名只作为现有 M1 ON CONFLICT SQL 的三列兼容入口；此断言防止它退回
        # 会阻断多 scope 的两列约束。
        "uq_sync_cursors_connection_resource": (
            "connection_id",
            "resource_kind",
            "scope_key",
        ),
        "uq_sync_cursors_connection_resource_scope": (
            "connection_id",
            "resource_kind",
            "scope_key",
        ),
    }
    assert _m2_ownership_guard_modes(empty_migration_database) == {
        "fk_connection_capabilities_connection_user": (True, True),
        "fk_provider_calendars_connection_user": (True, True),
        "fk_users_default_calendar_connection_id_user_id": (True, True),
        "fk_users_default_mail_connection_id_user_id": (True, True),
    }
    assert _m2_owned_user_columns(empty_migration_database) == {
        "connection_capabilities": "NO",
        "provider_calendars": "NO",
    }
    assert "ck_users_meeting_buffer_minutes" in _m2_user_check_constraint_names(
        empty_migration_database
    )
    command.check(alembic_config)


def test_checkpoint_migration_matches_langgraph_setup_contract(
    empty_migration_database: URL,
) -> None:
    """Alembic 创建的 checkpoint Schema 必须让 LangGraph setup 成为空操作。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    rendered_url = empty_migration_database.render_as_string(hide_password=False)
    set_alembic_database_url(alembic_config, rendered_url)
    command.upgrade(alembic_config, "head")

    async def read_contract() -> tuple[list[int], set[str]]:
        """读取供应商迁移版本与规范索引名。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                versions = list(
                    (
                        await connection.execute(
                            text("SELECT v FROM checkpoint_migrations ORDER BY v")
                        )
                    ).scalars()
                )
                indexes = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT indexname FROM pg_catalog.pg_indexes "
                                "WHERE tablename IN ('checkpoints', 'checkpoint_blobs', "
                                "'checkpoint_writes')"
                            )
                        )
                    ).scalars()
                )
                return versions, indexes
        finally:
            await engine.dispose()

    before_versions, before_indexes = asyncio.run(read_contract())

    async def run_setup() -> None:
        """运行供应商 setup；正确迁移时它不应再应用任何 DDL 迁移。"""
        async with postgres_checkpointer(rendered_url) as saver:
            await saver.setup()

    asyncio.run(run_setup())
    versions, indexes = asyncio.run(read_contract())
    assert before_versions == list(range(10))
    assert before_indexes == {
        "checkpoints_pkey",
        "checkpoint_blobs_pkey",
        "checkpoint_writes_pkey",
        "checkpoints_thread_id_idx",
        "checkpoint_blobs_thread_id_idx",
        "checkpoint_writes_thread_id_idx",
    }
    assert versions == list(range(10))
    assert {
        "checkpoints_thread_id_idx",
        "checkpoint_blobs_thread_id_idx",
        "checkpoint_writes_thread_id_idx",
    }.issubset(indexes)
