"""验证 Alembic 能从空数据库升级到当前任务持久化 Schema 且无元数据漂移。"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.mail import MailMessage, MailMessageUpsertResult
from ai_employee.infrastructure.db.alembic import set_alembic_database_url
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyMailSyncRepository


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
                        "'fk_oauth_attempts_target_connection_user', "
                        "'fk_email_threads_connection_user', "
                        "'fk_email_messages_connection_user', "
                        "'fk_email_messages_thread_connection_user', "
                        "'uq_oauth_connections_id_user_id', "
                        "'uq_email_threads_id_connection_user', "
                        "'uq_email_messages_connection_provider_message', "
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
                        "'fk_oauth_attempts_target_connection_user', "
                        "'fk_email_threads_connection_user', "
                        "'fk_connection_capabilities_connection_user', "
                        "'fk_email_messages_connection_user', "
                        "'fk_email_messages_thread_connection_user', "
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


def _email_message_identity_metadata(
    database_url: URL,
) -> tuple[tuple[str, str] | None, set[str]]:
    """读取邮件直接连接列及其唯一约束，锁定连接级 ImmutableId Schema。"""

    async def read_metadata() -> tuple[tuple[str, str] | None, set[str]]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                column = (
                    await connection.execute(
                        text(
                            "SELECT data_type, is_nullable FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'email_messages' "
                            "AND column_name = 'connection_id'"
                        )
                    )
                ).one_or_none()
                constraints = await connection.execute(
                    text(
                        "SELECT conname FROM pg_catalog.pg_constraint "
                        "WHERE conrelid = 'email_messages'::regclass AND contype = 'u'"
                    )
                )
                return (
                    (str(column[0]), str(column[1])) if column is not None else None,
                    {str(row[0]) for row in constraints},
                )
        finally:
            await engine.dispose()

    return asyncio.run(read_metadata())


def _email_identity_column_metadata(
    database_url: URL,
) -> dict[str, tuple[str, str]]:
    """读取邮件连接身份扩展列的数据类型与可空性。"""

    async def read_metadata() -> dict[str, tuple[str, str]]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'email_messages' "
                        "AND column_name IN ('connection_id', 'provider_updated_at') "
                        "ORDER BY column_name"
                    )
                )
                return {
                    str(row[0]): (str(row[1]), str(row[2]))
                    for row in result
                }
        finally:
            await engine.dispose()

    return asyncio.run(read_metadata())


def _email_identity_foreign_key_metadata(
    database_url: URL,
) -> dict[str, tuple[bool, bool, bool]]:
    """读取邮件身份组合外键的验证、可延迟与默认延迟状态。"""

    async def read_metadata() -> dict[str, tuple[bool, bool, bool]]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT conname, convalidated, condeferrable, condeferred "
                        "FROM pg_catalog.pg_constraint "
                        "WHERE conname IN ("
                        "'fk_email_threads_connection_user', "
                        "'fk_email_messages_connection_user', "
                        "'fk_email_messages_thread_connection_user'"
                        ")"
                    )
                )
                return {
                    str(row[0]): (bool(row[1]), bool(row[2]), bool(row[3]))
                    for row in result
                }
        finally:
            await engine.dispose()

    return asyncio.run(read_metadata())


def _email_identity_index_metadata(
    database_url: URL,
) -> dict[
    str,
    tuple[bool, bool, bool, bool, int, int, tuple[str | None, ...]],
]:
    """读取 0017 固定索引的完整键形状，不能静默丢弃表达式或 INCLUDE 项。

    返回值依次包含 valid、unique、无 predicate、无 expression、key attribute 数、
    总 attribute 数和完整 ``indkey`` 位置。表达式在 ``indkey`` 中以 ``attnum=0``
    表示，因此用 ``None`` 保留其原始位置；若改用 INNER JOIN，测试 helper 本身会复现
    生产迁移的盲区，无法证明 wrong-shape 对象确实未被替换。
    """

    async def read_metadata() -> dict[
        str,
        tuple[bool, bool, bool, bool, int, int, tuple[str | None, ...]],
    ]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT index_class.relname, index_info.indisvalid, "
                        "index_info.indisunique, index_info.indpred IS NULL, "
                        "index_info.indexprs IS NULL, index_info.indnkeyatts, "
                        "index_info.indnatts, ARRAY("
                        "SELECT CASE WHEN index_key.attnum = 0 THEN NULL "
                        "ELSE attribute.attname::text END "
                        "FROM unnest(index_info.indkey) WITH ORDINALITY "
                        "AS index_key(attnum, position) "
                        "LEFT JOIN pg_catalog.pg_attribute AS attribute "
                        "ON attribute.attrelid = index_info.indrelid "
                        "AND attribute.attnum = index_key.attnum "
                        "ORDER BY index_key.position"
                        ") FROM pg_catalog.pg_index AS index_info "
                        "JOIN pg_catalog.pg_class AS index_class "
                        "ON index_class.oid = index_info.indexrelid "
                        "JOIN pg_catalog.pg_class AS table_class "
                        "ON table_class.oid = index_info.indrelid "
                        "JOIN pg_catalog.pg_namespace AS namespace "
                        "ON namespace.oid = table_class.relnamespace "
                        "WHERE namespace.nspname = 'public' AND index_class.relname IN ("
                        "'uq_email_messages_connection_provider_message', "
                        "'uq_email_threads_id_connection_user'"
                        ")"
                    )
                )
                return {
                    str(row[0]): (
                        bool(row[1]),
                        bool(row[2]),
                        bool(row[3]),
                        bool(row[4]),
                        int(row[5]),
                        int(row[6]),
                        tuple(str(value) if value is not None else None for value in row[7]),
                    )
                    for row in result
                }
        finally:
            await engine.dispose()

    return asyncio.run(read_metadata())


def _email_message_count(database_url: URL) -> int:
    """读取邮件迁移前后业务行数量，降级不得通过删行恢复约束。"""

    async def read_count() -> int:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return int(
                    await connection.scalar(text("SELECT count(*) FROM email_messages")) or 0
                )
        finally:
            await engine.dispose()

    return asyncio.run(read_count())


def _oauth_attempt_binding_columns(
    database_url: URL,
) -> dict[tuple[str, str], tuple[str, str, str | None]]:
    """读取 OAuth attempt 目标绑定与连接授权代际列的类型、可空性和默认值。"""

    async def read_columns() -> dict[tuple[str, str], tuple[str, str, str | None]]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT table_name, column_name, data_type, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND ("
                        "(table_name = 'oauth_attempts' AND column_name IN ("
                        "'target_connection_id', 'target_authorization_generation', "
                        "'invalidated_at')) OR "
                        "(table_name = 'oauth_connections' "
                        "AND column_name = 'authorization_generation'))"
                    )
                )
                return {
                    (row[0], row[1]): (
                        str(row[2]),
                        str(row[3]),
                        row[4] if isinstance(row[4], str) else None,
                    )
                    for row in result
                }
        finally:
            await engine.dispose()

    return asyncio.run(read_columns())


def _oauth_attempt_binding_check_names(database_url: URL) -> set[str]:
    """读取 OAuth 目标/代际配对与非负约束，防止半绑定持久事实。"""

    async def read_names() -> set[str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT conname FROM pg_catalog.pg_constraint "
                        "WHERE conname IN ("
                        "'ck_oauth_attempts_target_generation_pair', "
                        "'ck_oauth_connections_authorization_generation_nonnegative')"
                    )
                )
                return {str(row[0]) for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_names())


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


def _sync_cursor_rows(
    database_url: URL,
) -> tuple[tuple[str, str, str | None], ...]:
    """读取迁移保真测试所需的资源种类、scope 与 opaque cursor。"""

    async def read_rows() -> tuple[tuple[str, str, str | None], ...]:
        """按固定 ID 排序读取，避免用 cursor 内容参与测试排序或日志。"""
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text("SELECT resource_kind, scope_key, cursor FROM sync_cursors ORDER BY id")
                )
                return tuple((row[0], row[1], row[2]) for row in rows)
        finally:
            await engine.dispose()

    return asyncio.run(read_rows())


def _seed_provider_neutral_cursor_migration_rows(database_url: URL) -> None:
    """在 0012 Schema 写入合成连接及三种游标，供 0013 前后逐行比较。"""

    async def seed_rows() -> None:
        """只向当前测试生成的临时库写入固定合成事实。"""
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users ("
                        "id, email, display_name, password_hash, timezone, locale, "
                        "brief_time, is_active, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000101', "
                        "'cursor-owner@example.test', 'Cursor Owner', NULL, 'UTC', "
                        "'zh-CN', '08:00:00', true, now(), now()"
                        ")"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO oauth_connections ("
                        "id, user_id, provider, provider_account_id, account_email, scopes, "
                        "status, last_error_code, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000102', "
                        "'00000000-0000-0000-0000-000000000101', 'google', "
                        "'synthetic-cursor-account', 'cursor-owner@example.test', "
                        "'[]'::jsonb, 'connected', NULL, now(), now()"
                        ")"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO sync_cursors ("
                        "id, connection_id, resource_kind, scope_key, cursor"
                        ") VALUES "
                        "('00000000-0000-0000-0000-000000000111', "
                        "'00000000-0000-0000-0000-000000000102', "
                        "'gmail', 'mailbox', 'synthetic-gmail-cursor'), "
                        "('00000000-0000-0000-0000-000000000112', "
                        "'00000000-0000-0000-0000-000000000102', "
                        "'calendar', 'primary', 'synthetic-calendar-cursor'), "
                        "('00000000-0000-0000-0000-000000000113', "
                        "'00000000-0000-0000-0000-000000000102', "
                        "'directory', 'visible', 'synthetic-directory-cursor')"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_rows())


def _seed_pre_identity_mail_rows(database_url: URL, *, duplicate: bool) -> None:
    """在 0015 Schema 写入可回填邮件；可选制造旧约束允许的连接级重复。"""

    async def seed_rows() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users ("
                        "id, email, display_name, password_hash, timezone, locale, "
                        "brief_time, is_active, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000201', "
                        "'mail-identity-owner@example.test', 'Mail Identity Owner', NULL, "
                        "'UTC', 'zh-CN', '08:00:00', true, now(), now()"
                        ")"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO oauth_connections ("
                        "id, user_id, provider, provider_account_id, account_email, scopes, "
                        "status, last_error_code, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000202', "
                        "'00000000-0000-0000-0000-000000000201', 'google', "
                        "'synthetic-mail-identity-account', "
                        "'mail-identity-owner@example.test', '[]'::jsonb, "
                        "'connected', NULL, now(), now()"
                        ")"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO email_threads ("
                        "id, user_id, connection_id, provider_thread_id, subject, "
                        "participants, latest_message_at, provider_url, created_at, updated_at"
                        ") VALUES "
                        "('00000000-0000-0000-0000-000000000211', "
                        "'00000000-0000-0000-0000-000000000201', "
                        "'00000000-0000-0000-0000-000000000202', "
                        "'synthetic-thread-before-migration', 'Synthetic thread one', "
                        "'[]'::jsonb, now(), 'https://example.test/thread-one', now(), now()), "
                        "('00000000-0000-0000-0000-000000000212', "
                        "'00000000-0000-0000-0000-000000000201', "
                        "'00000000-0000-0000-0000-000000000202', "
                        "'synthetic-thread-duplicate-migration', 'Synthetic thread two', "
                        "'[]'::jsonb, now(), 'https://example.test/thread-two', now(), now())"
                    )
                )
                message_rows = [
                    {
                        "id": "00000000-0000-0000-0000-000000000221",
                        "thread_id": "00000000-0000-0000-0000-000000000211",
                        "scope_key": "mailbox",
                    }
                ]
                if duplicate:
                    message_rows.append(
                        {
                            "id": "00000000-0000-0000-0000-000000000222",
                            "thread_id": "00000000-0000-0000-0000-000000000212",
                            "scope_key": "archive",
                        }
                    )
                for row in message_rows:
                    await connection.execute(
                        text(
                            "INSERT INTO email_messages ("
                            "id, user_id, thread_id, provider_message_id, received_at, "
                            "mailbox_scope_key, sender, recipients, subject, snippet, "
                            "body_ciphertext, body_nonce, body_key_version, labels, headers, "
                            "provider_url, created_at, updated_at"
                            ") VALUES ("
                            ":id, '00000000-0000-0000-0000-000000000201', :thread_id, "
                            "'synthetic-message-before-migration', now(), :scope_key, "
                            "'{}'::jsonb, '[]'::jsonb, 'Synthetic message', '', "
                            "NULL, NULL, NULL, '[]'::jsonb, '{}'::jsonb, "
                            "'https://example.test/message', now(), now()"
                            ")"
                        ),
                        row,
                    )
        finally:
            await engine.dispose()

    asyncio.run(seed_rows())


def _mail_identity_migration_state(database_url: URL) -> tuple[set[str], int, str | None, bool]:
    """读取迁移版本、消息数、回填连接与 direct column 是否存在。"""

    async def read_state() -> tuple[set[str], int, str | None, bool]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                revisions = {
                    str(value)
                    for value in (
                        await connection.execute(text("SELECT version_num FROM alembic_version"))
                    ).scalars()
                }
                message_count = int(
                    await connection.scalar(text("SELECT count(*) FROM email_messages")) or 0
                )
                column_exists = bool(
                    await connection.scalar(
                        text(
                            "SELECT EXISTS ("
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'email_messages' "
                            "AND column_name = 'connection_id'"
                            ")"
                        )
                    )
                )
                connection_id: str | None = None
                if column_exists and message_count == 1:
                    value = await connection.scalar(
                        text("SELECT connection_id FROM email_messages LIMIT 1")
                    )
                    connection_id = str(value) if value is not None else None
                return revisions, message_count, connection_id, column_exists
        finally:
            await engine.dispose()

    return asyncio.run(read_state())


def _upsert_identity_migration_message(
    database_url: URL,
    *,
    provider_message_id: str = "synthetic-message-before-migration",
    provider_thread_id: str,
    provider_updated_at: datetime,
    mailbox_scope_key: str,
) -> MailMessageUpsertResult:
    """在目标迁移 Schema 上运行真实 Repository upsert，而非直接拼 SQL。

    同一个 helper 会依次用于 0016、已完成并发索引但尚未 ``USING INDEX`` 的中间态和
    0017 contract，确保应用代码没有按进程缓存一次性探测结果。消息 ID 固定为迁移前
    已存在的合成 ImmutableId，从而同时覆盖 legacy 窗口中的跨 thread move 保护。
    """

    async def upsert() -> MailMessageUpsertResult:
        engine = create_async_engine(database_url, poolclass=NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions.begin() as session:
                repository = SqlAlchemyMailSyncRepository(session)
                return await repository.upsert_message(
                    user_id=UUID("00000000-0000-0000-0000-000000000201"),
                    connection_id=UUID("00000000-0000-0000-0000-000000000202"),
                    message=MailMessage(
                        provider_message_id=provider_message_id,
                        provider_thread_id=provider_thread_id,
                        provider_conversation_id="synthetic-migration-conversation",
                        internet_message_id="<synthetic-migration@example.test>",
                        mailbox_scope_key=mailbox_scope_key,
                        sender={"name": "Synthetic Sender", "email": "sender@example.test"},
                        recipients=(
                            {"name": "Synthetic Recipient", "email": "recipient@example.test"},
                        ),
                        subject="Synthetic identity migration projection",
                        sanitized_body="Synthetic encrypted body",
                        received_at=datetime(2026, 8, 8, 1, tzinfo=UTC),
                        sent_at=datetime(2026, 8, 8, 0, 59, tzinfo=UTC),
                        provider_updated_at=provider_updated_at,
                        labels=("synthetic",),
                        normalized_reply_headers={
                            "message-id": "<synthetic-migration@example.test>"
                        },
                        provider_url="https://example.test/message/migration",
                    ),
                    encrypted_body=EncryptedValue(
                        ciphertext=b"synthetic-ciphertext",
                        nonce=b"0123456789ab",
                        key_version=1,
                    ),
                )
        finally:
            await engine.dispose()

    return asyncio.run(upsert())


def _identity_message_projection_rows(
    database_url: URL,
    *,
    provider_message_id: str = "synthetic-message-before-migration",
) -> tuple[tuple[str | None, datetime | None, str, str], ...]:
    """读取一个合成 ImmutableId 的 direct connection、版本、thread 与 folder 投影。"""

    async def read_rows() -> tuple[tuple[str | None, datetime | None, str, str], ...]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT message.connection_id, message.provider_updated_at, "
                        "thread.provider_thread_id, message.mailbox_scope_key "
                        "FROM email_messages AS message "
                        "JOIN email_threads AS thread ON thread.id = message.thread_id "
                        "WHERE message.provider_message_id = :provider_message_id "
                        "ORDER BY message.id"
                    ),
                    {"provider_message_id": provider_message_id},
                )
                return tuple(
                    (
                        str(row[0]) if row[0] is not None else None,
                        row[1] if isinstance(row[1], datetime) else None,
                        str(row[2]),
                        str(row[3]),
                    )
                    for row in rows
                )
        finally:
            await engine.dispose()

    return asyncio.run(read_rows())


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
        "calendar_change_proposals",
        "calendar_change_snapshots",
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
        "mail_draft_versions",
        "mail_drafts",
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
    assert _alembic_revisions(empty_migration_database) == {"20260808_0017"}
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
        "uq_tool_executions_operation",
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
        "fk_email_threads_connection_user": (
            "connection_id",
            "user_id",
        ),
        "fk_email_messages_connection_user": (
            "connection_id",
            "user_id",
        ),
        "fk_email_messages_thread_connection_user": (
            "thread_id",
            "connection_id",
            "user_id",
        ),
        "fk_oauth_attempts_target_connection_user": (
            "target_connection_id",
            "user_id",
        ),
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
        "uq_email_messages_connection_provider_message": (
            "connection_id",
            "provider_message_id",
        ),
        "uq_email_threads_id_connection_user": ("id", "connection_id", "user_id"),
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
        "fk_email_threads_connection_user": (True, True),
        "fk_email_messages_connection_user": (True, True),
        "fk_email_messages_thread_connection_user": (True, True),
        "fk_oauth_attempts_target_connection_user": (True, True),
        "fk_connection_capabilities_connection_user": (True, True),
        "fk_provider_calendars_connection_user": (True, True),
        "fk_users_default_calendar_connection_id_user_id": (True, True),
        "fk_users_default_mail_connection_id_user_id": (True, True),
    }
    assert _email_message_identity_metadata(empty_migration_database) == (
        ("uuid", "NO"),
        {"uq_email_messages_connection_provider_message"},
    )
    assert _email_identity_column_metadata(empty_migration_database) == {
        "connection_id": ("uuid", "NO"),
        "provider_updated_at": ("timestamp with time zone", "YES"),
    }
    assert _email_identity_foreign_key_metadata(empty_migration_database) == {
        "fk_email_threads_connection_user": (True, True, True),
        "fk_email_messages_connection_user": (True, True, True),
        "fk_email_messages_thread_connection_user": (True, True, True),
    }
    binding_columns = _oauth_attempt_binding_columns(empty_migration_database)
    assert binding_columns[("oauth_attempts", "target_connection_id")] == ("uuid", "YES", None)
    assert binding_columns[("oauth_attempts", "target_authorization_generation")] == (
        "bigint",
        "YES",
        None,
    )
    assert binding_columns[("oauth_attempts", "invalidated_at")] == (
        "timestamp with time zone",
        "YES",
        None,
    )
    generation_column = binding_columns[("oauth_connections", "authorization_generation")]
    assert generation_column[:2] == ("bigint", "NO")
    assert generation_column[2] is not None and generation_column[2].startswith("0")
    assert _oauth_attempt_binding_check_names(empty_migration_database) == {
        "ck_oauth_attempts_target_generation_pair",
        "ck_oauth_connections_authorization_generation_nonnegative",
    }
    assert _m2_owned_user_columns(empty_migration_database) == {
        "connection_capabilities": "NO",
        "provider_calendars": "NO",
    }
    assert "ck_users_meeting_buffer_minutes" in _m2_user_check_constraint_names(
        empty_migration_database
    )
    command.check(alembic_config)


def test_mail_message_identity_migration_backfills_and_downgrades_without_row_loss(
    empty_migration_database: URL,
) -> None:
    """0016 必须从 thread 安全回填连接，并能无损恢复旧结构。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)

    command.upgrade(alembic_config, "20260808_0016")

    assert _mail_identity_migration_state(empty_migration_database) == (
        {"20260808_0016"},
        1,
        "00000000-0000-0000-0000-000000000202",
        True,
    )
    assert _email_identity_column_metadata(empty_migration_database) == {
        "connection_id": ("uuid", "YES"),
        "provider_updated_at": ("timestamp with time zone", "YES"),
    }
    assert _email_message_identity_metadata(empty_migration_database)[1] == {
        "uq_email_messages_thread_provider_message"
    }
    assert _m2_constraint_columns(empty_migration_database).get(
        "uq_email_messages_connection_provider_message"
    ) is None
    assert _email_identity_foreign_key_metadata(empty_migration_database) == {}

    command.downgrade(alembic_config, "20260808_0015")

    assert _mail_identity_migration_state(empty_migration_database) == (
        {"20260808_0015"},
        1,
        None,
        False,
    )


def test_mail_repository_upsert_spans_0016_index_window_and_0017(
    empty_migration_database: URL,
) -> None:
    """同一双写 Repository 必须覆盖 expand、并发索引中间态与 contract。

    0016 只存在 legacy message identity constraint；并发索引完成但尚未通过
    ``USING INDEX`` 挂载时，新索引已经开始执法；0017 最终只保留连接级约束。三次
    upsert 均移动同一个 ImmutableId 的 thread/folder 投影，任何错误 conflict target
    都会在真实 PostgreSQL 上失败或制造第二行。
    """
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    command.upgrade(alembic_config, "20260808_0016")

    inserted_updated_at = datetime(2026, 8, 8, 1, 30, tzinfo=UTC)
    assert _upsert_identity_migration_message(
        empty_migration_database,
        provider_message_id="synthetic-0016-dual-write-message",
        provider_thread_id="synthetic-thread-dual-write-insert",
        provider_updated_at=inserted_updated_at,
        mailbox_scope_key="synthetic-folder-dual-write",
    ) == MailMessageUpsertResult.APPLIED
    assert _identity_message_projection_rows(
        empty_migration_database,
        provider_message_id="synthetic-0016-dual-write-message",
    ) == (
        (
            "00000000-0000-0000-0000-000000000202",
            inserted_updated_at,
            "synthetic-thread-dual-write-insert",
            "synthetic-folder-dual-write",
        ),
    )

    legacy_updated_at = datetime(2026, 8, 8, 2, tzinfo=UTC)
    assert _upsert_identity_migration_message(
        empty_migration_database,
        provider_thread_id="synthetic-thread-legacy-upsert",
        provider_updated_at=legacy_updated_at,
        mailbox_scope_key="synthetic-folder-legacy",
    ) == MailMessageUpsertResult.APPLIED
    assert _identity_message_projection_rows(empty_migration_database) == (
        (
            "00000000-0000-0000-0000-000000000202",
            legacy_updated_at,
            "synthetic-thread-legacy-upsert",
            "synthetic-folder-legacy",
        ),
    )

    async def create_valid_unattached_index() -> None:
        """复现 0017 已建新索引、尚未进入 ``USING INDEX`` metadata 事务的窗口。"""
        engine = create_async_engine(
            empty_migration_database,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        "uq_email_messages_connection_provider_message "
                        "ON email_messages (connection_id, provider_message_id)"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(create_valid_unattached_index())
    index_updated_at = datetime(2026, 8, 8, 3, tzinfo=UTC)
    assert _upsert_identity_migration_message(
        empty_migration_database,
        provider_thread_id="synthetic-thread-index-upsert",
        provider_updated_at=index_updated_at,
        mailbox_scope_key="synthetic-folder-index",
    ) == MailMessageUpsertResult.APPLIED
    assert _identity_message_projection_rows(empty_migration_database) == (
        (
            "00000000-0000-0000-0000-000000000202",
            index_updated_at,
            "synthetic-thread-index-upsert",
            "synthetic-folder-index",
        ),
    )

    command.upgrade(alembic_config, "20260808_0017")
    contract_updated_at = datetime(2026, 8, 8, 4, tzinfo=UTC)
    assert _upsert_identity_migration_message(
        empty_migration_database,
        provider_thread_id="synthetic-thread-contract-upsert",
        provider_updated_at=contract_updated_at,
        mailbox_scope_key="synthetic-folder-contract",
    ) == MailMessageUpsertResult.APPLIED
    assert _identity_message_projection_rows(empty_migration_database) == (
        (
            "00000000-0000-0000-0000-000000000202",
            contract_updated_at,
            "synthetic-thread-contract-upsert",
            "synthetic-folder-contract",
        ),
    )


def test_mail_message_identity_contract_catches_up_0016_window_null_rows(
    empty_migration_database: URL,
) -> None:
    """0017 必须追赶 0016 后旧实例写入的 NULL，再执行 NOT NULL contract。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    command.upgrade(alembic_config, "20260808_0016")

    async def insert_old_application_row() -> None:
        """模拟尚未排空的旧实例按 legacy 列集合写入第二条合成消息。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO email_messages ("
                        "id, user_id, thread_id, provider_message_id, received_at, "
                        "mailbox_scope_key, sender, recipients, subject, snippet, "
                        "body_ciphertext, body_nonce, body_key_version, labels, headers, "
                        "provider_url, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000225', "
                        "'00000000-0000-0000-0000-000000000201', "
                        "'00000000-0000-0000-0000-000000000212', "
                        "'synthetic-0016-window-message', now(), 'synthetic-window-folder', "
                        "'{}'::jsonb, '[]'::jsonb, 'Synthetic window message', '', "
                        "NULL, NULL, NULL, '[]'::jsonb, '{}'::jsonb, "
                        "'https://example.test/message/window', now(), now())"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(insert_old_application_row())
    command.upgrade(alembic_config, "20260808_0017")

    async def read_caught_up_connection() -> tuple[str | None, int]:
        """读取旧式行的回填结果与剩余 NULL 数，证明 contract 前追赶完成。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(
                    text(
                        "SELECT connection_id FROM email_messages "
                        "WHERE id = '00000000-0000-0000-0000-000000000225'"
                    )
                )
                null_count = int(
                    await connection.scalar(
                        text("SELECT count(*) FROM email_messages WHERE connection_id IS NULL")
                    )
                    or 0
                )
                return (str(value) if value is not None else None, null_count)
        finally:
            await engine.dispose()

    assert asyncio.run(read_caught_up_connection()) == (
        "00000000-0000-0000-0000-000000000202",
        0,
    )
    assert _email_identity_column_metadata(empty_migration_database)["connection_id"] == (
        "uuid",
        "NO",
    )


def test_mail_message_identity_migration_fails_closed_on_historical_duplicates(
    empty_migration_database: URL,
) -> None:
    """旧 Schema 已有连接级重复时不得任意删除或选择一条完成升级。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=True)

    with pytest.raises(RuntimeError, match="connection-level duplicate"):
        command.upgrade(alembic_config, "20260808_0016")

    assert _mail_identity_migration_state(empty_migration_database) == (
        {"20260808_0015"},
        2,
        None,
        True,
    )
    assert _email_identity_column_metadata(empty_migration_database) == {
        "connection_id": ("uuid", "YES"),
        "provider_updated_at": ("timestamp with time zone", "YES"),
    }

    async def repair_duplicate() -> None:
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE email_messages "
                        "SET provider_message_id = 'synthetic-message-after-manual-repair' "
                        "WHERE id = '00000000-0000-0000-0000-000000000222'"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(repair_duplicate())
    command.upgrade(alembic_config, "20260808_0016")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0016"}
    assert _email_message_count(empty_migration_database) == 2


def _seed_second_mail_identity_connection(database_url: URL) -> None:
    """为连接级身份测试追加第二个用户/连接及相同供应商消息 ID。"""

    async def seed_rows() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users ("
                        "id, email, display_name, password_hash, timezone, locale, "
                        "brief_time, is_active, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000203', "
                        "'mail-identity-owner-two@example.test', 'Mail Identity Owner Two', NULL, "
                        "'UTC', 'zh-CN', '08:00:00', true, now(), now())"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO oauth_connections ("
                        "id, user_id, provider, provider_account_id, account_email, scopes, "
                        "status, last_error_code, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000204', "
                        "'00000000-0000-0000-0000-000000000203', 'google', "
                        "'synthetic-mail-identity-account-two', "
                        "'mail-identity-owner-two@example.test', '[]'::jsonb, "
                        "'connected', NULL, now(), now())"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO email_threads ("
                        "id, user_id, connection_id, provider_thread_id, subject, "
                        "participants, latest_message_at, provider_url, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000213', "
                        "'00000000-0000-0000-0000-000000000203', "
                        "'00000000-0000-0000-0000-000000000204', "
                        "'synthetic-thread-before-migration-two', 'Synthetic thread two', "
                        "'[]'::jsonb, now(), 'https://example.test/thread-two', now(), now())"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO email_messages ("
                        "id, user_id, thread_id, provider_message_id, received_at, "
                        "mailbox_scope_key, sender, recipients, subject, snippet, "
                        "body_ciphertext, body_nonce, body_key_version, labels, headers, "
                        "provider_url, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000223', "
                        "'00000000-0000-0000-0000-000000000203', "
                        "'00000000-0000-0000-0000-000000000213', "
                        "'synthetic-message-before-migration', now(), 'archive', "
                        "'{}'::jsonb, '[]'::jsonb, 'Synthetic message two', '', "
                        "NULL, NULL, NULL, '[]'::jsonb, '{}'::jsonb, "
                        "'https://example.test/message-two', now(), now())"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_rows())


def test_mail_message_identity_migration_fails_closed_on_ownership_mismatch(
    empty_migration_database: URL,
) -> None:
    """0016 不得把跨用户 message/thread 错配固化为 direct connection 事实。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    _seed_second_mail_identity_connection(empty_migration_database)

    async def corrupt_message_owner() -> None:
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE email_messages "
                        "SET user_id = '00000000-0000-0000-0000-000000000203' "
                        "WHERE id = '00000000-0000-0000-0000-000000000221'"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(corrupt_message_owner())
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        command.upgrade(alembic_config, "20260808_0016")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0015"}
    assert _email_message_count(empty_migration_database) == 2
    assert _email_identity_column_metadata(empty_migration_database) == {
        "connection_id": ("uuid", "YES"),
        "provider_updated_at": ("timestamp with time zone", "YES"),
    }


def test_mail_message_identity_contract_is_connection_scoped_and_ownership_safe(
    empty_migration_database: URL,
) -> None:
    """0017 切换连接级 ImmutableId，并以组合外键拒绝跨 thread/connection 伪造。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    _seed_second_mail_identity_connection(empty_migration_database)

    command.upgrade(alembic_config, "20260808_0016")
    command.upgrade(alembic_config, "20260808_0017")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0017"}
    assert _email_message_identity_metadata(empty_migration_database)[1] == {
        "uq_email_messages_connection_provider_message"
    }
    assert _email_identity_column_metadata(empty_migration_database)["connection_id"] == (
        "uuid",
        "NO",
    )
    assert _email_identity_foreign_key_metadata(empty_migration_database) == {
        "fk_email_threads_connection_user": (True, True, True),
        "fk_email_messages_connection_user": (True, True, True),
        "fk_email_messages_thread_connection_user": (True, True, True),
    }

    async def read_count() -> int:
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return int(
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM email_messages "
                            "WHERE provider_message_id = 'synthetic-message-before-migration'"
                        )
                    )
                    or 0
                )
        finally:
            await engine.dispose()

    assert asyncio.run(read_count()) == 2

    async def insert_cross_connection_thread() -> None:
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                # direct connection/user 与 thread ID 分别有效，只有三列组合 FK 能拒绝该错配。
                await connection.execute(
                    text(
                        "INSERT INTO email_messages ("
                        "id, user_id, connection_id, thread_id, provider_message_id, "
                        "received_at, mailbox_scope_key, sender, recipients, subject, snippet, "
                        "body_ciphertext, body_nonce, body_key_version, labels, headers, "
                        "provider_url, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000224', "
                        "'00000000-0000-0000-0000-000000000201', "
                        "'00000000-0000-0000-0000-000000000202', "
                        "'00000000-0000-0000-0000-000000000213', "
                        "'synthetic-cross-connection-message', now(), 'mailbox', "
                        "'{}'::jsonb, '[]'::jsonb, 'Synthetic mismatch', '', "
                        "NULL, NULL, NULL, '[]'::jsonb, '{}'::jsonb, "
                        "'https://example.test/message-mismatch', now(), now())"
                    )
                )
        finally:
            await engine.dispose()

    with pytest.raises(IntegrityError):
        asyncio.run(insert_cross_connection_thread())


def test_mail_message_identity_contract_upgrades_legacy_0016_shape(
    empty_migration_database: URL,
) -> None:
    """已应用旧版 0016 contract 时，0017 只补缺失对象并保持业务行。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    command.upgrade(alembic_config, "20260808_0016")

    async def emulate_legacy_contract() -> None:
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE email_messages ALTER COLUMN connection_id SET NOT NULL"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE email_messages ADD CONSTRAINT "
                        "fk_email_messages_connection_user FOREIGN KEY (connection_id, user_id) "
                        "REFERENCES oauth_connections (id, user_id) ON DELETE CASCADE "
                        "DEFERRABLE INITIALLY DEFERRED"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE email_messages ADD CONSTRAINT "
                        "uq_email_messages_connection_provider_message "
                        "UNIQUE (connection_id, provider_message_id)"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE email_messages DROP CONSTRAINT "
                        "uq_email_messages_thread_provider_message"
                    )
                )
                await connection.execute(
                    text("ALTER TABLE email_messages DROP COLUMN provider_updated_at")
                )
        finally:
            await engine.dispose()

    asyncio.run(emulate_legacy_contract())
    command.upgrade(alembic_config, "20260808_0017")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0017"}
    assert _email_message_count(empty_migration_database) == 1
    assert _email_identity_column_metadata(empty_migration_database) == {
        "connection_id": ("uuid", "NO"),
        "provider_updated_at": ("timestamp with time zone", "YES"),
    }
    assert _email_identity_foreign_key_metadata(empty_migration_database) == {
        "fk_email_threads_connection_user": (True, True, True),
        "fk_email_messages_connection_user": (True, True, True),
        "fk_email_messages_thread_connection_user": (True, True, True),
    }


def test_mail_message_identity_contract_rejects_wrong_legacy_foreign_key(
    empty_migration_database: URL,
) -> None:
    """同名但列、目标、级联或延迟语义错误的旧约束必须 fail closed。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    command.upgrade(alembic_config, "20260808_0016")

    async def add_wrong_foreign_key() -> None:
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE email_messages ADD CONSTRAINT "
                        "fk_email_messages_connection_user FOREIGN KEY (user_id) "
                        "REFERENCES users (id) ON DELETE RESTRICT"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(add_wrong_foreign_key())
    with pytest.raises(RuntimeError, match="unexpected definition"):
        command.upgrade(alembic_config, "20260808_0017")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0016"}
    assert _email_message_count(empty_migration_database) == 1


def test_mail_message_identity_contract_downgrades_without_row_loss(
    empty_migration_database: URL,
) -> None:
    """0017→0016→0015 只撤销对应 Schema，不删除连接级邮件事实。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    _seed_second_mail_identity_connection(empty_migration_database)
    command.upgrade(alembic_config, "20260808_0017")

    assert _email_message_count(empty_migration_database) == 2
    command.downgrade(alembic_config, "20260808_0016")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0016"}
    assert _email_message_count(empty_migration_database) == 2
    assert _email_identity_column_metadata(empty_migration_database) == {
        "connection_id": ("uuid", "YES"),
        "provider_updated_at": ("timestamp with time zone", "YES"),
    }
    assert _email_message_identity_metadata(empty_migration_database)[1] == {
        "uq_email_messages_thread_provider_message"
    }
    assert _email_identity_foreign_key_metadata(empty_migration_database) == {}

    command.downgrade(alembic_config, "20260808_0015")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0015"}
    assert _email_message_count(empty_migration_database) == 2
    assert _email_identity_column_metadata(empty_migration_database) == {}


def test_mail_message_identity_contract_preserves_wrong_shape_invalid_index(
    empty_migration_database: URL,
) -> None:
    """同名 invalid 索引若不是精确目标形状，0017 必须保留对象并 fail closed。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    command.upgrade(alembic_config, "20260808_0016")

    async def seed_wrong_shape_duplicate() -> None:
        """制造相同 connection/folder、不同 provider ID，保持目标 identity 本身合法。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO email_messages ("
                        "id, user_id, connection_id, thread_id, provider_message_id, "
                        "received_at, mailbox_scope_key, sender, recipients, subject, snippet, "
                        "body_ciphertext, body_nonce, body_key_version, labels, headers, "
                        "provider_url, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000226', "
                        "'00000000-0000-0000-0000-000000000201', "
                        "'00000000-0000-0000-0000-000000000202', "
                        "'00000000-0000-0000-0000-000000000212', "
                        "'synthetic-wrong-shape-index-message', now(), 'mailbox', "
                        "'{}'::jsonb, '[]'::jsonb, 'Synthetic wrong shape row', '', "
                        "NULL, NULL, NULL, '[]'::jsonb, '{}'::jsonb, "
                        "'https://example.test/message/wrong-shape', now(), now())"
                    )
                )
        finally:
            await engine.dispose()

    async def create_wrong_shape_invalid_index() -> None:
        """让错误列集合的并发唯一索引失败并留下 ``indisvalid=false`` catalog 行。"""
        engine = create_async_engine(
            empty_migration_database,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        "uq_email_messages_connection_provider_message "
                        "ON email_messages (connection_id, mailbox_scope_key)"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_wrong_shape_duplicate())
    with pytest.raises(IntegrityError):
        asyncio.run(create_wrong_shape_invalid_index())
    expected_index = {
        "uq_email_messages_connection_provider_message": (
            False,
            True,
            True,
            True,
            2,
            2,
            ("connection_id", "mailbox_scope_key"),
        )
    }
    assert _email_identity_index_metadata(empty_migration_database) == expected_index

    error_message: str | None = None
    try:
        command.upgrade(alembic_config, "20260808_0017")
    except RuntimeError as error:
        error_message = str(error)

    assert (
        error_message,
        _alembic_revisions(empty_migration_database),
        _email_identity_index_metadata(empty_migration_database),
    ) == (
        "uq_email_messages_connection_provider_message has an unexpected index definition",
        {"20260808_0016"},
        expected_index,
    )


def test_mail_message_identity_contract_preserves_invalid_expression_index(
    empty_migration_database: URL,
) -> None:
    """额外表达式键即使 invalid 也不是迁移目标，0017 必须保留并 fail closed。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    command.upgrade(alembic_config, "20260808_0016")

    async def seed_duplicate_projection() -> None:
        """先制造连接级 ImmutableId 重复，让三键表达式索引稳定留下 invalid catalog。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO email_messages ("
                        "id, user_id, connection_id, thread_id, provider_message_id, "
                        "received_at, mailbox_scope_key, sender, recipients, subject, snippet, "
                        "body_ciphertext, body_nonce, body_key_version, labels, headers, "
                        "provider_url, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000227', "
                        "'00000000-0000-0000-0000-000000000201', "
                        "'00000000-0000-0000-0000-000000000202', "
                        "'00000000-0000-0000-0000-000000000212', "
                        "'synthetic-message-before-migration', now(), 'expression-index', "
                        "'{}'::jsonb, '[]'::jsonb, 'Synthetic expression duplicate', '', "
                        "NULL, NULL, NULL, '[]'::jsonb, '{}'::jsonb, "
                        "'https://example.test/message/expression-duplicate', now(), now())"
                    )
                )
        finally:
            await engine.dispose()

    async def create_invalid_expression_index() -> None:
        """并发构建带额外 ``lower`` 键的索引，并因前两键重复留下 invalid 对象。"""
        engine = create_async_engine(
            empty_migration_database,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        "uq_email_messages_connection_provider_message "
                        "ON email_messages ("
                        "connection_id, provider_message_id, lower(provider_message_id)"
                        ")"
                    )
                )
        finally:
            await engine.dispose()

    async def repair_duplicate_projection() -> None:
        """修复测试数据重复，使 0017 能进入索引形状检查而非被 preflight 提前拒绝。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE email_messages "
                        "SET provider_message_id = 'synthetic-expression-index-repaired' "
                        "WHERE id = '00000000-0000-0000-0000-000000000227'"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_duplicate_projection())
    with pytest.raises(IntegrityError):
        asyncio.run(create_invalid_expression_index())
    asyncio.run(repair_duplicate_projection())

    expected_index = {
        "uq_email_messages_connection_provider_message": (
            False,
            True,
            True,
            False,
            3,
            3,
            ("connection_id", "provider_message_id", None),
        )
    }
    assert _email_identity_index_metadata(empty_migration_database) == expected_index

    error_message: str | None = None
    try:
        command.upgrade(alembic_config, "20260808_0017")
    except RuntimeError as error:
        error_message = str(error)

    assert (
        error_message,
        _alembic_revisions(empty_migration_database),
        _email_identity_index_metadata(empty_migration_database),
    ) == (
        "uq_email_messages_connection_provider_message has an unexpected index definition",
        {"20260808_0016"},
        expected_index,
    )


def test_mail_message_identity_contract_rebuilds_only_named_invalid_index(
    empty_migration_database: URL,
) -> None:
    """失败的并发唯一索引可在修复数据后按固定名称安全重建。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    command.upgrade(alembic_config, "20260808_0016")

    async def seed_duplicate_projection() -> None:
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO email_messages ("
                        "id, user_id, connection_id, thread_id, provider_message_id, "
                        "received_at, mailbox_scope_key, sender, recipients, subject, snippet, "
                        "body_ciphertext, body_nonce, body_key_version, labels, headers, "
                        "provider_url, created_at, updated_at"
                        ") VALUES ("
                        "'00000000-0000-0000-0000-000000000222', "
                        "'00000000-0000-0000-0000-000000000201', "
                        "'00000000-0000-0000-0000-000000000202', "
                        "'00000000-0000-0000-0000-000000000212', "
                        "'synthetic-message-before-migration', now(), 'archive', "
                        "'{}'::jsonb, '[]'::jsonb, 'Synthetic duplicate projection', '', "
                        "NULL, NULL, NULL, '[]'::jsonb, '{}'::jsonb, "
                        "'https://example.test/message-duplicate', now(), now())"
                    )
                )
        finally:
            await engine.dispose()

    async def create_invalid_index() -> None:
        engine = create_async_engine(
            empty_migration_database,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        "uq_email_messages_connection_provider_message "
                        "ON email_messages (connection_id, provider_message_id)"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_duplicate_projection())
    with pytest.raises(IntegrityError):
        asyncio.run(create_invalid_index())

    assert _email_identity_index_metadata(empty_migration_database) == {
        "uq_email_messages_connection_provider_message": (
            False,
            True,
            True,
            True,
            2,
            2,
            ("connection_id", "provider_message_id"),
        )
    }

    async def repair_duplicate_projection() -> None:
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE email_messages "
                        "SET provider_message_id = 'synthetic-message-after-index-repair' "
                        "WHERE id = '00000000-0000-0000-0000-000000000222'"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(repair_duplicate_projection())
    command.upgrade(alembic_config, "20260808_0017")

    assert _email_identity_index_metadata(empty_migration_database) == {
        "uq_email_messages_connection_provider_message": (
            True,
            True,
            True,
            True,
            2,
            2,
            ("connection_id", "provider_message_id"),
        ),
        "uq_email_threads_id_connection_user": (
            True,
            True,
            True,
            True,
            3,
            3,
            ("id", "connection_id", "user_id"),
        ),
    }


def test_mail_message_identity_contract_attaches_existing_valid_indexes(
    empty_migration_database: URL,
) -> None:
    """0017 重跑应复用已完成但尚未挂载的两个有效并发索引。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    command.upgrade(alembic_config, "20260808_0016")

    async def create_valid_indexes() -> None:
        engine = create_async_engine(
            empty_migration_database,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        "uq_email_messages_connection_provider_message "
                        "ON email_messages (connection_id, provider_message_id)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY uq_email_threads_id_connection_user "
                        "ON email_threads (id, connection_id, user_id)"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(create_valid_indexes())
    command.upgrade(alembic_config, "20260808_0017")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0017"}
    assert _email_message_identity_metadata(empty_migration_database)[1] == {
        "uq_email_messages_connection_provider_message"
    }
    assert _m2_constraint_columns(empty_migration_database)[
        "uq_email_threads_id_connection_user"
    ] == ("id", "connection_id", "user_id")


def test_provider_neutral_cursor_migration_renames_only_gmail_resource_kind(
    empty_migration_database: URL,
) -> None:
    """0013 只重命名 Gmail 资源，逐行保留数量、scope 与 opaque cursor。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    command.upgrade(alembic_config, "20260806_0012")
    _seed_provider_neutral_cursor_migration_rows(empty_migration_database)
    before = _sync_cursor_rows(empty_migration_database)

    command.upgrade(alembic_config, "20260806_0013")
    after = _sync_cursor_rows(empty_migration_database)

    assert len(after) == len(before) == 3
    assert tuple((scope, cursor) for _, scope, cursor in after) == tuple(
        (scope, cursor) for _, scope, cursor in before
    )
    assert before == (
        ("gmail", "mailbox", "synthetic-gmail-cursor"),
        ("calendar", "primary", "synthetic-calendar-cursor"),
        ("directory", "visible", "synthetic-directory-cursor"),
    )
    assert after == (
        ("mail", "mailbox", "synthetic-gmail-cursor"),
        ("calendar", "primary", "synthetic-calendar-cursor"),
        ("directory", "visible", "synthetic-directory-cursor"),
    )


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
