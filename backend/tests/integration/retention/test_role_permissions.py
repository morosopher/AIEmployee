"""以真实 PostgreSQL 角色验证保留与隐私清理的最小权限边界。"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    EmailMessageModel,
    EmailThreadModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.workers.privacy import PrivacyDeletionWorker
from ai_employee.workers.retention import RetentionCleanupWorker

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_APP_ROLE_PASSWORD = "app-role-integration-password"
_RETENTION_ROLE_PASSWORD = "retention-role-integration-password"


async def _assert_retention_update_is_denied(
    engine: AsyncEngine,
    statement: str,
    parameters: Mapping[str, object],
) -> None:
    """断言 retention 角色无法修改未列入清理协议的字段。

    Args:
        engine: 使用 retention 专用 DSN 创建的异步引擎。
        statement: 仅由测试代码定义的参数化 UPDATE 语句，禁止拼接用户输入。
        parameters: 仅含合成 UUID 的绑定参数，避免测试日志出现真实数据。

    Raises:
        AssertionError: PostgreSQL 未返回权限拒绝 SQLSTATE 时抛出。
    """
    async with engine.connect() as connection:
        with pytest.raises(DBAPIError) as error:
            await connection.execute(text(statement), parameters)
        await connection.rollback()
    assert getattr(error.value.orig, "sqlstate", None) == "42501"


@pytest.fixture
def initialized_database_roles(database_url: str, tmp_path: Path) -> tuple[str, str]:
    """经生产初始化脚本创建临时测试库的应用和 retention DSN。

    密码为测试文件中固定的合成值，只写入 ``tmp_path`` 供脚本标准输入读取；测试失败时
    不回显子进程输出，避免未来基础设施错误意外将连接信息带入 pytest 日志。
    """
    if shutil.which("psql") is None:
        pytest.skip("database role permission integration test requires the PostgreSQL psql client")

    base_url = make_url(database_url)
    if base_url.database is None or base_url.username is None or base_url.password is None:
        pytest.fail("configured integration database URL must include database credentials")

    app_password_file = tmp_path / "app-password"
    retention_password_file = tmp_path / "retention-password"
    app_password_file.write_text(_APP_ROLE_PASSWORD, encoding="utf-8")
    retention_password_file.write_text(_RETENTION_ROLE_PASSWORD, encoding="utf-8")
    environment = os.environ.copy()
    environment.update({
        "POSTGRES_DB": base_url.database,
        "PGHOST": base_url.host or "",
        "PGPORT": str(base_url.port or 5432),
        "PGUSER": base_url.username,
        "PGPASSWORD": base_url.password,
        "APP_DATABASE_PASSWORD_FILE": str(app_password_file),
        "RETENTION_DATABASE_PASSWORD_FILE": str(retention_password_file),
    })
    result = subprocess.run(
        ["bash", str(REPOSITORY_ROOT / "scripts" / "init-db-roles.sh")],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail("database role initialization script failed")

    app_url = base_url.set(username="ai_employee_app", password=_APP_ROLE_PASSWORD)
    retention_url = base_url.set(username="ai_employee_retention", password=_RETENTION_ROLE_PASSWORD)
    return app_url.render_as_string(hide_password=False), retention_url.render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_retention_role_runs_source_cleanup_and_application_cannot_mutate_audit_events(
    database_url: str,
    initialized_database_roles: tuple[str, str],
) -> None:
    """保留角色仅可更新清理字段，且能执行真实的先读后删流程。"""
    app_url, retention_url = initialized_database_roles
    owner_factory = build_session_factory(database_url)
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
                resource_kind="gmail",
                cursor="synthetic-cursor",
                last_success_at=datetime.now(UTC),
            )
            session.add_all([
                EmailMessageModel(
                    user_id=user.id,
                    thread_id=thread.id,
                    provider_message_id="role-permission-message",
                    received_at=datetime.now(UTC),
                    sender={}, recipients=[], subject="Synthetic source", snippet="Synthetic",
                    body_ciphertext=b"synthetic", body_nonce=b"123456789012", body_key_version=1,
                    labels=[], headers={}, provider_url="https://example.test/message",
                ),
                sync_cursor,
            ])
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
        await _assert_retention_update_is_denied(
            retention_engine,
            "UPDATE email_messages SET subject = subject WHERE user_id = :user_id",
            {"user_id": user_id},
        )
        await _assert_retention_update_is_denied(
            retention_engine,
            "UPDATE users SET timezone = timezone WHERE id = :user_id",
            {"user_id": user_id},
        )
        await _assert_retention_update_is_denied(
            retention_engine,
            "UPDATE users SET brief_time = brief_time WHERE id = :user_id",
            {"user_id": user_id},
        )
        await _assert_retention_update_is_denied(
            retention_engine,
            "UPDATE sync_cursors SET resource_kind = resource_kind WHERE id = :cursor_id",
            {"cursor_id": cursor_id},
        )
        await _assert_retention_update_is_denied(
            retention_engine,
            "UPDATE sync_cursors SET connection_id = connection_id WHERE id = :cursor_id",
            {"cursor_id": cursor_id},
        )
        await _assert_retention_update_is_denied(
            retention_engine,
            "UPDATE sync_cursors SET id = id WHERE id = :cursor_id",
            {"cursor_id": cursor_id},
        )

        # 该路径会 SELECT 来源主键，再 DELETE 邮件与线程并 UPDATE 游标，不能由宽泛授权替代。
        await PrivacyDeletionWorker(retention_factory).clear_source_cache(user_id=user_id, batch_size=10)
        # 定期保留扫描还会读取所有相关空表并追加审计，覆盖专用角色的完整入口权限。
        assert await RetentionCleanupWorker(retention_factory).execute(now=datetime.now(UTC)) == 1

        async with owner_factory() as session:
            assert await session.scalar(
                select(func.count()).select_from(EmailMessageModel).where(EmailMessageModel.user_id == user_id)
            ) == 0
            cursor = await session.scalar(select(SyncCursorModel).where(SyncCursorModel.connection_id == connection.id))
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
