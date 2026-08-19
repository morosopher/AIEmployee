"""验证 Alembic 能从空数据库升级到当前任务持久化 Schema 且无元数据漂移。"""

import asyncio
import inspect
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import URL, Engine, create_engine, event, text
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
from sqlalchemy.engine import Connection, RootTransaction
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.application.ports.calendar_aad_migration_guard import (
    CalendarAadMigrationInvariantError,
)
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.mail import MailMessage, MailMessageUpsertResult
from ai_employee.infrastructure.db.alembic import (
    AlembicMigrationInvariantError,
    load_published_alembic_authority,
    set_alembic_database_url,
)
from ai_employee.infrastructure.db.database_access import _ROLE_MEMBERSHIPS_SQL
from ai_employee.infrastructure.db.database_maintenance import DATABASE_RESTORE_FACTS_SQL
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyMailSyncRepository
from tests.integration.alembic_commands import (
    run_alembic_check,
    run_alembic_downgrade,
    run_alembic_stamp,
    run_alembic_upgrade,
)

_CALENDAR_AAD_REVISION = "20260809_0019"
_CALENDAR_AAD_GUARD_ATTRIBUTE = "calendar_aad_0019_guard"


def _calendar_aad_config(database_url: URL, guard: object | None = None) -> Config:
    """构造指向 disposable target 的 0019 配置，并仅通过冻结 attribute 注入 guard。"""
    config = Config(Path(__file__).resolve().parents[3] / "alembic.ini")
    set_alembic_database_url(config, database_url.render_as_string(hide_password=False))
    if guard is not None:
        config.attributes[_CALENDAR_AAD_GUARD_ATTRIBUTE] = guard
    return config


@dataclass(slots=True)
class _CalendarAadMigrationGuardFake:
    """记录 0019 两阶段调用，并在最终阶段从同一事务读取版本和目标授权。"""

    reject_before_commit: bool = False
    calls: list[tuple[Connection, RootTransaction, str]] = field(default_factory=list)
    observed_revision: str | None = None

    def verify(self, *, connection: Connection, phase: str) -> None:
        """只接受冻结阶段；最终阶段必须已经看见版本行和完整 destination grants。"""
        assert isinstance(connection, Connection)
        assert phase in {"before_mutation", "before_commit"}
        transaction = connection.get_transaction()
        assert isinstance(transaction, RootTransaction)
        assert transaction.is_active
        self.calls.append((connection, transaction, phase))
        if phase == "before_commit":
            from ai_employee.infrastructure.db.database_grants import (
                GrantPhase,
                verify_object_grants,
            )

            self.observed_revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            verify_object_grants(
                connection,
                revision=_CALENDAR_AAD_REVISION,
                phase=GrantPhase.BASELINE,
            )
            if self.reject_before_commit:
                raise RuntimeError("synthetic final calendar AAD guard rejection")


@dataclass(slots=True)
class _ReplacingCalendarAadMigrationGuardFake:
    """首阶段替换 Config attribute，用于证明两阶段只消费预先冻结的同一实例。"""

    config: Config
    replacement: _CalendarAadMigrationGuardFake
    calls: list[tuple[Connection, RootTransaction, str]] = field(default_factory=list)

    def verify(self, *, connection: Connection, phase: str) -> None:
        """记录调用；mutation 前替换 attribute，正确 final callback 仍必须调用本实例。"""
        assert isinstance(connection, Connection)
        assert phase in {"before_mutation", "before_commit"}
        transaction = connection.get_transaction()
        assert isinstance(transaction, RootTransaction)
        assert transaction.is_active
        self.calls.append((connection, transaction, phase))
        if phase == "before_mutation":
            self.config.attributes[_CALENDAR_AAD_GUARD_ATTRIBUTE] = self.replacement
        else:
            from ai_employee.infrastructure.db.database_grants import (
                GrantPhase,
                verify_object_grants,
            )

            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == _CALENDAR_AAD_REVISION
            )
            verify_object_grants(
                connection,
                revision=_CALENDAR_AAD_REVISION,
                phase=GrantPhase.BASELINE,
            )


@dataclass(slots=True)
class _ReturningCalendarAadMigrationGuardFake:
    """在指定阶段返回同步非 None 或 lazy 结果，用于冻结调用点契约。"""

    return_phase: str
    lazy: bool = False
    calls: list[tuple[Connection, RootTransaction, str]] = field(default_factory=list)

    def verify(self, *, connection: Connection, phase: str) -> object | None:
        """记录同一事务，并只在目标阶段返回应被调用点拒绝的结果。"""
        assert isinstance(connection, Connection)
        assert phase in {"before_mutation", "before_commit"}
        transaction = connection.get_transaction()
        assert isinstance(transaction, RootTransaction)
        assert transaction.is_active
        self.calls.append((connection, transaction, phase))
        if phase != self.return_phase:
            return None
        if self.lazy:
            return self._lazy_result()
        return object()

    def _lazy_result(self) -> Iterator[object]:
        """返回未启动 generator；调用点只能关闭，不能执行其 body。"""
        yield object()


@dataclass(frozen=True, slots=True)
class _CalendarAad0019Fixture:
    """保存顶部 0019 migration tests 共用的 disposable 合成事实与同步引擎。"""

    engine: Engine
    user_id: str
    connection_id: str
    event_id: str


@dataclass(frozen=True, slots=True)
class _CalendarAad0019DataMatrix:
    """保存 0019 exact-pair 成功矩阵的稳定身份与 disposable 引擎。"""

    engine: Engine
    user_id: str
    connection_a_id: str
    connection_b_id: str
    shared_calendar_id: str
    unrelated_calendar_id: str
    event1_id: str
    event2_id: str
    unaffected_a_id: str
    unaffected_b_id: str


class _CalendarAad0019PartialTripleCase(str, Enum):
    """枚举本批次唯一覆盖的七种历史字段 partial 形状。"""

    DESCRIPTION_CIPHERTEXT_ONLY = "description_ciphertext_only"
    DESCRIPTION_NONCE_ONLY = "description_nonce_only"
    DESCRIPTION_KEY_ONLY = "description_key_only"
    DESCRIPTION_CIPHERTEXT_NONCE = "description_ciphertext_nonce"
    DESCRIPTION_CIPHERTEXT_KEY = "description_ciphertext_key"
    DESCRIPTION_NONCE_KEY = "description_nonce_key"
    LOCATION_CIPHERTEXT_NONCE = "location_ciphertext_nonce"


class _CalendarAad0019AccessRecoverabilityCase(str, Enum):
    """枚举 affected pair 必须具备的八种本地访问与目录事实缺口。"""

    MISSING_CURSOR = "missing_cursor"
    DISCONNECTED_CONNECTION = "disconnected_connection"
    DISABLED_CALENDAR_READ = "disabled_calendar_read"
    REVOKED_CALENDAR_READ = "revoked_calendar_read"
    MISSING_ACCESS_CREDENTIAL = "missing_access_credential"
    MISSING_REFRESH_CREDENTIAL = "missing_refresh_credential"
    MISSING_PROVIDER_CALENDAR = "missing_provider_calendar"
    PROVIDER_CALENDAR_ID_MISMATCH = "provider_calendar_id_mismatch"


@dataclass(frozen=True, slots=True)
class _CalendarAad0019ConstraintValues:
    """保存 CalendarEvent 两个独立四列 AEAD/AAD 组的一组精确测试值。"""

    description_ciphertext: bytes | None
    description_nonce: bytes | None
    description_key_version: int | None
    description_aad_version: int | None
    location_ciphertext: bytes | None
    location_nonce: bytes | None
    location_key_version: int | None
    location_aad_version: int | None


def _seed_calendar_aad_0019_data_matrix(database_url: URL) -> _CalendarAad0019DataMatrix:
    """种入只影响 connection A/shared pair 的完整 0018 历史事实矩阵。"""
    matrix = _CalendarAad0019DataMatrix(
        engine=create_engine(
            database_url.set(drivername="postgresql+psycopg"),
            poolclass=NullPool,
            hide_parameters=True,
        ),
        user_id="00000000-0000-0000-0000-000000001920",
        connection_a_id="00000000-0000-0000-0000-000000001921",
        connection_b_id="00000000-0000-0000-0000-000000001922",
        shared_calendar_id="共享:日历/α:β",
        unrelated_calendar_id="unrelated:日历/γ",
        event1_id="00000000-0000-0000-0000-000000001931",
        event2_id="00000000-0000-0000-0000-000000001932",
        unaffected_a_id="00000000-0000-0000-0000-000000001933",
        unaffected_b_id="00000000-0000-0000-0000-000000001934",
    )
    try:
        with matrix.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users ("
                    "email, display_name, password_hash, timezone, locale, brief_time, "
                    "is_active, id"
                    ") VALUES ("
                    "'calendar-aad-matrix@example.test', 'Calendar AAD Matrix', NULL, "
                    "'UTC', 'zh-CN', '08:00', TRUE, CAST(:user_id AS uuid)"
                    ")"
                ),
                {"user_id": matrix.user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO oauth_connections ("
                    "user_id, provider, provider_account_id, account_email, scopes, status, "
                    "last_error_code, id"
                    ") VALUES ("
                    "CAST(:user_id AS uuid), 'google', :provider_account_id, :account_email, "
                    "'[]'::jsonb, 'connected', NULL, CAST(:connection_id AS uuid)"
                    ")"
                ),
                (
                    {
                        "user_id": matrix.user_id,
                        "provider_account_id": "calendar-aad-matrix-a",
                        "account_email": "calendar-aad-matrix-a@example.test",
                        "connection_id": matrix.connection_a_id,
                    },
                    {
                        "user_id": matrix.user_id,
                        "provider_account_id": "calendar-aad-matrix-b",
                        "account_email": "calendar-aad-matrix-b@example.test",
                        "connection_id": matrix.connection_b_id,
                    },
                ),
            )
            connection.execute(
                text(
                    "INSERT INTO connection_capabilities ("
                    "user_id, connection_id, capability, status, actual_scopes, "
                    "last_verified_at, last_error_code, id"
                    ") VALUES ("
                    "CAST(:user_id AS uuid), CAST(:connection_id AS uuid), 'calendar.read', "
                    "'enabled', '[]'::jsonb, :last_verified_at, NULL, CAST(:id AS uuid)"
                    ")"
                ),
                (
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_a_id,
                        "last_verified_at": datetime(2030, 1, 1, 0, 1, tzinfo=UTC),
                        "id": "00000000-0000-0000-0000-000000001941",
                    },
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_b_id,
                        "last_verified_at": datetime(2030, 1, 1, 0, 2, tzinfo=UTC),
                        "id": "00000000-0000-0000-0000-000000001942",
                    },
                ),
            )
            connection.execute(
                text(
                    "INSERT INTO encrypted_credentials ("
                    "user_id, connection_id, credential_kind, ciphertext, nonce, key_version, "
                    "token_expires_at, id"
                    ") VALUES ("
                    "CAST(:user_id AS uuid), CAST(:connection_id AS uuid), :credential_kind, "
                    ":ciphertext, :nonce, :key_version, :token_expires_at, CAST(:id AS uuid)"
                    ")"
                ),
                (
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_a_id,
                        "credential_kind": "access_token",
                        "ciphertext": b"matrix-a-access-ciphertext",
                        "nonce": b"matrix-a-acc",
                        "key_version": 11,
                        "token_expires_at": datetime(2035, 1, 1, tzinfo=UTC),
                        "id": "00000000-0000-0000-0000-000000001951",
                    },
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_a_id,
                        "credential_kind": "refresh_token",
                        "ciphertext": b"matrix-a-refresh-ciphertext",
                        "nonce": b"matrix-a-ref",
                        "key_version": 12,
                        "token_expires_at": None,
                        "id": "00000000-0000-0000-0000-000000001952",
                    },
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_b_id,
                        "credential_kind": "access_token",
                        "ciphertext": b"matrix-b-access-ciphertext",
                        "nonce": b"matrix-b-acc",
                        "key_version": 21,
                        "token_expires_at": datetime(2035, 2, 1, tzinfo=UTC),
                        "id": "00000000-0000-0000-0000-000000001953",
                    },
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_b_id,
                        "credential_kind": "refresh_token",
                        "ciphertext": b"matrix-b-refresh-ciphertext",
                        "nonce": b"matrix-b-ref",
                        "key_version": 22,
                        "token_expires_at": None,
                        "id": "00000000-0000-0000-0000-000000001954",
                    },
                ),
            )
            connection.execute(
                text(
                    "INSERT INTO provider_calendars ("
                    "user_id, connection_id, provider_calendar_id, name, timezone, is_primary, "
                    "access_role, can_write, provider_url, id"
                    ") VALUES ("
                    "CAST(:user_id AS uuid), CAST(:connection_id AS uuid), :calendar_id, "
                    ":name, :timezone, :is_primary, :access_role, :can_write, :provider_url, "
                    "CAST(:id AS uuid)"
                    ")"
                ),
                (
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_a_id,
                        "calendar_id": matrix.shared_calendar_id,
                        "name": "A shared 日历",
                        "timezone": "Asia/Shanghai",
                        "is_primary": True,
                        "access_role": "owner",
                        "can_write": True,
                        "provider_url": "https://calendar.example.test/a/shared",
                        "id": "00000000-0000-0000-0000-000000001961",
                    },
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_b_id,
                        "calendar_id": matrix.shared_calendar_id,
                        "name": "B shared 日历",
                        "timezone": "Europe/Paris",
                        "is_primary": False,
                        "access_role": "reader",
                        "can_write": False,
                        "provider_url": "https://calendar.example.test/b/shared",
                        "id": "00000000-0000-0000-0000-000000001962",
                    },
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_a_id,
                        "calendar_id": matrix.unrelated_calendar_id,
                        "name": "A unrelated 日历",
                        "timezone": "America/New_York",
                        "is_primary": False,
                        "access_role": "writer",
                        "can_write": True,
                        "provider_url": "https://calendar.example.test/a/unrelated",
                        "id": "00000000-0000-0000-0000-000000001963",
                    },
                ),
            )
            connection.execute(
                text(
                    "INSERT INTO sync_cursors ("
                    "connection_id, resource_kind, scope_key, cursor, last_success_at, "
                    "last_attempt_at, last_error_code, id"
                    ") VALUES ("
                    "CAST(:connection_id AS uuid), 'calendar', :scope_key, :cursor, "
                    ":last_success_at, :last_attempt_at, :last_error_code, CAST(:id AS uuid)"
                    ")"
                ),
                (
                    {
                        "connection_id": matrix.connection_a_id,
                        "scope_key": matrix.shared_calendar_id,
                        "cursor": "cursor-a-shared-原始",
                        "last_success_at": datetime(2030, 2, 1, 1, tzinfo=UTC),
                        "last_attempt_at": datetime(2030, 2, 1, 2, tzinfo=UTC),
                        "last_error_code": "synthetic-a-shared-before",
                        "id": "00000000-0000-0000-0000-000000001971",
                    },
                    {
                        "connection_id": matrix.connection_b_id,
                        "scope_key": matrix.shared_calendar_id,
                        "cursor": "cursor-b-shared-原始",
                        "last_success_at": datetime(2030, 2, 2, 1, tzinfo=UTC),
                        "last_attempt_at": datetime(2030, 2, 2, 2, tzinfo=UTC),
                        "last_error_code": "synthetic-b-shared-before",
                        "id": "00000000-0000-0000-0000-000000001972",
                    },
                    {
                        "connection_id": matrix.connection_a_id,
                        "scope_key": matrix.unrelated_calendar_id,
                        "cursor": "cursor-a-unrelated-原始",
                        "last_success_at": datetime(2030, 2, 3, 1, tzinfo=UTC),
                        "last_attempt_at": datetime(2030, 2, 3, 2, tzinfo=UTC),
                        "last_error_code": "synthetic-a-unrelated-before",
                        "id": "00000000-0000-0000-0000-000000001973",
                    },
                    {
                        "connection_id": matrix.connection_a_id,
                        "scope_key": "directory",
                        "cursor": "directory-a-populated",
                        "last_success_at": datetime(2030, 2, 4, 1, tzinfo=UTC),
                        "last_attempt_at": datetime(2030, 2, 4, 2, tzinfo=UTC),
                        "last_error_code": "directory-a-before",
                        "id": "00000000-0000-0000-0000-000000001974",
                    },
                    {
                        "connection_id": matrix.connection_b_id,
                        "scope_key": "directory",
                        "cursor": "directory-b-populated",
                        "last_success_at": datetime(2030, 2, 5, 1, tzinfo=UTC),
                        "last_attempt_at": datetime(2030, 2, 5, 2, tzinfo=UTC),
                        "last_error_code": "directory-b-before",
                        "id": "00000000-0000-0000-0000-000000001975",
                    },
                ),
            )
            connection.execute(
                text(
                    "INSERT INTO calendar_events ("
                    "user_id, connection_id, provider_event_id, calendar_id, title, "
                    "description_ciphertext, description_nonce, description_key_version, "
                    "location_ciphertext, location_nonce, location_key_version, starts_at, "
                    "ends_at, all_day, transparency, status, timezone, recurring_event_id, "
                    "etag, organizer, attendees, access_role, can_edit, provider_url, "
                    "provider_updated_at, id"
                    ") VALUES ("
                    "CAST(:user_id AS uuid), CAST(:connection_id AS uuid), :provider_event_id, "
                    ":calendar_id, :title, :description_ciphertext, :description_nonce, "
                    ":description_key_version, :location_ciphertext, :location_nonce, "
                    ":location_key_version, :starts_at, :ends_at, :all_day, :transparency, "
                    ":status, :timezone, :recurring_event_id, :etag, CAST(:organizer AS jsonb), "
                    "CAST(:attendees AS jsonb), :access_role, :can_edit, :provider_url, "
                    ":provider_updated_at, CAST(:id AS uuid)"
                    ")"
                ),
                (
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_a_id,
                        "provider_event_id": "shared:event/α",
                        "calendar_id": matrix.shared_calendar_id,
                        "title": "A event1 描述",
                        "description_ciphertext": b"a-event1-description-ciphertext",
                        "description_nonce": b"a1-desc-001!",
                        "description_key_version": 31,
                        "location_ciphertext": None,
                        "location_nonce": None,
                        "location_key_version": None,
                        "starts_at": datetime(2031, 1, 1, 1, tzinfo=UTC),
                        "ends_at": datetime(2031, 1, 1, 2, tzinfo=UTC),
                        "all_day": False,
                        "transparency": "opaque",
                        "status": "confirmed",
                        "timezone": "Asia/Shanghai",
                        "recurring_event_id": None,
                        "etag": "etag-a-event1",
                        "organizer": '{"email":"organizer-a1@example.test"}',
                        "attendees": '[{"email":"attendee-a1@example.test"}]',
                        "access_role": "owner",
                        "can_edit": True,
                        "provider_url": "https://calendar.example.test/a/event1",
                        "provider_updated_at": datetime(2030, 12, 1, 1, tzinfo=UTC),
                        "id": matrix.event1_id,
                    },
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_a_id,
                        "provider_event_id": "second:event/β",
                        "calendar_id": matrix.shared_calendar_id,
                        "title": "A event2 双字段",
                        "description_ciphertext": b"a-event2-description-ciphertext",
                        "description_nonce": b"a2-desc-002!",
                        "description_key_version": 32,
                        "location_ciphertext": b"a-event2-location-ciphertext",
                        "location_nonce": b"a2-locat-002",
                        "location_key_version": 42,
                        "starts_at": datetime(2031, 1, 2, 3, tzinfo=UTC),
                        "ends_at": datetime(2031, 1, 2, 4, tzinfo=UTC),
                        "all_day": True,
                        "transparency": "transparent",
                        "status": "tentative",
                        "timezone": "Europe/Paris",
                        "recurring_event_id": "series-a-event2",
                        "etag": "etag-a-event2",
                        "organizer": '{"email":"organizer-a2@example.test"}',
                        "attendees": '[{"email":"attendee-a2@example.test"}]',
                        "access_role": "writer",
                        "can_edit": False,
                        "provider_url": "https://calendar.example.test/a/event2",
                        "provider_updated_at": datetime(2030, 12, 2, 2, tzinfo=UTC),
                        "id": matrix.event2_id,
                    },
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_a_id,
                        "provider_event_id": "empty:event/γ",
                        "calendar_id": matrix.shared_calendar_id,
                        "title": "A unaffected empty",
                        "description_ciphertext": None,
                        "description_nonce": None,
                        "description_key_version": None,
                        "location_ciphertext": None,
                        "location_nonce": None,
                        "location_key_version": None,
                        "starts_at": datetime(2031, 1, 3, 5, tzinfo=UTC),
                        "ends_at": datetime(2031, 1, 3, 6, tzinfo=UTC),
                        "all_day": False,
                        "transparency": "opaque",
                        "status": "cancelled",
                        "timezone": "UTC",
                        "recurring_event_id": None,
                        "etag": "etag-a-empty",
                        "organizer": None,
                        "attendees": "[]",
                        "access_role": "reader",
                        "can_edit": False,
                        "provider_url": "https://calendar.example.test/a/empty",
                        "provider_updated_at": datetime(2030, 12, 3, 3, tzinfo=UTC),
                        "id": matrix.unaffected_a_id,
                    },
                    {
                        "user_id": matrix.user_id,
                        "connection_id": matrix.connection_b_id,
                        "provider_event_id": "shared:event/α",
                        "calendar_id": matrix.shared_calendar_id,
                        "title": "B same opaque identity empty",
                        "description_ciphertext": None,
                        "description_nonce": None,
                        "description_key_version": None,
                        "location_ciphertext": None,
                        "location_nonce": None,
                        "location_key_version": None,
                        "starts_at": datetime(2031, 1, 4, 7, tzinfo=UTC),
                        "ends_at": datetime(2031, 1, 4, 8, tzinfo=UTC),
                        "all_day": False,
                        "transparency": "opaque",
                        "status": "confirmed",
                        "timezone": "America/New_York",
                        "recurring_event_id": "series-b-shared",
                        "etag": "etag-b-shared",
                        "organizer": '{"email":"organizer-b@example.test"}',
                        "attendees": '[{"email":"attendee-b@example.test"}]',
                        "access_role": "reader",
                        "can_edit": False,
                        "provider_url": "https://calendar.example.test/b/shared",
                        "provider_updated_at": datetime(2030, 12, 4, 4, tzinfo=UTC),
                        "id": matrix.unaffected_b_id,
                    },
                ),
            )
    except Exception:
        matrix.engine.dispose()
        raise
    return matrix


def _calendar_aad_0019_matrix_rows(
    matrix: _CalendarAad0019DataMatrix,
    *,
    table: str,
) -> tuple[tuple[tuple[str, object], ...], ...]:
    """读取事件或游标的完整有序行，并只从事件投影排除新增 AAD 版本列。"""
    if table == "calendar_events":
        statement = text(
            "SELECT * FROM calendar_events WHERE user_id = CAST(:user_id AS uuid) "
            "ORDER BY connection_id, calendar_id, provider_event_id, id"
        )
        parameters = {"user_id": matrix.user_id}
        excluded = {"description_aad_version", "location_aad_version"}
    elif table == "sync_cursors":
        statement = text(
            "SELECT * FROM sync_cursors WHERE connection_id IN ("
            "CAST(:connection_a_id AS uuid), CAST(:connection_b_id AS uuid)"
            ") ORDER BY connection_id, resource_kind, scope_key, id"
        )
        parameters = {
            "connection_a_id": matrix.connection_a_id,
            "connection_b_id": matrix.connection_b_id,
        }
        excluded = set()
    else:
        raise AssertionError("unknown calendar AAD matrix table")
    with matrix.engine.connect() as connection:
        return tuple(
            tuple((key, value) for key, value in row._mapping.items() if key not in excluded)
            for row in connection.execute(statement, parameters)
        )


def _calendar_aad_0019_expected_cursors(
    before: tuple[tuple[tuple[str, object], ...], ...],
    *,
    matrix: _CalendarAad0019DataMatrix,
) -> tuple[tuple[tuple[str, object], ...], ...]:
    """只为 A/shared exact non-directory pair 构造三个字段变化的完整预期行。"""
    expected: list[tuple[tuple[str, object], ...]] = []
    for row in before:
        values = dict(row)
        if (
            str(values["connection_id"]) == matrix.connection_a_id
            and values["resource_kind"] == "calendar"
            and values["scope_key"] == matrix.shared_calendar_id
        ):
            values.update(
                cursor=None,
                last_success_at=None,
                last_error_code="calendar_event_resync_required",
            )
        expected.append(tuple((key, values[key]) for key, _value in row))
    return tuple(expected)


def _apply_calendar_aad_0019_partial_triple(
    matrix: _CalendarAad0019DataMatrix,
    partial_case: _CalendarAad0019PartialTripleCase,
) -> None:
    """把指定历史字段改成 partial 三元组，同时保留其余本地恢复事实。"""
    description_shapes: dict[
        _CalendarAad0019PartialTripleCase,
        tuple[bytes | None, bytes | None, int | None],
    ] = {
        _CalendarAad0019PartialTripleCase.DESCRIPTION_CIPHERTEXT_ONLY: (
            b"partial-description-ciphertext",
            None,
            None,
        ),
        _CalendarAad0019PartialTripleCase.DESCRIPTION_NONCE_ONLY: (
            None,
            b"partialnonce",
            None,
        ),
        _CalendarAad0019PartialTripleCase.DESCRIPTION_KEY_ONLY: (None, None, 73),
        _CalendarAad0019PartialTripleCase.DESCRIPTION_CIPHERTEXT_NONCE: (
            b"partial-description-ciphertext",
            b"partialnonce",
            None,
        ),
        _CalendarAad0019PartialTripleCase.DESCRIPTION_CIPHERTEXT_KEY: (
            b"partial-description-ciphertext",
            None,
            73,
        ),
        _CalendarAad0019PartialTripleCase.DESCRIPTION_NONCE_KEY: (
            None,
            b"partialnonce",
            73,
        ),
    }
    with matrix.engine.begin() as connection:
        if partial_case is _CalendarAad0019PartialTripleCase.LOCATION_CIPHERTEXT_NONCE:
            connection.execute(
                text(
                    "UPDATE calendar_events SET "
                    "location_ciphertext = :ciphertext, location_nonce = :nonce, "
                    "location_key_version = NULL WHERE id = CAST(:event_id AS uuid)"
                ),
                {
                    "ciphertext": b"partial-location-ciphertext",
                    "nonce": b"partiallocat",
                    "event_id": matrix.event2_id,
                },
            )
            return
        ciphertext, nonce, key_version = description_shapes[partial_case]
        connection.execute(
            text(
                "UPDATE calendar_events SET "
                "description_ciphertext = :ciphertext, description_nonce = :nonce, "
                "description_key_version = :key_version "
                "WHERE id = CAST(:event_id AS uuid)"
            ),
            {
                "ciphertext": ciphertext,
                "nonce": nonce,
                "key_version": key_version,
                "event_id": matrix.event1_id,
            },
        )


def _apply_calendar_aad_0019_access_recoverability_gap(
    matrix: _CalendarAad0019DataMatrix,
    recoverability_case: _CalendarAad0019AccessRecoverabilityCase,
) -> None:
    """精确破坏 A/shared affected pair 的一个访问或目录恢复前提。"""
    with matrix.engine.begin() as connection:
        if recoverability_case is _CalendarAad0019AccessRecoverabilityCase.MISSING_CURSOR:
            result = connection.execute(
                text(
                    "DELETE FROM sync_cursors WHERE connection_id = CAST(:connection_id AS uuid) "
                    "AND resource_kind = 'calendar' AND scope_key = :calendar_id"
                ),
                {
                    "connection_id": matrix.connection_a_id,
                    "calendar_id": matrix.shared_calendar_id,
                },
            )
        elif (
            recoverability_case
            is _CalendarAad0019AccessRecoverabilityCase.DISCONNECTED_CONNECTION
        ):
            result = connection.execute(
                text(
                    "UPDATE oauth_connections SET status = 'disconnected' "
                    "WHERE id = CAST(:connection_id AS uuid)"
                ),
                {"connection_id": matrix.connection_a_id},
            )
        elif recoverability_case in {
            _CalendarAad0019AccessRecoverabilityCase.DISABLED_CALENDAR_READ,
            _CalendarAad0019AccessRecoverabilityCase.REVOKED_CALENDAR_READ,
        }:
            capability_status = (
                "disabled"
                if recoverability_case
                is _CalendarAad0019AccessRecoverabilityCase.DISABLED_CALENDAR_READ
                else "revoked"
            )
            result = connection.execute(
                text(
                    "UPDATE connection_capabilities SET status = :status "
                    "WHERE connection_id = CAST(:connection_id AS uuid) "
                    "AND capability = 'calendar.read'"
                ),
                {
                    "status": capability_status,
                    "connection_id": matrix.connection_a_id,
                },
            )
        elif recoverability_case in {
            _CalendarAad0019AccessRecoverabilityCase.MISSING_ACCESS_CREDENTIAL,
            _CalendarAad0019AccessRecoverabilityCase.MISSING_REFRESH_CREDENTIAL,
        }:
            credential_kind = (
                "access_token"
                if recoverability_case
                is _CalendarAad0019AccessRecoverabilityCase.MISSING_ACCESS_CREDENTIAL
                else "refresh_token"
            )
            result = connection.execute(
                text(
                    "DELETE FROM encrypted_credentials "
                    "WHERE connection_id = CAST(:connection_id AS uuid) "
                    "AND credential_kind = :credential_kind"
                ),
                {
                    "connection_id": matrix.connection_a_id,
                    "credential_kind": credential_kind,
                },
            )
        elif (
            recoverability_case
            is _CalendarAad0019AccessRecoverabilityCase.MISSING_PROVIDER_CALENDAR
        ):
            result = connection.execute(
                text(
                    "DELETE FROM provider_calendars "
                    "WHERE connection_id = CAST(:connection_id AS uuid) "
                    "AND provider_calendar_id = :calendar_id"
                ),
                {
                    "connection_id": matrix.connection_a_id,
                    "calendar_id": matrix.shared_calendar_id,
                },
            )
        elif (
            recoverability_case
            is _CalendarAad0019AccessRecoverabilityCase.PROVIDER_CALENDAR_ID_MISMATCH
        ):
            # 0018 的组合归属 FK 不允许伪造跨用户目录行，因此保留行并改为不精确 opaque ID。
            result = connection.execute(
                text(
                    "UPDATE provider_calendars SET provider_calendar_id = :mismatched_id "
                    "WHERE connection_id = CAST(:connection_id AS uuid) "
                    "AND provider_calendar_id = :calendar_id"
                ),
                {
                    "mismatched_id": f"{matrix.shared_calendar_id}:mismatched",
                    "connection_id": matrix.connection_a_id,
                    "calendar_id": matrix.shared_calendar_id,
                },
            )
        else:
            raise AssertionError("unknown calendar AAD access recoverability case")
        assert result.rowcount == 1


def _calendar_aad_0019_partial_fingerprint(
    matrix: _CalendarAad0019DataMatrix,
) -> tuple[object, ...]:
    """读取 partial 拒绝前后的全量业务行、事件 Schema 与 baseline grants。"""
    from ai_employee.infrastructure.db.database_grants import GrantPhase, read_object_grants

    table_names = (
        "users",
        "oauth_connections",
        "connection_capabilities",
        "encrypted_credentials",
        "provider_calendars",
        "sync_cursors",
        "calendar_events",
    )
    with matrix.engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        # 表名来自上方封闭常量；全表读取能同时捕获原行变化和猜测生成的新 marker。
        business_rows = tuple(
            (
                table_name,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        text(f"SELECT * FROM {table_name} ORDER BY id")
                    )
                ),
            )
            for table_name in table_names
        )
        columns = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT attribute.attnum, attribute.attname, "
                    "format_type(attribute.atttypid, attribute.atttypmod), "
                    "attribute.attnotnull, "
                    "pg_get_expr(default_info.adbin, default_info.adrelid) "
                    "FROM pg_catalog.pg_attribute AS attribute "
                    "LEFT JOIN pg_catalog.pg_attrdef AS default_info "
                    "ON default_info.adrelid = attribute.attrelid "
                    "AND default_info.adnum = attribute.attnum "
                    "WHERE attribute.attrelid = 'public.calendar_events'::regclass "
                    "AND attribute.attnum > 0 AND NOT attribute.attisdropped "
                    "ORDER BY attribute.attnum"
                )
            )
        )
        constraints = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT constraint_info.conname, constraint_info.contype::text, "
                    "constraint_info.convalidated, constraint_info.condeferrable, "
                    "constraint_info.condeferred, "
                    "pg_get_constraintdef(constraint_info.oid, TRUE) "
                    "FROM pg_catalog.pg_constraint AS constraint_info "
                    "WHERE constraint_info.conrelid = 'public.calendar_events'::regclass "
                    "ORDER BY constraint_info.conname"
                )
            )
        )
        grants = read_object_grants(
            connection,
            revision=str(revision),
            phase=GrantPhase.BASELINE,
        )
        return revision, business_rows, columns, constraints, grants


def _seed_calendar_aad_0019_complete_description(database_url: URL) -> _CalendarAad0019Fixture:
    """在 revision 0018 种入一个具备本地恢复关联的完整 description AEAD 三元组。"""
    user_id = "00000000-0000-0000-0000-000000001901"
    connection_id = "00000000-0000-0000-0000-000000001902"
    event_id = "00000000-0000-0000-0000-000000001903"
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users ("
                    "email, display_name, password_hash, timezone, locale, brief_time, "
                    "is_active, id"
                    ") VALUES ("
                    "'calendar-aad-0019@example.test', 'Calendar AAD 0019', NULL, "
                    "'UTC', 'zh-CN', '08:00', TRUE, CAST(:user_id AS uuid)"
                    ")"
                ),
                {"user_id": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO oauth_connections ("
                    "user_id, provider, provider_account_id, account_email, scopes, status, "
                    "last_error_code, id"
                    ") VALUES ("
                    "CAST(:user_id AS uuid), 'google', 'calendar-aad-0019-subject', "
                    "'calendar-aad-0019@example.test', '[]'::jsonb, 'connected', NULL, "
                    "CAST(:connection_id AS uuid)"
                    ")"
                ),
                {"user_id": user_id, "connection_id": connection_id},
            )
            connection.execute(
                text(
                    "INSERT INTO connection_capabilities ("
                    "user_id, connection_id, capability, status, actual_scopes, "
                    "last_verified_at, last_error_code, id"
                    ") VALUES ("
                    "CAST(:user_id AS uuid), CAST(:connection_id AS uuid), 'calendar.read', "
                    "'enabled', '[]'::jsonb, '2030-01-01T00:00:00+00:00', NULL, "
                    "'00000000-0000-0000-0000-000000001904'::uuid"
                    ")"
                ),
                {"user_id": user_id, "connection_id": connection_id},
            )
            connection.execute(
                text(
                    "INSERT INTO encrypted_credentials ("
                    "user_id, connection_id, credential_kind, ciphertext, nonce, key_version, "
                    "token_expires_at, id"
                    ") VALUES "
                    "(CAST(:user_id AS uuid), CAST(:connection_id AS uuid), 'access_token', "
                    ":access_ciphertext, :access_nonce, 1, '2030-01-02T00:00:00+00:00', "
                    "'00000000-0000-0000-0000-000000001905'::uuid), "
                    "(CAST(:user_id AS uuid), CAST(:connection_id AS uuid), 'refresh_token', "
                    ":refresh_ciphertext, :refresh_nonce, 1, NULL, "
                    "'00000000-0000-0000-0000-000000001906'::uuid)"
                ),
                {
                    "user_id": user_id,
                    "connection_id": connection_id,
                    "access_ciphertext": b"synthetic-access-ciphertext",
                    "access_nonce": b"accessnonce!",
                    "refresh_ciphertext": b"synthetic-refresh-ciphertext",
                    "refresh_nonce": b"refreshnonce",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO provider_calendars ("
                    "user_id, connection_id, provider_calendar_id, name, timezone, is_primary, "
                    "access_role, can_write, provider_url, id"
                    ") VALUES ("
                    "CAST(:user_id AS uuid), CAST(:connection_id AS uuid), 'primary', "
                    "'Synthetic primary', 'UTC', TRUE, 'owner', TRUE, "
                    "'https://calendar.example.test/primary', "
                    "'00000000-0000-0000-0000-000000001907'::uuid"
                    ")"
                ),
                {"user_id": user_id, "connection_id": connection_id},
            )
            connection.execute(
                text(
                    "INSERT INTO sync_cursors ("
                    "connection_id, resource_kind, scope_key, cursor, last_success_at, "
                    "last_attempt_at, last_error_code, id"
                    ") VALUES ("
                    "CAST(:connection_id AS uuid), 'calendar', 'primary', "
                    "'synthetic-calendar-cursor', '2030-01-01T00:00:00+00:00', "
                    "'2030-01-01T00:01:00+00:00', NULL, "
                    "'00000000-0000-0000-0000-000000001908'::uuid"
                    ")"
                ),
                {"connection_id": connection_id},
            )
            connection.execute(
                text(
                    "INSERT INTO calendar_events ("
                    "user_id, connection_id, provider_event_id, calendar_id, title, "
                    "description_ciphertext, description_nonce, description_key_version, "
                    "location_ciphertext, location_nonce, location_key_version, starts_at, "
                    "ends_at, all_day, transparency, status, timezone, recurring_event_id, "
                    "etag, organizer, attendees, access_role, can_edit, provider_url, "
                    "provider_updated_at, id"
                    ") VALUES ("
                    "CAST(:user_id AS uuid), CAST(:connection_id AS uuid), "
                    "'historical-event', 'primary', 'Historical encrypted event', "
                    ":description_ciphertext, :description_nonce, 1, NULL, NULL, NULL, "
                    "'2030-01-02T01:00:00+00:00', '2030-01-02T02:00:00+00:00', FALSE, "
                    "'opaque', 'confirmed', 'UTC', NULL, 'historical-etag', NULL, '[]'::jsonb, "
                    "'owner', TRUE, 'https://calendar.example.test/historical-event', "
                    "'2030-01-01T00:00:00+00:00', CAST(:event_id AS uuid)"
                    ")"
                ),
                {
                    "user_id": user_id,
                    "connection_id": connection_id,
                    "event_id": event_id,
                    "description_ciphertext": b"synthetic-description-ciphertext",
                    "description_nonce": b"123456789012",
                },
            )
    except Exception:
        engine.dispose()
        raise
    return _CalendarAad0019Fixture(
        engine=engine,
        user_id=user_id,
        connection_id=connection_id,
        event_id=event_id,
    )


def _calendar_aad_0019_fingerprint(fixture: _CalendarAad0019Fixture) -> tuple[object, ...]:
    """读取 revision、全部关联业务行、CalendarEvent catalog 与当前 baseline grants。"""
    from ai_employee.infrastructure.db.database_grants import GrantPhase, read_object_grants

    with fixture.engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        business_rows = tuple(
            (
                table_name,
                tuple(tuple(row) for row in connection.execute(text(query), parameters)),
            )
            for table_name, query, parameters in (
                (
                    "users",
                    "SELECT * FROM users WHERE id = CAST(:user_id AS uuid)",
                    {"user_id": fixture.user_id},
                ),
                (
                    "oauth_connections",
                    "SELECT * FROM oauth_connections WHERE id = CAST(:connection_id AS uuid)",
                    {"connection_id": fixture.connection_id},
                ),
                (
                    "connection_capabilities",
                    (
                        "SELECT * FROM connection_capabilities "
                        "WHERE connection_id = CAST(:connection_id AS uuid) ORDER BY id"
                    ),
                    {"connection_id": fixture.connection_id},
                ),
                (
                    "encrypted_credentials",
                    (
                        "SELECT * FROM encrypted_credentials "
                        "WHERE connection_id = CAST(:connection_id AS uuid) ORDER BY id"
                    ),
                    {"connection_id": fixture.connection_id},
                ),
                (
                    "provider_calendars",
                    (
                        "SELECT * FROM provider_calendars "
                        "WHERE connection_id = CAST(:connection_id AS uuid) ORDER BY id"
                    ),
                    {"connection_id": fixture.connection_id},
                ),
                (
                    "sync_cursors",
                    (
                        "SELECT * FROM sync_cursors "
                        "WHERE connection_id = CAST(:connection_id AS uuid) ORDER BY id"
                    ),
                    {"connection_id": fixture.connection_id},
                ),
                (
                    "calendar_events",
                    "SELECT * FROM calendar_events WHERE id = CAST(:event_id AS uuid)",
                    {"event_id": fixture.event_id},
                ),
            )
        )
        columns = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT attribute.attnum, attribute.attname, "
                    "format_type(attribute.atttypid, attribute.atttypmod), "
                    "attribute.attnotnull, "
                    "pg_get_expr(default_info.adbin, default_info.adrelid) "
                    "FROM pg_catalog.pg_attribute AS attribute "
                    "LEFT JOIN pg_catalog.pg_attrdef AS default_info "
                    "ON default_info.adrelid = attribute.attrelid "
                    "AND default_info.adnum = attribute.attnum "
                    "WHERE attribute.attrelid = 'public.calendar_events'::regclass "
                    "AND attribute.attnum > 0 AND NOT attribute.attisdropped "
                    "ORDER BY attribute.attnum"
                )
            )
        )
        constraints = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT constraint_info.conname, constraint_info.contype::text, "
                    "constraint_info.convalidated, constraint_info.condeferrable, "
                    "constraint_info.condeferred, "
                    "pg_get_constraintdef(constraint_info.oid, TRUE) "
                    "FROM pg_catalog.pg_constraint AS constraint_info "
                    "WHERE constraint_info.conrelid = 'public.calendar_events'::regclass "
                    "ORDER BY constraint_info.conname"
                )
            )
        )
        grants = read_object_grants(
            connection,
            revision=str(revision),
            phase=GrantPhase.BASELINE,
        )
        return revision, business_rows, columns, constraints, grants


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """迁移契约只操作 UUID 临时库，不先改变共享 Task 13 target 的 revision/posture。"""
    yield


@pytest.fixture(autouse=True)
async def isolated_database() -> AsyncIterator[None]:
    """本模块不读写共享业务表，因此关闭全局 TRUNCATE，避免掩盖零写断言。"""
    yield


def test_calendar_aad_guard_is_not_consumed_by_ordinary_upgrade_to_other_revision(
    empty_migration_database: URL,
) -> None:
    """普通升级到 0018 不得把合法 Calendar guard 误当作其他 revision 的 callback。"""
    guard = _CalendarAadMigrationGuardFake()
    config = _calendar_aad_config(empty_migration_database, guard)

    run_alembic_upgrade(config, "20260809_0018")

    assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
    assert guard.calls == []


def test_calendar_aad_0019_official_stamp_fails_closed_and_rolls_back(
    empty_migration_database: URL,
) -> None:
    """官方 0018→0019 stamp 必须由真实 ``is_stamp`` callback 拒绝并整体回滚。

    Alembic 可能先在外层事务内尝试修改 version row，故这里只比较事务结束后的完整
    revision、业务行、CalendarEvent Schema 与 baseline grants，不伪造 callback metadata，
    也不要求 SQL 层从未尝试 version-row mutation。Calendar guard 属于 0019 migration
    operation/final callback，不得在被 grant lifecycle 拒绝的 stamp 路径上收到任何调用。
    """
    bootstrap_config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(bootstrap_config, "20260809_0018")
    fixture = _seed_calendar_aad_0019_complete_description(empty_migration_database)
    guard = _CalendarAadMigrationGuardFake()
    guarded_config = _calendar_aad_config(empty_migration_database, guard)

    try:
        before = _calendar_aad_0019_fingerprint(fixture)
        migration_error: Exception | None = None
        try:
            run_alembic_stamp(guarded_config, _CALENDAR_AAD_REVISION)
        except Exception as error:  # noqa: BLE001 - 精确核验 lifecycle 的稳定异常边界。
            migration_error = error

        assert _calendar_aad_0019_fingerprint(fixture) == before
        assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
        assert guard.calls == []

        # 当前 Cycle 5 必须先因 0019 尚未发布而 RED；Task 27 发布后才核验真实 StampStep。
        authority = load_published_alembic_authority(guarded_config)
        assert authority.head_revision == _CALENDAR_AAD_REVISION
        assert migration_error is not None
        assert type(migration_error) is AlembicMigrationInvariantError
        assert type(migration_error).__module__ == "ai_employee.infrastructure.db.alembic"
        assert type(migration_error).__name__ == "AlembicMigrationInvariantError"
        assert str(migration_error) == "alembic migration invariant violation"
    finally:
        fixture.engine.dispose()


def test_calendar_aad_0019_guard_fresh_empty_database_uses_strict_zero_bootstrap(
    empty_migration_database: URL,
) -> None:
    """全新空库必须通过 ordinary upgrade head 的内建 strict-empty guard 到达 0019。"""
    config = _calendar_aad_config(empty_migration_database)

    run_alembic_upgrade(config, "head")

    assert _alembic_revisions(empty_migration_database) == {_CALENDAR_AAD_REVISION}


def test_calendar_aad_0019_guard_revision_0018_strict_empty_uses_zero_bootstrap(
    empty_migration_database: URL,
) -> None:
    """0018 严格空 affected set 必须通过 ordinary upgrade head 到达 0019。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")

    run_alembic_upgrade(config, "head")

    assert _alembic_revisions(empty_migration_database) == {_CALENDAR_AAD_REVISION}


def test_calendar_aad_0019_guard_typed_fake_runs_both_phases_in_one_root_transaction(
    empty_migration_database: URL,
) -> None:
    """注入的 exact Fake 必须在同一连接与同一未关闭根事务内收到两个冻结阶段。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    guard = _CalendarAadMigrationGuardFake()
    guarded_config = _calendar_aad_config(empty_migration_database, guard)

    run_alembic_upgrade(guarded_config, "head")

    assert len(guard.calls) == 2
    first_connection, first_transaction, first_phase = guard.calls[0]
    final_connection, final_transaction, final_phase = guard.calls[1]
    assert (first_phase, final_phase) == ("before_mutation", "before_commit")
    assert first_connection is final_connection
    assert first_transaction is final_transaction
    assert guard.observed_revision == _CALENDAR_AAD_REVISION
    assert _alembic_revisions(empty_migration_database) == {_CALENDAR_AAD_REVISION}


def test_calendar_aad_0019_migrates_only_complete_groups_and_exact_affected_pair(
    empty_migration_database: URL,
) -> None:
    """0019 只标记完整历史组，并只失效 A/shared 的精确非 directory cursor。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    matrix = _seed_calendar_aad_0019_data_matrix(empty_migration_database)
    guard = _CalendarAadMigrationGuardFake()
    guarded_config = _calendar_aad_config(empty_migration_database, guard)

    try:
        events_before = _calendar_aad_0019_matrix_rows(matrix, table="calendar_events")
        cursors_before = _calendar_aad_0019_matrix_rows(matrix, table="sync_cursors")

        run_alembic_upgrade(guarded_config, "head")

        assert _calendar_aad_0019_matrix_rows(matrix, table="calendar_events") == events_before
        assert _calendar_aad_0019_matrix_rows(
            matrix,
            table="sync_cursors",
        ) == _calendar_aad_0019_expected_cursors(cursors_before, matrix=matrix)
        with matrix.engine.connect() as connection:
            aad_versions = tuple(
                tuple(row)
                for row in connection.execute(
                    text(
                        "SELECT id::text, description_aad_version, location_aad_version "
                        "FROM calendar_events WHERE user_id = CAST(:user_id AS uuid) "
                        "ORDER BY id"
                    ),
                    {"user_id": matrix.user_id},
                )
            )
        assert aad_versions == (
            (matrix.event1_id, 1, None),
            (matrix.event2_id, 1, 1),
            (matrix.unaffected_a_id, None, None),
            (matrix.unaffected_b_id, None, None),
        )
        assert len(guard.calls) == 2
        first_connection, first_transaction, first_phase = guard.calls[0]
        final_connection, final_transaction, final_phase = guard.calls[1]
        assert (first_phase, final_phase) == ("before_mutation", "before_commit")
        assert first_connection is final_connection
        assert first_transaction is final_transaction
        assert guard.observed_revision == _CALENDAR_AAD_REVISION
        assert _alembic_revisions(empty_migration_database) == {_CALENDAR_AAD_REVISION}
    finally:
        matrix.engine.dispose()


def test_calendar_aad_0019_has_independent_description_and_location_constraints(
    empty_migration_database: URL,
) -> None:
    """0019 必须为 description 与 location 建立两个独立、不可延迟的四列检查约束。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "head")
    description_constraint = "ck_calendar_events_description_aead_with_aad_all_or_none"
    location_constraint = "ck_calendar_events_location_aead_with_aad_all_or_none"
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )

    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT constraint_info.conname, constraint_info.contype::text, "
                    "constraint_info.convalidated, constraint_info.condeferrable, "
                    "constraint_info.condeferred, "
                    "ARRAY_AGG(attribute.attname ORDER BY key_info.position) "
                    "FROM pg_catalog.pg_constraint AS constraint_info "
                    "JOIN pg_catalog.pg_class AS table_info "
                    "ON table_info.oid = constraint_info.conrelid "
                    "JOIN pg_catalog.pg_namespace AS namespace_info "
                    "ON namespace_info.oid = table_info.relnamespace "
                    "JOIN unnest(constraint_info.conkey) WITH ORDINALITY "
                    "AS key_info(attnum, position) ON TRUE "
                    "JOIN pg_catalog.pg_attribute AS attribute "
                    "ON attribute.attrelid = constraint_info.conrelid "
                    "AND attribute.attnum = key_info.attnum "
                    "WHERE namespace_info.nspname = 'public' "
                    "AND table_info.relname = 'calendar_events' "
                    "AND constraint_info.conname IN (:description_name, :location_name) "
                    "GROUP BY constraint_info.conname, constraint_info.contype, "
                    "constraint_info.convalidated, constraint_info.condeferrable, "
                    "constraint_info.condeferred "
                    "ORDER BY constraint_info.conname"
                ),
                {
                    "description_name": description_constraint,
                    "location_name": location_constraint,
                },
            )
            actual = {
                str(row[0]): (
                    str(row[1]),
                    bool(row[2]),
                    bool(row[3]),
                    bool(row[4]),
                    tuple(str(column) for column in row[5]),
                )
                for row in rows
            }
    finally:
        engine.dispose()

    assert actual == {
        description_constraint: (
            "c",
            True,
            False,
            False,
            (
                "description_ciphertext",
                "description_nonce",
                "description_key_version",
                "description_aad_version",
            ),
        ),
        location_constraint: (
            "c",
            True,
            False,
            False,
            (
                "location_ciphertext",
                "location_nonce",
                "location_key_version",
                "location_aad_version",
            ),
        ),
    }


def test_calendar_aad_0019_description_and_location_constraints_enforce_real_dml(
    empty_migration_database: URL,
) -> None:
    """两个四列约束必须独立接受 NULL/v1/v2，并拒绝 partial 与未知版本。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    matrix = _seed_calendar_aad_0019_data_matrix(empty_migration_database)
    events_before = _calendar_aad_0019_matrix_rows(matrix, table="calendar_events")
    guard = _CalendarAadMigrationGuardFake()
    guarded_config = _calendar_aad_config(empty_migration_database, guard)

    empty = _CalendarAad0019ConstraintValues(None, None, None, None, None, None, None, None)
    description_v1 = _CalendarAad0019ConstraintValues(
        b"constraint-description-v1",
        b"desc-nonce01",
        101,
        1,
        None,
        None,
        None,
        None,
    )
    description_v2 = _CalendarAad0019ConstraintValues(
        b"constraint-description-v2",
        b"desc-nonce02",
        102,
        2,
        None,
        None,
        None,
        None,
    )
    location_v1 = _CalendarAad0019ConstraintValues(
        None,
        None,
        None,
        None,
        b"constraint-location-v1",
        b"loc-nonce001",
        201,
        1,
    )
    location_v2 = _CalendarAad0019ConstraintValues(
        None,
        None,
        None,
        None,
        b"constraint-location-v2",
        b"loc-nonce002",
        202,
        2,
    )
    description_v1_location_v2 = _CalendarAad0019ConstraintValues(
        description_v1.description_ciphertext,
        description_v1.description_nonce,
        description_v1.description_key_version,
        description_v1.description_aad_version,
        location_v2.location_ciphertext,
        location_v2.location_nonce,
        location_v2.location_key_version,
        location_v2.location_aad_version,
    )
    legal_cases: tuple[tuple[str, _CalendarAad0019ConstraintValues], ...] = (
        ("both_null", empty),
        ("description_v1_location_null", description_v1),
        ("description_v2_location_null", description_v2),
        ("description_null_location_v1", location_v1),
        ("description_null_location_v2", location_v2),
        ("description_v1_location_v2", description_v1_location_v2),
    )
    invalid_cases: tuple[tuple[str, _CalendarAad0019ConstraintValues], ...] = (
        (
            "description_partial",
            _CalendarAad0019ConstraintValues(
                b"partial-description",
                b"desc-nonce03",
                103,
                None,
                None,
                None,
                None,
                None,
            ),
        ),
        (
            "description_unknown_version",
            _CalendarAad0019ConstraintValues(
                b"unknown-description",
                b"desc-nonce04",
                104,
                3,
                None,
                None,
                None,
                None,
            ),
        ),
        (
            "location_partial",
            _CalendarAad0019ConstraintValues(
                None,
                None,
                None,
                None,
                b"partial-location",
                b"loc-nonce003",
                203,
                None,
            ),
        ),
        (
            "location_unknown_version",
            _CalendarAad0019ConstraintValues(
                None,
                None,
                None,
                None,
                b"unknown-location",
                b"loc-nonce004",
                204,
                3,
            ),
        ),
    )
    update_event = text(
        "UPDATE calendar_events SET "
        "description_ciphertext = :description_ciphertext, "
        "description_nonce = :description_nonce, "
        "description_key_version = :description_key_version, "
        "description_aad_version = :description_aad_version, "
        "location_ciphertext = :location_ciphertext, "
        "location_nonce = :location_nonce, "
        "location_key_version = :location_key_version, "
        "location_aad_version = :location_aad_version "
        "WHERE id = CAST(:event_id AS uuid)"
    )
    read_event = text(
        "SELECT description_ciphertext, description_nonce, description_key_version, "
        "description_aad_version, location_ciphertext, location_nonce, "
        "location_key_version, location_aad_version "
        "FROM calendar_events WHERE id = CAST(:event_id AS uuid)"
    )

    try:
        run_alembic_upgrade(guarded_config, "head")
        with matrix.engine.connect() as connection:
            outer_transaction = connection.begin()
            try:
                for case_name, values in legal_cases:
                    savepoint = connection.begin_nested()
                    try:
                        result = connection.execute(
                            update_event,
                            {
                                "event_id": matrix.event1_id,
                                "description_ciphertext": values.description_ciphertext,
                                "description_nonce": values.description_nonce,
                                "description_key_version": values.description_key_version,
                                "description_aad_version": values.description_aad_version,
                                "location_ciphertext": values.location_ciphertext,
                                "location_nonce": values.location_nonce,
                                "location_key_version": values.location_key_version,
                                "location_aad_version": values.location_aad_version,
                            },
                        )
                        assert result.rowcount == 1, case_name
                        actual = tuple(
                            connection.execute(
                                read_event,
                                {"event_id": matrix.event1_id},
                            ).one()
                        )
                        assert actual == (
                            values.description_ciphertext,
                            values.description_nonce,
                            values.description_key_version,
                            values.description_aad_version,
                            values.location_ciphertext,
                            values.location_nonce,
                            values.location_key_version,
                            values.location_aad_version,
                        ), case_name
                    finally:
                        if savepoint.is_active:
                            savepoint.rollback()

                for case_name, values in invalid_cases:
                    savepoint = connection.begin_nested()
                    try:
                        try:
                            connection.execute(
                                update_event,
                                {
                                    "event_id": matrix.event1_id,
                                    "description_ciphertext": values.description_ciphertext,
                                    "description_nonce": values.description_nonce,
                                    "description_key_version": values.description_key_version,
                                    "description_aad_version": values.description_aad_version,
                                    "location_ciphertext": values.location_ciphertext,
                                    "location_nonce": values.location_nonce,
                                    "location_key_version": values.location_key_version,
                                    "location_aad_version": values.location_aad_version,
                                },
                            )
                        except IntegrityError:
                            pass
                        else:
                            pytest.fail(
                                f"{case_name} must violate calendar event AAD constraint"
                            )
                    finally:
                        if savepoint.is_active:
                            savepoint.rollback()
            finally:
                if outer_transaction.is_active:
                    outer_transaction.rollback()

        assert _calendar_aad_0019_matrix_rows(matrix, table="calendar_events") == events_before
    finally:
        matrix.engine.dispose()


def test_calendar_aad_0019_downgrade_is_forward_only_and_zero_mutation(
    empty_migration_database: URL,
) -> None:
    """0019 downgrade 必须在任一版本行、Schema、业务行或授权写入前明确拒绝。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    matrix = _seed_calendar_aad_0019_data_matrix(empty_migration_database)
    guard = _CalendarAadMigrationGuardFake()
    guarded_config = _calendar_aad_config(empty_migration_database, guard)

    def versioned_event_rows() -> tuple[tuple[object, ...], ...]:
        """读取含两个 AAD version 列在内的完整事件行，冻结 downgrade 前后字节事实。"""
        with matrix.engine.connect() as connection:
            return tuple(
                tuple(row)
                for row in connection.execute(
                    text("SELECT * FROM calendar_events ORDER BY id")
                )
            )

    try:
        run_alembic_upgrade(guarded_config, "head")
        assert _alembic_revisions(empty_migration_database) == {_CALENDAR_AAD_REVISION}
        before = _calendar_aad_0019_partial_fingerprint(matrix)
        events_before = versioned_event_rows()
        guard_phases_before = tuple(
            phase for _connection, _transaction, phase in guard.calls
        )
        assert guard_phases_before == ("before_mutation", "before_commit")
        mutation_attempts: list[str] = []
        normalized_restore_facts_sql = " ".join(
            str(
                text(DATABASE_RESTORE_FACTS_SQL).compile(dialect=PGDialect_psycopg())
            )
            .casefold()
            .replace('"', "")
            .split()
        )
        normalized_role_memberships_sql = " ".join(
            str(text(_ROLE_MEMBERSHIPS_SQL).compile(dialect=PGDialect_psycopg()))
            .casefold()
            .replace('"', "")
            .split()
        )

        def reject_downgrade_mutation(
            _connection: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            """只放行已知只读 lifecycle SQL；任一 downgrade 写语句在发送前失败。"""
            normalized = " ".join(statement.casefold().replace('"', "").split())
            if normalized.startswith(("select ", "show ", "set ")) or (
                normalized
                in {normalized_restore_facts_sql, normalized_role_memberships_sql}
            ):
                return
            command = normalized.partition(" ")[0] or "empty"
            mutation_attempts.append(command)
            raise AssertionError("calendar AAD 0019 downgrade reached mutation")

        event.listen(Engine, "before_cursor_execute", reject_downgrade_mutation)
        try:
            with pytest.raises(
                RuntimeError,
                match=r"^calendar event field AAD migration cannot be downgraded safely$",
            ):
                run_alembic_downgrade(
                    _calendar_aad_config(empty_migration_database),
                    "20260809_0018",
                )
        finally:
            event.remove(Engine, "before_cursor_execute", reject_downgrade_mutation)

        assert mutation_attempts == []
        assert _alembic_revisions(empty_migration_database) == {_CALENDAR_AAD_REVISION}
        assert _calendar_aad_0019_partial_fingerprint(matrix) == before
        assert versioned_event_rows() == events_before
        assert tuple(phase for _connection, _transaction, phase in guard.calls) == (
            "before_mutation",
            "before_commit",
        )
    finally:
        matrix.engine.dispose()


@pytest.mark.parametrize(
    "partial_case",
    tuple(_CalendarAad0019PartialTripleCase),
    ids=lambda partial_case: partial_case.value,
)
def test_calendar_aad_0019_partial_triple_fails_closed_before_mutation(
    empty_migration_database: URL,
    partial_case: _CalendarAad0019PartialTripleCase,
) -> None:
    """任一历史字段 partial 三元组都必须在 0019 首写前精确拒绝并保持零变化。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    matrix = _seed_calendar_aad_0019_data_matrix(empty_migration_database)
    _apply_calendar_aad_0019_partial_triple(matrix, partial_case)
    guard = _CalendarAadMigrationGuardFake()
    guarded_config = _calendar_aad_config(empty_migration_database, guard)
    before = _calendar_aad_0019_partial_fingerprint(matrix)
    mutation_attempts: list[str] = []
    migration_error: Exception | None = None

    def reject_mutation(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """若 partial preflight 未在首个 0019 mutation 前拒绝，则立即暴露失败。"""
        normalized = " ".join(statement.casefold().replace('"', "").split())
        target_mutation = normalized.startswith(
            (
                "alter table calendar_events ",
                "alter table public.calendar_events ",
                "grant ",
                "revoke ",
                "update calendar_events ",
                "update public.calendar_events ",
                "update sync_cursors ",
                "update public.sync_cursors ",
                "update alembic_version ",
                "update public.alembic_version ",
            )
        )
        if target_mutation:
            mutation_attempts.append("calendar_aad_0019_mutation")
            raise AssertionError("partial calendar AAD triple reached mutation")

    event.listen(Engine, "before_cursor_execute", reject_mutation)
    try:
        try:
            run_alembic_upgrade(guarded_config, "head")
        except Exception as error:  # noqa: BLE001 - RED 精确冻结未来专用 invariant。
            migration_error = error
    finally:
        event.remove(Engine, "before_cursor_execute", reject_mutation)

    try:
        assert mutation_attempts == []
        assert migration_error is not None
        assert (
            type(migration_error).__module__
            == "ai_employee.application.ports.calendar_aad_migration_guard"
        )
        assert type(migration_error).__name__ == "CalendarAadMigrationInvariantError"
        assert str(migration_error) == "calendar AAD migration invariant violation"
        assert [phase for _connection, _transaction, phase in guard.calls] == [
            "before_mutation"
        ]
        assert _calendar_aad_0019_partial_fingerprint(matrix) == before
        assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
    finally:
        matrix.engine.dispose()


@pytest.mark.parametrize(
    "recoverability_case",
    tuple(_CalendarAad0019AccessRecoverabilityCase),
    ids=lambda recoverability_case: recoverability_case.value,
)
def test_calendar_aad_0019_access_recoverability_gap_fails_closed_before_mutation(
    empty_migration_database: URL,
    recoverability_case: _CalendarAad0019AccessRecoverabilityCase,
) -> None:
    """affected pair 缺任一本地访问或目录事实时必须在 0019 首写前拒绝。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    matrix = _seed_calendar_aad_0019_data_matrix(empty_migration_database)

    try:
        _apply_calendar_aad_0019_access_recoverability_gap(matrix, recoverability_case)
        guard = _CalendarAadMigrationGuardFake()
        guarded_config = _calendar_aad_config(empty_migration_database, guard)
        before = _calendar_aad_0019_partial_fingerprint(matrix)
        mutation_attempts: list[str] = []
        migration_error: Exception | None = None

        def reject_mutation(
            _connection: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            """若 recoverability preflight 未先拒绝，则拦截任一 0019 专属首写。"""
            normalized = " ".join(statement.casefold().replace('"', "").split())
            target_mutation = normalized.startswith(
                (
                    "alter table calendar_events ",
                    "alter table public.calendar_events ",
                    "grant ",
                    "revoke ",
                    "update calendar_events ",
                    "update public.calendar_events ",
                    "update sync_cursors ",
                    "update public.sync_cursors ",
                    "update alembic_version ",
                    "update public.alembic_version ",
                )
            )
            if target_mutation:
                mutation_attempts.append("calendar_aad_0019_mutation")
                raise AssertionError("calendar AAD recoverability gap reached mutation")

        event.listen(Engine, "before_cursor_execute", reject_mutation)
        try:
            try:
                run_alembic_upgrade(guarded_config, "head")
            except Exception as error:  # noqa: BLE001 - RED 精确冻结未来专用 invariant。
                migration_error = error
        finally:
            event.remove(Engine, "before_cursor_execute", reject_mutation)

        assert mutation_attempts == []
        assert _calendar_aad_0019_partial_fingerprint(matrix) == before
        assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
        assert migration_error is not None
        assert (
            type(migration_error).__module__
            == "ai_employee.application.ports.calendar_aad_migration_guard"
        )
        assert type(migration_error).__name__ == "CalendarAadMigrationInvariantError"
        assert str(migration_error) == "calendar AAD migration invariant violation"
        assert [phase for _connection, _transaction, phase in guard.calls] == [
            "before_mutation"
        ]
    finally:
        matrix.engine.dispose()


def test_calendar_aad_0019_cross_user_event_fails_closed_before_mutation(
    empty_migration_database: URL,
) -> None:
    """affected event 与 owning connection 用户不一致时必须在 0019 首写前拒绝。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    matrix = _seed_calendar_aad_0019_data_matrix(empty_migration_database)
    second_user_id = "00000000-0000-0000-0000-000000001929"

    try:
        with matrix.engine.begin() as connection:
            inserted = connection.execute(
                text(
                    "INSERT INTO users ("
                    "email, display_name, password_hash, timezone, locale, brief_time, "
                    "is_active, id"
                    ") VALUES ("
                    "'calendar-aad-cross-user@example.test', 'Calendar AAD Cross User', NULL, "
                    "'UTC', 'zh-CN', '08:00', TRUE, CAST(:user_id AS uuid)"
                    ")"
                ),
                {"user_id": second_user_id},
            )
            changed = connection.execute(
                text(
                    "UPDATE calendar_events SET user_id = CAST(:second_user_id AS uuid) "
                    "WHERE id = CAST(:event_id AS uuid)"
                ),
                {"second_user_id": second_user_id, "event_id": matrix.event1_id},
            )
            assert inserted.rowcount == 1
            assert changed.rowcount == 1

        guard = _CalendarAadMigrationGuardFake()
        guarded_config = _calendar_aad_config(empty_migration_database, guard)
        before = _calendar_aad_0019_partial_fingerprint(matrix)
        mutation_attempts: list[str] = []
        migration_error: Exception | None = None

        def reject_mutation(
            _connection: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            """若 cross-user preflight 未先拒绝，则拦截任一 0019 专属首写。"""
            normalized = " ".join(statement.casefold().replace('"', "").split())
            target_mutation = normalized.startswith(
                (
                    "alter table calendar_events ",
                    "alter table public.calendar_events ",
                    "grant ",
                    "revoke ",
                    "update calendar_events ",
                    "update public.calendar_events ",
                    "update sync_cursors ",
                    "update public.sync_cursors ",
                    "update alembic_version ",
                    "update public.alembic_version ",
                )
            )
            if target_mutation:
                mutation_attempts.append("calendar_aad_0019_mutation")
                raise AssertionError("cross-user calendar AAD event reached mutation")

        event.listen(Engine, "before_cursor_execute", reject_mutation)
        try:
            try:
                run_alembic_upgrade(guarded_config, "head")
            except Exception as error:  # noqa: BLE001 - RED 精确冻结未来专用 invariant。
                migration_error = error
        finally:
            event.remove(Engine, "before_cursor_execute", reject_mutation)

        assert mutation_attempts == []
        assert _calendar_aad_0019_partial_fingerprint(matrix) == before
        assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
        assert migration_error is not None
        assert (
            type(migration_error).__module__
            == "ai_employee.application.ports.calendar_aad_migration_guard"
        )
        assert type(migration_error).__name__ == "CalendarAadMigrationInvariantError"
        assert str(migration_error) == "calendar AAD migration invariant violation"
        assert [phase for _connection, _transaction, phase in guard.calls] == [
            "before_mutation"
        ]
    finally:
        matrix.engine.dispose()


def test_calendar_aad_0019_guard_rejects_truthy_non_protocol_before_mutation(
    empty_migration_database: URL,
) -> None:
    """Config 中的 truthy object 不能冒充 runtime-checkable typed guard。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    fixture = _seed_calendar_aad_0019_complete_description(empty_migration_database)
    invalid_config = _calendar_aad_config(empty_migration_database, object())
    before = _calendar_aad_0019_fingerprint(fixture)
    mutation_attempts: list[str] = []
    migration_error: Exception | None = None

    def reject_mutation(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """若非法 guard 在首个 0019 mutation 前未拒绝，则立即暴露测试失败。"""
        normalized = " ".join(statement.casefold().replace('"', "").split())
        if normalized.startswith(
            (
                "alter table calendar_events ",
                "alter table public.calendar_events ",
                "update calendar_events ",
                "update public.calendar_events ",
                "update sync_cursors ",
                "update public.sync_cursors ",
                "update alembic_version ",
                "update public.alembic_version ",
            )
        ):
            mutation_attempts.append("calendar_aad_0019_mutation")
            raise AssertionError("invalid calendar AAD guard reached mutation")

    event.listen(Engine, "before_cursor_execute", reject_mutation)
    try:
        try:
            run_alembic_upgrade(invalid_config, "head")
        except Exception as error:  # noqa: BLE001 - RED 精确冻结未来专用 invariant。
            migration_error = error
        else:
            raise AssertionError("truthy non-protocol calendar AAD guard was accepted")
    finally:
        event.remove(Engine, "before_cursor_execute", reject_mutation)

    try:
        assert mutation_attempts == []
        assert migration_error is not None
        assert (
            type(migration_error).__module__
            == "ai_employee.application.ports.calendar_aad_migration_guard"
        )
        assert type(migration_error).__name__ == "CalendarAadMigrationInvariantError"
        assert str(migration_error) == "calendar AAD migration invariant violation"
        assert _calendar_aad_0019_fingerprint(fixture) == before
    finally:
        fixture.engine.dispose()


def test_calendar_aad_0019_guard_freezes_one_identity_before_running_migrations(
    empty_migration_database: URL,
) -> None:
    """mutation 前替换 Config attribute 也不得让 final 阶段解析出第二个 guard。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    replacement = _CalendarAadMigrationGuardFake()
    guard = _ReplacingCalendarAadMigrationGuardFake(config=config, replacement=replacement)
    config.attributes[_CALENDAR_AAD_GUARD_ATTRIBUTE] = guard

    run_alembic_upgrade(config, "head")

    assert len(guard.calls) == 2
    first_connection, first_transaction, first_phase = guard.calls[0]
    final_connection, final_transaction, final_phase = guard.calls[1]
    assert (first_phase, final_phase) == ("before_mutation", "before_commit")
    assert first_connection is final_connection
    assert first_transaction is final_transaction
    assert replacement.calls == []
    assert _alembic_revisions(empty_migration_database) == {_CALENDAR_AAD_REVISION}


def test_calendar_aad_0019_guard_final_rejection_rolls_back_entire_step(
    empty_migration_database: URL,
) -> None:
    """final guard 拒绝必须回滚 0019 DDL/DML、version row、cursor 与 grant delta。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    fixture = _seed_calendar_aad_0019_complete_description(empty_migration_database)
    before = _calendar_aad_0019_fingerprint(fixture)
    guard = _CalendarAadMigrationGuardFake(reject_before_commit=True)
    guarded_config = _calendar_aad_config(empty_migration_database, guard)

    try:
        with pytest.raises(
            RuntimeError,
            match=r"^synthetic final calendar AAD guard rejection$",
        ):
            run_alembic_upgrade(guarded_config, "head")

        assert len(guard.calls) == 2
        first_connection, first_transaction, first_phase = guard.calls[0]
        final_connection, final_transaction, final_phase = guard.calls[1]
        assert (first_phase, final_phase) == ("before_mutation", "before_commit")
        assert first_connection is final_connection
        assert first_transaction is final_transaction
        assert guard.observed_revision == _CALENDAR_AAD_REVISION
        assert _calendar_aad_0019_fingerprint(fixture) == before
        assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
    finally:
        fixture.engine.dispose()


def test_calendar_aad_0019_guard_non_none_final_result_rolls_back_entire_step(
    empty_migration_database: URL,
) -> None:
    """before_commit 返回非 None 必须以稳定错误回滚完整 0019 transaction。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    fixture = _seed_calendar_aad_0019_complete_description(empty_migration_database)
    before = _calendar_aad_0019_fingerprint(fixture)
    guard = _ReturningCalendarAadMigrationGuardFake(return_phase="before_commit")
    guarded_config = _calendar_aad_config(empty_migration_database, guard)

    try:
        with pytest.raises(
            CalendarAadMigrationInvariantError,
            match=r"^calendar AAD migration invariant violation$",
        ):
            run_alembic_upgrade(guarded_config, "head")

        assert [phase for _connection, _transaction, phase in guard.calls] == [
            "before_mutation",
            "before_commit",
        ]
        assert guard.calls[0][0] is guard.calls[1][0]
        assert guard.calls[0][1] is guard.calls[1][1]
        assert _calendar_aad_0019_fingerprint(fixture) == before
        assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
    finally:
        fixture.engine.dispose()


def test_calendar_aad_0019_guard_lazy_mutation_result_is_zero_write(
    empty_migration_database: URL,
) -> None:
    """before_mutation lazy 结果必须在首个 0019 DDL/DML 前稳定拒绝。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    fixture = _seed_calendar_aad_0019_complete_description(empty_migration_database)
    before = _calendar_aad_0019_fingerprint(fixture)
    guard = _ReturningCalendarAadMigrationGuardFake(
        return_phase="before_mutation",
        lazy=True,
    )
    guarded_config = _calendar_aad_config(empty_migration_database, guard)
    mutation_attempts: list[str] = []

    def reject_first_calendar_aad_0019_mutation(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """若 lazy 返回值被忽略并触达首个 0019 mutation，则立即暴露绕过。"""
        normalized = " ".join(statement.casefold().replace('"', "").split())
        if normalized.startswith(
            (
                "alter table calendar_events ",
                "alter table public.calendar_events ",
                "update calendar_events ",
                "update public.calendar_events ",
                "update sync_cursors ",
                "update public.sync_cursors ",
                "update alembic_version ",
                "update public.alembic_version ",
            )
        ):
            mutation_attempts.append("calendar_aad_0019_mutation")
            raise AssertionError("lazy calendar AAD guard result reached mutation")

    event.listen(Engine, "before_cursor_execute", reject_first_calendar_aad_0019_mutation)
    try:
        with pytest.raises(
            CalendarAadMigrationInvariantError,
            match=r"^calendar AAD migration invariant violation$",
        ):
            run_alembic_upgrade(guarded_config, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", reject_first_calendar_aad_0019_mutation)

    try:
        assert mutation_attempts == []
        assert [phase for _connection, _transaction, phase in guard.calls] == [
            "before_mutation"
        ]
        assert _calendar_aad_0019_fingerprint(fixture) == before
        assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
    finally:
        fixture.engine.dispose()


def test_calendar_aad_0019_guard_nonempty_affected_set_without_guard_is_zero_write(
    empty_migration_database: URL,
) -> None:
    """0018 非空 affected set 缺 typed guard 时必须在首个 0019 DDL/DML 前拒绝。"""
    config = _calendar_aad_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    fixture = _seed_calendar_aad_0019_complete_description(empty_migration_database)

    try:
        before = _calendar_aad_0019_fingerprint(fixture)
        assert before[0] == "20260809_0018"
        assert _CALENDAR_AAD_GUARD_ATTRIBUTE not in config.attributes

        mutation_attempts: list[str] = []
        migration_error: Exception | None = None

        def reject_first_calendar_aad_0019_mutation(
            _connection: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            """在 SQL 发往 PostgreSQL 前拦截任一 0019 专属 DDL/DML。"""
            normalized = " ".join(statement.casefold().replace('"', "").split())
            target_mutation = normalized.startswith(
                (
                    "alter table calendar_events ",
                    "alter table public.calendar_events ",
                    "update calendar_events ",
                    "update public.calendar_events ",
                    "update sync_cursors ",
                    "update public.sync_cursors ",
                    "update alembic_version ",
                    "update public.alembic_version ",
                )
            ) or (
                normalized.startswith(("grant ", "revoke "))
                and any(
                    target in normalized
                    for target in (
                        "calendar_events",
                        "sync_cursors",
                        "checkpoint_migrations_v_seq",
                    )
                )
            )
            if target_mutation:
                mutation_attempts.append("calendar_aad_0019_mutation")
                raise AssertionError("calendar AAD 0019 mutation reached PostgreSQL")

        event.listen(Engine, "before_cursor_execute", reject_first_calendar_aad_0019_mutation)
        try:
            run_alembic_upgrade(config, "head")
        except Exception as error:  # noqa: BLE001 - 测试需核验未来专用异常的精确类型与消息。
            migration_error = error
        finally:
            event.remove(Engine, "before_cursor_execute", reject_first_calendar_aad_0019_mutation)

        assert _calendar_aad_0019_fingerprint(fixture) == before
        assert mutation_attempts == []
        assert migration_error is not None
        assert (
            type(migration_error).__module__
            == "ai_employee.application.ports.calendar_aad_migration_guard"
        )
        assert type(migration_error).__name__ == "CalendarAadMigrationInvariantError"
        assert str(migration_error) == "calendar AAD migration invariant violation"
        assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
    finally:
        fixture.engine.dispose()


def test_integration_session_migration_routes_only_through_typed_lifecycle() -> None:
    """会话级迁移必须复用 typed migrate，且 Candidate 不得被 fixture 隐式 bootstrap。

    该契约只审阅测试入口源码，因此可以在不连接共享 Task 13 数据库的情况下运行。真实
    Candidate 的零写拒绝由 maintenance gate 集成测试覆盖；这里防止后续把直接 Alembic
    或 role-bootstrap 重新塞回所有 integration test 的 autouse fixture。
    """
    integration_conftest = Path(__file__).resolve().parents[1] / "conftest.py"
    source = integration_conftest.read_text(encoding="utf-8")
    fixture_start = source.index("def migrated_database(")
    fixture_end = source.index("def _bootstrap_empty_migration_database(")
    fixture_source = source[fixture_start:fixture_end]

    assert "migrate_database(" in fixture_source
    assert "SqlAlchemyDatabaseMaintenanceContext(" in fixture_source
    assert "run_alembic_upgrade_on_connection" in fixture_source
    assert "run_alembic_upgrade(" not in fixture_source
    assert "set_alembic_database_url(" not in fixture_source
    assert "role_bootstrap_database" not in fixture_source
    assert "bootstrap_candidate_to_baseline" not in fixture_source


class TestCandidateMigrationZeroWrite:
    """在共享 Candidate 上只验证拒绝路径，局部关闭全局 migration/TRUNCATE fixture。"""

    @pytest.fixture(autouse=True, name="migrated_database")
    def _without_session_migration(self) -> Iterator[None]:
        """Candidate 用例不得先由全局 typed migration 改变待验证的 admission 前提。"""
        yield

    @pytest.fixture(autouse=True, name="isolated_database")
    async def _without_application_truncate(self) -> AsyncIterator[None]:
        """零写用例自行比较完整指纹，禁止全局 fixture TRUNCATE 业务表。"""
        yield

    def test_direct_candidate_migrate_rejects_without_catalog_or_audit_writes(
        self,
        database_url: str,
    ) -> None:
        """Candidate 必须在 runner 首写前拒绝，且 revision/ACL/authority/audit 保持不变。"""
        from sqlalchemy import create_engine
        from sqlalchemy.engine import Connection, make_url
        from sqlalchemy.pool import NullPool

        from ai_employee.infrastructure.db.database_access import (
            BootstrapCaller,
            read_database_access_snapshot_sync,
        )
        from ai_employee.infrastructure.db.database_grants import GrantPhase, read_object_grants
        from ai_employee.infrastructure.db.database_maintenance import (
            DatabaseMaintenanceInvariantError,
            SqlAlchemyDatabaseMaintenanceContext,
            migrate_database,
            read_database_restore_facts,
        )

        target_url = make_url(database_url).set(drivername="postgresql+psycopg")
        target_name = target_url.database
        if target_name is None:
            raise AssertionError("validated integration URL must include a target database")
        management_engine = create_engine(
            target_url.set(database="postgres"),
            poolclass=NullPool,
            hide_parameters=True,
        )
        target_engine = create_engine(target_url, poolclass=NullPool, hide_parameters=True)
        migration_connections: list[Connection] = []

        def capture_zero_write_fingerprint() -> tuple[object, ...]:
            """读取足以识别 migration/version/grant/authority/audit mutation 的完整事实。"""
            with target_engine.connect() as connection:
                target_oid = connection.execute(
                    text("SELECT oid FROM pg_database WHERE datname = current_database()")
                ).scalar_one()
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                audit_count = connection.execute(
                    text("SELECT count(*) FROM audit_events")
                ).scalar_one()
                return (
                    revision,
                    audit_count,
                    read_database_restore_facts(
                        connection,
                        target_database_oid=target_oid,
                    ),
                    read_database_access_snapshot_sync(
                        connection,
                        target_database_oid=target_oid,
                    ),
                    read_object_grants(
                        connection,
                        revision=str(revision),
                        phase=GrantPhase.BASELINE,
                    ),
                )

        try:
            before = capture_zero_write_fingerprint()
            context = SqlAlchemyDatabaseMaintenanceContext(
                management_engine=management_engine,
                target_engine=target_engine,
                target_database_name=target_name,
                bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
                app_password=None,
                retention_password=None,
                migration_runner=migration_connections.append,
                published_authority=load_published_alembic_authority(
                    Config(Path(__file__).resolve().parents[3] / "alembic.ini")
                ),
            )

            with pytest.raises(
                DatabaseMaintenanceInvariantError,
                match=r"^database maintenance invariant violation$",
            ):
                migrate_database(context)

            assert migration_connections == []
            assert capture_zero_write_fingerprint() == before
            assert before[0] == "20260809_0018"
        finally:
            target_engine.dispose()
            management_engine.dispose()


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
                        "'uq_calendar_events_connection_calendar_provider_event', "
                        "'fk_users_default_mail_connection_id_user_id', "
                        "'fk_users_default_calendar_connection_id_user_id'"
                        ") GROUP BY constraint_info.conname"
                    )
                )
                return {row[0]: tuple(str(column_name) for column_name in row[1]) for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_columns())


def _calendar_event_identity_constraints(database_url: URL) -> dict[str, tuple[str, ...]]:
    """读取 CalendarEvent 的全部唯一约束及其有序列，排除主键等非身份约束。"""

    async def read_constraints() -> dict[str, tuple[str, ...]]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT constraint_info.conname, "
                        "ARRAY_AGG(attribute.attname ORDER BY key_info.position) "
                        "FROM pg_catalog.pg_constraint AS constraint_info "
                        "JOIN pg_catalog.pg_class AS table_info "
                        "ON table_info.oid = constraint_info.conrelid "
                        "JOIN pg_catalog.pg_namespace AS namespace_info "
                        "ON namespace_info.oid = table_info.relnamespace "
                        "JOIN unnest(constraint_info.conkey) WITH ORDINALITY "
                        "AS key_info(attnum, position) ON TRUE "
                        "JOIN pg_catalog.pg_attribute AS attribute "
                        "ON attribute.attrelid = table_info.oid "
                        "AND attribute.attnum = key_info.attnum "
                        "WHERE namespace_info.nspname = 'public' "
                        "AND table_info.relname = 'calendar_events' "
                        "AND constraint_info.contype = 'u' "
                        "GROUP BY constraint_info.conname"
                    )
                )
                return {
                    str(row[0]): tuple(str(column_name) for column_name in row[1]) for row in rows
                }
        finally:
            await engine.dispose()

    return asyncio.run(read_constraints())


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
                return {str(row[0]): (str(row[1]), str(row[2])) for row in result}
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
                return {str(row[0]): (bool(row[1]), bool(row[2]), bool(row[3])) for row in result}
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
    run_alembic_upgrade(alembic_config, "head")

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
    assert _alembic_revisions(empty_migration_database) == {_CALENDAR_AAD_REVISION}
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
        "uq_calendar_events_connection_calendar_provider_event": (
            "connection_id",
            "calendar_id",
            "provider_event_id",
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
    run_alembic_check(alembic_config)


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
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)

    run_alembic_upgrade(alembic_config, "20260808_0016")

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
    assert (
        _m2_constraint_columns(empty_migration_database).get(
            "uq_email_messages_connection_provider_message"
        )
        is None
    )
    assert _email_identity_foreign_key_metadata(empty_migration_database) == {}

    run_alembic_downgrade(alembic_config, "20260808_0015")

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
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    run_alembic_upgrade(alembic_config, "20260808_0016")

    inserted_updated_at = datetime(2026, 8, 8, 1, 30, tzinfo=UTC)
    assert (
        _upsert_identity_migration_message(
            empty_migration_database,
            provider_message_id="synthetic-0016-dual-write-message",
            provider_thread_id="synthetic-thread-dual-write-insert",
            provider_updated_at=inserted_updated_at,
            mailbox_scope_key="synthetic-folder-dual-write",
        )
        == MailMessageUpsertResult.APPLIED
    )
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
    assert (
        _upsert_identity_migration_message(
            empty_migration_database,
            provider_thread_id="synthetic-thread-legacy-upsert",
            provider_updated_at=legacy_updated_at,
            mailbox_scope_key="synthetic-folder-legacy",
        )
        == MailMessageUpsertResult.APPLIED
    )
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
    assert (
        _upsert_identity_migration_message(
            empty_migration_database,
            provider_thread_id="synthetic-thread-index-upsert",
            provider_updated_at=index_updated_at,
            mailbox_scope_key="synthetic-folder-index",
        )
        == MailMessageUpsertResult.APPLIED
    )
    assert _identity_message_projection_rows(empty_migration_database) == (
        (
            "00000000-0000-0000-0000-000000000202",
            index_updated_at,
            "synthetic-thread-index-upsert",
            "synthetic-folder-index",
        ),
    )

    run_alembic_upgrade(alembic_config, "20260808_0017")
    contract_updated_at = datetime(2026, 8, 8, 4, tzinfo=UTC)
    assert (
        _upsert_identity_migration_message(
            empty_migration_database,
            provider_thread_id="synthetic-thread-contract-upsert",
            provider_updated_at=contract_updated_at,
            mailbox_scope_key="synthetic-folder-contract",
        )
        == MailMessageUpsertResult.APPLIED
    )
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
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    run_alembic_upgrade(alembic_config, "20260808_0016")

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
    run_alembic_upgrade(alembic_config, "20260808_0017")

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


def test_mail_repository_falls_back_to_legacy_during_new_index_build(
    empty_migration_database: URL,
) -> None:
    """新唯一索引正在并发构建时，仍须通过精确 legacy constraint 保持可写。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    run_alembic_upgrade(alembic_config, "20260808_0016")

    async def exercise_build_window() -> tuple[str | None, MailMessageUpsertResult | None]:
        """用条件轮询观察真实 ``indisready=true/indisvalid=false`` 中间态。"""
        index_name = "uq_email_messages_connection_provider_message"
        blocker_engine = create_async_engine(
            empty_migration_database,
            poolclass=NullPool,
            isolation_level="REPEATABLE READ",
        )
        builder_engine = create_async_engine(
            empty_migration_database,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        # 轮询连接只读 catalog；AUTOCOMMIT 防止每次 SELECT 留下第二个长快照，
        # 否则释放显式 blocker 后 CREATE INDEX CONCURRENTLY 仍会无法进入 valid。
        observer_engine = create_async_engine(
            empty_migration_database,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        cleanup_engine = create_async_engine(
            empty_migration_database,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        blocker_connection = await blocker_engine.connect()
        builder_connection = await builder_engine.connect()
        observer_connection = await observer_engine.connect()
        blocker_transaction = None
        build_task: asyncio.Task[None] | None = None
        error_message: str | None = None
        upsert_result: MailMessageUpsertResult | None = None
        try:
            blocker_transaction = await blocker_connection.begin()
            # REPEATABLE READ snapshot 必须在 CREATE INDEX CONCURRENTLY 之前读取目标表，
            # 这样索引在第二次扫描后会稳定等待该事务，而不是偶然直接完成到 valid。
            await blocker_connection.execute(text("SELECT count(*) FROM email_messages"))

            async def build_index() -> None:
                await builder_connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        f"{index_name} ON email_messages (connection_id, provider_message_id)"
                    )
                )

            build_task = asyncio.create_task(build_index())
            async with asyncio.timeout(30):
                while True:
                    row = (
                        await observer_connection.execute(
                            text(
                                "SELECT index_class.relkind::text, table_class.relname, "
                                "index_info.indisready, index_info.indisvalid, "
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
                                "ORDER BY index_key.position) "
                                "FROM pg_catalog.pg_class AS index_class "
                                "JOIN pg_catalog.pg_namespace AS namespace "
                                "ON namespace.oid = index_class.relnamespace "
                                "JOIN pg_catalog.pg_index AS index_info "
                                "ON index_info.indexrelid = index_class.oid "
                                "JOIN pg_catalog.pg_class AS table_class "
                                "ON table_class.oid = index_info.indrelid "
                                "WHERE namespace.nspname = current_schema() "
                                "AND index_class.relname = :index_name"
                            ),
                            {"index_name": index_name},
                        )
                    ).one_or_none()
                    if row is not None and (
                        row[0] == "i"
                        and row[1] == "email_messages"
                        and bool(row[2])
                        and not bool(row[3])
                        and bool(row[4])
                        and bool(row[5])
                        and bool(row[6])
                        and int(row[7]) == 2
                        and int(row[8]) == 2
                        and tuple(row[9]) == ("connection_id", "provider_message_id")
                    ):
                        break
                    if build_task.done():
                        build_task.result()
                        raise AssertionError(
                            "CREATE INDEX CONCURRENTLY completed before invalid build state"
                        )
                    # 这是条件轮询的让步间隔；退出条件始终是 catalog 状态或任务结果，
                    # 不是依赖某个固定睡眠时长猜测 PostgreSQL 的构建进度。
                    await asyncio.sleep(0.02)

            try:
                upsert_result = await asyncio.to_thread(
                    _upsert_identity_migration_message,
                    empty_migration_database,
                    provider_thread_id="synthetic-index-build-window-thread",
                    provider_updated_at=datetime(2026, 8, 9, 1, tzinfo=UTC),
                    mailbox_scope_key="synthetic-index-build-window",
                )
            except RuntimeError as error:
                error_message = str(error)

            await blocker_transaction.rollback()
            blocker_transaction = None
            await asyncio.wait_for(build_task, timeout=30)
            build_task = None
            return error_message, upsert_result
        finally:
            if blocker_transaction is not None:
                await blocker_transaction.rollback()
            if build_task is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(build_task, return_exceptions=True),
                        timeout=30,
                    )
                except TimeoutError:
                    build_task.cancel()
                    await asyncio.gather(build_task, return_exceptions=True)
            try:
                async with cleanup_engine.connect() as connection:
                    await connection.execute(
                        text(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
                    )
            finally:
                await blocker_connection.close()
                await builder_connection.close()
                await observer_connection.close()
                await blocker_engine.dispose()
                await builder_engine.dispose()
                await observer_engine.dispose()
                await cleanup_engine.dispose()

    error_message, upsert_result = asyncio.run(exercise_build_window())
    assert (error_message, upsert_result) == (None, MailMessageUpsertResult.APPLIED)
    assert _identity_message_projection_rows(empty_migration_database) == (
        (
            "00000000-0000-0000-0000-000000000202",
            datetime(2026, 8, 9, 1, tzinfo=UTC),
            "synthetic-index-build-window-thread",
            "synthetic-index-build-window",
        ),
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
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=True)

    with pytest.raises(RuntimeError, match="connection-level duplicate"):
        run_alembic_upgrade(alembic_config, "20260808_0016")

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
    run_alembic_upgrade(alembic_config, "20260808_0016")

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
    run_alembic_upgrade(alembic_config, "20260808_0015")
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
        run_alembic_upgrade(alembic_config, "20260808_0016")

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
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    _seed_second_mail_identity_connection(empty_migration_database)

    run_alembic_upgrade(alembic_config, "20260808_0016")
    run_alembic_upgrade(alembic_config, "20260808_0017")

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
    """提前完成 0017 contract 的旧版 0016 shape 必须在 operation 前拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    run_alembic_upgrade(alembic_config, "20260808_0016")

    async def emulate_legacy_contract() -> None:
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("ALTER TABLE email_messages ALTER COLUMN connection_id SET NOT NULL")
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
    before_rows = _mail_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0017_resume_preflight_probe(alembic_config)

    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"
    assert mutation_attempts == ()
    assert _alembic_revisions(empty_migration_database) == {"20260808_0016"}
    assert _email_message_count(empty_migration_database) == 1
    assert _email_identity_column_metadata(empty_migration_database) == {
        "connection_id": ("uuid", "NO"),
    }
    assert _mail_resume_rows(empty_migration_database) == before_rows
    assert _email_message_identity_metadata(empty_migration_database)[1] == {
        "uq_email_messages_connection_provider_message"
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
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    run_alembic_upgrade(alembic_config, "20260808_0016")

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
    before_rows = _mail_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0017_resume_preflight_probe(alembic_config)

    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"
    assert mutation_attempts == ()
    assert _alembic_revisions(empty_migration_database) == {"20260808_0016"}
    assert _email_message_count(empty_migration_database) == 1
    assert _mail_resume_rows(empty_migration_database) == before_rows


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
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    _seed_second_mail_identity_connection(empty_migration_database)
    run_alembic_upgrade(alembic_config, "20260808_0017")

    assert _email_message_count(empty_migration_database) == 2
    run_alembic_downgrade(alembic_config, "20260808_0016")

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

    run_alembic_downgrade(alembic_config, "20260808_0015")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0015"}
    assert _email_message_count(empty_migration_database) == 2
    assert _email_identity_column_metadata(empty_migration_database) == {}


def test_mail_message_identity_contract_preserves_wrong_shape_invalid_index(
    empty_migration_database: URL,
) -> None:
    """同名 invalid 错形索引必须由 0017 preflight 保留并在 operation 前拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    run_alembic_upgrade(alembic_config, "20260808_0016")

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

    before_rows = _mail_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0017_resume_preflight_probe(alembic_config)

    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"
    assert mutation_attempts == ()
    assert _alembic_revisions(empty_migration_database) == {"20260808_0016"}
    assert _email_identity_index_metadata(empty_migration_database) == expected_index
    assert _mail_resume_rows(empty_migration_database) == before_rows


def test_mail_message_identity_contract_preserves_invalid_expression_index(
    empty_migration_database: URL,
) -> None:
    """额外表达式键即使 invalid 也必须由 0017 preflight 保留并拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    run_alembic_upgrade(alembic_config, "20260808_0016")

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

    before_rows = _mail_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0017_resume_preflight_probe(alembic_config)

    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"
    assert mutation_attempts == ()
    assert _alembic_revisions(empty_migration_database) == {"20260808_0016"}
    assert _email_identity_index_metadata(empty_migration_database) == expected_index
    assert _mail_resume_rows(empty_migration_database) == before_rows


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
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    run_alembic_upgrade(alembic_config, "20260808_0016")

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
    run_alembic_upgrade(alembic_config, "20260808_0017")

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
    run_alembic_upgrade(alembic_config, "20260808_0015")
    _seed_pre_identity_mail_rows(empty_migration_database, duplicate=False)
    run_alembic_upgrade(alembic_config, "20260808_0016")

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
    run_alembic_upgrade(alembic_config, "20260808_0017")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0017"}
    assert _email_message_identity_metadata(empty_migration_database)[1] == {
        "uq_email_messages_connection_provider_message"
    }
    assert _m2_constraint_columns(empty_migration_database)[
        "uq_email_threads_id_connection_user"
    ] == ("id", "connection_id", "user_id")


def test_calendar_event_identity_migration_attaches_existing_valid_index(
    empty_migration_database: URL,
) -> None:
    """0018 可复用已完成但尚未挂载的三元索引，避免重复并发建索引。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    run_alembic_upgrade(alembic_config, "20260808_0017")

    async def create_valid_index() -> None:
        """模拟并发索引已完成但 metadata contract 尚未提交的中断窗口。"""
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
                        "uq_calendar_events_connection_calendar_provider_event "
                        "ON calendar_events (connection_id, calendar_id, provider_event_id)"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(create_valid_index())
    run_alembic_upgrade(alembic_config, "20260809_0018")

    assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
    assert _calendar_event_identity_constraints(empty_migration_database) == {
        "uq_calendar_events_connection_calendar_provider_event": (
            "connection_id",
            "calendar_id",
            "provider_event_id",
        )
    }


def test_calendar_event_identity_migration_rejects_deferrable_named_constraint(
    empty_migration_database: URL,
) -> None:
    """提前出现的可延迟 destination constraint 必须在 0018 operation 前拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    run_alembic_upgrade(alembic_config, "20260808_0017")

    async def create_deferrable_constraint() -> None:
        """模拟列形状正确但不能供 PostgreSQL upsert 使用的人工约束。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE calendar_events ADD CONSTRAINT "
                        "uq_calendar_events_connection_calendar_provider_event "
                        "UNIQUE (connection_id, calendar_id, provider_event_id) "
                        "DEFERRABLE INITIALLY DEFERRED"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(create_deferrable_constraint())
    before_rows = _calendar_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0018_resume_preflight_probe(alembic_config)

    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"
    assert mutation_attempts == ()

    assert _alembic_revisions(empty_migration_database) == {"20260808_0017"}
    assert _calendar_resume_rows(empty_migration_database) == before_rows
    assert _calendar_event_identity_constraints(empty_migration_database) == {
        "uq_calendar_events_connection_calendar_provider_event": (
            "connection_id",
            "calendar_id",
            "provider_event_id",
        ),
        "uq_calendar_events_connection_provider_event": (
            "connection_id",
            "provider_event_id",
        ),
    }

    async def read_constraint_mode() -> tuple[bool, bool]:
        """读取错误约束模式，证明迁移没有删除或替换同名对象。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT constraint_info.condeferrable, "
                            "constraint_info.condeferred "
                            "FROM pg_catalog.pg_constraint AS constraint_info "
                            "JOIN pg_catalog.pg_class AS table_info "
                            "ON table_info.oid = constraint_info.conrelid "
                            "JOIN pg_catalog.pg_namespace AS namespace_info "
                            "ON namespace_info.oid = table_info.relnamespace "
                            "WHERE namespace_info.nspname = current_schema() "
                            "AND table_info.relname = 'calendar_events' "
                            "AND constraint_info.conname = "
                            "'uq_calendar_events_connection_calendar_provider_event'"
                        )
                    )
                ).one()
                return bool(row[0]), bool(row[1])
        finally:
            await engine.dispose()

    assert asyncio.run(read_constraint_mode()) == (True, True)


def test_calendar_event_identity_migration_rejects_wrong_shape_named_index(
    empty_migration_database: URL,
) -> None:
    """同名但列形状错误的 destination index 必须保留并 preflight 拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    run_alembic_upgrade(alembic_config, "20260808_0017")

    async def create_wrong_shape_index() -> None:
        """创建同名二列索引，复现人工/错误部署对象。"""
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
                        "uq_calendar_events_connection_calendar_provider_event "
                        "ON calendar_events (connection_id, provider_event_id)"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(create_wrong_shape_index())
    before_rows = _calendar_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0018_resume_preflight_probe(alembic_config)

    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"
    assert mutation_attempts == ()
    assert _alembic_revisions(empty_migration_database) == {"20260808_0017"}
    assert _calendar_resume_rows(empty_migration_database) == before_rows

    async def read_index_columns() -> tuple[str, ...]:
        """读取错误对象，证明 fail closed 没有清理非本迁移目标。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                row = await connection.execute(
                    text(
                        "SELECT ARRAY("
                        "SELECT attribute.attname FROM unnest(index_info.indkey) "
                        "WITH ORDINALITY AS index_key(attnum, position) "
                        "JOIN pg_catalog.pg_attribute AS attribute "
                        "ON attribute.attrelid = index_info.indrelid "
                        "AND attribute.attnum = index_key.attnum "
                        "ORDER BY index_key.position) "
                        "FROM pg_catalog.pg_index AS index_info "
                        "JOIN pg_catalog.pg_class AS index_class "
                        "ON index_class.oid = index_info.indexrelid "
                        "WHERE index_class.relname = "
                        "'uq_calendar_events_connection_calendar_provider_event'"
                    )
                )
                row_value = row.scalar_one()
                return tuple(str(column) for column in row_value)
        finally:
            await engine.dispose()

    assert asyncio.run(read_index_columns()) == ("connection_id", "provider_event_id")


def test_calendar_event_identity_migration_downgrades_without_cross_calendar_duplicates(
    empty_migration_database: URL,
) -> None:
    """没有跨日历重复时 downgrade 可恢复旧约束且不保留三元身份。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    run_alembic_upgrade(alembic_config, "20260809_0018")
    run_alembic_downgrade(alembic_config, "20260808_0017")

    assert _alembic_revisions(empty_migration_database) == {"20260808_0017"}
    assert _calendar_event_identity_constraints(empty_migration_database) == {
        "uq_calendar_events_connection_provider_event": (
            "connection_id",
            "provider_event_id",
        )
    }


def test_calendar_event_identity_migration_scopes_ids_per_calendar(
    empty_migration_database: URL,
) -> None:
    """0018 保留历史事件、放宽跨日历同 ID，并继续拒绝相同三元组。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    run_alembic_upgrade(alembic_config, "20260808_0017")

    user_id = "00000000-0000-0000-0000-000000000301"
    connection_id = "00000000-0000-0000-0000-000000000302"
    historical_event_id = "00000000-0000-0000-0000-000000000303"

    async def seed_historical_event() -> None:
        """只使用 0017 已存在列写入一个合成 primary 历史事件。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users ("
                        "email, display_name, password_hash, timezone, locale, brief_time, "
                        "is_active, id"
                        ") VALUES ("
                        "'calendar-identity@example.test', 'Calendar Identity', NULL, "
                        "'UTC', 'zh-CN', '08:00', TRUE, CAST(:user_id AS uuid)"
                        ")"
                    ),
                    {"user_id": user_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO oauth_connections ("
                        "user_id, provider, provider_account_id, account_email, scopes, "
                        "status, last_error_code, id"
                        ") VALUES ("
                        "CAST(:user_id AS uuid), 'google', 'calendar-identity-subject', "
                        "'calendar-identity@example.test', '[]'::jsonb, 'connected', NULL, "
                        "CAST(:connection_id AS uuid)"
                        ")"
                    ),
                    {"user_id": user_id, "connection_id": connection_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO calendar_events ("
                        "user_id, connection_id, provider_event_id, calendar_id, title, "
                        "starts_at, ends_at, all_day, transparency, status, timezone, etag, "
                        "provider_url, id"
                        ") VALUES ("
                        "CAST(:user_id AS uuid), CAST(:connection_id AS uuid), "
                        "'shared-event-id', 'primary', 'Historical primary event', "
                        "'2030-01-02T01:00:00+00:00', '2030-01-02T02:00:00+00:00', FALSE, "
                        "'opaque', 'confirmed', 'UTC', 'historical-etag', "
                        "'https://calendar.example.test/primary/shared-event-id', "
                        "CAST(:event_id AS uuid)"
                        ")"
                    ),
                    {
                        "user_id": user_id,
                        "connection_id": connection_id,
                        "event_id": historical_event_id,
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_historical_event())
    run_alembic_upgrade(alembic_config, "20260809_0018")

    async def read_events() -> tuple[tuple[str, str, str], ...]:
        """读取迁移后的业务行，验证迁移不删除、合并或重写历史投影。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT id::text, calendar_id, title FROM calendar_events "
                        "ORDER BY calendar_id, id"
                    )
                )
                return tuple((str(row[0]), str(row[1]), str(row[2])) for row in rows)
        finally:
            await engine.dispose()

    assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
    read_events_result = asyncio.run(read_events())
    assert read_events_result
    assert read_events_result == ((historical_event_id, "primary", "Historical primary event"),)
    assert _calendar_event_identity_constraints(empty_migration_database) == {
        "uq_calendar_events_connection_calendar_provider_event": (
            "connection_id",
            "calendar_id",
            "provider_event_id",
        )
    }

    async def insert_event(*, event_id: str, calendar_id: str, title: str) -> None:
        """插入一个完整合成事件，用真实唯一约束验证三元身份语义。"""
        engine = create_async_engine(empty_migration_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO calendar_events ("
                        "user_id, connection_id, provider_event_id, calendar_id, title, "
                        "starts_at, ends_at, all_day, transparency, status, timezone, etag, "
                        "provider_url, id"
                        ") VALUES ("
                        "CAST(:user_id AS uuid), CAST(:connection_id AS uuid), "
                        "'shared-event-id', :calendar_id, :title, "
                        "'2030-01-03T01:00:00+00:00', '2030-01-03T02:00:00+00:00', FALSE, "
                        "'opaque', 'confirmed', 'UTC', 'secondary-etag', "
                        "'https://calendar.example.test/secondary/shared-event-id', "
                        "CAST(:event_id AS uuid)"
                        ")"
                    ),
                    {
                        "user_id": user_id,
                        "connection_id": connection_id,
                        "calendar_id": calendar_id,
                        "title": title,
                        "event_id": event_id,
                    },
                )
        finally:
            await engine.dispose()

    secondary_event_id = "00000000-0000-0000-0000-000000000304"
    asyncio.run(
        insert_event(
            event_id=secondary_event_id,
            calendar_id="readonly@example.test",
            title="Readonly event with shared ID",
        )
    )
    with pytest.raises(IntegrityError):
        asyncio.run(
            insert_event(
                event_id="00000000-0000-0000-0000-000000000305",
                calendar_id="readonly@example.test",
                title="Duplicate readonly identity",
            )
        )

    assert asyncio.run(read_events()) == (
        (historical_event_id, "primary", "Historical primary event"),
        (
            secondary_event_id,
            "readonly@example.test",
            "Readonly event with shared ID",
        ),
    )
    with pytest.raises(RuntimeError, match="cannot restore legacy calendar event identity"):
        run_alembic_downgrade(alembic_config, "20260808_0017")
    assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
    assert len(asyncio.run(read_events())) == 2


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
    run_alembic_upgrade(alembic_config, "20260806_0012")
    _seed_provider_neutral_cursor_migration_rows(empty_migration_database)
    before = _sync_cursor_rows(empty_migration_database)

    run_alembic_upgrade(alembic_config, "20260806_0013")
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
    run_alembic_upgrade(alembic_config, "head")

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


@dataclass(frozen=True, slots=True)
class _RevisionResumeScenario:
    """描述 0016～0018 一个固定 source→destination 崩溃恢复场景。"""

    source_revision: str
    destination_revision: str
    artifact_table: str
    artifact_name: str


_REVISION_RESUME_SCENARIOS = (
    _RevisionResumeScenario(
        source_revision="20260808_0015",
        destination_revision="20260808_0016",
        artifact_table="email_messages",
        artifact_name="connection_id",
    ),
    _RevisionResumeScenario(
        source_revision="20260808_0016",
        destination_revision="20260808_0017",
        artifact_table="email_messages",
        artifact_name="uq_email_messages_connection_provider_message",
    ),
    _RevisionResumeScenario(
        source_revision="20260808_0017",
        destination_revision="20260809_0018",
        artifact_table="calendar_events",
        artifact_name="uq_calendar_events_connection_calendar_provider_event",
    ),
)


@dataclass(frozen=True, slots=True)
class _LifecycleStepProjection:
    """提供 Cycle 6 callback metadata 负例使用的最小 Alembic step 投影。"""

    is_upgrade: bool
    is_stamp: bool
    source_revision_ids: tuple[str, ...]
    destination_revision_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LifecycleContextProjection:
    """冻结 callback 必须使用的同步 Connection identity。"""

    connection: Connection


@dataclass(slots=True)
class _OrderedCalendarAadGuardFake:
    """把 0019 final guard 纳入逐步 lifecycle 顺序记录。"""

    events: list[tuple[str, str, str]]
    calls: list[tuple[Connection, str]] = field(default_factory=list)

    def verify(self, *, connection: Connection, phase: str) -> None:
        """只记录无内容阶段；before_commit 必须发生在 destination verify 之后。"""
        assert phase in {"before_mutation", "before_commit"}
        self.calls.append((connection, phase))
        if phase == "before_commit":
            self.events.append(("guard", "20260809_0019", phase))


@dataclass(slots=True)
class _TransactionalFailureCalendarAadGuardFake:
    """在最终 guard 写入合成副作用后失败，用于证明同事务完整回滚。"""

    reject_before_commit: bool
    calls: list[tuple[Connection, str]] = field(default_factory=list)

    def verify(self, *, connection: Connection, phase: str) -> None:
        """最终阶段只改合成 display_name，失败后该写入必须与 0019 一起回滚。"""
        assert phase in {"before_mutation", "before_commit"}
        self.calls.append((connection, phase))
        if phase != "before_commit":
            return
        connection.execute(
            text(
                "UPDATE users SET display_name = 'Synthetic transactional guard marker' "
                "WHERE email = 'calendar-aad-0019@example.test'"
            )
        )
        if self.reject_before_commit:
            raise RuntimeError("synthetic transactional lifecycle failure")


def _cycle6_config(database_url: URL, guard: object | None = None) -> Config:
    """构造 Cycle 6 disposable Alembic 配置，不记录含凭据 URL。"""
    return _calendar_aad_config(database_url, guard)


def _seed_cycle6_mail_rows(database_url: URL) -> None:
    """在 0015 写入两条稳定邮件，供 partial backfill 与行指纹测试复用。"""
    _seed_pre_identity_mail_rows(database_url, duplicate=False)
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO email_messages ("
                    "id, user_id, thread_id, provider_message_id, received_at, "
                    "mailbox_scope_key, sender, recipients, subject, snippet, "
                    "body_ciphertext, body_nonce, body_key_version, labels, headers, "
                    "provider_url, created_at, updated_at"
                    ") VALUES ("
                    "'00000000-0000-0000-0000-000000000222', "
                    "'00000000-0000-0000-0000-000000000201', "
                    "'00000000-0000-0000-0000-000000000212', "
                    "'synthetic-message-resume-two', now(), 'archive', "
                    "'{}'::jsonb, '[]'::jsonb, 'Synthetic resume message', '', "
                    "NULL, NULL, NULL, '[]'::jsonb, '{}'::jsonb, "
                    "'https://example.test/message/resume-two', now(), now()"
                    ")"
                )
            )
    finally:
        engine.dispose()


def _set_cycle6_provider_updated_at(
    database_url: URL,
    *,
    message_id: str,
    provider_updated_at: datetime,
) -> None:
    """为 0017 resume fixture 写入一条既有非 NULL supplier version。"""
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE email_messages "
                    "SET provider_updated_at = :provider_updated_at "
                    "WHERE id = CAST(:message_id AS uuid)"
                ),
                {
                    "message_id": message_id,
                    "provider_updated_at": provider_updated_at,
                },
            )
    finally:
        engine.dispose()


def _seed_cycle6_calendar_row(database_url: URL) -> None:
    """在 0017 写入一条合成 CalendarEvent，证明 0018 candidate 不改业务行。"""
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users ("
                    "id, email, display_name, password_hash, timezone, locale, "
                    "brief_time, is_active, created_at, updated_at"
                    ") VALUES ("
                    "'00000000-0000-0000-0000-000000006201', "
                    "'cycle6-calendar@example.test', 'Cycle 6 Calendar', NULL, "
                    "'UTC', 'zh-CN', '08:00:00', TRUE, now(), now()"
                    ")"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO oauth_connections ("
                    "id, user_id, provider, provider_account_id, account_email, scopes, "
                    "status, last_error_code, created_at, updated_at"
                    ") VALUES ("
                    "'00000000-0000-0000-0000-000000006202', "
                    "'00000000-0000-0000-0000-000000006201', 'google', "
                    "'cycle6-calendar-account', 'cycle6-calendar@example.test', "
                    "'[]'::jsonb, 'connected', NULL, now(), now()"
                    ")"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO calendar_events ("
                    "id, user_id, connection_id, provider_event_id, calendar_id, title, "
                    "starts_at, ends_at, all_day, transparency, status, timezone, etag, "
                    "provider_url, created_at, updated_at"
                    ") VALUES ("
                    "'00000000-0000-0000-0000-000000006203', "
                    "'00000000-0000-0000-0000-000000006201', "
                    "'00000000-0000-0000-0000-000000006202', "
                    "'cycle6-event', 'primary', 'Cycle 6 event', "
                    "'2030-01-01T01:00:00+00:00', '2030-01-01T02:00:00+00:00', "
                    "FALSE, 'opaque', 'confirmed', 'UTC', 'cycle6-etag', "
                    "'https://calendar.example.test/cycle6-event', now(), now()"
                    ")"
                )
            )
    finally:
        engine.dispose()


def _mail_resume_rows(database_url: URL) -> tuple[tuple[str, str | None, str], ...]:
    """读取邮件 ID、DML-owned connection 投影与跨 Schema 业务指纹。"""
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            has_provider_updated_at = bool(
                connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = 'public' "
                        "AND table_name = 'email_messages' "
                        "AND column_name = 'provider_updated_at'"
                        ")"
                    )
                ).scalar_one()
            )
            # source 0015 尚无 provider_updated_at；显式补一个 JSON null，使其与
            # 0016/0017 中真实 NULL 的同一字段 canonical 相等。字段一旦存在，真实值
            # （包括 NULL）都会进入 fingerprint；跨 revision 唯一允许变化的只有
            # DML-owned connection_id。
            provider_expression = (
                "to_jsonb(message)->'provider_updated_at'"
                if has_provider_updated_at
                else "CAST(NULL AS jsonb)"
            )
            rows = connection.execute(
                text(
                    "SELECT message.id::text, to_jsonb(message)->>'connection_id', "
                    "md5(((to_jsonb(message) || "
                    "jsonb_build_object('provider_updated_at', "
                    f"{provider_expression})) - 'connection_id')::text) "
                    "FROM email_messages AS message ORDER BY message.id"
                )
            )
            return tuple(
                (
                    str(row[0]),
                    str(row[1]) if row[1] is not None else None,
                    str(row[2]),
                )
                for row in rows
            )
    finally:
        engine.dispose()


def _mail_provider_updated_at_rows(
    database_url: URL,
) -> tuple[tuple[str, datetime | None], ...]:
    """读取 provider_updated_at，兼容 0015 缺列并保留 SQL NULL 语义。"""
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            has_provider_updated_at = bool(
                connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = 'public' "
                        "AND table_name = 'email_messages' "
                        "AND column_name = 'provider_updated_at'"
                        ")"
                    )
                ).scalar_one()
            )
            provider_expression = (
                "message.provider_updated_at"
                if has_provider_updated_at
                else "CAST(NULL AS timestamptz)"
            )
            rows = connection.execute(
                text(
                    "SELECT message.id::text, "
                    f"{provider_expression} "
                    "FROM email_messages AS message ORDER BY message.id"
                )
            )
            return tuple(
                (
                    str(row[0]),
                    row[1] if isinstance(row[1], datetime) else None,
                )
                for row in rows
            )
    finally:
        engine.dispose()


def _mail_resume_candidate_rows(
    database_url: URL,
) -> tuple[tuple[str, str | None, str | None, str], ...]:
    """读取 0016 两个 expand 列、行身份与 source 业务列指纹。

    ``to_jsonb`` 允许同一 helper 同时读取 source、只出现一个 expand 列的 partial
    catalog，以及完整 crash candidate；缺失键与 SQL ``NULL`` 都规范为 ``None``。
    该 helper 为了跨 partial catalog 保持 SQL 可执行，排除两个可能尚不存在的列；
    ``provider_updated_at`` 的值由独立的 ``_mail_provider_updated_at_rows`` 冻结。
    完整 source→destination fingerprint 则统一使用 ``_mail_resume_rows``，其中只排除
    DML-owned ``connection_id``。
    """
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            has_provider_updated_at = bool(
                connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = 'public' "
                        "AND table_name = 'email_messages' "
                        "AND column_name = 'provider_updated_at'"
                        ")"
                    )
                ).scalar_one()
            )
            provider_expression = (
                "to_jsonb(message)->'provider_updated_at'"
                if has_provider_updated_at
                else "CAST(NULL AS jsonb)"
            )
            rows = connection.execute(
                text(
                    "SELECT message.id::text, to_jsonb(message)->>'connection_id', "
                    f"{provider_expression}::text, "
                    "md5(((to_jsonb(message) || "
                    "jsonb_build_object('provider_updated_at', "
                    f"{provider_expression})) - 'connection_id')::text) "
                    "FROM email_messages AS message ORDER BY message.id"
                )
            )
            return tuple(
                (
                    str(row[0]),
                    str(row[1]) if row[1] is not None else None,
                    str(row[2]) if row[2] is not None else None,
                    str(row[3]),
                )
                for row in rows
            )
    finally:
        engine.dispose()


def _calendar_resume_rows(database_url: URL) -> tuple[tuple[str, str], ...]:
    """读取 0018 前后 CalendarEvent 身份和完整行指纹。"""
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT event.id::text, md5(to_jsonb(event)::text) "
                    "FROM calendar_events AS event ORDER BY event.id"
                )
            )
            return tuple((str(row[0]), str(row[1])) for row in rows)
    finally:
        engine.dispose()


def _seed_exact_resume_candidate(
    database_url: URL,
    scenario: _RevisionResumeScenario,
) -> None:
    """只写入对应 revision 真实 crash 后可能持久化的精确 artifact/DML 子集。"""
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        if scenario.destination_revision == "20260808_0016":
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE email_messages ADD COLUMN connection_id uuid NULL")
                )
                connection.execute(
                    text(
                        "ALTER TABLE email_messages ADD COLUMN provider_updated_at "
                        "timestamp with time zone NULL"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE email_messages AS message SET connection_id = thread.connection_id "
                        "FROM email_threads AS thread "
                        "WHERE message.thread_id = thread.id "
                        "AND message.id = '00000000-0000-0000-0000-000000000221'"
                    )
                )
        elif scenario.destination_revision == "20260808_0017":
            with engine.begin() as connection:
                # 模拟 0016 部署窗口内旧实例写入的剩余 NULL；第一行保持已提交合法子集。
                connection.execute(
                    text(
                        "UPDATE email_messages SET connection_id = NULL "
                        "WHERE id = '00000000-0000-0000-0000-000000000222'"
                    )
                )
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        "uq_email_messages_connection_provider_message "
                        "ON email_messages (connection_id, provider_message_id)"
                    )
                )
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY uq_email_threads_id_connection_user "
                        "ON email_threads (id, connection_id, user_id)"
                    )
                )
        elif scenario.destination_revision == "20260809_0018":
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        "uq_calendar_events_connection_calendar_provider_event "
                        "ON calendar_events (connection_id, calendar_id, provider_event_id)"
                    )
                )
        else:
            raise AssertionError("unknown Cycle 6 resume destination")
    finally:
        engine.dispose()


def _resume_artifact_exists(
    database_url: URL,
    scenario: _RevisionResumeScenario,
) -> bool:
    """证明已提交 candidate 在失败后仍可见，不把 autocommit 误报成整体回滚。"""
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            if scenario.destination_revision == "20260808_0016":
                return bool(
                    connection.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :table_name "
                            "AND column_name = :artifact_name)"
                        ),
                        {
                            "table_name": scenario.artifact_table,
                            "artifact_name": scenario.artifact_name,
                        },
                    ).scalar_one()
                )
            return bool(
                connection.execute(
                    text(
                        "SELECT to_regclass('public.' || CAST(:artifact_name AS text)) "
                        "IS NOT NULL"
                    ),
                    {"artifact_name": scenario.artifact_name},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _seed_unknown_resume_artifact(
    database_url: URL,
    scenario: _RevisionResumeScenario,
) -> None:
    """加入该 revision 从未声明的 column/constraint，正确 admission 必须 fail closed。"""
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            if scenario.destination_revision == "20260808_0017":
                connection.execute(
                    text(
                        "ALTER TABLE email_messages ADD CONSTRAINT "
                        "ck_cycle6_unknown_resume_artifact CHECK (subject IS NOT NULL)"
                    )
                )
            else:
                connection.execute(
                    text(
                        f"ALTER TABLE {scenario.artifact_table} "
                        "ADD COLUMN cycle6_unknown_resume_artifact integer NULL"
                    )
                )
    finally:
        engine.dispose()


def _unknown_resume_artifact_exists(
    database_url: URL,
    scenario: _RevisionResumeScenario,
) -> bool:
    """读取未知 artifact，验证 fail-closed 没有替调用方清理 catalog。"""
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            if scenario.destination_revision == "20260808_0017":
                return bool(
                    connection.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint "
                            "WHERE conname = 'ck_cycle6_unknown_resume_artifact')"
                        )
                    ).scalar_one()
                )
            return bool(
                connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :table_name "
                        "AND column_name = 'cycle6_unknown_resume_artifact')"
                    ),
                    {"table_name": scenario.artifact_table},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _prepare_resume_source(
    database_url: URL,
    scenario: _RevisionResumeScenario,
) -> Config:
    """逐步到达 scenario source 并写入用于验证 DML 边界的合成业务行。"""
    config = _cycle6_config(database_url)
    run_alembic_upgrade(config, scenario.source_revision)
    if scenario.destination_revision in {"20260808_0016", "20260808_0017"}:
        _seed_cycle6_mail_rows(database_url)
    else:
        _seed_cycle6_calendar_row(database_url)
    return config


def _run_0016_resume_preflight_probe(config: Config) -> tuple[Exception | None, tuple[str, ...]]:
    """运行 0016 重入并在首个 revision mutation 前设置只记录不清理的绊线。

    正确 resume admission 必须先抛出 ``ObjectGrantInvariantError``，因此监听器不应观察
    到 nullable expand DDL、固定批次 backfill、版本行或授权写入。当前 RED 若越过
    preflight，监听器会在 SQL 发往 PostgreSQL 前抛错；调用方仍可继续核对 durable
    candidate、旧 revision 与业务行指纹，避免把测试注入误报成整体回滚证明。

    Args:
        config: 已绑定 disposable source 数据库的 Cycle 6 Alembic 配置。

    Returns:
        捕获到的迁移异常，以及所有越过 preflight 的 mutation 类别。
    """
    mutation_attempts: list[str] = []
    migration_error: Exception | None = None

    def reject_0016_mutation(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """若 descriptor 未在 operation 前拒绝，记录稳定类别并中止首条写入。"""
        normalized = " ".join(statement.casefold().replace('"', "").split())
        if normalized.startswith(
            (
                "alter table email_messages ",
                "alter table public.email_messages ",
                "update alembic_version ",
                "update public.alembic_version ",
                "grant ",
                "revoke ",
            )
        ):
            mutation_attempts.append("0016_revision_mutation")
            raise AssertionError("0016 resume descriptor reached revision mutation")
        if normalized.startswith("with batch as ") and (
            "update email_messages as message set connection_id" in normalized
        ):
            mutation_attempts.append("0016_bounded_backfill")
            raise AssertionError("0016 resume descriptor reached bounded backfill")

    event.listen(Engine, "before_cursor_execute", reject_0016_mutation)
    try:
        try:
            run_alembic_upgrade(config, "20260808_0016")
        except Exception as error:  # noqa: BLE001 - RED 冻结未来 typed invariant 边界。
            migration_error = error
    finally:
        event.remove(Engine, "before_cursor_execute", reject_0016_mutation)
    return migration_error, tuple(mutation_attempts)


def _run_0017_resume_preflight_probe(config: Config) -> tuple[Exception | None, tuple[str, ...]]:
    """运行 0017 重入并在 catch-up 或任一 contract mutation 前设置绊线。

    0017 的真实持久顺序是先执行 bounded ``NULL → owning connection_id`` 追赶，
    再按 message、thread 顺序创建并发索引，最后才进入约束事务。closed resume
    descriptor 必须在这些原 revision 操作之前拒绝错误形状索引或提前 destination
    constraint；监听器只中止首次越界写入，不删除或修复 durable catalog。

    Args:
        config: 已绑定 0016 disposable source 数据库的 Cycle 6 Alembic 配置。

    Returns:
        捕获到的迁移异常，以及所有越过 preflight 的 mutation 类别。
    """
    mutation_attempts: list[str] = []
    migration_error: Exception | None = None

    def reject_0017_mutation(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """若 descriptor 未先拒绝，记录首个 catch-up/catalog/version/grant 写入。"""
        normalized = " ".join(statement.casefold().replace('"', "").split())
        if normalized.startswith("with batch as ") and (
            "update email_messages as message set connection_id" in normalized
        ):
            mutation_attempts.append("0017_bounded_backfill")
            raise AssertionError("0017 resume descriptor reached bounded backfill")
        if normalized.startswith(
            (
                "alter table email_messages ",
                "alter table public.email_messages ",
                "alter table email_threads ",
                "alter table public.email_threads ",
                "create unique index concurrently ",
                "drop index concurrently ",
                "update alembic_version ",
                "update public.alembic_version ",
                "grant ",
                "revoke ",
            )
        ):
            mutation_attempts.append("0017_revision_mutation")
            raise AssertionError("0017 resume descriptor reached revision mutation")

    event.listen(Engine, "before_cursor_execute", reject_0017_mutation)
    try:
        try:
            run_alembic_upgrade(config, "20260808_0017")
        except Exception as error:  # noqa: BLE001 - RED 冻结未来 typed invariant 边界。
            migration_error = error
    finally:
        event.remove(Engine, "before_cursor_execute", reject_0017_mutation)
    return migration_error, tuple(mutation_attempts)


def _run_0018_resume_preflight_probe(config: Config) -> tuple[Exception | None, tuple[str, ...]]:
    """运行 0018 重入并证明 catalog admission 先于全部 contract mutation。

    0018 只有一个可归因的事务外三元索引 artifact；它不拥有业务行 DML。监听器覆盖
    并发索引、metadata contract、版本行与授权写入，确保错误 column/index/constraint
    或提前 destination constraint 不会被 migration-local cleanup 触碰。
    """
    mutation_attempts: list[str] = []
    migration_error: Exception | None = None

    def reject_0018_mutation(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """记录并中止 0018 首条 DDL、version 或 grant mutation。"""
        normalized = " ".join(statement.casefold().replace('"', "").split())
        if normalized.startswith(
            (
                "create unique index concurrently ",
                "drop index concurrently ",
                "alter table calendar_events ",
                "alter table public.calendar_events ",
                "update alembic_version ",
                "update public.alembic_version ",
                "grant ",
                "revoke ",
            )
        ):
            mutation_attempts.append("0018_revision_mutation")
            raise AssertionError("0018 resume descriptor reached revision mutation")

    event.listen(Engine, "before_cursor_execute", reject_0018_mutation)
    try:
        try:
            run_alembic_upgrade(config, "20260809_0018")
        except Exception as error:  # noqa: BLE001 - RED 冻结未来 typed invariant 边界。
            migration_error = error
    finally:
        event.remove(Engine, "before_cursor_execute", reject_0018_mutation)
    return migration_error, tuple(mutation_attempts)


@pytest.mark.parametrize(
    "drift_sql",
    (
        pytest.param(
            "REVOKE SELECT ON TABLE public.users FROM ai_employee_app",
            id="missing",
        ),
        pytest.param(
            "GRANT TRIGGER ON TABLE public.users TO ai_employee_retention",
            id="extra",
        ),
    ),
)
def test_object_grant_lifecycle_rejects_source_drift_before_revision_operation(
    empty_migration_database: URL,
    monkeypatch: pytest.MonkeyPatch,
    drift_sql: str,
) -> None:
    """post-admission missing/extra ACL 必须由 migration precheck 零写拒绝。"""
    import ai_employee.infrastructure.db.alembic as alembic_module
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    config = _cycle6_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    before_revision = _alembic_revisions(empty_migration_database)
    mutation_attempts: list[str] = []
    precheck_calls: list[str] = []
    listener_installed = False
    original_precheck = alembic_module.verify_pre_migration_object_grants

    def record_precheck(
        connection: Connection,
        *,
        revision: str,
        destination_revision: str | None,
        phase: object,
    ) -> object:
        """证明 drift 由 ``run_migrations`` 前的 source inventory verifier 捕获。"""
        precheck_calls.append(revision)
        return original_precheck(
            connection,
            revision=revision,
            destination_revision=destination_revision,
            phase=phase,
        )

    def reject_revision_mutation(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """drift 提交后任何 0019 DDL/DML/version/grant 都表示 precheck 太晚。"""
        normalized = " ".join(statement.casefold().replace('"', "").split())
        if normalized.startswith(
            (
                "alter table calendar_events ",
                "alter table public.calendar_events ",
                "update calendar_events ",
                "update public.calendar_events ",
                "update sync_cursors ",
                "update public.sync_cursors ",
                "update alembic_version ",
                "update public.alembic_version ",
                "grant ",
                "revoke ",
            )
        ):
            mutation_attempts.append(normalized.split(" ", maxsplit=1)[0])
            raise AssertionError("source grant drift reached revision mutation")

    def inject_post_admission_drift(_bound_config: Config) -> None:
        """在 authority bind 后用独立已提交连接制造 source ACL drift。"""
        nonlocal listener_installed
        with engine.begin() as connection:
            connection.execute(text(drift_sql))
        # listener 必须在合成 drift 提交后才安装，否则会把测试自己的 GRANT/REVOKE
        # 误计为 migration mutation，掩盖真正的 source-precheck 顺序。
        event.listen(Engine, "before_cursor_execute", reject_revision_mutation)
        listener_installed = True

    monkeypatch.setattr(
        alembic_module,
        "verify_pre_migration_object_grants",
        record_precheck,
    )
    try:
        try:
            with pytest.raises(
                ObjectGrantInvariantError,
                match=r"^object grant invariant violation$",
            ):
                run_alembic_upgrade(
                    config,
                    "head",
                    after_bind_before_command=inject_post_admission_drift,
                )
        finally:
            if listener_installed:
                event.remove(Engine, "before_cursor_execute", reject_revision_mutation)

        assert precheck_calls == ["20260809_0018"]
        assert mutation_attempts == []
        assert _alembic_revisions(empty_migration_database) == before_revision == {
            "20260809_0018"
        }
        with engine.connect() as connection:
            if drift_sql.startswith("REVOKE"):
                assert not bool(
                    connection.execute(
                        text(
                            "SELECT has_table_privilege("
                            "'ai_employee_app', 'public.users', 'SELECT')"
                        )
                    ).scalar_one()
                )
            else:
                assert bool(
                    connection.execute(
                        text(
                            "SELECT has_table_privilege("
                            "'ai_employee_retention', 'public.users', 'TRIGGER')"
                        )
                    ).scalar_one()
                )
    finally:
        engine.dispose()


def test_first_install_inventory_advances_one_verified_grant_step_at_a_time(
    empty_migration_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """base 首装只由 Alembic 建 version 表，之后每步同连接完成 delta/完整验证。"""
    import ai_employee.infrastructure.db.alembic as alembic_module
    import ai_employee.infrastructure.db.database_grants as grants_module

    assert _public_table_names(empty_migration_database) == set()
    events: list[tuple[str, str, str]] = []
    lifecycle_connections: list[Connection] = []
    current_stage = "idle"
    original_precheck = alembic_module.verify_pre_migration_object_grants
    original_delta = alembic_module.apply_migration_grant_delta
    original_verify = alembic_module.verify_object_grants
    original_read_catalog = grants_module._read_catalog

    def record_catalog(connection: Connection, *, revision: str, phase: object) -> object:
        """记录 canonical catalog read 所属阶段，仍执行真实 PostgreSQL reader。"""
        # 配置/authority bootstrap 与命令成功后的 stable-admission 复核都会合法读取
        # catalog，但它们位于 ``run_migrations`` 的逐步 callback 链之外。source precheck
        # 打开观察窗口，0019 ``before_commit`` guard 则关闭它；这样原有末事件断言仍精确
        # 证明 final guard 是受测迁移链最后一个可失败 callback，而不会吞掉真实 catalog I/O。
        final_guard_seen = bool(events) and events[-1] == (
            "guard",
            "20260809_0019",
            "before_commit",
        )
        if current_stage != "idle" and not final_guard_seen:
            events.append(("catalog", revision, current_stage))
            lifecycle_connections.append(connection)
        return original_read_catalog(connection, revision=revision, phase=phase)

    def record_precheck(
        connection: Connection,
        *,
        revision: str,
        phase: object,
        **kwargs: object,
    ) -> object:
        """冻结首次 source 必须是 exact base；额外参数留给 Cycle 6 resume target。"""
        nonlocal current_stage
        current_stage = "source"
        events.append(("source", revision, "verify"))
        lifecycle_connections.append(connection)
        return original_precheck(connection, revision=revision, phase=phase, **kwargs)

    def record_delta(
        connection: Connection,
        *,
        source_revision: str,
        destination_revision: str,
        phase: object,
        before: object,
    ) -> None:
        """逐 step 记录 catalog diff 与精确授权 delta 的边界。"""
        nonlocal current_stage
        current_stage = "delta"
        events.append(("delta", source_revision, destination_revision))
        lifecycle_connections.append(connection)
        original_delta(
            connection,
            source_revision=source_revision,
            destination_revision=destination_revision,
            phase=phase,
            before=before,
        )

    def record_destination(
        connection: Connection,
        *,
        revision: str,
        phase: object,
    ) -> object:
        """验证每个 destination inventory 都已包含 alembic_version 完整 owner ACL。"""
        nonlocal current_stage
        current_stage = "destination"
        events.append(("destination", revision, "verify"))
        lifecycle_connections.append(connection)
        snapshot = original_verify(connection, revision=revision, phase=phase)
        alembic_owner_privileges = {
            grant.privilege_type
            for grant in snapshot.grants
            if grant.object_name == "alembic_version" and grant.grantee == grant.grantor
        }
        assert alembic_owner_privileges == {
            "DELETE",
            "INSERT",
            "MAINTAIN",
            "REFERENCES",
            "SELECT",
            "TRIGGER",
            "TRUNCATE",
            "UPDATE",
        }
        return snapshot

    monkeypatch.setattr(grants_module, "_read_catalog", record_catalog)
    monkeypatch.setattr(
        alembic_module,
        "verify_pre_migration_object_grants",
        record_precheck,
    )
    monkeypatch.setattr(alembic_module, "apply_migration_grant_delta", record_delta)
    monkeypatch.setattr(alembic_module, "verify_object_grants", record_destination)

    guard = _OrderedCalendarAadGuardFake(events)
    config = _cycle6_config(empty_migration_database, guard)
    published_revisions = load_published_alembic_authority(config).revisions
    run_alembic_upgrade(config, "head")

    deltas = [event_ for event_ in events if event_[0] == "delta"]
    destinations = [event_ for event_ in events if event_[0] == "destination"]
    expected_pairs = tuple(
        (published_revisions[index], published_revisions[index + 1])
        for index in range(len(published_revisions) - 1)
    )
    assert events[0] == ("source", "base", "verify")
    assert tuple((event_[1], event_[2]) for event_ in deltas) == expected_pairs
    assert tuple(event_[1] for event_ in destinations) == tuple(
        destination_revision for _source_revision, destination_revision in expected_pairs
    )
    for delta_event, destination_event in zip(deltas, destinations, strict=True):
        assert events.index(delta_event) < events.index(destination_event)
        delta_catalog_reads = [
            event_
            for event_ in events[
                events.index(delta_event) : events.index(destination_event)
            ]
            if event_[0] == "catalog" and event_[2] == "delta"
        ]
        assert delta_catalog_reads
    assert events[-1] == ("guard", "20260809_0019", "before_commit")
    assert len(guard.calls) == 2
    assert all(connection is lifecycle_connections[0] for connection in lifecycle_connections)
    assert all(connection is lifecycle_connections[0] for connection, _phase in guard.calls)
    assert set(inspect.signature(alembic_module.MigrationGrantLifecycle.on_version_apply).parameters) == {
        "self",
        "ctx",
        "step",
        "heads",
        "run_args",
    }
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for name, parameter in inspect.signature(
            alembic_module.MigrationGrantLifecycle.on_version_apply
        ).parameters.items()
        if name != "self"
    )


@pytest.mark.parametrize(
    "metadata_case",
    (
        pytest.param("stamp", id="stamp"),
        pytest.param("downgrade", id="downgrade"),
        pytest.param("branch", id="branch"),
        pytest.param("connection", id="connection"),
    ),
)
def test_object_grant_lifecycle_rejects_invalid_callback_metadata_before_grants(
    empty_migration_database: URL,
    metadata_case: str,
) -> None:
    """stamp/downgrade/branch/cross-connection metadata 均须在首条 grant 前拒绝。"""
    import ai_employee.infrastructure.db.alembic as alembic_module
    from ai_employee.infrastructure.db.database_grants import GrantPhase

    config = _cycle6_config(empty_migration_database)
    run_alembic_upgrade(config, "20260809_0018")
    authority = load_published_alembic_authority(config)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    mutation_attempts: list[str] = []

    def reject_grant(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """非法 metadata 若触发授权 SQL，立即把顺序错误暴露为测试失败。"""
        if statement.lstrip().casefold().startswith(("grant ", "revoke ")):
            mutation_attempts.append("grant")
            raise AssertionError("invalid callback metadata reached grant SQL")

    event.listen(Engine, "before_cursor_execute", reject_grant)
    try:
        with engine.connect() as connection, engine.connect() as other_connection:
            lifecycle = alembic_module.MigrationGrantLifecycle(
                connection=connection,
                authority=authority,
                expected_current_revision="20260809_0018",
                expected_target_revision="20260809_0019",
                phase=GrantPhase.BASELINE,
            )
            lifecycle.bind_calendar_aad_guard(_CalendarAadMigrationGuardFake())
            lifecycle.verify_before_migrations()
            step = _LifecycleStepProjection(
                is_upgrade=True,
                is_stamp=metadata_case == "stamp",
                source_revision_ids=("20260809_0018",),
                destination_revision_ids=("20260809_0019",),
            )
            heads = {"20260809_0019"}
            if metadata_case == "downgrade":
                step = _LifecycleStepProjection(
                    is_upgrade=False,
                    is_stamp=False,
                    source_revision_ids=("20260809_0018",),
                    destination_revision_ids=("20260808_0017",),
                )
                heads = {"20260808_0017"}
            elif metadata_case == "branch":
                step = _LifecycleStepProjection(
                    is_upgrade=True,
                    is_stamp=False,
                    source_revision_ids=("20260809_0018", "synthetic_branch"),
                    destination_revision_ids=("20260809_0019",),
                )
            callback_connection = (
                other_connection if metadata_case == "connection" else connection
            )
            with pytest.raises(AlembicMigrationInvariantError):
                lifecycle.on_version_apply(
                    ctx=_LifecycleContextProjection(callback_connection),
                    step=step,
                    heads=heads,
                    run_args={},
                )
    finally:
        event.remove(Engine, "before_cursor_execute", reject_grant)
        engine.dispose()
    assert mutation_attempts == []
    assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}


@pytest.mark.parametrize(
    "failure_point",
    (
        pytest.param("grant_sql", id="grant-sql"),
        pytest.param("destination_verify", id="destination-verify"),
        pytest.param("callback_connection", id="callback-connection"),
        pytest.param("before_commit", id="before-commit"),
    ),
)
def test_transactional_rollback_0019_covers_operation_version_grants_and_guard(
    empty_migration_database: URL,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    """0019 任一 callback failure 都必须整体回滚 DDL/DML/version/ACL/guard 写入。"""
    import ai_employee.infrastructure.db.alembic as alembic_module

    base_config = _cycle6_config(empty_migration_database)
    run_alembic_upgrade(base_config, "20260809_0018")
    fixture = _seed_calendar_aad_0019_complete_description(empty_migration_database)
    before = _calendar_aad_0019_fingerprint(fixture)
    guard = _TransactionalFailureCalendarAadGuardFake(
        reject_before_commit=failure_point == "before_commit"
    )
    config = _cycle6_config(empty_migration_database, guard)
    grant_listener: object | None = None
    foreign_engine: Engine | None = None
    foreign_connection: Connection | None = None

    if failure_point == "grant_sql":

        def reject_grant_sql(
            _connection: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            """在 0019 第一条真实 GRANT/REVOKE 发往 PostgreSQL 前注入失败。"""
            if statement.lstrip().casefold().startswith(("grant ", "revoke ")):
                raise RuntimeError("synthetic transactional lifecycle failure")

        grant_listener = reject_grant_sql
        event.listen(Engine, "before_cursor_execute", reject_grant_sql)
    elif failure_point == "destination_verify":
        original_verify = alembic_module.verify_object_grants

        def reject_destination_verify(
            connection: Connection,
            *,
            revision: str,
            phase: object,
        ) -> object:
            """先证明 destination inventory 完整，再模拟 verifier 最终失败。"""
            snapshot = original_verify(connection, revision=revision, phase=phase)
            if revision == "20260809_0019":
                raise RuntimeError("synthetic transactional lifecycle failure")
            return snapshot

        monkeypatch.setattr(alembic_module, "verify_object_grants", reject_destination_verify)
    elif failure_point == "callback_connection":
        foreign_engine = create_engine(
            empty_migration_database.set(drivername="postgresql+psycopg"),
            poolclass=NullPool,
            hide_parameters=True,
        )
        foreign_connection = foreign_engine.connect()
        original_callback = alembic_module.MigrationGrantLifecycle.on_version_apply

        def substitute_callback_connection(
            self: object,
            *,
            ctx: object,
            step: object,
            heads: set[str],
            run_args: dict[str, object],
        ) -> None:
            """保留真实 metadata，只替换 connection identity 后调用生产 validator。"""
            del ctx
            assert foreign_connection is not None
            original_callback(
                self,
                ctx=_LifecycleContextProjection(foreign_connection),
                step=step,
                heads=heads,
                run_args=run_args,
            )

        monkeypatch.setattr(
            alembic_module.MigrationGrantLifecycle,
            "on_version_apply",
            substitute_callback_connection,
        )

    try:
        with pytest.raises(
            (AlembicMigrationInvariantError, RuntimeError),
            match=r"(alembic migration invariant violation|synthetic transactional lifecycle failure)",
        ):
            run_alembic_upgrade(config, "head")
        assert _calendar_aad_0019_fingerprint(fixture) == before
        assert _alembic_revisions(empty_migration_database) == {"20260809_0018"}
        assert [phase for _connection, phase in guard.calls] == (
            ["before_mutation", "before_commit"]
            if failure_point == "before_commit"
            else ["before_mutation"]
        )
    finally:
        if grant_listener is not None:
            event.remove(Engine, "before_cursor_execute", grant_listener)
        if foreign_connection is not None:
            foreign_connection.close()
        if foreign_engine is not None:
            foreign_engine.dispose()
        fixture.engine.dispose()


@pytest.mark.parametrize(
    "scenario",
    tuple(pytest.param(scenario, id=scenario.destination_revision[-4:]) for scenario in _REVISION_RESUME_SCENARIOS),
)
def test_autocommit_resume_accepts_only_exact_durable_candidate(
    empty_migration_database: URL,
    scenario: _RevisionResumeScenario,
) -> None:
    """合法 committed artifact/subset 只由原 revision 补完，且不改非 owned 列或行集合。"""
    config = _prepare_resume_source(empty_migration_database, scenario)
    if scenario.destination_revision == "20260808_0017":
        _set_cycle6_provider_updated_at(
            empty_migration_database,
            message_id="00000000-0000-0000-0000-000000000221",
            provider_updated_at=datetime(2032, 1, 2, 3, 4, tzinfo=UTC),
        )
    before_provider_updated_at = _mail_provider_updated_at_rows(empty_migration_database)
    before_rows = (
        _mail_resume_rows(empty_migration_database)
        if scenario.destination_revision != "20260809_0018"
        else _calendar_resume_rows(empty_migration_database)
    )
    _seed_exact_resume_candidate(empty_migration_database, scenario)
    assert _resume_artifact_exists(empty_migration_database, scenario)
    durable_rows = (
        _mail_resume_rows(empty_migration_database)
        if scenario.destination_revision != "20260809_0018"
        else _calendar_resume_rows(empty_migration_database)
    )

    run_alembic_upgrade(config, scenario.destination_revision)

    after_rows = (
        _mail_resume_rows(empty_migration_database)
        if scenario.destination_revision != "20260809_0018"
        else _calendar_resume_rows(empty_migration_database)
    )
    assert _mail_provider_updated_at_rows(empty_migration_database) == before_provider_updated_at
    assert _alembic_revisions(empty_migration_database) == {scenario.destination_revision}
    assert tuple((row[0], row[-1]) for row in after_rows) == tuple(
        (row[0], row[-1]) for row in before_rows
    )
    assert len(after_rows) == len(before_rows)
    if scenario.destination_revision != "20260809_0018":
        assert any(row[1] is None for row in durable_rows)
        assert {row[1] for row in after_rows} == {
            "00000000-0000-0000-0000-000000000202"
        }
    else:
        assert after_rows == before_rows == durable_rows
    if scenario.destination_revision == "20260808_0016":
        assert all(value is None for _, value in before_provider_updated_at)
    elif scenario.destination_revision == "20260808_0017":
        assert {value for _, value in before_provider_updated_at} == {
            None,
            datetime(2032, 1, 2, 3, 4, tzinfo=UTC),
        }


def test_autocommit_resume_0017_accepts_only_first_exact_index_in_real_order(
    empty_migration_database: URL,
) -> None:
    """0017 在只完成第一枚精确并发索引时必须沿原顺序继续而非误拒绝。

    该现场对应同一个 autocommit block 已提交 message index、尚未开始 thread
    index 的真实中断点。重入仍由原 revision 完成 pending connection 投影、第二枚
    索引和 metadata contract；测试同时冻结行集合与非 owned 业务列指纹。
    """
    scenario = _REVISION_RESUME_SCENARIOS[1]
    config = _prepare_resume_source(empty_migration_database, scenario)
    _set_cycle6_provider_updated_at(
        empty_migration_database,
        message_id="00000000-0000-0000-0000-000000000221",
        provider_updated_at=datetime(2033, 2, 3, 4, 5, tzinfo=UTC),
    )
    before_provider_updated_at = _mail_provider_updated_at_rows(empty_migration_database)
    before_rows = _mail_resume_rows(empty_migration_database)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX CONCURRENTLY "
                    "uq_email_messages_connection_provider_message "
                    "ON email_messages (connection_id, provider_message_id)"
                )
            )
    finally:
        engine.dispose()

    assert _email_identity_index_metadata(empty_migration_database) == {
        "uq_email_messages_connection_provider_message": (
            True,
            True,
            True,
            True,
            2,
            2,
            ("connection_id", "provider_message_id"),
        )
    }

    run_alembic_upgrade(config, scenario.destination_revision)

    after_rows = _mail_resume_rows(empty_migration_database)
    assert _mail_provider_updated_at_rows(empty_migration_database) == before_provider_updated_at
    assert _alembic_revisions(empty_migration_database) == {scenario.destination_revision}
    assert tuple((row[0], row[-1]) for row in after_rows) == tuple(
        (row[0], row[-1]) for row in before_rows
    )
    assert {row[1] for row in after_rows} == {
        "00000000-0000-0000-0000-000000000202"
    }
    assert {value for _, value in before_provider_updated_at} == {
        None,
        datetime(2033, 2, 3, 4, 5, tzinfo=UTC),
    }
    assert _m2_constraint_columns(empty_migration_database)[
        "uq_email_messages_connection_provider_message"
    ] == ("connection_id", "provider_message_id")
    assert _m2_constraint_columns(empty_migration_database)[
        "uq_email_threads_id_connection_user"
    ] == ("id", "connection_id", "user_id")


def test_autocommit_resume_0017_rejects_wrong_shape_index_before_operation(
    empty_migration_database: URL,
) -> None:
    """0017 同名错误形状索引必须由 admission 在 catch-up 前拒绝并保留。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    scenario = _REVISION_RESUME_SCENARIOS[1]
    config = _prepare_resume_source(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX CONCURRENTLY "
                    "uq_email_messages_connection_provider_message "
                    "ON email_messages (connection_id, mailbox_scope_key)"
                )
            )
    finally:
        engine.dispose()

    expected_index = {
        "uq_email_messages_connection_provider_message": (
            True,
            True,
            True,
            True,
            2,
            2,
            ("connection_id", "mailbox_scope_key"),
        )
    }
    durable_rows = _mail_resume_rows(empty_migration_database)
    assert any(row[1] is None for row in durable_rows)
    assert _email_identity_index_metadata(empty_migration_database) == expected_index

    migration_error, mutation_attempts = _run_0017_resume_preflight_probe(config)

    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    assert _email_identity_index_metadata(empty_migration_database) == expected_index
    assert _mail_resume_rows(empty_migration_database) == durable_rows
    assert mutation_attempts == ()
    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"


def test_autocommit_resume_0017_rejects_early_destination_constraint_before_operation(
    empty_migration_database: URL,
) -> None:
    """0017 提前挂载 destination constraint 不是 autocommit candidate，必须零写拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    scenario = _REVISION_RESUME_SCENARIOS[1]
    config = _prepare_resume_source(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE email_messages ADD CONSTRAINT "
                    "uq_email_messages_connection_provider_message "
                    "UNIQUE (connection_id, provider_message_id)"
                )
            )
    finally:
        engine.dispose()

    durable_rows = _mail_resume_rows(empty_migration_database)
    assert any(row[1] is None for row in durable_rows)
    assert _m2_constraint_columns(empty_migration_database)[
        "uq_email_messages_connection_provider_message"
    ] == ("connection_id", "provider_message_id")

    migration_error, mutation_attempts = _run_0017_resume_preflight_probe(config)

    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    assert _m2_constraint_columns(empty_migration_database)[
        "uq_email_messages_connection_provider_message"
    ] == ("connection_id", "provider_message_id")
    assert _mail_resume_rows(empty_migration_database) == durable_rows
    assert mutation_attempts == ()
    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"


def test_autocommit_resume_0018_accepts_exact_first_index_in_real_order(
    empty_migration_database: URL,
) -> None:
    """0018 已提交精确三元索引时必须沿原顺序挂载 contract。"""
    scenario = _REVISION_RESUME_SCENARIOS[2]
    config = _prepare_resume_source(empty_migration_database, scenario)
    before_rows = _calendar_resume_rows(empty_migration_database)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX CONCURRENTLY "
                    "uq_calendar_events_connection_calendar_provider_event "
                    "ON calendar_events (connection_id, calendar_id, provider_event_id)"
                )
            )
    finally:
        engine.dispose()

    run_alembic_upgrade(config, scenario.destination_revision)

    assert _alembic_revisions(empty_migration_database) == {scenario.destination_revision}
    assert _calendar_resume_rows(empty_migration_database) == before_rows
    assert _calendar_event_identity_constraints(empty_migration_database) == {
        "uq_calendar_events_connection_calendar_provider_event": (
            "connection_id",
            "calendar_id",
            "provider_event_id",
        )
    }


def test_autocommit_resume_0018_rejects_wrong_shape_index_before_operation(
    empty_migration_database: URL,
) -> None:
    """0018 同名错误形状三元索引必须保留并在任何 DDL 前拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    scenario = _REVISION_RESUME_SCENARIOS[2]
    config = _prepare_resume_source(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX CONCURRENTLY "
                    "uq_calendar_events_connection_calendar_provider_event "
                    "ON calendar_events (connection_id, provider_event_id)"
                )
            )
    finally:
        engine.dispose()

    before_rows = _calendar_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0018_resume_preflight_probe(config)

    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"
    assert mutation_attempts == ()
    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    assert _calendar_resume_rows(empty_migration_database) == before_rows


def test_autocommit_resume_0018_rejects_early_destination_constraint_before_operation(
    empty_migration_database: URL,
) -> None:
    """0018 提前挂载三元 destination constraint 不是合法 resume artifact。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    scenario = _REVISION_RESUME_SCENARIOS[2]
    config = _prepare_resume_source(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE calendar_events ADD CONSTRAINT "
                    "uq_calendar_events_connection_calendar_provider_event "
                    "UNIQUE (connection_id, calendar_id, provider_event_id)"
                )
            )
    finally:
        engine.dispose()

    before_rows = _calendar_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0018_resume_preflight_probe(config)

    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"
    assert mutation_attempts == ()
    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    assert _calendar_resume_rows(empty_migration_database) == before_rows


@pytest.mark.parametrize(
    "artifact_sql",
    (
        pytest.param(
            "ALTER TABLE calendar_events ADD COLUMN cycle6_unknown_calendar_column integer NULL",
            id="unknown-column",
        ),
        pytest.param(
            "CREATE UNIQUE INDEX cycle6_unknown_calendar_index ON calendar_events (connection_id, calendar_id)",
            id="unknown-index",
        ),
        pytest.param(
            "ALTER TABLE calendar_events ADD CONSTRAINT ck_cycle6_unknown_calendar_constraint CHECK (calendar_id IS NOT NULL)",
            id="unknown-constraint",
        ),
    ),
)
def test_autocommit_resume_0018_rejects_unknown_catalog_before_operation(
    empty_migration_database: URL,
    artifact_sql: str,
) -> None:
    """0018 source catalog 是闭集，未知列、索引或约束不得被清理或忽略。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    scenario = _REVISION_RESUME_SCENARIOS[2]
    config = _prepare_resume_source(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(text(artifact_sql))
    finally:
        engine.dispose()

    before_rows = _calendar_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0018_resume_preflight_probe(config)

    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"
    assert mutation_attempts == ()
    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    assert _calendar_resume_rows(empty_migration_database) == before_rows


def test_autocommit_resume_0018_rejects_wrong_legacy_identity_before_operation(
    empty_migration_database: URL,
) -> None:
    """0018 缺失或错误 legacy 二元 identity 时不得直接创建三元 contract。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    scenario = _REVISION_RESUME_SCENARIOS[2]
    config = _prepare_resume_source(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE calendar_events DROP CONSTRAINT "
                    "uq_calendar_events_connection_provider_event"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE calendar_events ADD CONSTRAINT "
                    "uq_calendar_events_connection_provider_event "
                    "UNIQUE (connection_id, calendar_id)"
                )
            )
    finally:
        engine.dispose()

    before_rows = _calendar_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0018_resume_preflight_probe(config)

    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"
    assert mutation_attempts == ()
    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    assert _calendar_resume_rows(empty_migration_database) == before_rows


@pytest.mark.parametrize(
    ("present_column", "expected_metadata"),
    (
        pytest.param(
            "connection_id",
            {"connection_id": ("uuid", "YES")},
            id="connection-id-only",
        ),
        pytest.param(
            "provider_updated_at",
            {"provider_updated_at": ("timestamp with time zone", "YES")},
            id="provider-updated-at-only",
        ),
    ),
)
def test_autocommit_resume_0016_rejects_partial_owned_columns_before_operation(
    empty_migration_database: URL,
    present_column: str,
    expected_metadata: dict[str, tuple[str, str]],
) -> None:
    """0016 两个 nullable expand 列只出现其一时必须在原 operation 前拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    scenario = _REVISION_RESUME_SCENARIOS[0]
    config = _prepare_resume_source(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            if present_column == "connection_id":
                connection.execute(
                    text("ALTER TABLE email_messages ADD COLUMN connection_id uuid NULL")
                )
            elif present_column == "provider_updated_at":
                connection.execute(
                    text(
                        "ALTER TABLE email_messages ADD COLUMN provider_updated_at "
                        "timestamp with time zone NULL"
                    )
                )
            else:
                raise AssertionError("unknown 0016 partial owned column")
    finally:
        engine.dispose()

    durable_rows = _mail_resume_candidate_rows(empty_migration_database)
    assert all(row[1] is None for row in durable_rows)
    assert all(row[2] is None for row in durable_rows)
    migration_error, mutation_attempts = _run_0016_resume_preflight_probe(config)

    # 即使当前 RED 越过 preflight，绊线也会在 SQL 发出前中止，因此这些断言能证明
    # 测试没有替 production code 清理 partial catalog 或补写 pending NULL。
    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    assert _email_identity_column_metadata(empty_migration_database) == expected_metadata
    assert _mail_resume_candidate_rows(empty_migration_database) == durable_rows
    assert mutation_attempts == ()
    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"


def test_autocommit_resume_0016_rejects_wrong_shape_fresh_source_column_before_operation(
    empty_migration_database: URL,
) -> None:
    """0015 fresh source 的同名错形列必须在任何 0016 operation 前拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    scenario = _REVISION_RESUME_SCENARIOS[0]
    config = _prepare_resume_source(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            # 保留同名 source 列但放宽 NOT NULL；这是 0015 不可能留下的物理形状，
            # 同时不会改变既有行值或索引，便于证明 admission 先于任一 migration mutation。
            connection.execute(
                text(
                    "ALTER TABLE email_messages "
                    "ALTER COLUMN provider_message_id DROP NOT NULL"
                )
            )
    finally:
        engine.dispose()

    before_rows = _mail_resume_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0016_resume_preflight_probe(config)

    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    shape_engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with shape_engine.connect() as connection:
            shape = connection.execute(
                text(
                    "SELECT udt_name, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'email_messages' "
                    "AND column_name = 'provider_message_id'"
                )
            ).one()
            assert (str(shape[0]), str(shape[1])) == ("varchar", "YES")
    finally:
        shape_engine.dispose()
    assert _mail_resume_rows(empty_migration_database) == before_rows
    assert mutation_attempts == ()
    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"


def test_autocommit_resume_0016_rejects_wrong_shape_candidate_source_column_before_operation(
    empty_migration_database: URL,
) -> None:
    """0016 candidate 的非 owned source 列错形时必须在任何原 operation 前拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    scenario = _REVISION_RESUME_SCENARIOS[0]
    config = _prepare_resume_source(empty_migration_database, scenario)
    _seed_exact_resume_candidate(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            # 两个 migration-owned 列保持精确 candidate 形状，只放宽既有 0015 列，
            # 证明 admission 不能用列名闭集掩盖 source catalog 的 nullable 漂移。
            connection.execute(
                text(
                    "ALTER TABLE email_messages "
                    "ALTER COLUMN provider_message_id DROP NOT NULL"
                )
            )
    finally:
        engine.dispose()

    durable_rows = _mail_resume_candidate_rows(empty_migration_database)
    migration_error, mutation_attempts = _run_0016_resume_preflight_probe(config)

    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    assert _resume_artifact_exists(empty_migration_database, scenario)
    shape_engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with shape_engine.connect() as connection:
            shape = connection.execute(
                text(
                    "SELECT udt_name, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'email_messages' "
                    "AND column_name = 'provider_message_id'"
                )
            ).one()
            assert (str(shape[0]), str(shape[1])) == ("varchar", "YES")
    finally:
        shape_engine.dispose()
    assert _mail_resume_candidate_rows(empty_migration_database) == durable_rows
    assert mutation_attempts == ()
    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"


def test_autocommit_resume_0016_rejects_unknown_provider_updated_at_before_backfill(
    empty_migration_database: URL,
) -> None:
    """0016 从未写入的 provider_updated_at 非 NULL 必须在 bounded backfill 前拒绝。"""
    from ai_employee.infrastructure.db.database_grants import ObjectGrantInvariantError

    scenario = _REVISION_RESUME_SCENARIOS[0]
    config = _prepare_resume_source(empty_migration_database, scenario)
    _seed_exact_resume_candidate(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE email_messages SET provider_updated_at = :provider_updated_at "
                    "WHERE id = '00000000-0000-0000-0000-000000000221'"
                ),
                {"provider_updated_at": datetime(2031, 1, 2, 3, 4, tzinfo=UTC)},
            )
    finally:
        engine.dispose()

    durable_rows = _mail_resume_candidate_rows(empty_migration_database)
    assert any(row[1] is None for row in durable_rows)
    assert any(row[2] is not None for row in durable_rows)
    migration_error, mutation_attempts = _run_0016_resume_preflight_probe(config)

    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    assert _email_identity_column_metadata(empty_migration_database) == {
        "connection_id": ("uuid", "YES"),
        "provider_updated_at": ("timestamp with time zone", "YES"),
    }
    assert _mail_resume_candidate_rows(empty_migration_database) == durable_rows
    assert mutation_attempts == ()
    assert type(migration_error) is ObjectGrantInvariantError
    assert str(migration_error) == "object grant invariant violation"


@pytest.mark.parametrize(
    "scenario",
    tuple(pytest.param(scenario, id=scenario.destination_revision[-4:]) for scenario in _REVISION_RESUME_SCENARIOS),
)
def test_autocommit_resume_rejects_unknown_artifact_without_cleaning_it(
    empty_migration_database: URL,
    scenario: _RevisionResumeScenario,
) -> None:
    """未知 column/constraint 必须在原 revision operation 前拒绝并原样保留现场。"""
    config = _prepare_resume_source(empty_migration_database, scenario)
    _seed_exact_resume_candidate(empty_migration_database, scenario)
    _seed_unknown_resume_artifact(empty_migration_database, scenario)
    durable_rows = (
        _mail_resume_rows(empty_migration_database)
        if scenario.destination_revision != "20260809_0018"
        else _calendar_resume_rows(empty_migration_database)
    )

    with pytest.raises(RuntimeError, match=r"object grant invariant violation"):
        run_alembic_upgrade(config, scenario.destination_revision)

    assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
    assert _resume_artifact_exists(empty_migration_database, scenario)
    assert _unknown_resume_artifact_exists(empty_migration_database, scenario)
    assert (
        _mail_resume_rows(empty_migration_database)
        if scenario.destination_revision != "20260809_0018"
        else _calendar_resume_rows(empty_migration_database)
    ) == durable_rows


@pytest.mark.parametrize(
    "scenario",
    tuple(
        pytest.param(scenario, id=scenario.destination_revision[-4:])
        for scenario in _REVISION_RESUME_SCENARIOS[:2]
    ),
)
def test_autocommit_resume_rejects_wrong_business_dml_before_filling_pending_rows(
    empty_migration_database: URL,
    scenario: _RevisionResumeScenario,
) -> None:
    """错误 non-NULL connection 必须在 bounded backfill 前拒绝，剩余 NULL 不得被补写。"""
    config = _prepare_resume_source(empty_migration_database, scenario)
    _seed_exact_resume_candidate(empty_migration_database, scenario)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO oauth_connections ("
                    "id, user_id, provider, provider_account_id, account_email, scopes, "
                    "status, last_error_code, created_at, updated_at"
                    ") VALUES ("
                    "'00000000-0000-0000-0000-000000006299', "
                    "'00000000-0000-0000-0000-000000000201', 'google', "
                    "'cycle6-wrong-owner-google', "
                    "'cycle6-wrong-owner-google@example.test', "
                    "'[]'::jsonb, 'connected', NULL, now(), now()"
                    ")"
                )
            )
            connection.execute(
                text(
                    "UPDATE email_messages SET connection_id = "
                    "'00000000-0000-0000-0000-000000006299' "
                    "WHERE id = '00000000-0000-0000-0000-000000000221'"
                )
            )
        durable_rows = _mail_resume_rows(empty_migration_database)
        assert any(row[1] is None for row in durable_rows)

        with pytest.raises(RuntimeError, match=r"object grant invariant violation"):
            run_alembic_upgrade(config, scenario.destination_revision)

        assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
        assert _resume_artifact_exists(empty_migration_database, scenario)
        assert _mail_resume_rows(empty_migration_database) == durable_rows
    finally:
        engine.dispose()


def test_autocommit_resume_extra_acl_preserves_candidate_and_old_revision(
    empty_migration_database: URL,
) -> None:
    """0018 candidate 上额外 ACL 只报告 durable candidate + 旧 revision，不做权限修复。"""
    scenario = _REVISION_RESUME_SCENARIOS[-1]
    config = _prepare_resume_source(empty_migration_database, scenario)
    _seed_exact_resume_candidate(empty_migration_database, scenario)
    before_rows = _calendar_resume_rows(empty_migration_database)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "GRANT TRIGGER ON TABLE public.calendar_events "
                    "TO ai_employee_retention"
                )
            )
        with pytest.raises(RuntimeError):
            run_alembic_upgrade(config, scenario.destination_revision)
        assert _alembic_revisions(empty_migration_database) == {scenario.source_revision}
        assert _resume_artifact_exists(empty_migration_database, scenario)
        assert _calendar_resume_rows(empty_migration_database) == before_rows
        with engine.connect() as connection:
            assert bool(
                connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'ai_employee_retention', 'public.calendar_events', 'TRIGGER')"
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()
