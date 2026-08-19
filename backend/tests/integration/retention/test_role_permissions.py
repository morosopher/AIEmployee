"""以真实 PostgreSQL 角色验证保留与隐私清理的最小权限边界。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ai_employee.domain.errors import InternalInvariantError
from ai_employee.infrastructure.db.alembic import (
    load_published_alembic_authority,
    run_alembic_upgrade_on_connection,
    set_alembic_database_url,
)
from ai_employee.infrastructure.db.database_access import (
    APP_RUNTIME_ROLE_NAME,
    RETENTION_RUNTIME_ROLE_NAME,
    BootstrapCaller,
)
from ai_employee.infrastructure.db.database_grants import (
    GrantPhase,
    ObjectKind,
    read_object_grants,
    verify_object_grants,
)
from ai_employee.infrastructure.db.database_maintenance import (
    SqlAlchemyDatabaseMaintenanceContext,
    role_bootstrap_database,
)
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedDatabaseUrl
from ai_employee.infrastructure.db.database_url import validate_test_database_url
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    EmailMessageModel,
    EmailThreadModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.session import (
    ManagedAsyncSessionMaker,
    build_session_factory,
)
from ai_employee.workers.privacy import PrivacyDeletionWorker
from ai_employee.workers.retention import RetentionCleanupWorker
from tests.integration.alembic_commands import run_alembic_upgrade
from tests.integration.disposable_database import (
    DisposableDatabaseCleanupError,
    managed_disposable_database_cleanup,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_APP_ROLE_PASSWORD = "app-role-integration-password"
_RETENTION_ROLE_PASSWORD = "retention-role-integration-password"

# 0018 中这些表已存在，但 retention 只能在 0019 grant delta 后取得下列权限。标识符是
# 测试代码冻结的常量，不接受环境、fixture 或数据库返回值，后续生成 SQL 时不存在注入输入。
_RETENTION_0019_ONLY_TABLE_PRIVILEGES = (
    ("calendar_change_proposals", "DELETE"),
    ("calendar_change_proposals", "SELECT"),
    ("calendar_change_snapshots", "DELETE"),
    ("calendar_change_snapshots", "SELECT"),
    ("connection_capabilities", "DELETE"),
    ("connection_capabilities", "SELECT"),
    ("mail_draft_versions", "DELETE"),
    ("mail_draft_versions", "SELECT"),
    ("mail_drafts", "DELETE"),
    ("mail_drafts", "SELECT"),
    ("oauth_attempts", "DELETE"),
    ("oauth_attempts", "SELECT"),
    ("provider_calendars", "DELETE"),
    ("provider_calendars", "SELECT"),
    ("sync_cursors", "DELETE"),
)

# 只列 0018 已存在、但到 0019 才允许 retention UPDATE 的列。两个 AAD version 列由
# 0019 才创建，下面的 owner catalog 断言单独证明它们在 0018 根本不存在。
_RETENTION_0019_ONLY_EXISTING_UPDATE_COLUMNS = (
    ("approval_requests", "payload_ciphertext"),
    ("approval_requests", "payload_key_version"),
    ("approval_requests", "payload_nonce"),
    ("approval_requests", "status"),
    ("calendar_change_proposals", "status"),
    ("calendar_change_proposals", "updated_at"),
    ("calendar_change_snapshots", "content_ciphertext"),
    ("calendar_change_snapshots", "content_key_version"),
    ("calendar_change_snapshots", "content_nonce"),
    ("calendar_events", "description_ciphertext"),
    ("calendar_events", "description_key_version"),
    ("calendar_events", "description_nonce"),
    ("calendar_events", "location_ciphertext"),
    ("calendar_events", "location_key_version"),
    ("calendar_events", "location_nonce"),
    ("calendar_events", "updated_at"),
    ("email_messages", "updated_at"),
    ("mail_draft_versions", "body_ciphertext"),
    ("mail_draft_versions", "body_key_version"),
    ("mail_draft_versions", "body_nonce"),
    ("mail_drafts", "status"),
    ("mail_drafts", "updated_at"),
    ("task_runs", "approval_checkpoint_recovery_at"),
    ("task_runs", "error_code"),
    ("task_runs", "finished_at"),
    ("task_runs", "lease_expires_at"),
    ("task_runs", "lease_owner"),
    ("task_runs", "retry_recovery_at"),
    ("task_runs", "scheduled_for"),
    ("task_runs", "status"),
    ("task_runs", "updated_at"),
    ("tool_executions", "error_code"),
    ("tool_executions", "status"),
    ("users", "brief_time"),
    ("users", "default_calendar_connection_id"),
    ("users", "default_calendar_id"),
    ("users", "default_mail_connection_id"),
    ("users", "locale"),
    ("users", "meeting_buffer_minutes"),
    ("users", "timezone"),
    ("users", "updated_at"),
    ("users", "working_hours"),
)

# Step 17 是 destination runtime 权限的独立事实来源。这里逐表冻结完整矩阵，禁止从
# production 私有 registry、当前 catalog 或 Worker 查询形状反推测试期望。
_DESTINATION_RETENTION_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "approval_requests": frozenset({"DELETE", "SELECT"}),
    "audit_events": frozenset({"DELETE", "INSERT", "SELECT"}),
    "calendar_change_proposals": frozenset({"DELETE", "SELECT"}),
    "calendar_change_snapshots": frozenset({"DELETE", "SELECT"}),
    "calendar_events": frozenset({"DELETE", "SELECT"}),
    "connection_capabilities": frozenset({"DELETE", "SELECT"}),
    "conversations": frozenset({"DELETE", "SELECT"}),
    "daily_brief_items": frozenset({"DELETE", "SELECT"}),
    "daily_briefs": frozenset({"DELETE", "SELECT"}),
    "email_analyses": frozenset({"DELETE", "SELECT"}),
    "email_messages": frozenset({"DELETE", "SELECT"}),
    "email_threads": frozenset({"DELETE", "SELECT"}),
    "encrypted_credentials": frozenset({"DELETE", "SELECT"}),
    "llm_invocations": frozenset({"DELETE", "SELECT"}),
    "mail_draft_versions": frozenset({"DELETE", "SELECT"}),
    "mail_drafts": frozenset({"DELETE", "SELECT"}),
    "messages": frozenset({"DELETE", "SELECT"}),
    "oauth_attempts": frozenset({"DELETE", "SELECT"}),
    "oauth_connections": frozenset({"DELETE", "SELECT"}),
    "outbox_events": frozenset({"DELETE", "SELECT"}),
    "provider_calendars": frozenset({"DELETE", "SELECT"}),
    "sync_cursors": frozenset({"DELETE", "SELECT"}),
    "task_runs": frozenset({"DELETE", "SELECT"}),
    "task_steps": frozenset({"DELETE", "SELECT"}),
    "tool_executions": frozenset({"DELETE", "SELECT"}),
    "user_sessions": frozenset({"DELETE", "SELECT"}),
    "users": frozenset({"SELECT"}),
}

_DESTINATION_RETENTION_UPDATE_COLUMNS: dict[str, frozenset[str]] = {
    "approval_requests": frozenset(
        {"payload_ciphertext", "payload_key_version", "payload_nonce", "status"}
    ),
    "calendar_change_proposals": frozenset({"status", "updated_at"}),
    "calendar_change_snapshots": frozenset(
        {"content_ciphertext", "content_key_version", "content_nonce"}
    ),
    "calendar_events": frozenset(
        {
            "description_aad_version",
            "description_ciphertext",
            "description_key_version",
            "description_nonce",
            "location_aad_version",
            "location_ciphertext",
            "location_key_version",
            "location_nonce",
            "updated_at",
        }
    ),
    "email_messages": frozenset(
        {"body_ciphertext", "body_key_version", "body_nonce", "updated_at"}
    ),
    "mail_draft_versions": frozenset(
        {"body_ciphertext", "body_key_version", "body_nonce"}
    ),
    "mail_drafts": frozenset({"status", "updated_at"}),
    "sync_cursors": frozenset(
        {"cursor", "last_attempt_at", "last_error_code", "last_success_at"}
    ),
    "task_runs": frozenset(
        {
            "approval_checkpoint_recovery_at",
            "error_code",
            "finished_at",
            "lease_expires_at",
            "lease_owner",
            "retry_recovery_at",
            "scheduled_for",
            "status",
            "updated_at",
        }
    ),
    "tool_executions": frozenset({"error_code", "status"}),
    "users": frozenset(
        {
            "brief_time",
            "default_calendar_connection_id",
            "default_calendar_id",
            "default_mail_connection_id",
            "display_name",
            "email",
            "email_body_retention_days",
            "is_active",
            "locale",
            "meeting_buffer_minutes",
            "password_hash",
            "source_metadata_retention_days",
            "timezone",
            "updated_at",
            "working_hours",
            "workspace_history_retention_days",
        }
    ),
}

_M1_RETENTION_DELETE_ONLY_TABLES = frozenset(
    {
        "conversations",
        "daily_brief_items",
        "daily_briefs",
        "email_analyses",
        "email_threads",
        "encrypted_credentials",
        "llm_invocations",
        "messages",
        "outbox_events",
        "task_steps",
        "user_sessions",
    }
)

_DESTINATION_UNPRIVILEGED_TABLES = frozenset(
    {
        "alembic_version",
        "checkpoint_blobs",
        "checkpoint_migrations",
        "checkpoint_writes",
        "checkpoints",
    }
)

_POSTGRESQL_TABLE_PRIVILEGES = frozenset(
    {
        "DELETE",
        "INSERT",
        "MAINTAIN",
        "REFERENCES",
        "SELECT",
        "TRIGGER",
        "TRUNCATE",
        "UPDATE",
    }
)


async def _assert_retention_statement_is_denied(
    engine: AsyncEngine,
    statement: str,
    parameters: Mapping[str, object],
) -> None:
    """断言 retention 角色执行未列入当前 revision 协议的语句返回 ``42501``。

    Args:
        engine: 使用 retention 专用 DSN 创建的异步引擎。
        statement: 仅由测试代码定义的参数化语句，禁止拼接用户输入。
        parameters: 仅含合成 UUID 的绑定参数，避免测试日志出现真实数据。

    Raises:
        AssertionError: PostgreSQL 未返回权限拒绝 SQLSTATE 时抛出。
    """
    async with engine.connect() as connection:
        with pytest.raises(DBAPIError) as error:
            await connection.execute(text(statement), parameters)
        await connection.rollback()
    assert getattr(error.value.orig, "sqlstate", None) == "42501"


class _FinalizeBarrierPrivacyDeletionWorker(PrivacyDeletionWorker):
    """让两个独立 retention Worker 同时进入最终串行化临界区。"""

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        finalize_barrier: asyncio.Barrier,
    ) -> None:
        """保存独立 factory 与只用于测试的双参与者 barrier。"""
        super().__init__(session_factory)
        self._finalize_barrier = finalize_barrier

    async def _finalize_deleted_user(self, *, user_id: UUID, request_id: str) -> None:
        """等待两个 Worker 都完成前置删除后，再竞争真实 PostgreSQL 行锁。"""
        await self._finalize_barrier.wait()
        await super()._finalize_deleted_user(user_id=user_id, request_id=request_id)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """禁止全局 fixture 迁移共享 Task 13 数据库。

    本模块只在 UUID disposable database 中执行 typed role-bootstrap → migrate；共享
    ``TEST_DATABASE_URL`` 只作为已验证 management anchor，不能被隐式 bootstrap、迁移或
    role mutation。
    """
    yield


@pytest.fixture(autouse=True)
async def isolated_database() -> AsyncIterator[None]:
    """覆盖全局 TRUNCATE fixture；临时数据库由模块 fixture 整体隔离并最终删除。"""
    yield


@dataclass(frozen=True, slots=True)
class _DisposableRoleDatabase:
    """保存 disposable database 的三种登录 URL，并从 repr 隐藏全部凭据。"""

    owner_url: str = field(repr=False)
    app_url: str = field(repr=False)
    retention_url: str = field(repr=False)

    def __repr__(self) -> str:
        """返回不含用户名、主机、数据库名或密码的稳定调试表示。"""
        return "<disposable role database>"

    def upgrade_to_0019(self) -> None:
        """经测试专用三锁入口把当前 disposable database 精确升级到 0019。

        发布权威断言必须先于 Alembic command 或任何数据库 mutation；Cycle 5 RED 因此只
        在尚未发布的 0019 head 上失败。发布后，同一 helper 仍消费 typed authority、绑定
        外部连接并核验 migration 结果，最后由 owner 再验证 destination baseline ACL。
        """
        owner_sync_url = make_url(self.owner_url).set(drivername="postgresql+psycopg")
        owner_engine = create_engine(
            owner_sync_url,
            poolclass=NullPool,
            hide_parameters=True,
        )
        config = Config(REPOSITORY_ROOT / "backend" / "alembic.ini")
        set_alembic_database_url(
            config,
            owner_sync_url.render_as_string(hide_password=False),
        )
        try:
            published_authority = load_published_alembic_authority(config)
            assert published_authority.head_revision == "20260809_0019"
            run_alembic_upgrade(config, "20260809_0019")
            with owner_engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                assert revision == "20260809_0019"
                verify_object_grants(
                    connection,
                    revision="20260809_0019",
                    phase=GrantPhase.BASELINE,
                )
        finally:
            owner_engine.dispose()


@pytest.fixture(scope="module")
def disposable_role_database(
    database_url: ValidatedDatabaseUrl,
) -> Iterator[_DisposableRoleDatabase]:
    """创建 UUID disposable database，并精确初始化到 0018 source revision。

    ``TEST_DATABASE_URL`` 只作为通过 loopback/显式端口/``_test`` 校验的 management
    anchor。首写前同时证明随机数据库和两个 fixed roles 均 absent；任一预存角色都会
    fail closed，禁止轮换未知密码。setup 成功后 app/retention 登录及 0018/0019 权限断言
    全部指向临时数据库；``finally`` 精确复核 database=0、roles=0。
    """
    validated = validate_test_database_url(database_url)
    database_name = f"ai_employee_retention_{uuid4().hex}_test"
    owner_async_url = validated.for_database(database_name)
    management_engine = create_engine(
        validated.maintenance_url().set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    published_authority = load_published_alembic_authority(
        Config(REPOSITORY_ROOT / "backend" / "alembic.ini")
    )
    try:
        with managed_disposable_database_cleanup(
            management_engine,
            database_name=database_name,
        ) as cleanup:
            try:
                cleanup.assert_initial_absence()
            except DisposableDatabaseCleanupError:
                pytest.fail("BLOCKED: retention target or fixed runtime roles already exist")
            if not cleanup.create_database():
                pytest.fail("BLOCKED: configured test role lacks CREATEDB")

            target_engine = create_engine(
                owner_async_url.set(drivername="postgresql+psycopg"),
                poolclass=NullPool,
                hide_parameters=True,
            )
            try:
                context = SqlAlchemyDatabaseMaintenanceContext(
                    management_engine=management_engine,
                    target_engine=target_engine,
                    target_database_name=database_name,
                    bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
                    app_password=_APP_ROLE_PASSWORD,
                    retention_password=_RETENTION_ROLE_PASSWORD,
                    migration_runner=partial(
                        run_alembic_upgrade_on_connection,
                        config_path=REPOSITORY_ROOT / "backend" / "alembic.ini",
                    ),
                    published_authority=published_authority,
                )
                try:
                    role_bootstrap_database(context)
                except BaseException as original_error:
                    try:
                        cleanup.record_created_runtime_roles_if_safe()
                    except DisposableDatabaseCleanupError as cleanup_error:
                        raise cleanup_error from original_error
                    raise
                if cleanup.record_created_runtime_roles_if_safe() is not True:
                    raise AssertionError(
                        "retention disposable bootstrap did not create safe runtime roles"
                    )
                config = Config(REPOSITORY_ROOT / "backend" / "alembic.ini")
                set_alembic_database_url(
                    config,
                    owner_async_url.set(drivername="postgresql+psycopg").render_as_string(
                        hide_password=False
                    ),
                )
                run_alembic_upgrade(config, "20260809_0018")
                with target_engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    verify_object_grants(
                        connection,
                        revision="20260809_0018",
                        phase=GrantPhase.BASELINE,
                    )
                if revision != "20260809_0018":
                    raise AssertionError(
                        "disposable retention database did not reach revision 0018"
                    )

                app_url = owner_async_url.set(
                    username=APP_RUNTIME_ROLE_NAME,
                    password=_APP_ROLE_PASSWORD,
                )
                retention_url = owner_async_url.set(
                    username=RETENTION_RUNTIME_ROLE_NAME,
                    password=_RETENTION_ROLE_PASSWORD,
                )
                yield _DisposableRoleDatabase(
                    owner_url=owner_async_url.render_as_string(hide_password=False),
                    app_url=app_url.render_as_string(hide_password=False),
                    retention_url=retention_url.render_as_string(hide_password=False),
                )
            finally:
                # cleanup 的首个 DROP 前必须先关闭所有可能持 target session 的 Engine。
                target_engine.dispose()
    finally:
        management_engine.dispose()


@pytest.mark.asyncio
async def test_revision_0018_retention_inventory_denies_every_0019_only_privilege(
    disposable_role_database: _DisposableRoleDatabase,
) -> None:
    """0018 必须精确匹配冻结 inventory，不能由 role-bootstrap 提前修成 0019 policy。"""
    retention_url = disposable_role_database.retention_url
    owner_engine = create_engine(
        make_url(disposable_role_database.owner_url).set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
    )
    try:
        with owner_engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == "20260809_0018"
            verify_object_grants(
                connection,
                revision="20260809_0018",
                phase=GrantPhase.BASELINE,
            )
            aad_columns = set(
                connection.execute(
                    text(
                        "SELECT attname FROM pg_catalog.pg_attribute "
                        "WHERE attrelid = 'calendar_events'::regclass "
                        "AND attnum > 0 AND NOT attisdropped "
                        "AND attname IN ('description_aad_version', 'location_aad_version')"
                    )
                ).scalars()
            )
            assert aad_columns == set()
            # 0018 仍保留 checkpoint sequence USAGE；0019 的显式 delta 才会收窄为仅 audit。
            assert (
                connection.execute(
                    text(
                        "SELECT has_sequence_privilege("
                        "'ai_employee_retention', 'checkpoint_migrations_v_seq', 'USAGE')"
                    )
                ).scalar_one()
                is True
            )
    finally:
        owner_engine.dispose()

    retention_engine = create_async_engine(retention_url, poolclass=NullPool)
    preparer = retention_engine.sync_engine.dialect.identifier_preparer
    try:
        for table_name, privilege in _RETENTION_0019_ONLY_TABLE_PRIVILEGES:
            quoted_table = preparer.quote_identifier(table_name)
            if privilege == "SELECT":
                statement = f"SELECT 1 FROM {quoted_table} LIMIT 0"
            elif privilege == "DELETE":
                statement = f"DELETE FROM {quoted_table} WHERE FALSE"
            else:  # pragma: no cover - 冻结常量只允许 SELECT/DELETE。
                raise AssertionError("unexpected frozen table privilege")
            await _assert_retention_statement_is_denied(retention_engine, statement, {})

        for table_name, column_name in _RETENTION_0019_ONLY_EXISTING_UPDATE_COLUMNS:
            quoted_table = preparer.quote_identifier(table_name)
            quoted_column = preparer.quote_identifier(column_name)
            await _assert_retention_statement_is_denied(
                retention_engine,
                f"UPDATE {quoted_table} SET {quoted_column} = DEFAULT WHERE FALSE",
                {},
            )
    finally:
        await retention_engine.dispose()


@pytest.mark.asyncio
async def test_retention_role_runs_source_cleanup_and_application_cannot_mutate_audit_events(
    disposable_role_database: _DisposableRoleDatabase,
) -> None:
    """保留角色仅可更新清理字段，且能执行真实的先读后删流程。"""
    owner_url = disposable_role_database.owner_url
    app_url = disposable_role_database.app_url
    retention_url = disposable_role_database.retention_url
    owner_factory = build_session_factory(owner_url)
    retention_factory = build_session_factory(retention_url)
    retention_engine = create_async_engine(retention_url)
    app_engine = create_async_engine(app_url)
    try:
        async with owner_factory.begin() as session:
            user = UserModel(
                email="role-permission@example.test",
                display_name="Synthetic role test user",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="google",
                provider_account_id="role-permission",
                account_email="role-permission@example.test",
                scopes=["gmail.readonly"],
                status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()
            thread = EmailThreadModel(
                user_id=user.id,
                connection_id=connection.id,
                provider_thread_id="role-permission-thread",
                subject="Synthetic role permission source",
                participants=[],
                latest_message_at=datetime.now(UTC),
                provider_url="https://example.test/thread",
                provider_updated_at=None,
            )
            session.add(thread)
            await session.flush()
            sync_cursor = SyncCursorModel(
                connection_id=connection.id,
                resource_kind="mail",
                scope_key="mailbox",
                cursor="synthetic-cursor",
                last_success_at=datetime.now(UTC),
            )
            session.add_all(
                [
                    EmailMessageModel(
                        user_id=user.id,
                        connection_id=connection.id,
                        thread_id=thread.id,
                        provider_message_id="role-permission-message",
                        received_at=datetime.now(UTC),
                        sender={},
                        recipients=[],
                        subject="Synthetic source",
                        snippet="Synthetic",
                        body_ciphertext=b"synthetic",
                        body_nonce=b"123456789012",
                        body_key_version=1,
                        labels=[],
                        headers={},
                        provider_url="https://example.test/message",
                    ),
                    sync_cursor,
                ]
            )
            await session.flush()
            user_id = user.id
            cursor_id = sync_cursor.id

        # 正文抹除与用户匿名化字段是 Worker 唯一允许的 UPDATE 目标，必须在专用角色下成功。
        async with retention_engine.begin() as retention_connection:
            email_update = await retention_connection.execute(
                text(
                    "UPDATE email_messages SET body_ciphertext = body_ciphertext "
                    "WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            )
            user_update = await retention_connection.execute(
                text("UPDATE users SET display_name = display_name WHERE id = :user_id"),
                {"user_id": user_id},
            )
            cursor_update = await retention_connection.execute(
                text("UPDATE sync_cursors SET cursor = cursor WHERE id = :cursor_id"),
                {"cursor_id": cursor_id},
            )
        assert email_update.rowcount == 1
        assert user_update.rowcount == 1
        assert cursor_update.rowcount == 1
        await _assert_retention_statement_is_denied(
            retention_engine,
            "UPDATE email_messages SET subject = subject WHERE user_id = :user_id",
            {"user_id": user_id},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "UPDATE users SET timezone = timezone WHERE id = :user_id",
            {"user_id": user_id},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "UPDATE users SET brief_time = brief_time WHERE id = :user_id",
            {"user_id": user_id},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "UPDATE sync_cursors SET resource_kind = resource_kind WHERE id = :cursor_id",
            {"cursor_id": cursor_id},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "UPDATE sync_cursors SET connection_id = connection_id WHERE id = :cursor_id",
            {"cursor_id": cursor_id},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "UPDATE sync_cursors SET id = id WHERE id = :cursor_id",
            {"cursor_id": cursor_id},
        )

        # 该路径会 SELECT 来源主键，再 DELETE 邮件与线程并 UPDATE 游标，不能由宽泛授权替代。
        await PrivacyDeletionWorker(retention_factory).clear_source_cache(
            user_id=user_id, batch_size=10
        )
        # 定期保留扫描还会读取所有相关空表并追加审计，覆盖专用角色的完整入口权限。
        assert await RetentionCleanupWorker(retention_factory).execute(now=datetime.now(UTC)) == 1

        async with owner_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(EmailMessageModel)
                    .where(EmailMessageModel.user_id == user_id)
                )
                == 0
            )
            cursor = await session.scalar(
                select(SyncCursorModel).where(SyncCursorModel.connection_id == connection.id)
            )
            assert cursor is not None
            assert cursor.cursor is None
            assert cursor.last_success_at is None

        async with app_engine.connect() as connection:
            with pytest.raises(DBAPIError) as update_error:
                await connection.execute(text("UPDATE audit_events SET event_type = event_type"))
            await connection.rollback()
        assert getattr(update_error.value.orig, "sqlstate", None) == "42501"

        async with app_engine.connect() as connection:
            with pytest.raises(DBAPIError) as delete_error:
                await connection.execute(text("DELETE FROM audit_events"))
            await connection.rollback()
        assert getattr(delete_error.value.orig, "sqlstate", None) == "42501"
    finally:
        await app_engine.dispose()
        await retention_engine.dispose()
        await retention_factory.dispose()
        await owner_factory.dispose()


@pytest.mark.asyncio
async def test_revision_0019_retention_login_matches_complete_destination_matrix(
    disposable_role_database: _DisposableRoleDatabase,
) -> None:
    """0019 retention 登录必须真实匹配 Step 17 完整 destination 最小权限矩阵。"""
    disposable_role_database.upgrade_to_0019()

    owner_engine = create_engine(
        make_url(disposable_role_database.owner_url).set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    expected_tables = frozenset(_DESTINATION_RETENTION_TABLE_PRIVILEGES) | (
        _DESTINATION_UNPRIVILEGED_TABLES
    )
    table_columns: dict[str, frozenset[str]] = {}
    insert_shapes: dict[str, tuple[tuple[str, str], ...]] = {}
    synthetic_user_id = uuid4()
    try:
        with owner_engine.begin() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == "20260809_0019"
            verified = verify_object_grants(
                connection,
                revision="20260809_0019",
                phase=GrantPhase.BASELINE,
            )
            raw = read_object_grants(
                connection,
                revision="20260809_0019",
                phase=GrantPhase.BASELINE,
            )
            assert raw == verified
            baseline_grants = raw

            retention_grants = tuple(
                grant
                for grant in raw.grants
                if grant.grantee == RETENTION_RUNTIME_ROLE_NAME
            )
            assert retention_grants
            assert all(grant.is_grantable is False for grant in retention_grants)

            actual_schema_privileges = {
                (grant.object_name, grant.privilege_type)
                for grant in retention_grants
                if grant.object_kind is ObjectKind.SCHEMA
            }
            assert actual_schema_privileges == {("public", "USAGE")}

            actual_sequence_privileges = {
                (grant.object_name, grant.privilege_type)
                for grant in retention_grants
                if grant.object_kind is ObjectKind.SEQUENCE
            }
            assert actual_sequence_privileges == {("audit_events_id_seq", "USAGE")}

            # actual 的对象键必须完全来自 raw catalog；若由 expected 键驱动，已有表或
            # 未授权表上的额外 REFERENCES/TRIGGER 等 tuple 会在比较前被静默丢弃。
            mutable_actual_table_privileges: dict[str, set[str]] = {}
            for grant in retention_grants:
                if grant.object_kind is ObjectKind.TABLE:
                    mutable_actual_table_privileges.setdefault(
                        grant.object_name, set()
                    ).add(grant.privilege_type)
            actual_table_privileges = {
                table_name: frozenset(privileges)
                for table_name, privileges in mutable_actual_table_privileges.items()
            }
            assert actual_table_privileges == _DESTINATION_RETENTION_TABLE_PRIVILEGES

            mutable_actual_update_columns: dict[str, set[str]] = {}
            for grant in retention_grants:
                if grant.object_kind is not ObjectKind.COLUMN:
                    continue
                assert grant.privilege_type == "UPDATE"
                assert grant.column_name is not None
                mutable_actual_update_columns.setdefault(grant.object_name, set()).add(
                    grant.column_name
                )
            actual_update_columns = {
                table_name: frozenset(column_names)
                for table_name, column_names in mutable_actual_update_columns.items()
            }
            assert actual_update_columns == _DESTINATION_RETENTION_UPDATE_COLUMNS

            app_audit_privileges = {
                grant.privilege_type
                for grant in raw.grants
                if grant.grantee == APP_RUNTIME_ROLE_NAME
                and grant.object_kind is ObjectKind.TABLE
                and grant.object_name == "audit_events"
            }
            assert "UPDATE" not in app_audit_privileges
            assert "DELETE" not in app_audit_privileges

            catalog_tables = {
                grant.object_name
                for grant in raw.grants
                if grant.object_kind is ObjectKind.TABLE
            }
            assert catalog_tables == expected_tables
            assert _M1_RETENTION_DELETE_ONLY_TABLES <= expected_tables
            for table_name in _M1_RETENTION_DELETE_ONLY_TABLES:
                assert _DESTINATION_RETENTION_TABLE_PRIVILEGES[table_name] == frozenset(
                    {"DELETE", "SELECT"}
                )
                assert table_name not in _DESTINATION_RETENTION_UPDATE_COLUMNS

            column_rows = connection.execute(
                text(
                    "SELECT relation.relname AS table_name, "
                    "attribute.attname AS column_name, "
                    "format_type(attribute.atttypid, attribute.atttypmod) AS type_sql, "
                    "attribute.attidentity AS identity_kind, "
                    "attribute.attgenerated AS generated_kind "
                    "FROM pg_catalog.pg_class AS relation "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "JOIN pg_catalog.pg_attribute AS attribute "
                    "ON attribute.attrelid = relation.oid "
                    "WHERE namespace.nspname = 'public' "
                    "AND relation.relkind IN ('r', 'p') "
                    "AND attribute.attnum > 0 "
                    "AND NOT attribute.attisdropped "
                    "ORDER BY relation.relname, attribute.attnum"
                )
            ).mappings()
            mutable_columns: dict[str, list[str]] = {
                table_name: [] for table_name in expected_tables
            }
            mutable_insert_shapes: dict[str, list[tuple[str, str]]] = {
                table_name: [] for table_name in expected_tables
            }
            for row in column_rows:
                table_name = row["table_name"]
                column_name = row["column_name"]
                type_sql = row["type_sql"]
                identity_kind = row["identity_kind"]
                generated_kind = row["generated_kind"]
                assert isinstance(table_name, str) and table_name in expected_tables
                assert isinstance(column_name, str) and column_name
                assert isinstance(type_sql, str) and type_sql
                assert isinstance(identity_kind, str)
                assert isinstance(generated_kind, str)
                mutable_columns[table_name].append(column_name)
                if identity_kind == "" and generated_kind == "":
                    mutable_insert_shapes[table_name].append((column_name, type_sql))
            table_columns = {
                table_name: frozenset(columns)
                for table_name, columns in mutable_columns.items()
            }
            insert_shapes = {
                table_name: tuple(columns)
                for table_name, columns in mutable_insert_shapes.items()
            }
            assert all(table_columns.values())
            assert all(insert_shapes.values())
            for table_name, allowed_columns in _DESTINATION_RETENTION_UPDATE_COLUMNS.items():
                assert allowed_columns <= table_columns[table_name]
                assert table_columns[table_name] - allowed_columns

            connection.execute(
                text(
                    "INSERT INTO users ("
                    "id, email, display_name, password_hash, timezone, locale, brief_time, "
                    "is_active, email_body_retention_days, source_metadata_retention_days, "
                    "workspace_history_retention_days, working_hours, meeting_buffer_minutes"
                    ") VALUES ("
                    ":user_id, :email, 'Synthetic destination ACL user', NULL, 'UTC', "
                    "'zh-CN', '08:00:00'::time, TRUE, 30, 180, 365, '{}'::jsonb, 10"
                    ")"
                ),
                {
                    "user_id": synthetic_user_id,
                    "email": f"retention-destination-{synthetic_user_id.hex}@example.test",
                },
            )
    finally:
        owner_engine.dispose()

    retention_engine = create_async_engine(
        disposable_role_database.retention_url,
        poolclass=NullPool,
        hide_parameters=True,
    )
    app_engine = create_async_engine(
        disposable_role_database.app_url,
        poolclass=NullPool,
        hide_parameters=True,
    )
    retention_factories = (
        build_session_factory(disposable_role_database.retention_url),
        build_session_factory(disposable_role_database.retention_url),
    )
    owner_factory = build_session_factory(disposable_role_database.owner_url)
    preparer = retention_engine.sync_engine.dialect.identifier_preparer
    quoted_schema = preparer.quote_identifier("public")
    try:
        missing_user_id = uuid4()
        missing_request_id = "synthetic-missing-user-finalization"
        with pytest.raises(InternalInvariantError) as missing_user_error:
            await PrivacyDeletionWorker(
                retention_factories[0]
            )._finalize_deleted_user(
                user_id=missing_user_id,
                request_id=missing_request_id,
            )
        assert missing_user_error.value.error_code == "privacy_deletion_user_missing"
        assert missing_user_error.value.message == "privacy deletion user is missing"
        assert missing_user_error.value.metadata == {}
        async with owner_factory() as session:
            missing_user_audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.user_id == missing_user_id,
                    AuditEventModel.event_type == "privacy.deletion_completed",
                    AuditEventModel.event_metadata["request_id"].astext
                    == missing_request_id,
                )
            )
        assert missing_user_audit_count == 0

        # audit_events 的 SELECT 权限不得隐式扩大为行锁权限；旧实现对完成审计执行
        # FOR UPDATE 会要求表级 UPDATE，并必须由真实 retention 登录稳定返回 42501。
        await _assert_retention_statement_is_denied(
            retention_engine,
            "SELECT id FROM public.audit_events "
            "WHERE user_id = :user_id "
            "AND event_type = 'privacy.deletion_completed' "
            "AND metadata ->> 'request_id' = :request_id "
            "FOR UPDATE",
            {
                "user_id": synthetic_user_id,
                "request_id": "retention-destination-audit-lock-probe",
            },
        )

        # 所有允许的表级权限与列级 UPDATE 都由 retention 登录真实执行；WHERE FALSE
        # 避免 DEFAULT、外键、非空约束或业务状态副作用。users.is_active 的列授权属于 ACL
        # 契约；本测试稍后通过真实 privacy Worker 同 request 重入证明业务状态不会被重新激活。
        async with retention_engine.begin() as connection:
            for table_name, privileges in _DESTINATION_RETENTION_TABLE_PRIVILEGES.items():
                quoted_table = (
                    f"{quoted_schema}.{preparer.quote_identifier(table_name)}"
                )
                if "SELECT" in privileges:
                    await connection.execute(text(f"SELECT 1 FROM {quoted_table} LIMIT 0"))
                if "DELETE" in privileges:
                    await connection.execute(text(f"DELETE FROM {quoted_table} WHERE FALSE"))
                assert privileges <= frozenset({"DELETE", "INSERT", "SELECT"})

            for table_name, column_names in _DESTINATION_RETENTION_UPDATE_COLUMNS.items():
                quoted_table = (
                    f"{quoted_schema}.{preparer.quote_identifier(table_name)}"
                )
                for column_name in column_names:
                    quoted_column = preparer.quote_identifier(column_name)
                    await connection.execute(
                        text(
                            f"UPDATE {quoted_table} SET {quoted_column} = {quoted_column} "
                            "WHERE FALSE"
                        )
                    )

            await connection.execute(
                text(
                    "INSERT INTO public.audit_events ("
                    "user_id, task_id, event_type, actor_type, actor_id, metadata"
                    ") VALUES ("
                    ":user_id, NULL, 'retention.destination.synthetic', "
                    "'system', NULL, '{}'::jsonb"
                    ")"
                ),
                {"user_id": synthetic_user_id},
            )
            await connection.execute(
                text("SELECT nextval('public.audit_events_id_seq'::regclass)")
            )

        for table_name in sorted(expected_tables):
            privileges = _DESTINATION_RETENTION_TABLE_PRIVILEGES.get(
                table_name, frozenset()
            )
            quoted_table = f"{quoted_schema}.{preparer.quote_identifier(table_name)}"

            if "SELECT" not in privileges:
                await _assert_retention_statement_is_denied(
                    retention_engine,
                    f"SELECT 1 FROM {quoted_table} LIMIT 0",
                    {},
                )
            if "INSERT" not in privileges:
                insert_shape = insert_shapes[table_name]
                quoted_columns = ", ".join(
                    preparer.quote_identifier(column_name)
                    for column_name, _ in insert_shape
                )
                empty_values = ", ".join(
                    f"NULL::{type_sql}" for _, type_sql in insert_shape
                )
                await _assert_retention_statement_is_denied(
                    retention_engine,
                    f"INSERT INTO {quoted_table} ({quoted_columns}) "
                    f"SELECT {empty_values} WHERE FALSE",
                    {},
                )
            if "DELETE" not in privileges:
                await _assert_retention_statement_is_denied(
                    retention_engine,
                    f"DELETE FROM {quoted_table} WHERE FALSE",
                    {},
                )

            allowed_update_columns = _DESTINATION_RETENTION_UPDATE_COLUMNS.get(
                table_name, frozenset()
            )
            omitted_update_columns = table_columns[table_name] - allowed_update_columns
            assert omitted_update_columns
            for column_name in sorted(omitted_update_columns):
                quoted_column = preparer.quote_identifier(column_name)
                await _assert_retention_statement_is_denied(
                    retention_engine,
                    f"UPDATE {quoted_table} SET {quoted_column} = {quoted_column} WHERE FALSE",
                    {},
                )

            # REFERENCES/TRIGGER 没有可安全构造且语法合法的零写语句；上面的 canonical raw
            # multiset 已逐 tuple 证明它们连同 grant option 都不存在。其余可执行权限仍由
            # 真实 SQL 拒绝，避免退化成 effective-permission 探测。
            omitted_privileges = _POSTGRESQL_TABLE_PRIVILEGES - privileges
            assert {"REFERENCES", "TRIGGER", "TRUNCATE", "MAINTAIN"} <= (
                omitted_privileges
            )
            await _assert_retention_statement_is_denied(
                retention_engine,
                f"TRUNCATE TABLE {quoted_table}",
                {},
            )
            await _assert_retention_statement_is_denied(
                retention_engine,
                f"REINDEX TABLE {quoted_table}",
                {},
            )

        await _assert_retention_statement_is_denied(
            retention_engine,
            "DELETE FROM public.users WHERE FALSE",
            {},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "UPDATE public.audit_events SET event_type = event_type WHERE FALSE",
            {},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "CREATE TABLE public.retention_destination_forbidden_probe (id integer)",
            {},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "SELECT nextval('public.checkpoint_migrations_v_seq'::regclass)",
            {},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "SELECT last_value FROM public.checkpoint_migrations_v_seq",
            {},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "SELECT setval('public.checkpoint_migrations_v_seq'::regclass, 1, FALSE)",
            {},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "SELECT last_value FROM public.audit_events_id_seq",
            {},
        )
        await _assert_retention_statement_is_denied(
            retention_engine,
            "SELECT setval('public.audit_events_id_seq'::regclass, 1, FALSE)",
            {},
        )

        await _assert_retention_statement_is_denied(
            app_engine,
            "UPDATE public.audit_events SET event_type = event_type WHERE FALSE",
            {},
        )
        await _assert_retention_statement_is_denied(
            app_engine,
            "DELETE FROM public.audit_events WHERE FALSE",
            {},
        )

        reactivation_request_id = "retention-destination-reactivation-guard"
        finalize_barrier = asyncio.Barrier(2)
        privacy_workers = tuple(
            _FinalizeBarrierPrivacyDeletionWorker(factory, finalize_barrier)
            for factory in retention_factories
        )
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    worker.delete_all_data(
                        user_id=synthetic_user_id,
                        request_id=reactivation_request_id,
                        batch_size=10,
                    )
                    for worker in privacy_workers
                )
            ),
            timeout=15.0,
        )
        async with owner_factory() as session:
            anonymized_user = (
                await session.execute(
                    select(
                        UserModel.email,
                        UserModel.display_name,
                        UserModel.password_hash,
                        UserModel.is_active,
                        UserModel.email_body_retention_days,
                        UserModel.source_metadata_retention_days,
                        UserModel.workspace_history_retention_days,
                    ).where(UserModel.id == synthetic_user_id)
                )
            ).one()
            completed_audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.user_id == synthetic_user_id,
                    AuditEventModel.event_type == "privacy.deletion_completed",
                    AuditEventModel.event_metadata["request_id"].astext
                    == reactivation_request_id,
                )
            )
        assert tuple(anonymized_user) == (
            f"deleted-{synthetic_user_id}@invalid.local",
            "Deleted User",
            None,
            False,
            30,
            180,
            365,
        )
        assert completed_audit_count == 1

        post_owner_engine = create_engine(
            make_url(disposable_role_database.owner_url).set(
                drivername="postgresql+psycopg"
            ),
            poolclass=NullPool,
            hide_parameters=True,
        )
        try:
            with post_owner_engine.connect() as connection:
                assert (
                    read_object_grants(
                        connection,
                        revision="20260809_0019",
                        phase=GrantPhase.BASELINE,
                    )
                    == baseline_grants
                )
        finally:
            post_owner_engine.dispose()
    finally:
        await owner_factory.dispose()
        for retention_factory in reversed(retention_factories):
            await retention_factory.dispose()
        await app_engine.dispose()
        await retention_engine.dispose()
