"""验证 M2 连接能力、作用域游标、源字段与用户工作设置的数据库不变量。"""

import asyncio
import json
from datetime import UTC, datetime, time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.infrastructure.db import models as db_models
from ai_employee.infrastructure.db.alembic import set_alembic_database_url
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStore
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.testing.test_support import (
    TestSupportFixtureService as SupportFixtureService,
)

M2_REVISION = "20260806_0011"
GOOGLE_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"

DEFAULT_WORKING_HOURS = {
    "monday": [["09:00", "18:00"]],
    "tuesday": [["09:00", "18:00"]],
    "wednesday": [["09:00", "18:00"]],
    "thursday": [["09:00", "18:00"]],
    "friday": [["09:00", "18:00"]],
    "saturday": [],
    "sunday": [],
}

CAPABILITY_CONNECTION_OWNER_FK = "fk_connection_capabilities_connection_user"
PROVIDER_CALENDAR_CONNECTION_OWNER_FK = "fk_provider_calendars_connection_user"
DEFAULT_MAIL_CONNECTION_OWNER_FK = "fk_users_default_mail_connection_id_user_id"
DEFAULT_CALENDAR_CONNECTION_OWNER_FK = "fk_users_default_calendar_connection_id_user_id"
MICROSOFT_IDENTITY_CHECK = "ck_oauth_connections_microsoft_identity"


def _alembic_config(database_url: URL) -> Config:
    """构造仅指向 fixture 临时库的 Alembic 配置，不读取应用数据库环境。"""
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        config,
        database_url.render_as_string(hide_password=False),
    )
    return config


def _integrity_error_details(error: IntegrityError) -> tuple[str | None, str | None]:
    """从 asyncpg 异常边界提取 SQLSTATE 与命名约束，避免依赖错误文案。"""
    if error.orig is None:
        return None, None
    cause = error.orig.__cause__
    sqlstate = getattr(cause, "sqlstate", None)
    constraint_name = getattr(cause, "constraint_name", None)
    return (
        sqlstate if isinstance(sqlstate, str) else None,
        constraint_name if isinstance(constraint_name, str) else None,
    )


def _synthetic_user(*, ordinal: str) -> UserModel:
    """构造只含合成身份数据、依赖 M2 Python 工作设置默认值的用户。"""
    return UserModel(
        email=f"m2-schema-{ordinal}@example.test",
        display_name=f"M2 Schema {ordinal}",
        password_hash=None,
        timezone="UTC",
        locale="zh-CN",
        brief_time=time(8, 0),
        is_active=True,
    )


async def _seed_two_users_and_connections(
    session_factory: ManagedAsyncSessionMaker,
) -> tuple[UUID, UUID, UUID, UUID]:
    """提交两套互不归属的用户与连接，供组合外键反例交叉引用。"""
    async with session_factory.begin() as session:
        first_user = _synthetic_user(ordinal="first")
        second_user = _synthetic_user(ordinal="second")
        session.add_all((first_user, second_user))
        await session.flush()

        first_connection = OAuthConnectionModel(
            user_id=first_user.id,
            provider="google",
            provider_account_id="m2-schema-first",
            account_email="first@example.test",
            scopes=[],
            status="connected",
            last_error_code=None,
        )
        second_connection = OAuthConnectionModel(
            user_id=second_user.id,
            provider="google",
            provider_account_id="m2-schema-second",
            account_email="second@example.test",
            scopes=[],
            status="connected",
            last_error_code=None,
        )
        session.add_all((first_connection, second_connection))
        await session.flush()

        return (
            first_user.id,
            second_user.id,
            first_connection.id,
            second_connection.id,
        )


@pytest.mark.asyncio
async def test_m2_connection_and_source_defaults(database_url: str) -> None:
    """新 ORM 对象应得到完整工作设置和仅限可信 M1 范围的安全兼容默认值。"""
    session_factory = build_session_factory(database_url)
    try:
        capability_model = db_models.ConnectionCapabilityModel
        async with session_factory.begin() as session:
            capability_count = await session.scalar(
                select(func.count()).select_from(capability_model)
            )
            user = _synthetic_user(ordinal="defaults")
            session.add(user)
            await session.flush()

            attempt = OAuthAttemptModel(
                state_hash=b"s" * 32,
                user_id=user.id,
                encrypted_pkce_verifier=b"synthetic-pkce",
                nonce=b"n" * 12,
                key_version=1,
                expires_at=datetime(2026, 8, 7, 1, 0, tzinfo=UTC),
                consumed_at=None,
                created_at=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
            )
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="google",
                provider_account_id="m2-schema-defaults",
                account_email="defaults@example.test",
                scopes=[],
                status="connected",
                last_error_code=None,
            )
            session.add_all((attempt, connection))
            await session.flush()

            gmail_cursor = SyncCursorModel(
                connection_id=connection.id,
                resource_kind="gmail",
                cursor="synthetic-mail-cursor",
            )
            calendar_cursor = SyncCursorModel(
                connection_id=connection.id,
                resource_kind="calendar",
                cursor="synthetic-calendar-cursor",
            )
            session.add_all((gmail_cursor, calendar_cursor))
            await session.flush()

            assert capability_count == 0
            assert user.meeting_buffer_minutes == 10
            assert user.working_hours == DEFAULT_WORKING_HOURS
            assert set(user.working_hours) == {
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            }
            assert attempt.provider == "google"
            assert attempt.requested_capabilities == []
            assert attempt.oidc_nonce_hash is None
            assert connection.provider_tenant_id == ""
            assert connection.account_type == "google"
            assert gmail_cursor.scope_key == "mailbox"
            assert calendar_cursor.scope_key == "primary"
            assert UserModel.__table__.c.default_calendar_id.type.length == 512
            assert (
                db_models.ProviderCalendarModel.__table__.c.provider_calendar_id.type.length == 512
            )
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_sync_cursor_allows_distinct_scopes_for_one_resource(database_url: str) -> None:
    """主约束与旧名兼容约束都为三列时，同一资源的两个 scope 必须可以并存。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _synthetic_user(ordinal="cursor-scopes")
            session.add(user)
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="google",
                provider_account_id="m2-cursor-scopes",
                account_email="cursor-scopes@example.test",
                scopes=[],
                status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()
            session.add_all(
                (
                    SyncCursorModel(
                        connection_id=connection.id,
                        resource_kind="gmail",
                        scope_key="mailbox",
                        cursor="mailbox-cursor",
                    ),
                    SyncCursorModel(
                        connection_id=connection.id,
                        resource_kind="gmail",
                        scope_key="sent",
                        cursor="sent-cursor",
                    ),
                )
            )

        async with session_factory() as session:
            cursor_count = await session.scalar(
                select(func.count())
                .select_from(SyncCursorModel)
                .where(
                    SyncCursorModel.connection_id == connection.id,
                    SyncCursorModel.resource_kind == "gmail",
                )
            )
        assert cursor_count == 2
    finally:
        await session_factory.dispose()


@pytest.mark.parametrize(
    "existing_mail_cursor",
    (None, "migrated-mail-cursor"),
    ids=("new-connection", "reconnected-after-0013"),
)
@pytest.mark.asyncio
async def test_connection_token_save_uses_only_canonical_source_cursor_names(
    database_url: str,
    existing_mail_cursor: str | None,
) -> None:
    """新建和重连都只能保留 ``mail/mailbox`` 与 ``calendar/primary`` 两个游标。

    0013 已把历史 ``gmail`` 行原位迁移为 ``mail``。OAuth 回调若仍创建 Gmail 命名行，
    重连会把同一邮箱拆成两套恢复位置；本测试同时锁定新连接的规范名称与迁移后重连的
    游标保真，禁止生产调用方再次制造双游标。
    """
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _synthetic_user(ordinal=f"canonical-cursors-{existing_mail_cursor is not None}")
            session.add(user)
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="google",
                provider_account_id=f"canonical-cursors-{existing_mail_cursor is not None}",
                account_email="canonical-cursors@example.test",
                scopes=[],
                status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()
            if existing_mail_cursor is not None:
                session.add(
                    SyncCursorModel(
                        connection_id=connection.id,
                        resource_kind="mail",
                        scope_key="mailbox",
                        cursor=existing_mail_cursor,
                    )
                )
                await session.flush()

            await SqlAlchemyConnectionStore(session).save_connection_tokens(
                user_id=user.id,
                connection_id=connection.id,
                access_token=EncryptedValue(b"synthetic-access", b"a" * 12, 1),
                refresh_token=None,
                expires_at=datetime(2026, 8, 7, 1, 0, tzinfo=UTC),
            )
            connection_id = connection.id

        async with session_factory() as session:
            cursor_rows = tuple(
                (
                    await session.execute(
                        select(
                            SyncCursorModel.resource_kind,
                            SyncCursorModel.scope_key,
                            SyncCursorModel.cursor,
                        )
                        .where(SyncCursorModel.connection_id == connection_id)
                        .order_by(SyncCursorModel.resource_kind, SyncCursorModel.scope_key)
                    )
                ).all()
            )

        assert cursor_rows == (
            ("calendar", "primary", None),
            ("mail", "mailbox", existing_mail_cursor),
        )
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_test_support_source_uses_canonical_cursors_and_enabled_read_capabilities(
    database_url: str,
    tmp_path: Path,
) -> None:
    """E2E 合成来源必须满足真实同步仓储的规范游标与 enabled 能力前置条件。"""
    session_factory = build_session_factory(database_url)
    master_key_file = tmp_path / "master-key"
    master_key_file.write_text(
        "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        encoding="utf-8",
    )
    try:
        async with session_factory.begin() as session:
            user = _synthetic_user(ordinal="test-support-source")
            session.add(user)
            await session.flush()
            user_id = user.id

        service = SupportFixtureService(
            session_factory,
            object(),  # type: ignore[arg-type]
            app_master_key_file=master_key_file,
        )
        connection_id = await service.seed_google_source(user_id=user_id)

        async with session_factory() as session:
            cursor_rows = tuple(
                (
                    await session.execute(
                        select(
                            SyncCursorModel.resource_kind,
                            SyncCursorModel.scope_key,
                            SyncCursorModel.cursor,
                        )
                        .where(SyncCursorModel.connection_id == connection_id)
                        .order_by(SyncCursorModel.resource_kind, SyncCursorModel.scope_key)
                    )
                ).all()
            )
            capability_rows = tuple(
                (
                    await session.execute(
                        select(
                            ConnectionCapabilityModel.capability,
                            ConnectionCapabilityModel.status,
                            ConnectionCapabilityModel.actual_scopes,
                        )
                        .where(ConnectionCapabilityModel.connection_id == connection_id)
                        .order_by(ConnectionCapabilityModel.capability)
                    )
                ).all()
            )

        assert cursor_rows == (
            ("calendar", "primary", "synthetic-cursor"),
            ("mail", "mailbox", "synthetic-cursor"),
        )
        assert capability_rows == (
            ("calendar.read", "enabled", [GOOGLE_CALENDAR_READ_SCOPE]),
            ("mail.read", "enabled", [GOOGLE_GMAIL_READ_SCOPE]),
        )
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_opaque_calendar_id_is_shared_by_directory_default_and_event(
    database_url: str,
) -> None:
    """超过 M1 长度的 opaque 日历 ID 必须在目录、默认值与事件事实中无损一致。"""
    opaque_calendar_id = "calendar-" + ("c" * 300)
    assert 255 < len(opaque_calendar_id) <= 512
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _synthetic_user(ordinal="opaque-calendar")
            session.add(user)
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="google",
                provider_account_id="m2-opaque-calendar",
                account_email="opaque-calendar@example.test",
                scopes=[],
                status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()

            user.default_calendar_connection_id = connection.id
            user.default_calendar_id = opaque_calendar_id
            session.add_all(
                (
                    db_models.ProviderCalendarModel(
                        user_id=user.id,
                        connection_id=connection.id,
                        provider_calendar_id=opaque_calendar_id,
                        name="Synthetic opaque calendar",
                        timezone="UTC",
                        is_primary=True,
                        access_role="owner",
                        can_write=True,
                        provider_url=None,
                    ),
                    db_models.CalendarEventModel(
                        user_id=user.id,
                        connection_id=connection.id,
                        provider_event_id="opaque-calendar-event",
                        calendar_id=opaque_calendar_id,
                        title="Synthetic event",
                        description_ciphertext=None,
                        description_nonce=None,
                        description_key_version=None,
                        location_ciphertext=None,
                        location_nonce=None,
                        location_key_version=None,
                        starts_at=datetime(2026, 8, 7, 1, 0, tzinfo=UTC),
                        ends_at=datetime(2026, 8, 7, 2, 0, tzinfo=UTC),
                        all_day=False,
                        transparency="opaque",
                        status="confirmed",
                        timezone="UTC",
                        recurring_event_id=None,
                        etag="opaque-calendar-etag",
                        organizer=None,
                        attendees=None,
                        access_role="owner",
                        can_edit=True,
                        provider_url="https://example.test/opaque-calendar-event",
                        provider_updated_at=None,
                    ),
                )
            )
            user_id = user.id
            connection_id = connection.id

        async with session_factory() as session:
            stored_default = await session.scalar(
                select(UserModel.default_calendar_id).where(UserModel.id == user_id)
            )
            stored_directory_id = await session.scalar(
                select(db_models.ProviderCalendarModel.provider_calendar_id).where(
                    db_models.ProviderCalendarModel.connection_id == connection_id
                )
            )
            stored_event_id = await session.scalar(
                select(db_models.CalendarEventModel.calendar_id).where(
                    db_models.CalendarEventModel.connection_id == connection_id
                )
            )

        assert stored_default == opaque_calendar_id
        assert stored_directory_id == opaque_calendar_id
        assert stored_event_id == opaque_calendar_id
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_scoped_cursor_persists_opaque_delta_link_beyond_legacy_limit(
    database_url: str,
) -> None:
    """Microsoft Graph deltaLink 超过 512 字符时仍必须作为 opaque 游标完整持久化。"""
    delta_link = "https://graph.microsoft.example.test/v1.0/me/calendarView/delta?token=" + (
        "d" * 600
    )
    assert len(delta_link) > 512
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _synthetic_user(ordinal="opaque-delta-link")
            session.add(user)
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="microsoft",
                provider_account_id="m2-opaque-delta-link",
                provider_tenant_id="tenant-opaque-delta-link",
                account_type="personal",
                account_email="opaque-delta-link@example.test",
                scopes=[],
                status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()
            cursor = SyncCursorModel(
                connection_id=connection.id,
                resource_kind="calendar",
                scope_key="opaque-calendar-scope",
                cursor=delta_link,
            )
            session.add(cursor)
            await session.flush()
            cursor_id = cursor.id

        async with session_factory() as session:
            stored_delta_link = await session.scalar(
                select(SyncCursorModel.cursor).where(SyncCursorModel.id == cursor_id)
            )

        assert stored_delta_link == delta_link
    finally:
        await session_factory.dispose()


@pytest.mark.parametrize(
    "identity_fields",
    (
        {},
        {"provider_tenant_id": "", "account_type": "personal"},
        {"provider_tenant_id": "tenant-synthetic", "account_type": "google"},
    ),
)
@pytest.mark.asyncio
async def test_microsoft_connection_requires_normalized_tenant_and_account_type(
    database_url: str,
    identity_fields: dict[str, str],
) -> None:
    """显式 Microsoft 连接不得被 Google 兼容默认伪装成完整规范身份。"""
    session_factory = build_session_factory(database_url)
    try:
        with pytest.raises(IntegrityError) as raised:
            async with session_factory.begin() as session:
                user = _synthetic_user(ordinal=f"invalid-microsoft-{len(identity_fields)}")
                session.add(user)
                await session.flush()
                session.add(
                    OAuthConnectionModel(
                        user_id=user.id,
                        provider="microsoft",
                        provider_account_id=f"invalid-microsoft-{len(identity_fields)}",
                        account_email="invalid-microsoft@example.test",
                        scopes=[],
                        status="connected",
                        last_error_code=None,
                        **identity_fields,
                    )
                )
                await session.flush()

        assert _integrity_error_details(raised.value) == (
            "23514",
            MICROSOFT_IDENTITY_CHECK,
        )
    finally:
        await session_factory.dispose()


@pytest.mark.parametrize("account_type", ("personal", "work_school"))
@pytest.mark.asyncio
async def test_microsoft_normalized_account_types_are_accepted(
    database_url: str,
    account_type: str,
) -> None:
    """规格允许的个人和工作/学校身份都必须通过同一数据库真实性约束。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _synthetic_user(ordinal=f"valid-microsoft-{account_type}")
            session.add(user)
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="microsoft",
                provider_account_id=f"valid-microsoft-{account_type}",
                provider_tenant_id=f"tenant-{account_type}",
                account_type=account_type,
                account_email=f"valid-microsoft-{account_type}@example.test",
                scopes=[],
                status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id

        async with session_factory() as session:
            stored_account_type = await session.scalar(
                select(OAuthConnectionModel.account_type).where(
                    OAuthConnectionModel.id == connection_id
                )
            )
        assert stored_account_type == account_type
    finally:
        await session_factory.dispose()


def test_m1_google_rows_are_backfilled_without_inventing_source_facts(
    empty_migration_database: URL,
) -> None:
    """从 0010 升级时应保留游标，精确映射读 scope，并令未知写投影 fail-safe。"""
    config = _alembic_config(empty_migration_database)
    command.upgrade(config, "20260804_0010")

    user_id = uuid4()
    enabled_connection_id = uuid4()
    disabled_connection_id = uuid4()
    attempt_id = uuid4()
    gmail_cursor_id = uuid4()
    calendar_cursor_id = uuid4()
    thread_id = uuid4()
    message_id = uuid4()
    event_id = uuid4()
    now = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
    enabled_scopes = [
        GOOGLE_GMAIL_SEND_SCOPE,
        GOOGLE_GMAIL_READ_SCOPE,
        GOOGLE_CALENDAR_WRITE_SCOPE,
        GOOGLE_CALENDAR_READ_SCOPE,
        GOOGLE_GMAIL_READ_SCOPE,
    ]
    disabled_scopes = [
        f"{GOOGLE_GMAIL_READ_SCOPE}.extra",
        GOOGLE_GMAIL_SEND_SCOPE,
        GOOGLE_CALENDAR_WRITE_SCOPE,
    ]
    legacy_calendar_cursor = "legacy-calendar-" + ("c" * 496)
    assert len(legacy_calendar_cursor) == 512

    async def seed_m1_rows() -> None:
        """只用 0010 已存在列写入合成 M1 历史事实。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users ("
                        "email, display_name, password_hash, timezone, locale, brief_time, "
                        "is_active, id"
                        ") VALUES ("
                        ":email, :display_name, NULL, 'UTC', 'zh-CN', :brief_time, TRUE, :id"
                        ")"
                    ),
                    {
                        "email": "m1-backfill@example.test",
                        "display_name": "M1 Backfill",
                        "brief_time": time(8, 0),
                        "id": user_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO oauth_attempts ("
                        "state_hash, user_id, encrypted_pkce_verifier, nonce, key_version, "
                        "expires_at, consumed_at, created_at, id"
                        ") VALUES ("
                        ":state_hash, :user_id, :verifier, :nonce, 1, :expires_at, NULL, "
                        ":created_at, :id"
                        ")"
                    ),
                    {
                        "state_hash": b"a" * 32,
                        "user_id": user_id,
                        "verifier": b"synthetic-verifier",
                        "nonce": b"o" * 12,
                        "expires_at": now,
                        "created_at": now,
                        "id": attempt_id,
                    },
                )
                for connection_id, provider_account_id, scopes in (
                    (enabled_connection_id, "m1-enabled", enabled_scopes),
                    (disabled_connection_id, "m1-disabled", disabled_scopes),
                ):
                    await connection.execute(
                        text(
                            "INSERT INTO oauth_connections ("
                            "user_id, provider, provider_account_id, account_email, scopes, "
                            "status, last_error_code, id"
                            ") VALUES ("
                            ":user_id, 'google', :provider_account_id, :account_email, "
                            "CAST(:scopes AS jsonb), 'connected', NULL, :id"
                            ")"
                        ),
                        {
                            "user_id": user_id,
                            "provider_account_id": provider_account_id,
                            "account_email": f"{provider_account_id}@example.test",
                            "scopes": json.dumps(scopes),
                            "id": connection_id,
                        },
                    )
                for cursor_id, resource_kind, cursor_value in (
                    (gmail_cursor_id, "gmail", "gmail-history-42"),
                    (calendar_cursor_id, "calendar", legacy_calendar_cursor),
                ):
                    await connection.execute(
                        text(
                            "INSERT INTO sync_cursors ("
                            "connection_id, resource_kind, cursor, id"
                            ") VALUES (:connection_id, :resource_kind, :cursor, :id)"
                        ),
                        {
                            "connection_id": enabled_connection_id,
                            "resource_kind": resource_kind,
                            "cursor": cursor_value,
                            "id": cursor_id,
                        },
                    )
                await connection.execute(
                    text(
                        "INSERT INTO email_threads ("
                        "user_id, connection_id, provider_thread_id, subject, participants, "
                        "latest_message_at, provider_url, id"
                        ") VALUES ("
                        ":user_id, :connection_id, 'thread-1', 'Synthetic subject', "
                        "'[]'::jsonb, :latest_message_at, 'https://example.test/thread-1', :id"
                        ")"
                    ),
                    {
                        "user_id": user_id,
                        "connection_id": enabled_connection_id,
                        "latest_message_at": now,
                        "id": thread_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO email_messages ("
                        "user_id, thread_id, provider_message_id, received_at, sender, "
                        "recipients, subject, snippet, body_ciphertext, body_nonce, "
                        "body_key_version, labels, headers, provider_url, id"
                        ") VALUES ("
                        ":user_id, :thread_id, 'message-1', :received_at, "
                        "'{\"address\":\"sender@example.test\"}'::jsonb, '[]'::jsonb, "
                        "'Synthetic subject', 'Synthetic snippet', NULL, NULL, NULL, "
                        "'[]'::jsonb, '{}'::jsonb, 'https://example.test/message-1', :id"
                        ")"
                    ),
                    {
                        "user_id": user_id,
                        "thread_id": thread_id,
                        "received_at": now,
                        "id": message_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO calendar_events ("
                        "user_id, connection_id, provider_event_id, calendar_id, title, "
                        "starts_at, ends_at, all_day, transparency, status, timezone, etag, "
                        "provider_url, id"
                        ") VALUES ("
                        ":user_id, :connection_id, 'event-1', 'primary', 'Synthetic event', "
                        ":starts_at, :ends_at, FALSE, 'opaque', 'confirmed', 'UTC', "
                        "'etag-1', 'https://example.test/event-1', :id"
                        ")"
                    ),
                    {
                        "user_id": user_id,
                        "connection_id": enabled_connection_id,
                        "starts_at": now,
                        "ends_at": datetime(2026, 8, 7, 1, 0, tzinfo=UTC),
                        "id": event_id,
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_m1_rows())
    command.upgrade(config, M2_REVISION)

    async def read_backfill() -> dict[str, object]:
        """读取升级后的规范化结果，完整值均为合成测试数据。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                capability_rows = (
                    await connection.execute(
                        text(
                            "SELECT connection_id, capability, status, actual_scopes "
                            "FROM connection_capabilities ORDER BY connection_id, capability"
                        )
                    )
                ).all()
                cursor_rows = (
                    await connection.execute(
                        text(
                            "SELECT id, resource_kind, scope_key, cursor "
                            "FROM sync_cursors ORDER BY resource_kind"
                        )
                    )
                ).all()
                user_row = (
                    await connection.execute(
                        text(
                            "SELECT working_hours, meeting_buffer_minutes, "
                            "default_mail_connection_id, default_calendar_connection_id, "
                            "default_calendar_id FROM users WHERE id = :user_id"
                        ),
                        {"user_id": user_id},
                    )
                ).one()
                attempt_row = (
                    await connection.execute(
                        text(
                            "SELECT provider, requested_capabilities, oidc_nonce_hash "
                            "FROM oauth_attempts WHERE id = :attempt_id"
                        ),
                        {"attempt_id": attempt_id},
                    )
                ).one()
                connection_rows = (
                    await connection.execute(
                        text(
                            "SELECT id, provider_tenant_id, account_type "
                            "FROM oauth_connections ORDER BY id"
                        )
                    )
                ).all()
                message_row = (
                    await connection.execute(
                        text(
                            "SELECT internet_message_id, provider_conversation_id, sent_at, "
                            "mailbox_scope_key FROM email_messages WHERE id = :message_id"
                        ),
                        {"message_id": message_id},
                    )
                ).one()
                event_row = (
                    await connection.execute(
                        text(
                            "SELECT organizer, attendees, access_role, can_edit "
                            "FROM calendar_events WHERE id = :event_id"
                        ),
                        {"event_id": event_id},
                    )
                ).one()
                return {
                    "capabilities": capability_rows,
                    "cursors": cursor_rows,
                    "user": user_row,
                    "attempt": attempt_row,
                    "connections": connection_rows,
                    "message": message_row,
                    "event": event_row,
                }
        finally:
            await engine.dispose()

    result = asyncio.run(read_backfill())
    capabilities = {
        (row.connection_id, row.capability): (row.status, row.actual_scopes)
        for row in result["capabilities"]
    }
    normalized_enabled_scopes = sorted(set(enabled_scopes))
    normalized_disabled_scopes = sorted(set(disabled_scopes))
    assert len(capabilities) == 8
    assert capabilities[(enabled_connection_id, "mail.read")] == (
        "enabled",
        normalized_enabled_scopes,
    )
    assert capabilities[(enabled_connection_id, "calendar.read")] == (
        "enabled",
        normalized_enabled_scopes,
    )
    assert capabilities[(enabled_connection_id, "mail.send")] == (
        "disabled",
        normalized_enabled_scopes,
    )
    assert capabilities[(enabled_connection_id, "calendar.write")] == (
        "disabled",
        normalized_enabled_scopes,
    )
    for capability in ("mail.read", "calendar.read", "mail.send", "calendar.write"):
        assert capabilities[(disabled_connection_id, capability)] == (
            "disabled",
            normalized_disabled_scopes,
        )

    cursors = {row.resource_kind: (row.id, row.scope_key, row.cursor) for row in result["cursors"]}
    assert cursors == {
        "calendar": (calendar_cursor_id, "primary", legacy_calendar_cursor),
        "gmail": (gmail_cursor_id, "mailbox", "gmail-history-42"),
    }
    user_row = result["user"]
    assert user_row.working_hours == DEFAULT_WORKING_HOURS
    assert user_row.meeting_buffer_minutes == 10
    assert user_row.default_mail_connection_id is None
    assert user_row.default_calendar_connection_id is None
    assert user_row.default_calendar_id is None
    attempt_row = result["attempt"]
    assert attempt_row.provider == "google"
    assert attempt_row.requested_capabilities == []
    assert attempt_row.oidc_nonce_hash is None
    assert {(row.provider_tenant_id, row.account_type) for row in result["connections"]} == {
        ("", "google")
    }
    message_row = result["message"]
    assert message_row.internet_message_id is None
    assert message_row.provider_conversation_id is None
    assert message_row.sent_at is None
    assert message_row.mailbox_scope_key == "mailbox"
    event_row = result["event"]
    assert event_row.organizer is None
    assert event_row.attendees is None
    assert event_row.access_role is None
    assert event_row.can_edit is False


@pytest.mark.asyncio
async def test_cross_user_connection_capability_is_rejected(database_url: str) -> None:
    """能力记录的显式用户与连接拥有者不一致时必须由命名组合外键拒绝。"""
    session_factory = build_session_factory(database_url)
    try:
        first_user_id, _, _, second_connection_id = await _seed_two_users_and_connections(
            session_factory
        )
        flush_completed = False
        with pytest.raises(IntegrityError) as raised:
            async with session_factory.begin() as session:
                session.add(
                    db_models.ConnectionCapabilityModel(
                        user_id=first_user_id,
                        connection_id=second_connection_id,
                        capability="mail.read",
                        status="disabled",
                        actual_scopes=[],
                        last_verified_at=None,
                        last_error_code=None,
                    )
                )
                await session.flush()
                flush_completed = True

        assert flush_completed is True
        assert _integrity_error_details(raised.value) == (
            "23503",
            CAPABILITY_CONNECTION_OWNER_FK,
        )
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_cross_user_provider_calendar_is_rejected(database_url: str) -> None:
    """日历目录记录不能把一个用户与另一个用户的连接拼接为伪造归属。"""
    session_factory = build_session_factory(database_url)
    try:
        first_user_id, _, _, second_connection_id = await _seed_two_users_and_connections(
            session_factory
        )
        flush_completed = False
        with pytest.raises(IntegrityError) as raised:
            async with session_factory.begin() as session:
                session.add(
                    db_models.ProviderCalendarModel(
                        user_id=first_user_id,
                        connection_id=second_connection_id,
                        provider_calendar_id="calendar-synthetic",
                        name="Synthetic calendar",
                        timezone="UTC",
                        is_primary=False,
                        access_role="reader",
                        can_write=False,
                        provider_url=None,
                    )
                )
                await session.flush()
                flush_completed = True

        assert flush_completed is True
        assert _integrity_error_details(raised.value) == (
            "23503",
            PROVIDER_CALENDAR_CONNECTION_OWNER_FK,
        )
    finally:
        await session_factory.dispose()


@pytest.mark.parametrize(
    ("default_kind", "constraint_name"),
    (
        ("mail", DEFAULT_MAIL_CONNECTION_OWNER_FK),
        ("calendar", DEFAULT_CALENDAR_CONNECTION_OWNER_FK),
    ),
)
@pytest.mark.asyncio
async def test_cross_user_default_connection_is_rejected(
    database_url: str,
    default_kind: str,
    constraint_name: str,
) -> None:
    """用户默认邮件或日历连接都必须与该用户自身的 ``id`` 组合匹配。"""
    session_factory = build_session_factory(database_url)
    try:
        first_user_id, _, _, second_connection_id = await _seed_two_users_and_connections(
            session_factory
        )
        flush_completed = False
        with pytest.raises(IntegrityError) as raised:
            async with session_factory.begin() as session:
                user = await session.get(UserModel, first_user_id)
                assert user is not None
                if default_kind == "mail":
                    user.default_mail_connection_id = second_connection_id
                else:
                    user.default_calendar_connection_id = second_connection_id
                    user.default_calendar_id = "calendar-synthetic"
                await session.flush()
                flush_completed = True

        assert flush_completed is True
        assert _integrity_error_details(raised.value) == ("23503", constraint_name)
    finally:
        await session_factory.dispose()
