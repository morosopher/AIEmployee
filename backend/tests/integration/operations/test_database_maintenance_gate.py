"""以真实 PostgreSQL 17 验证 canonical database-access 与 typed lifecycle 边界。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Connection, Engine, Row, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from ai_employee.infrastructure.db.alembic import load_published_alembic_authority
from ai_employee.infrastructure.db.database_access import (
    APP_RUNTIME_ROLE_NAME,
    RETENTION_RUNTIME_ROLE_NAME,
    BootstrapCaller,
    BootstrapCandidate,
    DatabaseAccessSnapshot,
    DatabaseAclProfile,
    DatabaseAclTuple,
    RuntimeRoleSnapshot,
    classify_bootstrap_candidate,
    read_database_access_snapshot,
    read_database_access_snapshot_sync,
)
from ai_employee.infrastructure.db.database_grants import (
    COLUMN_GRANTS_SQL,
    SCHEMA_GRANTS_SQL,
    SEQUENCE_GRANTS_SQL,
    TABLE_GRANTS_SQL,
    BootstrapGrantPosture,
    GrantPhase,
    apply_bootstrap_object_grants,
    verify_bootstrap_object_grants,
    verify_object_grants,
)
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedDatabaseUrl

TARGET_IDENTITY_DOMAIN = b"ai_employee.restore_target.v1\0"
LOW_SYSTEM_IDENTIFIER = 72623859790382856
LOW_SYSTEM_IDENTIFIER_ASCII = b"72623859790382856"
HIGH_SYSTEM_IDENTIFIER_ASCII = b"18446744073709551615"
TARGET_DATABASE_NAME_UTF8 = b"ai_employee_restore_test"
EXPECTED_LOW_TARGET_DIGEST = "71f328c1e8eb5d9cd2f8e1f815afac4b723f94a274900853c895fd9d72e60e49"
EXPECTED_HIGH_TARGET_DIGEST = "ad341314fd966ed68cfc480a025a162a7c8832ad16be408e627925b1d910637b"
EXPECTED_LIFECYCLE_LOCK_KEY = -9116408252019116299
EXPECTED_TARGET_LOCK_KEY = 5971001870339114140
COMPLETION_AUTHORITY_DOMAIN = b"ai_employee.restore_completion_authority.v1\0"
PUBLISHED_AUTHORITY = load_published_alembic_authority(
    Config(Path(__file__).resolve().parents[3] / "alembic.ini")
)


class _SafeEngineStub:
    """为不打开真实 lifecycle session 的测试声明参数脱敏能力。"""

    hide_parameters = True


_SAFE_ENGINE_STUB = cast(Engine, _SafeEngineStub())


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """覆盖全局写型迁移 fixture，保证本模块只在明确用例中触发 mutation。"""
    yield


@pytest.fixture(autouse=True)
async def isolated_database() -> AsyncIterator[None]:
    """覆盖全局 TRUNCATE fixture；本模块自行比较每个用例前后的零写快照。"""
    yield


EXPECTED_DATABASE_ACL_SQL = """SELECT acl.grantee, acl.grantor, acl.privilege_type, acl.is_grantable
FROM pg_database AS db
CROSS JOIN LATERAL aclexplode(
    COALESCE(db.datacl, acldefault('d', db.datdba))
) AS acl
WHERE db.oid = :target_database_oid"""

_DATABASE_IDENTITY_SQL = """
SELECT
    db.oid AS target_database_oid,
    db.datdba AS owner_oid,
    db.datacl AS raw_database_acl,
    current_user AS current_role,
    (
        SELECT runtime_role.oid
        FROM pg_roles AS runtime_role
        WHERE runtime_role.rolname = current_user
    ) AS current_role_oid,
    current_setting('server_version_num')::integer AS server_version_num
FROM pg_database AS db
WHERE db.datname = current_database()
"""

_RUNTIME_ROLES_SQL = """
SELECT
    runtime_role.rolname,
    runtime_role.oid,
    runtime_role.rolcanlogin,
    runtime_role.rolinherit,
    runtime_role.rolsuper,
    runtime_role.rolcreatedb,
    runtime_role.rolcreaterole,
    runtime_role.rolreplication,
    runtime_role.rolbypassrls,
    runtime_role.rolconnlimit,
    runtime_role.rolvaliduntil,
    runtime_role.rolconfig
FROM pg_roles AS runtime_role
WHERE runtime_role.rolname IN (:app_role_name, :retention_role_name)
ORDER BY runtime_role.rolname
"""

_MEMBERSHIPS_SQL = """
WITH runtime_roles AS (
    SELECT runtime_role.oid
    FROM pg_roles AS runtime_role
    WHERE runtime_role.rolname IN (:app_role_name, :retention_role_name)
)
SELECT membership.roleid, membership.member, membership.grantor, membership.admin_option
FROM pg_auth_members AS membership
WHERE membership.roleid IN (SELECT oid FROM runtime_roles)
   OR membership.member IN (SELECT oid FROM runtime_roles)
ORDER BY membership.roleid, membership.member, membership.grantor, membership.admin_option
"""

_RESTORE_FACTS_SQL = """
SELECT setting.setrole, config.value
FROM pg_db_role_setting AS setting
CROSS JOIN LATERAL unnest(setting.setconfig) AS config(value)
WHERE setting.setdatabase = :target_database_oid
  AND split_part(config.value, '=', 1) IN (
      'ai_employee.maintenance_gate',
      'ai_employee.restore_call_authority',
      'ai_employee.restore_completion'
  )
ORDER BY setting.setrole, config.value
"""

_ACTIVE_NON_OWNER_SESSIONS_SQL = """
SELECT activity.pid, activity.usesysid
FROM pg_stat_activity AS activity
WHERE activity.datid = :target_database_oid
  AND activity.backend_type = 'client backend'
  AND activity.pid <> pg_backend_pid()
  AND activity.usesysid IS DISTINCT FROM :owner_oid
ORDER BY activity.pid
"""

_SCHEMA_CATALOG_QUERIES = (
    (
        "namespace",
        """
        SELECT namespace.nspname, namespace.nspowner
        FROM pg_namespace AS namespace
        WHERE namespace.nspname = 'public'
        ORDER BY namespace.nspname
        """,
    ),
    (
        "class",
        """
        SELECT
            namespace.nspname,
            relation.relname,
            relation.relkind,
            relation.relpersistence,
            relation.relowner
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
        ORDER BY relation.relname, relation.relkind
        """,
    ),
    (
        "column",
        """
        SELECT
            namespace.nspname,
            relation.relname,
            attribute.attnum,
            attribute.attname,
            format_type(attribute.atttypid, attribute.atttypmod),
            attribute.attnotnull,
            attribute.attidentity,
            attribute.attgenerated,
            pg_get_expr(default_value.adbin, default_value.adrelid, true)
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_attribute AS attribute ON attribute.attrelid = relation.oid
        LEFT JOIN pg_attrdef AS default_value
          ON default_value.adrelid = relation.oid
         AND default_value.adnum = attribute.attnum
        WHERE namespace.nspname = 'public'
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        ORDER BY relation.relname, attribute.attnum
        """,
    ),
    (
        "constraint",
        """
        SELECT
            namespace.nspname,
            relation.relname,
            constraint_row.conname,
            constraint_row.contype,
            pg_get_constraintdef(constraint_row.oid, true)
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
        ORDER BY relation.relname, constraint_row.conname
        """,
    ),
    (
        "index",
        """
        SELECT
            namespace.nspname,
            relation.relname,
            index_relation.relname,
            pg_get_indexdef(index_relation.oid)
        FROM pg_index AS index_row
        JOIN pg_class AS relation ON relation.oid = index_row.indrelid
        JOIN pg_class AS index_relation ON index_relation.oid = index_row.indexrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
        ORDER BY relation.relname, index_relation.relname
        """,
    ),
)

_BUSINESS_TABLES_SQL = """
SELECT relation.relname
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
  AND relation.relkind IN ('r', 'p')
  AND relation.relname <> 'alembic_version'
ORDER BY relation.relname
"""

_OBJECT_GRANT_CATALOG_SQL = (
    SCHEMA_GRANTS_SQL,
    TABLE_GRANTS_SQL,
    COLUMN_GRANTS_SQL,
    SEQUENCE_GRANTS_SQL,
)

_SYNTHETIC_ACTIVITY_CTE_SQL = """
WITH pg_stat_activity(datid, pid, usesysid, backend_type) AS (
    SELECT
        CAST(:activity_datid AS oid),
        CAST(:activity_pid AS integer),
        CAST(:activity_usesysid AS oid),
        CAST(:activity_backend_type AS text)
)
"""


def test_active_non_owner_session_queries_only_report_foreign_client_backends(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """维护门禁只把目标库中其他角色的 client backend 视为活动 session。

    测试用同名 CTE 遮蔽系统 ``pg_stat_activity``，从而确定性覆盖 autovacuum、
    非 owner client、owner client、当前 PID 与其他数据库，而不依赖 PostgreSQL
    后台进程的实际调度时序。production ``EXISTS`` 查询和 operations 零写快照查询
    必须保持同义，避免测试侧再次把 background worker 误报为外部业务进程。
    """
    import ai_employee.infrastructure.db.database_maintenance as maintenance_module

    _, target_url, _ = _sync_lifecycle_urls(database_url)
    engine = create_engine(target_url, poolclass=NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            identity = connection.execute(
                text(
                    "SELECT database.oid AS target_database_oid, "
                    "database.datdba AS owner_oid, pg_backend_pid() AS current_pid "
                    "FROM pg_database AS database "
                    "WHERE database.datname = current_database()"
                )
            ).mappings().one()
            target_database_oid = identity["target_database_oid"]
            owner_oid = identity["owner_oid"]
            current_pid = identity["current_pid"]
            if (
                type(target_database_oid) is not int
                or type(owner_oid) is not int
                or type(current_pid) is not int
            ):
                raise AssertionError("database activity identity has an unexpected type")

            cases = (
                (
                    "autovacuum-worker",
                    target_database_oid,
                    -101,
                    None,
                    "autovacuum worker",
                    False,
                ),
                (
                    "foreign-client",
                    target_database_oid,
                    -102,
                    owner_oid + 1,
                    "client backend",
                    True,
                ),
                (
                    "owner-client",
                    target_database_oid,
                    -103,
                    owner_oid,
                    "client backend",
                    False,
                ),
                (
                    "current-client",
                    target_database_oid,
                    current_pid,
                    owner_oid + 1,
                    "client backend",
                    False,
                ),
                (
                    "other-database-client",
                    target_database_oid + 1,
                    -104,
                    owner_oid + 1,
                    "client backend",
                    False,
                ),
            )
            production_query = text(
                _SYNTHETIC_ACTIVITY_CTE_SQL
                + maintenance_module._ACTIVE_NON_OWNER_SESSIONS_SQL
            )
            operations_query = text(
                _SYNTHETIC_ACTIVITY_CTE_SQL + _ACTIVE_NON_OWNER_SESSIONS_SQL
            )
            observed: list[tuple[str, bool, bool]] = []
            expected: list[tuple[str, bool, bool]] = []
            for (
                case_name,
                activity_datid,
                activity_pid,
                activity_usesysid,
                activity_backend_type,
                should_report,
            ) in cases:
                parameters = {
                    "target_database_oid": target_database_oid,
                    "owner_oid": owner_oid,
                    "activity_datid": activity_datid,
                    "activity_pid": activity_pid,
                    "activity_usesysid": activity_usesysid,
                    "activity_backend_type": activity_backend_type,
                }
                production_value = connection.execute(
                    production_query,
                    parameters,
                ).scalar_one()
                operations_rows = connection.execute(
                    operations_query,
                    parameters,
                ).all()
                if type(production_value) is not bool:
                    raise AssertionError("production activity query must return bool")
                observed.append((case_name, production_value, bool(operations_rows)))
                expected.append((case_name, should_report, should_report))

    finally:
        engine.dispose()

    assert observed == expected


def test_target_identity_low_and_high_bit_vectors_are_frozen() -> None:
    """目标 bytes、digest 与两类 advisory key 必须逐字匹配冻结向量。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        derive_database_target_identity,
        signed_system_identifier_to_uint64,
    )

    low = derive_database_target_identity(
        system_identifier_ascii=LOW_SYSTEM_IDENTIFIER_ASCII,
        database_name_utf8=TARGET_DATABASE_NAME_UTF8,
        target_name_verified=True,
    )
    high = derive_database_target_identity(
        system_identifier_ascii=HIGH_SYSTEM_IDENTIFIER_ASCII,
        database_name_utf8=TARGET_DATABASE_NAME_UTF8,
        target_name_verified=True,
    )

    assert signed_system_identifier_to_uint64(LOW_SYSTEM_IDENTIFIER) == LOW_SYSTEM_IDENTIFIER
    assert signed_system_identifier_to_uint64(-1) == 18446744073709551615
    assert low.identity_bytes == (
        TARGET_IDENTITY_DOMAIN + LOW_SYSTEM_IDENTIFIER_ASCII + b"\0" + TARGET_DATABASE_NAME_UTF8
    )
    assert not low.identity_bytes.endswith(b"\0")
    assert low.digest_hex == EXPECTED_LOW_TARGET_DIGEST
    assert low.lifecycle_lock_key == EXPECTED_LIFECYCLE_LOCK_KEY
    assert low.target_lock_key == EXPECTED_TARGET_LOCK_KEY
    assert high.digest_hex == EXPECTED_HIGH_TARGET_DIGEST


@pytest.mark.parametrize(
    "sql_value",
    (True, False, -(1 << 63) - 1, 1 << 63),
)
def test_signed_system_identifier_rejects_non_int64_input(sql_value: object) -> None:
    """SQL system identifier 只接受非 bool 的 signed int64。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        signed_system_identifier_to_uint64,
    )

    with pytest.raises(DatabaseMaintenanceInvariantError):
        signed_system_identifier_to_uint64(sql_value)


@pytest.mark.parametrize(
    "system_identifier_ascii",
    (
        b"",
        b"0",
        b"00",
        b"01",
        b"+1",
        b"-1",
        b" 1",
        b"1 ",
        b"18446744073709551616",
        "1",
    ),
)
def test_target_identity_rejects_noncanonical_unsigned_decimal(
    system_identifier_ascii: object,
) -> None:
    """目标身份中的 system identifier 必须是 canonical uint64 十进制 ASCII。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        derive_database_target_identity,
    )

    with pytest.raises(DatabaseMaintenanceInvariantError):
        derive_database_target_identity(
            system_identifier_ascii=system_identifier_ascii,
            database_name_utf8=TARGET_DATABASE_NAME_UTF8,
            target_name_verified=True,
        )


@pytest.mark.parametrize(
    "database_name_utf8",
    (
        b"",
        b"a" * 64,
        b"contains\0nul",
        b"invalid-utf8-\xff",
        "ai_employee_restore_test",
    ),
)
def test_target_identity_rejects_invalid_database_name_bytes(
    database_name_utf8: object,
) -> None:
    """数据库名必须是 1..63 bytes 的 exact UTF-8 且不含 NUL。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        derive_database_target_identity,
    )

    with pytest.raises(DatabaseMaintenanceInvariantError):
        derive_database_target_identity(
            system_identifier_ascii=LOW_SYSTEM_IDENTIFIER_ASCII,
            database_name_utf8=database_name_utf8,
            target_name_verified=True,
        )


def test_target_identity_preserves_unicode_and_case_without_normalization() -> None:
    """目标名不得 normalize 或 case-fold；不同原始 bytes 必须得到不同身份。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        derive_database_target_identity,
    )

    composed = derive_database_target_identity(
        system_identifier_ascii=LOW_SYSTEM_IDENTIFIER_ASCII,
        database_name_utf8="café_test".encode(),
        target_name_verified=True,
    )
    decomposed = derive_database_target_identity(
        system_identifier_ascii=LOW_SYSTEM_IDENTIFIER_ASCII,
        database_name_utf8="cafe\u0301_test".encode(),
        target_name_verified=True,
    )
    upper = derive_database_target_identity(
        system_identifier_ascii=LOW_SYSTEM_IDENTIFIER_ASCII,
        database_name_utf8=b"AI_EMPLOYEE_RESTORE_TEST",
        target_name_verified=True,
    )

    assert composed.identity_bytes != decomposed.identity_bytes
    assert composed.digest_hex != decomposed.digest_hex
    assert (
        upper.identity_bytes
        != derive_database_target_identity(
            system_identifier_ascii=LOW_SYSTEM_IDENTIFIER_ASCII,
            database_name_utf8=TARGET_DATABASE_NAME_UTF8,
            target_name_verified=True,
        ).identity_bytes
    )


def test_target_identity_rejects_unverified_absent_target_name() -> None:
    """未由 catalog 或 reset 全名确认取得的目标名不能生成锁 authority。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        derive_database_target_identity,
    )

    with pytest.raises(DatabaseMaintenanceInvariantError):
        derive_database_target_identity(
            system_identifier_ascii=LOW_SYSTEM_IDENTIFIER_ASCII,
            database_name_utf8=TARGET_DATABASE_NAME_UTF8,
            target_name_verified=False,
        )


def test_candidate_bootstrap_uses_management_target_schema_and_one_owner_transaction() -> None:
    """Candidate→baseline 必须保持固定锁序并在单一 owner transaction 内完成。"""
    from ai_employee.infrastructure.db.database_access import DatabaseAclProfile
    from ai_employee.infrastructure.db.database_grants import GrantPhase
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceContext,
        bootstrap_candidate_to_baseline,
    )

    events: list[str] = []
    candidate = object()

    class Transaction:
        def apply_safe_runtime_roles(self, observed_candidate: object) -> None:
            assert observed_candidate is candidate
            events.append("roles")

        def apply_database_acl(self, profile: DatabaseAclProfile) -> None:
            assert profile is DatabaseAclProfile.BASELINE
            events.append("database-acl")

        def apply_object_grants(self, *, revision: str, phase: GrantPhase) -> None:
            assert revision == "20260809_0018"
            assert phase is GrantPhase.BASELINE
            events.append("object-grants")

        def verify_pristine_idle_before_commit(self) -> None:
            events.append("verify-before-commit")

    class Target:
        revision = "20260809_0018"

        def assert_restore_facts_absent(self) -> None:
            events.append("restore-facts")

        def read_bootstrap_candidate(self) -> object:
            events.append("candidate")
            return candidate

        @contextmanager
        def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
            events.append("schema-enter")
            try:
                yield
            finally:
                events.append("schema-exit")

        @contextmanager
        def owner_transaction(self) -> Iterator[Transaction]:
            events.append("transaction-enter")
            try:
                yield Transaction()
                events.append("transaction-commit")
            finally:
                events.append("transaction-exit")

        def verify_pristine_idle_after_commit(self) -> None:
            events.append("verify-after-commit")

    class Management:
        @contextmanager
        def acquire_target_lock(self) -> Iterator[Target]:
            events.append("target-enter")
            try:
                yield Target()
            finally:
                events.append("target-exit")

    class Context:
        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[Management]:
            events.append("management-enter")
            try:
                yield Management()
            finally:
                events.append("management-exit")

    bootstrap_candidate_to_baseline(cast(DatabaseMaintenanceContext, Context()))

    assert events == [
        "management-enter",
        "target-enter",
        "restore-facts",
        "candidate",
        "schema-enter",
        "transaction-enter",
        "roles",
        "database-acl",
        "object-grants",
        "verify-before-commit",
        "transaction-commit",
        "transaction-exit",
        "verify-after-commit",
        "schema-exit",
        "target-exit",
        "management-exit",
    ]


def test_role_bootstrap_entry_reuses_management_target_schema_and_typed_target_runner() -> None:
    """standalone role-bootstrap 的 Candidate/steady 分支都必须由同一 target runner 收窄。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceContext,
        role_bootstrap_database,
    )

    events: list[str] = []

    class Target:
        def assert_role_bootstrap_allowed(self) -> None:
            events.append("role-bootstrap-admission")

        @contextmanager
        def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
            events.append("schema-enter")
            try:
                yield
            finally:
                events.append("schema-exit")

        def run_typed_role_bootstrap(self) -> None:
            events.append("role-bootstrap-runner")

    class Management:
        @contextmanager
        def acquire_target_lock(self) -> Iterator[Target]:
            events.append("target-enter")
            try:
                yield Target()
            finally:
                events.append("target-exit")

    class Context:
        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[Management]:
            events.append("management-enter")
            try:
                yield Management()
            finally:
                events.append("management-exit")

    role_bootstrap_database(cast(DatabaseMaintenanceContext, Context()))

    assert events == [
        "management-enter",
        "target-enter",
        "role-bootstrap-admission",
        "schema-enter",
        "role-bootstrap-runner",
        "schema-exit",
        "target-exit",
        "management-exit",
    ]


def test_direct_candidate_migration_rejects_before_schema_or_migration_writes() -> None:
    """普通 migrate 不能把 Candidate 当成第四个 steady state。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceContext,
        DatabaseMaintenanceInvariantError,
        migrate_database,
    )

    events: list[str] = []

    class Target:
        def assert_migration_allowed(self) -> None:
            events.append("admission")
            raise DatabaseMaintenanceInvariantError("database maintenance invariant violation")

        @contextmanager
        def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
            events.append("schema-enter")
            yield

        def run_typed_migration(self) -> None:
            events.append("migration-write")

    class Management:
        @contextmanager
        def acquire_target_lock(self) -> Iterator[Target]:
            events.append("target-enter")
            yield Target()

    class Context:
        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[Management]:
            events.append("management-enter")
            yield Management()

    with pytest.raises(DatabaseMaintenanceInvariantError):
        migrate_database(cast(DatabaseMaintenanceContext, Context()))

    assert events == ["management-enter", "target-enter", "admission"]


def test_protected_reset_keeps_one_management_lease_across_full_lifecycle() -> None:
    """reset 的确认、authority、drop/create、bootstrap 与 migrate 不能拆分 session。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceContext,
        reset_then_migrate,
    )

    events: list[str] = []

    class Management:
        def confirm_exact_non_production_target(self) -> None:
            events.append("confirm")

        def assert_reset_allowed(self) -> None:
            events.append("authority")

        def drop_and_create_target(self) -> None:
            events.append("drop-create")

        def bootstrap_new_target(self) -> None:
            events.append("bootstrap")

        def run_typed_migration(self) -> None:
            events.append("migrate")

    class Context:
        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[Management]:
            events.append("management-enter")
            try:
                yield Management()
            finally:
                events.append("management-exit")

    reset_then_migrate(cast(DatabaseMaintenanceContext, Context()))

    assert events == [
        "management-enter",
        "confirm",
        "authority",
        "drop-create",
        "bootstrap",
        "migrate",
        "management-exit",
    ]


@pytest.mark.parametrize(
    ("entry_name", "blocked_stage", "expected_events"),
    (
        pytest.param(
            "migrate",
            "management",
            ["management-lock-rejected"],
            id="migrate-management-held",
        ),
        pytest.param(
            "migrate",
            "target",
            ["management-enter", "target-lock-rejected", "management-exit"],
            id="migrate-target-held",
        ),
        pytest.param(
            "migrate",
            "schema",
            [
                "management-enter",
                "target-enter",
                "migration-admission",
                "schema-lock-rejected",
                "target-exit",
                "management-exit",
            ],
            id="migrate-schema-held",
        ),
        pytest.param(
            "role-bootstrap",
            "management",
            ["management-lock-rejected"],
            id="role-bootstrap-management-held",
        ),
        pytest.param(
            "role-bootstrap",
            "target",
            ["management-enter", "target-lock-rejected", "management-exit"],
            id="role-bootstrap-target-held",
        ),
        pytest.param(
            "role-bootstrap",
            "schema",
            [
                "management-enter",
                "target-enter",
                "role-bootstrap-admission",
                "schema-lock-rejected",
                "target-exit",
                "management-exit",
            ],
            id="role-bootstrap-schema-held",
        ),
        pytest.param(
            "reset",
            "management",
            ["management-lock-rejected"],
            id="reset-management-held",
        ),
        pytest.param(
            "reset",
            "target",
            [
                "management-enter",
                "reset-confirm",
                "target-lock-rejected",
                "management-exit",
            ],
            id="reset-target-held",
        ),
        pytest.param(
            "reset",
            "schema",
            [
                "management-enter",
                "reset-confirm",
                "target-enter",
                "schema-lock-rejected",
                "target-exit",
                "management-exit",
            ],
            id="reset-schema-held",
        ),
    ),
)
def test_online_entries_reject_held_lock_stage_before_any_mutation(
    entry_name: str,
    blocked_stage: str,
    expected_events: list[str],
) -> None:
    """三个 online entry 在任一 held-lock 阶段都须于所有写边界前停止。

    真实 PostgreSQL advisory-lock 持有/释放由本模块另一用例验证；此表驱动矩阵冻结
    public orchestration 在 management、target、schema 三个失败阶段的控制流。所有可能
    的 runner、DDL/DML、Alembic version、grant、AuditEvent 与 restore-authority 写入口
    都记录到独立列表，任一被触达都会使本用例失败。reset 仅使用 Fake，不执行真实
    drop/create、role-bootstrap 或 migration。
    """
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceContext,
        DatabaseMaintenanceInvariantError,
        migrate_database,
        reset_then_migrate,
        role_bootstrap_database,
    )

    events: list[str] = []
    mutation_events: list[str] = []

    class Target:
        def assert_migration_allowed(self) -> None:
            events.append("migration-admission")

        def assert_role_bootstrap_allowed(self) -> None:
            events.append("role-bootstrap-admission")

        def assert_reset_allowed_for_drop(self) -> None:
            events.append("reset-admission")

        @contextmanager
        def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
            if blocked_stage == "schema":
                events.append("schema-lock-rejected")
                raise DatabaseMaintenanceInvariantError("database maintenance invariant violation")
            events.append("schema-enter")
            try:
                yield
            finally:
                events.append("schema-exit")

        def run_typed_migration(self) -> None:
            mutation_events.extend(
                ["runner", "ddl", "dml", "version", "grant", "audit-event", "authority"]
            )

        def run_typed_role_bootstrap(self) -> None:
            mutation_events.extend(["runner", "ddl", "dml", "grant", "audit-event", "authority"])

    class Management:
        @contextmanager
        def acquire_target_lock(self) -> Iterator[Target]:
            if blocked_stage == "target":
                events.append("target-lock-rejected")
                raise DatabaseMaintenanceInvariantError("database maintenance invariant violation")
            events.append("target-enter")
            try:
                yield Target()
            finally:
                events.append("target-exit")

        def confirm_exact_non_production_target(self) -> None:
            events.append("reset-confirm")

        def assert_reset_allowed(self) -> None:
            # 与 production management lease 相同：reset 的首写 admission 也必须经过
            # target→schema，held lock 会在 drop/create authority 产生前中断。
            with (
                self.acquire_target_lock() as target,
                target.acquire_schema_lifecycle_lock(),
            ):
                target.assert_reset_allowed_for_drop()

        def drop_and_create_target(self) -> None:
            mutation_events.extend(["ddl", "authority"])

        def bootstrap_new_target(self) -> None:
            mutation_events.extend(["ddl", "dml", "grant", "audit-event", "authority"])

        def run_typed_migration(self) -> None:
            mutation_events.extend(
                ["runner", "ddl", "dml", "version", "grant", "audit-event", "authority"]
            )

    class Context:
        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[Management]:
            if blocked_stage == "management":
                events.append("management-lock-rejected")
                raise DatabaseMaintenanceInvariantError("database maintenance invariant violation")
            events.append("management-enter")
            try:
                yield Management()
            finally:
                events.append("management-exit")

    entrypoint = {
        "migrate": migrate_database,
        "role-bootstrap": role_bootstrap_database,
        "reset": reset_then_migrate,
    }[entry_name]
    with pytest.raises(
        DatabaseMaintenanceInvariantError,
        match=r"^database maintenance invariant violation$",
    ):
        entrypoint(cast(DatabaseMaintenanceContext, Context()))

    assert events == expected_events
    assert mutation_events == []


def test_sqlalchemy_target_typed_runner_rechecks_idle_and_uses_same_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """target runner 必须在 schema lock 内重验 idle，并发行同连接/identity token。"""
    from types import SimpleNamespace

    import ai_employee.infrastructure.db.alembic as alembic_module
    from ai_employee.infrastructure.db.alembic import OnlineMigrationAuthority
    from ai_employee.infrastructure.db.database_access import BootstrapCaller
    from ai_employee.infrastructure.db.database_grants import CatalogObjectSnapshot
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseAdmissionState,
        SqlAlchemyDatabaseMaintenanceContext,
        _DatabaseTargetCatalog,
        _SqlAlchemyTargetMaintenanceLease,
        derive_database_target_identity,
    )

    identity = derive_database_target_identity(
        system_identifier_ascii=b"1",
        database_name_utf8=b"synthetic_target",
        target_name_verified=True,
    )
    engine = create_engine("sqlite://")
    runner_authorities: list[OnlineMigrationAuthority] = []
    required_revisions: list[str | None] = []
    revision_reads = iter(("20260809_0018", PUBLISHED_AUTHORITY.head_revision))

    def fake_read_revision(
        _connection: Connection,
        *,
        authority: object,
    ) -> str:
        """提供 synthetic Alembic version rows，仍由 lifecycle 读取而非 runner 自报。"""
        del authority
        return next(revision_reads)

    def fake_precheck(
        connection: Connection,
        *,
        revision: str,
        destination_revision: str | None,
        phase: GrantPhase,
    ) -> CatalogObjectSnapshot:
        """在测试 fake 中保留 source admission 与同连接断言。"""
        assert connection is not None
        assert revision == "20260809_0018"
        assert destination_revision == PUBLISHED_AUTHORITY.head_revision
        return CatalogObjectSnapshot(revision=revision, phase=phase, grants=())

    def fake_delta(
        connection: Connection,
        *,
        source_revision: str,
        destination_revision: str,
        phase: GrantPhase,
        before: CatalogObjectSnapshot,
    ) -> None:
        """模拟真实 callback 的 grant delta，但不向 SQLite 发 PostgreSQL SQL。"""
        assert connection is not None
        assert source_revision == before.revision == "20260809_0018"
        assert destination_revision == PUBLISHED_AUTHORITY.head_revision
        assert phase is GrantPhase.BASELINE

    def fake_destination_verify(
        connection: Connection,
        *,
        revision: str,
        phase: GrantPhase,
    ) -> CatalogObjectSnapshot:
        """模拟 destination inventory verifier 的 typed 返回值。"""
        assert connection is not None
        assert revision == PUBLISHED_AUTHORITY.head_revision
        return CatalogObjectSnapshot(revision=revision, phase=phase, grants=())

    class SyntheticCalendarAadGuard:
        """为 synthetic lifecycle 提供 0019 所需的同连接 typed guard。"""

        def __init__(self, expected_connection: Connection) -> None:
            self.expected_connection = expected_connection
            self.phases: list[str] = []

        def verify(self, *, connection: Connection, phase: str) -> None:
            """只接受 lifecycle 绑定的连接与两个冻结阶段。"""
            assert connection is self.expected_connection
            assert phase in {"before_mutation", "before_commit"}
            self.phases.append(phase)

    monkeypatch.setattr(alembic_module, "read_current_alembic_revision", fake_read_revision)
    monkeypatch.setattr(
        alembic_module,
        "verify_pre_migration_object_grants",
        fake_precheck,
    )
    monkeypatch.setattr(alembic_module, "apply_migration_grant_delta", fake_delta)
    monkeypatch.setattr(alembic_module, "verify_object_grants", fake_destination_verify)

    def consume_online_authority(authority: OnlineMigrationAuthority) -> None:
        """模拟官方 Alembic env 已完整消费 token，不绕过 lease 结果核验。"""
        runner_authorities.append(authority)
        # 走与真实 env.py 相同的 lifecycle 边界：首阶段读取 source/grants，
        # callback 读取 destination 并完成最终 revision，再由 verify_after_migrations
        # 消费 token。不能直接改写私有 ``_finished``，否则绕过 production result proof。
        lifecycle = authority.grant_lifecycle
        calendar_guard = SyntheticCalendarAadGuard(authority.connection)
        lifecycle.bind_calendar_aad_guard(calendar_guard)
        lifecycle.verify_before_migrations()
        lifecycle.on_version_apply(
            ctx=SimpleNamespace(connection=authority.connection),
            step=SimpleNamespace(
                is_upgrade=True,
                is_stamp=False,
                source_revision_ids=("20260809_0018",),
                destination_revision_ids=(PUBLISHED_AUTHORITY.head_revision,),
            ),
            heads={PUBLISHED_AUTHORITY.head_revision},
            run_args={},
        )
        lifecycle.verify_after_migrations()
        assert calendar_guard.phases == ["before_commit"]

    try:
        with engine.connect() as connection:
            context = SqlAlchemyDatabaseMaintenanceContext(
                management_engine=_SAFE_ENGINE_STUB,
                target_engine=_SAFE_ENGINE_STUB,
                target_database_name="synthetic_target",
                bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
                app_password="synthetic-app-secret",
                retention_password="synthetic-retention-secret",
                migration_runner=consume_online_authority,
                published_authority=PUBLISHED_AUTHORITY,
            )
            target = _DatabaseTargetCatalog(
                identity=identity,
                database_oid=401,
                owner_oid=101,
                owner_role_name="synthetic_owner",
                database_name="synthetic_target",
            )
            lease = _SqlAlchemyTargetMaintenanceLease(
                context=context,
                connection=connection,
                target=target,
                revision="20260809_0018",
            )

            def fake_idle_read(
                self: object,
                *,
                require_revision: str | None,
            ) -> tuple[str, DatabaseAdmissionState]:
                del self
                required_revisions.append(require_revision)
                return (
                    require_revision or "20260809_0018",
                    DatabaseAdmissionState.PRISTINE_IDLE,
                )

            monkeypatch.setattr(
                _SqlAlchemyTargetMaintenanceLease,
                "_read_stable_admission",
                fake_idle_read,
            )
            lease.assert_reset_allowed_for_drop()
            assert lease._migration_admitted_revision is None
            lease.assert_migration_allowed()
            lease._schema_lock_held = True
            lease.run_typed_migration()

            assert len(runner_authorities) == 1
            online_authority = runner_authorities[0]
            assert online_authority.connection is connection
            assert online_authority.published_authority == PUBLISHED_AUTHORITY
            assert online_authority.expected_target_identity_digest == identity.digest_hex
            assert online_authority.expected_current_revision == "20260809_0018"
            assert online_authority.expected_target_revision == PUBLISHED_AUTHORITY.head_revision
            assert online_authority.grant_lifecycle.connection is connection
            assert required_revisions == [
                "20260809_0018",
                "20260809_0018",
                "20260809_0018",
                PUBLISHED_AUTHORITY.head_revision,
            ]
            assert lease.revision == PUBLISHED_AUTHORITY.head_revision
    finally:
        engine.dispose()


def test_sqlalchemy_target_active_state_rejects_even_forged_runner_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """active authority 即使遇到手工伪造的 admitted flag 也必须在 runner 前再次拒绝。"""
    from ai_employee.infrastructure.db.database_access import BootstrapCaller
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseAdmissionState,
        DatabaseMaintenanceInvariantError,
        SqlAlchemyDatabaseMaintenanceContext,
        _DatabaseTargetCatalog,
        _SqlAlchemyTargetMaintenanceLease,
        derive_database_target_identity,
    )

    class FakeConnection:
        def in_transaction(self) -> bool:
            return False

        def commit(self) -> None:
            raise AssertionError("active rejection must not commit")

        def rollback(self) -> None:
            raise AssertionError("active rejection has no open transaction")

    connection = cast(Connection, FakeConnection())
    runner_connections: list[Connection] = []
    context = SqlAlchemyDatabaseMaintenanceContext(
        management_engine=_SAFE_ENGINE_STUB,
        target_engine=_SAFE_ENGINE_STUB,
        target_database_name="synthetic_target",
        bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
        app_password="synthetic-app-secret",
        retention_password="synthetic-retention-secret",
        migration_runner=runner_connections.append,
        published_authority=PUBLISHED_AUTHORITY,
    )
    identity = derive_database_target_identity(
        system_identifier_ascii=b"1",
        database_name_utf8=b"synthetic_target",
        target_name_verified=True,
    )
    lease = _SqlAlchemyTargetMaintenanceLease(
        context=context,
        connection=connection,
        target=_DatabaseTargetCatalog(
            identity=identity,
            database_oid=401,
            owner_oid=101,
            owner_role_name="synthetic_owner",
            database_name="synthetic_target",
        ),
        revision="20260809_0018",
    )

    def fake_active_read(
        self: object,
        *,
        require_revision: str | None,
    ) -> tuple[str, DatabaseAdmissionState]:
        del self, require_revision
        return "20260809_0018", DatabaseAdmissionState.ACTIVE

    monkeypatch.setattr(
        _SqlAlchemyTargetMaintenanceLease,
        "_read_stable_admission",
        fake_active_read,
    )
    with pytest.raises(DatabaseMaintenanceInvariantError):
        lease.assert_reset_allowed_for_drop()
    with pytest.raises(DatabaseMaintenanceInvariantError):
        lease.assert_migration_allowed()

    lease._migration_admitted_revision = "20260809_0018"
    lease._schema_lock_held = True
    with pytest.raises(DatabaseMaintenanceInvariantError):
        lease.run_typed_migration()
    assert runner_connections == []


def test_sqlalchemy_protected_reset_reuses_one_live_management_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """concrete reset 必须用一个 management Connection 包住完整五阶段生命周期。"""
    from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect

    import ai_employee.infrastructure.db.database_maintenance as maintenance_module
    from ai_employee.infrastructure.db.database_access import BootstrapCaller, DatabaseAclProfile
    from ai_employee.infrastructure.db.database_grants import GrantPhase
    from ai_employee.infrastructure.db.database_maintenance import (
        ProtectedResetPolicy,
        SqlAlchemyDatabaseMaintenanceContext,
        _DatabaseTargetCatalog,
        _SqlAlchemyManagementLifecycleLease,
        derive_database_target_identity,
        reset_then_migrate,
    )

    events: list[str] = []

    class FakeManagementConnection:
        def __init__(self) -> None:
            self.dialect = postgresql_dialect()
            self.ddl_sql: list[str] = []
            self.created = False

        def execute(self, statement: object) -> object:
            sql = str(statement)
            self.ddl_sql.append(sql)
            events.append(f"management-ddl:{sql.split()[0].lower()}")
            if sql.startswith("CREATE DATABASE"):
                self.created = True
            return object()

        def execution_options(self, **options: object) -> FakeManagementConnection:
            assert options == {"isolation_level": "AUTOCOMMIT"}
            events.append("management-autocommit")
            return self

        def in_transaction(self) -> bool:
            return False

        def commit(self) -> None:
            raise AssertionError("autocommit reset DDL must not call commit")

        def rollback(self) -> None:
            raise AssertionError("no synthetic transaction is open")

    class FakeManagementConnect:
        def __init__(self, connection: FakeManagementConnection) -> None:
            self.connection = connection

        def __enter__(self) -> FakeManagementConnection:
            events.append("management-connect")
            return self.connection

        def __exit__(self, *args: object) -> None:
            del args
            events.append("management-close")

    class FakeManagementEngine:
        hide_parameters = True

        def __init__(self, connection: FakeManagementConnection) -> None:
            self.connection = connection
            self.connect_count = 0

        def connect(self) -> FakeManagementConnect:
            self.connect_count += 1
            return FakeManagementConnect(self.connection)

    class FakeTransaction:
        def apply_safe_runtime_roles(self, candidate: object) -> None:
            assert candidate is reset_candidate
            events.append("bootstrap-roles")

        def apply_database_acl(self, profile: DatabaseAclProfile) -> None:
            assert profile is DatabaseAclProfile.BASELINE
            events.append("bootstrap-database-acl")

        def apply_object_grants(self, *, revision: str, phase: GrantPhase) -> None:
            assert revision == "base"
            assert phase is GrantPhase.BASELINE
            events.append("bootstrap-object-grants")

        def verify_pristine_idle_before_commit(self) -> None:
            events.append("bootstrap-verify-before-commit")

    class FakeTarget:
        revision = "base"

        def __init__(self, phase: str) -> None:
            self.phase = phase

        def assert_reset_allowed_for_drop(self) -> None:
            assert self.phase == "authority"
            events.append("authority")

        def assert_restore_facts_absent(self) -> None:
            assert self.phase == "bootstrap"
            events.append("bootstrap-facts")

        def read_bootstrap_candidate(self) -> object:
            assert self.phase == "bootstrap"
            events.append("bootstrap-candidate")
            return reset_candidate

        @contextmanager
        def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
            events.append(f"{self.phase}-schema-enter")
            try:
                yield
            finally:
                events.append(f"{self.phase}-schema-exit")

        @contextmanager
        def owner_transaction(self) -> Iterator[FakeTransaction]:
            assert self.phase == "bootstrap"
            events.append("bootstrap-transaction-enter")
            try:
                yield FakeTransaction()
                events.append("bootstrap-transaction-commit")
            finally:
                events.append("bootstrap-transaction-exit")

        def verify_pristine_idle_after_commit(self) -> None:
            assert self.phase == "bootstrap"
            events.append("bootstrap-verify-after-commit")

        def assert_migration_allowed(self) -> None:
            assert self.phase == "migrate"
            events.append("migrate-admission")

        def run_typed_migration(self) -> None:
            assert self.phase == "migrate"
            events.append("migrate-runner")

    database_name = "synthetic_reset_test"
    identity = derive_database_target_identity(
        system_identifier_ascii=b"1",
        database_name_utf8=database_name.encode("utf-8"),
        target_name_verified=True,
    )
    old_target = _DatabaseTargetCatalog(
        identity=identity,
        database_oid=401,
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name=database_name,
    )
    new_target = _DatabaseTargetCatalog(
        identity=identity,
        database_oid=402,
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name=database_name,
    )
    management_connection = FakeManagementConnection()
    management_engine = FakeManagementEngine(management_connection)
    reset_candidate = object()

    def fake_catalog_read(
        connection: object,
        *,
        target_database_name: str,
        allow_absent: bool,
    ) -> object:
        assert connection is management_connection
        assert target_database_name == database_name
        assert allow_absent is (not management_connection.created)
        events.append("management-catalog")
        return new_target if management_connection.created else old_target

    def fake_acquire_lock(connection: object, *, lock_key: int) -> None:
        assert connection is management_connection
        assert lock_key == identity.lifecycle_lock_key
        events.append("management-lock-enter")

    def fake_release_lock(connection: object, *, lock_key: int) -> None:
        assert connection is management_connection
        assert lock_key == identity.lifecycle_lock_key
        events.append("management-lock-exit")

    def fake_no_sessions(connection: object, *, target: object) -> None:
        assert connection is management_connection
        assert target is old_target
        events.append("management-no-target-sessions")

    phases = iter(("authority", "bootstrap", "migrate"))

    @contextmanager
    def fake_target_lock(
        lease: object,
    ) -> Iterator[FakeTarget]:
        assert isinstance(lease, _SqlAlchemyManagementLifecycleLease)
        assert lease._connection is management_connection
        phase = next(phases)
        events.append(f"{phase}-target-enter")
        try:
            yield FakeTarget(phase)
        finally:
            events.append(f"{phase}-target-exit")

    monkeypatch.setattr(maintenance_module, "_read_management_target_catalog", fake_catalog_read)
    monkeypatch.setattr(maintenance_module, "_acquire_single_session_lock", fake_acquire_lock)
    monkeypatch.setattr(maintenance_module, "_release_single_session_lock", fake_release_lock)
    monkeypatch.setattr(maintenance_module, "_assert_no_target_sessions", fake_no_sessions)
    monkeypatch.setattr(
        _SqlAlchemyManagementLifecycleLease,
        "acquire_target_lock",
        fake_target_lock,
    )
    context = SqlAlchemyDatabaseMaintenanceContext(
        management_engine=cast(Engine, management_engine),
        target_engine=_SAFE_ENGINE_STUB,
        target_database_name=database_name,
        bootstrap_caller=BootstrapCaller.DB_RESET_POST_CREATE,
        app_password="synthetic-app-secret",
        retention_password="synthetic-retention-secret",
        migration_runner=cast(Callable[[Connection], None], lambda connection: None),
        published_authority=PUBLISHED_AUTHORITY,
        reset_policy=ProtectedResetPolicy(
            app_env="test",
            confirmed_database_name=database_name,
        ),
    )

    reset_then_migrate(context)

    assert management_engine.connect_count == 1
    assert management_connection.ddl_sql == [
        'DROP DATABASE "synthetic_reset_test"',
        'CREATE DATABASE "synthetic_reset_test" OWNER "synthetic_owner"',
    ]
    assert events == [
        "management-connect",
        "management-catalog",
        "management-lock-enter",
        "management-catalog",
        "authority-target-enter",
        "authority-schema-enter",
        "authority",
        "authority-schema-exit",
        "authority-target-exit",
        "management-no-target-sessions",
        "management-autocommit",
        "management-ddl:drop",
        "management-ddl:create",
        "management-catalog",
        "bootstrap-target-enter",
        "bootstrap-facts",
        "bootstrap-candidate",
        "bootstrap-schema-enter",
        "bootstrap-transaction-enter",
        "bootstrap-roles",
        "bootstrap-database-acl",
        "bootstrap-object-grants",
        "bootstrap-verify-before-commit",
        "bootstrap-transaction-commit",
        "bootstrap-transaction-exit",
        "bootstrap-verify-after-commit",
        "bootstrap-schema-exit",
        "bootstrap-target-exit",
        "migrate-target-enter",
        "migrate-admission",
        "migrate-schema-enter",
        "migrate-runner",
        "migrate-schema-exit",
        "migrate-target-exit",
        "management-lock-exit",
        "management-close",
    ]


@pytest.mark.parametrize("app_env", ["", "production", "staging", "Development"])
def test_protected_reset_policy_rejects_every_non_explicit_non_production_environment(
    app_env: str,
) -> None:
    """policy 只接受 exact development/test，不提供 production 或别名豁免。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        ProtectedResetPolicy,
    )

    with pytest.raises(
        DatabaseMaintenanceInvariantError,
        match=r"^database maintenance invariant violation$",
    ):
        ProtectedResetPolicy(
            app_env=app_env,
            confirmed_database_name="synthetic_reset_test",
        )


def test_protected_reset_policy_is_required_only_for_reset_and_binds_exact_name() -> None:
    """缺失 policy、普通入口误带 policy 与名称不匹配都在 destructive DDL 前拒绝。"""
    from ai_employee.infrastructure.db.database_access import BootstrapCaller
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        ProtectedResetPolicy,
        SqlAlchemyDatabaseMaintenanceContext,
        _DatabaseTargetCatalog,
        _SqlAlchemyManagementLifecycleLease,
        derive_database_target_identity,
    )

    common = {
        "management_engine": _SAFE_ENGINE_STUB,
        "target_engine": _SAFE_ENGINE_STUB,
        "target_database_name": "synthetic_reset_test",
        "app_password": "synthetic-app-secret",
        "retention_password": "synthetic-retention-secret",
        "migration_runner": cast(Callable[[Connection], None], lambda connection: None),
        "published_authority": PUBLISHED_AUTHORITY,
    }
    with pytest.raises(DatabaseMaintenanceInvariantError):
        SqlAlchemyDatabaseMaintenanceContext(
            **common,
            bootstrap_caller=BootstrapCaller.DB_RESET_POST_CREATE,
        )

    policy = ProtectedResetPolicy(
        app_env="test",
        confirmed_database_name="different_reset_test",
    )
    with pytest.raises(DatabaseMaintenanceInvariantError):
        SqlAlchemyDatabaseMaintenanceContext(
            **common,
            bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
            reset_policy=policy,
        )

    context = SqlAlchemyDatabaseMaintenanceContext(
        **common,
        bootstrap_caller=BootstrapCaller.DB_RESET_POST_CREATE,
        reset_policy=policy,
    )
    target_name = "synthetic_reset_test"
    target = _DatabaseTargetCatalog(
        identity=derive_database_target_identity(
            system_identifier_ascii=b"1",
            database_name_utf8=target_name.encode("utf-8"),
            target_name_verified=True,
        ),
        database_oid=401,
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name=target_name,
    )
    lease = _SqlAlchemyManagementLifecycleLease(
        context=context,
        connection=cast(Connection, object()),
        target=target,
    )
    with pytest.raises(DatabaseMaintenanceInvariantError):
        lease.confirm_exact_non_production_target()


class _FakeRestoreMappingResult:
    """提供 restore-fact reader 所需的 buffered mapping 行。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        """返回一份独立列表，避免 reader 意外修改测试输入。"""
        return list(self._rows)


class _FakeRestoreResult:
    """模拟同步 SQLAlchemy ``Result.mappings()``。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeRestoreMappingResult:
        """返回按稳定列名访问的 mapping 结果。"""
        return _FakeRestoreMappingResult(self._rows)


class _FakeRestoreConnection:
    """记录 restore-fact 查询，绝不提供任何 mutation 行为。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _FakeRestoreResult:
        """记录 SQL/参数并返回预置 catalog 行。"""
        self.calls.append((str(statement), {} if parameters is None else dict(parameters)))
        return _FakeRestoreResult(self.rows)


def _steady_access_snapshot(profile: DatabaseAclProfile) -> object:
    """构造 exact baseline/active access posture，独立于 production classifier。"""
    from ai_employee.infrastructure.db.database_access import (
        DatabaseAccessSnapshot,
        RuntimeRoleSnapshot,
    )

    owner_oid = 101
    app_oid = 201
    retention_oid = 202
    owner_acl = (
        DatabaseAclTuple(owner_oid, owner_oid, "CREATE", False),
        DatabaseAclTuple(owner_oid, owner_oid, "CONNECT", False),
        DatabaseAclTuple(owner_oid, owner_oid, "TEMPORARY", False),
    )
    runtime_acl = (
        DatabaseAclTuple(app_oid, owner_oid, "CONNECT", False),
        DatabaseAclTuple(retention_oid, owner_oid, "CONNECT", False),
    )
    acl_tuples = tuple(
        sorted(owner_acl if profile is DatabaseAclProfile.ACTIVE else (*owner_acl, *runtime_acl))
    )

    def safe_role(name: str, oid: int) -> RuntimeRoleSnapshot:
        return RuntimeRoleSnapshot(
            role_name=name,
            role_oid=oid,
            can_login=True,
            inherits=True,
            is_superuser=False,
            can_create_database=False,
            can_create_role=False,
            can_replicate=False,
            bypasses_rls=False,
            connection_limit=-1,
            valid_until=None,
            config=None,
        )

    return DatabaseAccessSnapshot(
        target_database_oid=401,
        owner_oid=owner_oid,
        acl_profile=profile,
        acl_tuples=acl_tuples,
        app_role=safe_role(APP_RUNTIME_ROLE_NAME, app_oid),
        retention_role=safe_role(RETENTION_RUNTIME_ROLE_NAME, retention_oid),
        memberships=(),
    )


def _active_restore_values() -> tuple[str, str]:
    """构造独立的 active gate/call authority 合成值。"""
    attempt = "11111111-1111-4111-8111-111111111111"
    target_digest = "a" * 64
    source_digest = "b" * 64
    expected_fingerprint = "c" * 64
    established_at = "2026-08-11T01:02:03.123456Z"
    gate = f"restore:v1:{attempt}:generic:{target_digest}:{source_digest}"
    # 字段矩阵逐行展示比单个超长 f-string 更适合审阅 restore-call 位置语义。
    call = "|".join(  # noqa: FLY002
        (
            "restore-call:v4",
            attempt,
            "generic",
            target_digest,
            source_digest,
            source_digest,
            "-",
            "0",
            "0",
            "gate_established",
            established_at,
            "-",
            "-",
            "-",
            "-",
            "-",
            "20260809_0018",
            expected_fingerprint,
            "-",
            "-",
            "-",
            "-",
        )
    )
    return gate, call


def _completed_restore_values() -> tuple[str, str]:
    """按 zero-slot 规则构造独立 completed call 与 completion GUC。"""
    attempt = "22222222-2222-4222-8222-222222222222"
    target_digest = "d" * 64
    source_digest = "e" * 64
    fingerprint = "f" * 64
    fields = [
        "restore-call:v4",
        attempt,
        "generic",
        target_digest,
        source_digest,
        source_digest,
        "-",
        "0",
        "1",
        "completed",
        "2026-08-11T01:02:03.123456Z",
        "-",
        "-",
        "-",
        "-",
        "-",
        "20260809_0018",
        fingerprint,
        "20260809_0018",
        fingerprint,
        "2026-08-11T01:03:04.654321Z",
        "0" * 64,
    ]
    zero_slot_call = "|".join(fields).encode("ascii")
    digest = sha256(COMPLETION_AUTHORITY_DOMAIN + zero_slot_call).hexdigest()
    fields[-1] = digest
    return "|".join(fields), f"restore_completion:v1:{digest}"


def test_restore_fact_reader_accepts_only_unique_database_wide_keys() -> None:
    """三项事实只能来自 target database 的 setrole=0 唯一 key。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DATABASE_RESTORE_FACTS_SQL,
        DatabaseRestoreFacts,
        read_database_restore_facts,
    )

    gate, call = _active_restore_values()
    connection = _FakeRestoreConnection(
        [
            {
                "setrole": 0,
                "setconfig": [
                    "unrelated.setting=preserved",
                    f"ai_employee.maintenance_gate={gate}",
                    f"ai_employee.restore_call_authority={call}",
                ],
                "current_maintenance_gate": gate,
                "current_restore_call_authority": call,
                "current_restore_completion": None,
            }
        ]
    )

    facts = read_database_restore_facts(
        cast(Connection, connection),
        target_database_oid=401,
    )

    assert facts == DatabaseRestoreFacts(
        maintenance_gate=gate,
        restore_call_authority=call,
        restore_completion=None,
    )
    assert connection.calls == [(DATABASE_RESTORE_FACTS_SQL, {"target_database_oid": 401})]


@pytest.mark.parametrize(
    "rows",
    (
        [{"setrole": 9, "setconfig": ["ai_employee.maintenance_gate=value"]}],
        [
            {
                "setrole": 0,
                "setconfig": [
                    "ai_employee.maintenance_gate=one",
                    "ai_employee.maintenance_gate=two",
                ],
            }
        ],
        [{"setrole": 0, "setconfig": "ai_employee.maintenance_gate=value"}],
        [{"setrole": 0, "setconfig": [b"ai_employee.maintenance_gate=value"]}],
        [{"setrole": True, "setconfig": ["ai_employee.maintenance_gate=value"]}],
    ),
)
def test_restore_fact_reader_rejects_role_specific_duplicate_or_malformed_rows(
    rows: list[dict[str, object]],
) -> None:
    """authority 漂移必须在任何 lifecycle mutation 前 fail closed。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        read_database_restore_facts,
    )

    connection = _FakeRestoreConnection(rows)
    with pytest.raises(DatabaseMaintenanceInvariantError):
        read_database_restore_facts(
            cast(Connection, connection),
            target_database_oid=401,
        )
    assert len(connection.calls) == 1


def test_stable_admission_accepts_only_exact_pristine_active_or_completed_states() -> None:
    """stable parser 必须保持三态闭集，并把 active 排除在 ordinary migration 外。"""
    from ai_employee.infrastructure.db.database_access import DatabaseAccessSnapshot
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseAdmissionState,
        DatabaseMaintenanceInvariantError,
        DatabaseRestoreFacts,
        assert_ordinary_migration_admission,
        classify_database_admission,
    )

    pristine = classify_database_admission(
        facts=DatabaseRestoreFacts(None, None, None),
        access_snapshot=cast(
            DatabaseAccessSnapshot,
            _steady_access_snapshot(DatabaseAclProfile.BASELINE),
        ),
        object_grants_match=True,
        # pristine 没有 authority 可绑定；canonical 合成 digest 仅满足 admission 输入契约。
        expected_target_identity_digest="0" * 64,
    )
    gate, active_call = _active_restore_values()
    active = classify_database_admission(
        facts=DatabaseRestoreFacts(gate, active_call, None),
        access_snapshot=cast(
            DatabaseAccessSnapshot,
            _steady_access_snapshot(DatabaseAclProfile.ACTIVE),
        ),
        object_grants_match=True,
        expected_target_identity_digest=active_call.split("|")[3],
    )
    completed_call, completion = _completed_restore_values()
    completed = classify_database_admission(
        facts=DatabaseRestoreFacts(None, completed_call, completion),
        access_snapshot=cast(
            DatabaseAccessSnapshot,
            _steady_access_snapshot(DatabaseAclProfile.BASELINE),
        ),
        object_grants_match=True,
        expected_target_identity_digest=completed_call.split("|")[3],
    )

    assert pristine is DatabaseAdmissionState.PRISTINE_IDLE
    assert active is DatabaseAdmissionState.ACTIVE
    assert completed is DatabaseAdmissionState.COMPLETED_IDLE
    assert_ordinary_migration_admission(pristine)
    assert_ordinary_migration_admission(completed)
    with pytest.raises(DatabaseMaintenanceInvariantError):
        assert_ordinary_migration_admission(active)


@pytest.mark.parametrize("completed", (False, True), ids=("pristine", "completed"))
def test_steady_role_bootstrap_reasserts_connect_and_object_grants_in_order(
    monkeypatch: pytest.MonkeyPatch,
    completed: bool,
) -> None:
    """steady 分支必须在同一事务按密码→CONNECT→对象 grants 顺序重申并保留 authority。"""
    import ai_employee.infrastructure.db.database_maintenance as maintenance_module
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseAdmissionState,
        DatabaseRestoreFacts,
        SqlAlchemyDatabaseMaintenanceContext,
        _DatabaseTargetCatalog,
        _SqlAlchemyTargetMaintenanceLease,
        _StableRoleBootstrapAdmission,
        derive_database_target_identity,
    )

    identity = derive_database_target_identity(
        system_identifier_ascii=b"1",
        database_name_utf8=b"synthetic_target",
        target_name_verified=True,
    )
    access_snapshot = cast(
        DatabaseAccessSnapshot,
        _steady_access_snapshot(DatabaseAclProfile.BASELINE),
    )
    if completed:
        call, completion = _completed_restore_values()
        facts = DatabaseRestoreFacts(None, call, completion)
        state = DatabaseAdmissionState.COMPLETED_IDLE
    else:
        facts = DatabaseRestoreFacts(None, None, None)
        state = DatabaseAdmissionState.PRISTINE_IDLE
    admission = _StableRoleBootstrapAdmission(
        revision="20260809_0018",
        state=state,
        facts=facts,
        access_snapshot=access_snapshot,
    )
    events: list[str] = []
    observed_admissions: list[_StableRoleBootstrapAdmission] = []
    read_labels = iter(
        (
            "admission-reread",
            "final-in-transaction-reread",
            "final-postcommit-reread",
        )
    )

    def observed_read() -> _StableRoleBootstrapAdmission:
        events.append(next(read_labels))
        observed_admissions.append(admission)
        return admission

    def observed_password_rotation(
        connection: Connection,
        snapshot: DatabaseAccessSnapshot,
        *,
        app_password: str,
        retention_password: str,
    ) -> None:
        del connection
        assert snapshot is access_snapshot
        assert app_password == "synthetic-app-secret"
        assert retention_password == "synthetic-retention-secret"
        events.append("password-rotation")

    def observed_connect_reassertion(
        connection: Connection,
        *,
        target_database_name: str,
        snapshot: DatabaseAccessSnapshot,
    ) -> None:
        del connection
        assert target_database_name == "synthetic_target"
        assert snapshot is access_snapshot
        events.append("connect-reassert")

    def observed_object_grants(
        connection: Connection,
        *,
        revision: str,
        phase: GrantPhase,
        posture: BootstrapGrantPosture,
    ) -> None:
        del connection
        assert revision == "20260809_0018"
        assert phase is GrantPhase.BASELINE
        assert posture is BootstrapGrantPosture.CURRENT
        events.append("object-grants-reassert")

    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            context = SqlAlchemyDatabaseMaintenanceContext(
                management_engine=_SAFE_ENGINE_STUB,
                target_engine=_SAFE_ENGINE_STUB,
                target_database_name="synthetic_target",
                bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
                app_password="synthetic-app-secret",
                retention_password="synthetic-retention-secret",
                migration_runner=lambda authority: None,
                published_authority=PUBLISHED_AUTHORITY,
            )
            lease = _SqlAlchemyTargetMaintenanceLease(
                context=context,
                connection=connection,
                target=_DatabaseTargetCatalog(
                    identity=identity,
                    database_oid=401,
                    owner_oid=101,
                    owner_role_name="synthetic_owner",
                    database_name="synthetic_target",
                ),
                revision="20260809_0018",
            )
            lease._schema_lock_held = True
            lease._role_bootstrap_admission = admission
            monkeypatch.setattr(lease, "_read_role_bootstrap_admission", observed_read)
            monkeypatch.setattr(
                maintenance_module,
                "rotate_safe_runtime_role_passwords",
                observed_password_rotation,
            )
            monkeypatch.setattr(
                maintenance_module,
                "apply_baseline_connect_reassertion",
                observed_connect_reassertion,
                raising=False,
            )
            monkeypatch.setattr(
                maintenance_module,
                "apply_bootstrap_object_grants",
                observed_object_grants,
            )

            lease.run_typed_role_bootstrap()

            assert lease._role_bootstrap_admission is None
    finally:
        engine.dispose()

    assert events == [
        "admission-reread",
        "password-rotation",
        "connect-reassert",
        "object-grants-reassert",
        "final-in-transaction-reread",
        "final-postcommit-reread",
    ]
    assert observed_admissions == [admission, admission, admission]


@pytest.mark.parametrize(
    "entry_case",
    (
        pytest.param("active-classification", id="active-other-target"),
        pytest.param("completed-migrate", id="completed-other-target-migrate"),
        pytest.param(
            "completed-role-bootstrap",
            id="completed-other-target-role-bootstrap",
        ),
    ),
)
def test_target_lease_rejects_cross_database_restore_authority_before_runner(
    monkeypatch: pytest.MonkeyPatch,
    entry_case: str,
) -> None:
    """自洽 gate/call/completion 也必须绑定当前 lease 的 target identity digest。"""
    import ai_employee.infrastructure.db.database_maintenance as maintenance_module
    from ai_employee.infrastructure.db.database_access import BootstrapCaller
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceContext,
        DatabaseMaintenanceInvariantError,
        DatabaseRestoreFacts,
        SqlAlchemyDatabaseMaintenanceContext,
        _DatabaseTargetCatalog,
        _SqlAlchemyTargetMaintenanceLease,
        derive_database_target_identity,
        migrate_database,
        role_bootstrap_database,
    )

    identity = derive_database_target_identity(
        system_identifier_ascii=b"1",
        database_name_utf8=b"current_target",
        target_name_verified=True,
    )
    if entry_case == "active-classification":
        gate, call = _active_restore_values()
        facts = DatabaseRestoreFacts(gate, call, None)
        access_snapshot = _steady_access_snapshot(DatabaseAclProfile.ACTIVE)
        foreign_target_digest = call.split("|")[3]
    else:
        call, completion = _completed_restore_values()
        facts = DatabaseRestoreFacts(None, call, completion)
        access_snapshot = _steady_access_snapshot(DatabaseAclProfile.BASELINE)
        foreign_target_digest = call.split("|")[3]
    assert foreign_target_digest != identity.digest_hex

    engine = create_engine("sqlite://")
    runner_events: list[str] = []
    try:
        with engine.connect() as connection:
            context = SqlAlchemyDatabaseMaintenanceContext(
                management_engine=_SAFE_ENGINE_STUB,
                target_engine=_SAFE_ENGINE_STUB,
                target_database_name="current_target",
                bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
                app_password="synthetic-app-secret",
                retention_password="synthetic-retention-secret",
                migration_runner=lambda authority: runner_events.append("alembic-runner"),
                published_authority=PUBLISHED_AUTHORITY,
            )
            lease = _SqlAlchemyTargetMaintenanceLease(
                context=context,
                connection=connection,
                target=_DatabaseTargetCatalog(
                    identity=identity,
                    database_oid=401,
                    owner_oid=101,
                    owner_role_name="synthetic_owner",
                    database_name="current_target",
                ),
                revision="20260809_0018",
            )
            monkeypatch.setattr(
                maintenance_module,
                "_read_target_catalog",
                lambda observed_connection, *, expected: None,
            )
            monkeypatch.setattr(
                maintenance_module,
                "_read_current_revision",
                lambda observed_connection, *, authority: "20260809_0018",
            )
            monkeypatch.setattr(
                maintenance_module,
                "_has_active_non_owner_sessions",
                lambda observed_connection, *, target: False,
            )
            monkeypatch.setattr(
                maintenance_module,
                "read_database_restore_facts",
                lambda observed_connection, *, target_database_oid: facts,
            )
            monkeypatch.setattr(
                maintenance_module,
                "read_database_access_snapshot_sync",
                lambda observed_connection, *, target_database_oid: access_snapshot,
            )
            monkeypatch.setattr(
                maintenance_module,
                "verify_object_grants",
                lambda observed_connection, *, revision, phase: object(),
            )

            if entry_case == "active-classification":
                with pytest.raises(
                    DatabaseMaintenanceInvariantError,
                    match=r"^database maintenance invariant violation$",
                ):
                    lease._read_stable_admission(require_revision="20260809_0018")
            else:

                class Target:
                    def assert_migration_allowed(self) -> None:
                        lease.assert_migration_allowed()

                    def assert_role_bootstrap_allowed(self) -> None:
                        lease.assert_role_bootstrap_allowed()

                    @contextmanager
                    def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
                        yield

                    def run_typed_migration(self) -> None:
                        runner_events.append("typed-migration-runner")

                    def run_typed_role_bootstrap(self) -> None:
                        runner_events.append("typed-role-bootstrap-runner")

                class Management:
                    @contextmanager
                    def acquire_target_lock(self) -> Iterator[Target]:
                        yield Target()

                class Context:
                    @contextmanager
                    def acquire_management_lifecycle_lock(self) -> Iterator[Management]:
                        yield Management()

                entrypoint = (
                    migrate_database
                    if entry_case == "completed-migrate"
                    else role_bootstrap_database
                )
                with pytest.raises(
                    DatabaseMaintenanceInvariantError,
                    match=r"^database maintenance invariant violation$",
                ):
                    entrypoint(cast(DatabaseMaintenanceContext, Context()))
    finally:
        engine.dispose()

    assert runner_events == []


def test_stable_admission_rejects_malformed_or_cross_posture_authority() -> None:
    """非法 fact/posture/object-grant 组合必须在任何 lifecycle mutation 前拒绝。"""
    from ai_employee.infrastructure.db.database_access import DatabaseAccessSnapshot
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        DatabaseRestoreFacts,
        classify_database_admission,
    )

    gate, active_call = _active_restore_values()
    completed_call, completion = _completed_restore_values()
    invalid_cases = (
        (DatabaseRestoreFacts(gate, None, None), DatabaseAclProfile.ACTIVE, True),
        (DatabaseRestoreFacts(None, active_call, None), DatabaseAclProfile.BASELINE, True),
        (DatabaseRestoreFacts(None, completed_call, None), DatabaseAclProfile.BASELINE, True),
        (
            DatabaseRestoreFacts(None, completed_call, completion[:-1] + "0"),
            DatabaseAclProfile.BASELINE,
            True,
        ),
        (DatabaseRestoreFacts(gate, active_call, completion), DatabaseAclProfile.ACTIVE, True),
        (DatabaseRestoreFacts(None, None, None), DatabaseAclProfile.ACTIVE, True),
        (DatabaseRestoreFacts(gate, active_call, None), DatabaseAclProfile.BASELINE, True),
        (DatabaseRestoreFacts(None, None, None), DatabaseAclProfile.BASELINE, False),
    )
    for invalid_facts, invalid_profile, grants_match in invalid_cases:
        with pytest.raises(DatabaseMaintenanceInvariantError):
            classify_database_admission(
                facts=invalid_facts,
                access_snapshot=cast(
                    DatabaseAccessSnapshot,
                    _steady_access_snapshot(invalid_profile),
                ),
                object_grants_match=grants_match,
                # 这些负例验证 fact/posture/grant 闭集，使用 canonical 合成当前 target。
                expected_target_identity_digest="0" * 64,
            )


@dataclass(frozen=True, slots=True)
class _LiveCatalogState:
    """保存一次只读采样的完整安全相关 catalog 与内容摘要。"""

    target_database_oid: int
    owner_oid: int
    raw_database_acl: tuple[str, ...] | None
    current_role: str
    current_role_oid: int
    server_version_num: int
    database_acl: tuple[tuple[object, ...], ...]
    runtime_roles: tuple[tuple[object, ...], ...]
    memberships: tuple[tuple[object, ...], ...]
    restore_facts: tuple[tuple[object, ...], ...]
    active_non_owner_sessions: tuple[tuple[object, ...], ...]
    schema_catalog: tuple[tuple[object, ...], ...]
    business_rows: tuple[tuple[str, int, str], ...]


def _rows_as_tuples(
    rows: Sequence[Row[tuple[object, ...]]],
) -> tuple[tuple[object, ...], ...]:
    """把 buffered SQLAlchemy Row 规范化为可直接比较的不可变 tuple。"""
    return tuple(tuple(row) for row in rows)


def _quote_identifier(identifier: str) -> str:
    """精确引用 catalog 返回的 PostgreSQL 标识符，避免动态表名成为 SQL 片段。"""
    return '"' + identifier.replace('"', '""') + '"'


async def _read_schema_catalog(
    connection: AsyncConnection,
) -> tuple[tuple[object, ...], ...]:
    """读取 public schema 的对象、列、约束与索引定义，不访问对象内容。"""
    rows: list[tuple[object, ...]] = []
    for category, sql in _SCHEMA_CATALOG_QUERIES:
        result = await connection.execute(text(sql))
        rows.extend((category, *tuple(row)) for row in result.all())
    return tuple(rows)


async def _read_business_row_summaries(
    connection: AsyncConnection,
) -> tuple[tuple[str, int, str], ...]:
    """在数据库内哈希每张业务表的规范 JSON 行，只返回计数与内容摘要。

    完整行值从不离开 PostgreSQL；测试只比较 content-free digest。动态标识符来自当前
    ``public`` catalog，并经过双引号转义，不能注入任意 SQL。测试数据库由上级 fixture
    清空，但仍保留摘要可以证明 reader 调用没有悄然写入未来新增的业务表。
    """
    table_result = await connection.execute(text(_BUSINESS_TABLES_SQL))
    summaries: list[tuple[str, int, str]] = []
    for row in table_result.mappings().all():
        table_name = row["relname"]
        if type(table_name) is not str:
            raise AssertionError("public table name must be text")
        quoted_table = _quote_identifier(table_name)
        summary_result = await connection.execute(
            text(
                "SELECT count(*)::bigint AS row_count, "
                "md5(COALESCE(string_agg(md5(to_jsonb(row_value)::text), '' "
                "ORDER BY md5(to_jsonb(row_value)::text)), '')) AS content_digest "
                f"FROM public.{quoted_table} AS row_value"
            )
        )
        summary = summary_result.mappings().one()
        row_count = summary["row_count"]
        content_digest = summary["content_digest"]
        if type(row_count) is not int or type(content_digest) is not str:
            raise AssertionError("business row summary has an unexpected type")
        summaries.append((table_name, row_count, content_digest))
    return tuple(summaries)


async def _capture_live_catalog_state(connection: AsyncConnection) -> _LiveCatalogState:
    """在同一连接上采样 ACL、角色、restore facts、schema 与业务摘要。"""
    identity_result = await connection.execute(text(_DATABASE_IDENTITY_SQL))
    identity = identity_result.mappings().one()
    target_database_oid = identity["target_database_oid"]
    owner_oid = identity["owner_oid"]
    raw_database_acl = identity["raw_database_acl"]
    current_role = identity["current_role"]
    current_role_oid = identity["current_role_oid"]
    server_version_num = identity["server_version_num"]
    if (
        type(target_database_oid) is not int
        or type(owner_oid) is not int
        or type(current_role) is not str
        or type(current_role_oid) is not int
        or type(server_version_num) is not int
    ):
        raise AssertionError("database identity catalog row has an unexpected type")
    if current_role_oid != owner_oid:
        # 必须先证明当前 backend 就是 database owner，随后排除自身 PID 才不会制造假阴性。
        raise AssertionError("integration connection role must own the target database")
    if raw_database_acl is not None:
        if not isinstance(raw_database_acl, list) or not all(
            type(value) is str for value in raw_database_acl
        ):
            raise AssertionError("database ACL array has an unexpected type")
        normalized_raw_acl: tuple[str, ...] | None = tuple(raw_database_acl)
    else:
        normalized_raw_acl = None

    catalog_parameters = {
        "target_database_oid": target_database_oid,
        "app_role_name": APP_RUNTIME_ROLE_NAME,
        "retention_role_name": RETENTION_RUNTIME_ROLE_NAME,
    }
    acl_result = await connection.execute(
        text(EXPECTED_DATABASE_ACL_SQL),
        {"target_database_oid": target_database_oid},
    )
    roles_result = await connection.execute(text(_RUNTIME_ROLES_SQL), catalog_parameters)
    memberships_result = await connection.execute(text(_MEMBERSHIPS_SQL), catalog_parameters)
    restore_result = await connection.execute(
        text(_RESTORE_FACTS_SQL),
        {"target_database_oid": target_database_oid},
    )
    session_result = await connection.execute(
        text(_ACTIVE_NON_OWNER_SESSIONS_SQL),
        {
            "target_database_oid": target_database_oid,
            "owner_oid": owner_oid,
        },
    )

    return _LiveCatalogState(
        target_database_oid=target_database_oid,
        owner_oid=owner_oid,
        raw_database_acl=normalized_raw_acl,
        current_role=current_role,
        current_role_oid=current_role_oid,
        server_version_num=server_version_num,
        database_acl=_rows_as_tuples(acl_result.all()),
        runtime_roles=_rows_as_tuples(roles_result.all()),
        memberships=_rows_as_tuples(memberships_result.all()),
        restore_facts=_rows_as_tuples(restore_result.all()),
        active_non_owner_sessions=_rows_as_tuples(session_result.all()),
        schema_catalog=await _read_schema_catalog(connection),
        business_rows=await _read_business_row_summaries(connection),
    )


def _expected_fresh_acl(owner_oid: int) -> tuple[DatabaseAclTuple, ...]:
    """依据动态 owner OID 构造独立的 PostgreSQL default database ACL 期望值。"""
    return tuple(
        sorted(
            (
                DatabaseAclTuple(owner_oid, owner_oid, "CREATE", False),
                DatabaseAclTuple(owner_oid, owner_oid, "CONNECT", False),
                DatabaseAclTuple(owner_oid, owner_oid, "TEMPORARY", False),
                DatabaseAclTuple(0, owner_oid, "CONNECT", False),
                DatabaseAclTuple(0, owner_oid, "TEMPORARY", False),
            )
        )
    )


@pytest.mark.asyncio
async def test_database_acl_live_default_expands_via_acldefault_without_mutation(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """真实 PostgreSQL 17 的 ``datacl NULL`` 必须展开为五条 fresh_default tuple。"""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            before = await _capture_live_catalog_state(connection)
            snapshot = await read_database_access_snapshot(
                connection,
                target_database_oid=before.target_database_oid,
            )
            after = await _capture_live_catalog_state(connection)
    finally:
        await engine.dispose()

    assert 170000 <= before.server_version_num < 180000
    assert before.raw_database_acl is None
    assert before.current_role_oid == before.owner_oid
    assert snapshot.target_database_oid == before.target_database_oid
    assert snapshot.owner_oid == before.owner_oid
    assert snapshot.acl_profile is DatabaseAclProfile.FRESH_DEFAULT
    assert snapshot.acl_tuples == _expected_fresh_acl(before.owner_oid)
    assert all(tuple_.grantor_oid == before.owner_oid for tuple_ in snapshot.acl_tuples)
    assert all(tuple_.is_grantable is False for tuple_ in snapshot.acl_tuples)
    assert after == before


@pytest.mark.asyncio
async def test_runtime_role_live_reader_records_absent_pair_and_zero_membership(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """Task 13 授权库当前两角色同时 absent，且双向 membership 必须为空。"""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            before = await _capture_live_catalog_state(connection)
            snapshot = await read_database_access_snapshot(
                connection,
                target_database_oid=before.target_database_oid,
            )
            after = await _capture_live_catalog_state(connection)
    finally:
        await engine.dispose()

    assert before.runtime_roles == ()
    assert before.memberships == ()
    assert before.current_role_oid == before.owner_oid
    assert snapshot.app_role is None
    assert snapshot.retention_role is None
    assert snapshot.memberships == ()
    assert after == before


@pytest.mark.asyncio
async def test_bootstrap_candidate_live_snapshot_is_classified_with_zero_mutation(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """live fresh/roles-absent 只能成为 role-bootstrap candidate，且所有事实保持不变。"""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            before = await _capture_live_catalog_state(connection)
            snapshot = await read_database_access_snapshot(
                connection,
                target_database_oid=before.target_database_oid,
            )
            candidate = classify_bootstrap_candidate(
                snapshot,
                caller=BootstrapCaller.ROLE_BOOTSTRAP,
                restore_facts_absent=before.restore_facts == (),
                has_active_non_owner_sessions=before.active_non_owner_sessions != (),
                # Cycle 3 才拥有真实 object-grant inventory；本循环只冻结显式闭集输入。
                object_grants_match=True,
            )
            after = await _capture_live_catalog_state(connection)
    finally:
        await engine.dispose()

    assert before.restore_facts == ()
    assert before.active_non_owner_sessions == ()
    assert before.current_role_oid == before.owner_oid
    assert candidate.caller is BootstrapCaller.ROLE_BOOTSTRAP
    assert candidate.source_profile is DatabaseAclProfile.FRESH_DEFAULT
    assert candidate.roles_exist is False
    assert candidate.snapshot == snapshot
    assert after == before


def _sync_lifecycle_urls(database_url: ValidatedDatabaseUrl) -> tuple[URL, URL, str]:
    """把已验证 asyncpg 测试 URL 转成同端点 psycopg target/management URL。"""
    target_url = make_url(str(database_url)).set(drivername="postgresql+psycopg")
    target_name = target_url.database
    if target_name is None:
        raise AssertionError("validated test URL must include a target database")
    return target_url.set(database="postgres"), target_url, target_name


@dataclass(frozen=True, slots=True)
class _TransactionalCatalogState:
    """保存 D 组 transaction case 前后必须完全一致的 catalog/数据事实。"""

    target_database_oid: int
    owner_oid: int
    raw_database_acl: tuple[str, ...] | None
    runtime_roles: tuple[tuple[object, ...], ...]
    memberships: tuple[tuple[object, ...], ...]
    database_acl: tuple[tuple[object, ...], ...]
    object_grants: tuple[tuple[object, ...], ...]
    restore_facts: tuple[tuple[object, ...], ...]
    alembic_versions: tuple[tuple[object, ...], ...]
    schema_catalog: tuple[tuple[object, ...], ...]
    business_rows: tuple[tuple[str, int, str], ...]


def _read_schema_catalog_sync(connection: Connection) -> tuple[tuple[object, ...], ...]:
    """同步读取 public schema 的对象、列、约束与索引定义。"""
    rows: list[tuple[object, ...]] = []
    for category, sql in _SCHEMA_CATALOG_QUERIES:
        result = connection.execute(text(sql))
        rows.extend((category, *tuple(row)) for row in result.all())
    return tuple(rows)


def _read_business_row_summaries_sync(
    connection: Connection,
) -> tuple[tuple[str, int, str], ...]:
    """同步计算所有业务表的 content-free row count 与 digest。"""
    table_result = connection.execute(text(_BUSINESS_TABLES_SQL))
    summaries: list[tuple[str, int, str]] = []
    for row in table_result.mappings().all():
        table_name = row["relname"]
        if type(table_name) is not str:
            raise AssertionError("public table name must be text")
        quoted_table = _quote_identifier(table_name)
        summary = (
            connection.execute(
                text(
                    "SELECT count(*)::bigint AS row_count, "
                    "md5(COALESCE(string_agg(md5(to_jsonb(row_value)::text), '' "
                    "ORDER BY md5(to_jsonb(row_value)::text)), '')) AS content_digest "
                    f"FROM public.{quoted_table} AS row_value"
                )
            )
            .mappings()
            .one()
        )
        row_count = summary["row_count"]
        content_digest = summary["content_digest"]
        if type(row_count) is not int or type(content_digest) is not str:
            raise AssertionError("business row summary has an unexpected type")
        summaries.append((table_name, row_count, content_digest))
    return tuple(summaries)


def _capture_sync_transactional_state(
    connection: Connection,
) -> _TransactionalCatalogState:
    """从同步 owner connection 读取 zero-write 与 rollback 所需的完整事实。

    该 helper 只用于决定本地是否有资格执行后续 mutation matrix；它不执行清理 SQL。
    只有 transaction rollback 后全新连接返回逐项相同的快照，后续测试才可继续。
    """
    identity = connection.execute(text(_DATABASE_IDENTITY_SQL)).mappings().one()
    target_database_oid = identity["target_database_oid"]
    owner_oid = identity["owner_oid"]
    raw_database_acl = identity["raw_database_acl"]
    if type(target_database_oid) is not int or type(owner_oid) is not int:
        raise AssertionError("transactional state identity must contain integer OIDs")
    if raw_database_acl is None:
        normalized_raw_acl: tuple[str, ...] | None = None
    elif isinstance(raw_database_acl, list) and all(
        type(value) is str for value in raw_database_acl
    ):
        normalized_raw_acl = tuple(raw_database_acl)
    else:
        raise AssertionError("transactional state raw database ACL is malformed")
    parameters = {
        "target_database_oid": target_database_oid,
        "app_role_name": APP_RUNTIME_ROLE_NAME,
        "retention_role_name": RETENTION_RUNTIME_ROLE_NAME,
    }
    roles = connection.execute(text(_RUNTIME_ROLES_SQL), parameters).all()
    memberships = connection.execute(text(_MEMBERSHIPS_SQL), parameters).all()
    database_acl = connection.execute(
        text(EXPECTED_DATABASE_ACL_SQL),
        {"target_database_oid": target_database_oid},
    ).all()
    restore_facts = connection.execute(
        text(_RESTORE_FACTS_SQL),
        {"target_database_oid": target_database_oid},
    ).all()
    object_grants: list[tuple[object, ...]] = []
    for sql in _OBJECT_GRANT_CATALOG_SQL:
        rows = connection.execute(text(sql), {"schema_name": "public"}).all()
        object_grants.extend(tuple(row) for row in rows)
    alembic_versions = connection.execute(
        text("SELECT version_num FROM alembic_version ORDER BY version_num")
    ).all()
    return _TransactionalCatalogState(
        target_database_oid=target_database_oid,
        owner_oid=owner_oid,
        raw_database_acl=normalized_raw_acl,
        runtime_roles=_rows_as_tuples(roles),
        memberships=_rows_as_tuples(memberships),
        database_acl=tuple(sorted(tuple(row) for row in database_acl)),
        object_grants=tuple(sorted(object_grants)),
        restore_facts=_rows_as_tuples(restore_facts),
        alembic_versions=_rows_as_tuples(alembic_versions),
        schema_catalog=_read_schema_catalog_sync(connection),
        business_rows=_read_business_row_summaries_sync(connection),
    )


@contextmanager
def _transactional_catalog_case(
    engine: Engine,
) -> Iterator[tuple[Connection, _TransactionalCatalogState]]:
    """为一个 mutation case 提供唯一 transaction，并从新连接复核完全回滚。

    fixture 不拥有 ``DROP ROLE`` 或其他补偿 SQL。若 case 失败、driver 报错或断言中断，
    内层 transaction 仍先 rollback；外层 ``finally`` 再从新连接比较完整状态并证明
    fixed role pair 仍 absent。这样每个参数 case 都不依赖前一个用例留下的 cluster 状态。
    """
    with engine.connect() as before_connection:
        before = _capture_sync_transactional_state(before_connection)
    assert before.runtime_roles == ()
    assert before.memberships == ()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                yield connection, before
            finally:
                if transaction.is_active:
                    transaction.rollback()
    finally:
        with engine.connect() as after_connection:
            after = _capture_sync_transactional_state(after_connection)
        assert after == before
        assert after.runtime_roles == ()
        assert after.memberships == ()


def _quoted_runtime_role(connection: Connection, role_name: str) -> str:
    """只引用两个 fixed runtime role 之一。"""
    if role_name not in {APP_RUNTIME_ROLE_NAME, RETENTION_RUNTIME_ROLE_NAME}:
        raise AssertionError("unexpected runtime role name")
    return connection.dialect.identifier_preparer.quote_identifier(role_name)


def _create_safe_runtime_roles(
    connection: Connection,
    *,
    role_names: tuple[str, ...] = (
        APP_RUNTIME_ROLE_NAME,
        RETENTION_RUNTIME_ROLE_NAME,
    ),
) -> None:
    """在当前 test-owned transaction 内创建无密码的 exact safe role setup。"""
    for role_name in role_names:
        quoted_role = _quoted_runtime_role(connection, role_name)
        connection.execute(
            text(
                f"CREATE ROLE {quoted_role} LOGIN NOSUPERUSER INHERIT "
                "NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS "
                "CONNECTION LIMIT -1"
            )
        )


def _apply_current_runtime_object_grants(connection: Connection) -> None:
    """从真实 pre-runtime inventory 应用 0018 baseline runtime grants。"""
    apply_bootstrap_object_grants(
        connection,
        revision="20260809_0018",
        phase=GrantPhase.BASELINE,
        posture=BootstrapGrantPosture.PRE_RUNTIME,
    )


def _apply_database_profile_setup(
    connection: Connection,
    *,
    target_database_name: str,
    profile: DatabaseAclProfile,
) -> None:
    """仅在 test transaction 内构造四个 frozen database ACL profile。"""
    if type(profile) is not DatabaseAclProfile:
        raise AssertionError("database ACL profile must be typed")
    if profile is DatabaseAclProfile.FRESH_DEFAULT:
        return
    quoted_database = connection.dialect.identifier_preparer.quote_identifier(target_database_name)
    quoted_app = _quoted_runtime_role(connection, APP_RUNTIME_ROLE_NAME)
    quoted_retention = _quoted_runtime_role(connection, RETENTION_RUNTIME_ROLE_NAME)
    if profile is DatabaseAclProfile.PRE_PROTOCOL_LEGACY:
        for quoted_role in (quoted_app, quoted_retention):
            connection.execute(
                text(f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_role}")
            )
        return
    connection.execute(text(f"REVOKE ALL PRIVILEGES ON DATABASE {quoted_database} FROM PUBLIC"))
    if profile is DatabaseAclProfile.BASELINE:
        for quoted_role in (quoted_app, quoted_retention):
            connection.execute(
                text(f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_role}")
            )
        return
    if profile is not DatabaseAclProfile.ACTIVE:
        raise AssertionError("unsupported database ACL profile")


def _expected_live_acl(
    *,
    profile: DatabaseAclProfile,
    owner_oid: int,
    app_role_oid: int | None,
    retention_role_oid: int | None,
) -> tuple[DatabaseAclTuple, ...]:
    """独立构造 real-catalog profile 的完整排序 ACL multiset。"""
    owner_acl = (
        DatabaseAclTuple(owner_oid, owner_oid, "CREATE", False),
        DatabaseAclTuple(owner_oid, owner_oid, "CONNECT", False),
        DatabaseAclTuple(owner_oid, owner_oid, "TEMPORARY", False),
    )
    if profile is DatabaseAclProfile.FRESH_DEFAULT:
        return tuple(
            sorted(
                (
                    *owner_acl,
                    DatabaseAclTuple(0, owner_oid, "CONNECT", False),
                    DatabaseAclTuple(0, owner_oid, "TEMPORARY", False),
                )
            )
        )
    if profile is DatabaseAclProfile.ACTIVE:
        return tuple(sorted(owner_acl))
    if app_role_oid is None or retention_role_oid is None:
        raise AssertionError("legacy/baseline ACL requires both runtime role OIDs")
    runtime_connect = (
        DatabaseAclTuple(app_role_oid, owner_oid, "CONNECT", False),
        DatabaseAclTuple(retention_role_oid, owner_oid, "CONNECT", False),
    )
    if profile is DatabaseAclProfile.PRE_PROTOCOL_LEGACY:
        return tuple(
            sorted(
                (
                    *owner_acl,
                    DatabaseAclTuple(0, owner_oid, "CONNECT", False),
                    DatabaseAclTuple(0, owner_oid, "TEMPORARY", False),
                    *runtime_connect,
                )
            )
        )
    if profile is DatabaseAclProfile.BASELINE:
        return tuple(sorted((*owner_acl, *runtime_connect)))
    raise AssertionError("unsupported database ACL profile")


def _assert_safe_runtime_role(role: RuntimeRoleSnapshot, *, role_name: str) -> None:
    """逐字段断言 real ``pg_roles`` row 精确满足 safe posture。"""
    assert role.role_name == role_name
    assert role.can_login is True
    assert role.inherits is True
    assert role.is_superuser is False
    assert role.can_create_database is False
    assert role.can_create_role is False
    assert role.can_replicate is False
    assert role.bypasses_rls is False
    assert role.connection_limit == -1
    assert role.valid_until is None
    assert role.config is None


def _live_bootstrap_candidate(
    connection: Connection,
    *,
    caller: BootstrapCaller,
) -> BootstrapCandidate:
    """用真实 facts/access/object-grant readers 分类当前 transaction 的 Candidate。

    ``object_grants_match=True`` 只在 production verifier 已完整返回后传给纯 classifier；
    该 bool 不是测试手工豁免。partial role pair 仍使用 pre-runtime verifier，随后由 access
    classifier 按 mixed role shape fail closed。
    """
    from ai_employee.infrastructure.db.database_maintenance import (
        read_database_restore_facts,
    )

    identity = connection.execute(text(_DATABASE_IDENTITY_SQL)).mappings().one()
    target_database_oid = identity["target_database_oid"]
    owner_oid = identity["owner_oid"]
    if type(target_database_oid) is not int or type(owner_oid) is not int:
        raise AssertionError("candidate identity OIDs must be integers")
    facts = read_database_restore_facts(
        connection,
        target_database_oid=target_database_oid,
    )
    snapshot = read_database_access_snapshot_sync(
        connection,
        target_database_oid=target_database_oid,
    )
    both_roles_present = snapshot.app_role is not None and snapshot.retention_role is not None
    posture = (
        BootstrapGrantPosture.CURRENT if both_roles_present else BootstrapGrantPosture.PRE_RUNTIME
    )
    verify_bootstrap_object_grants(
        connection,
        revision="20260809_0018",
        phase=GrantPhase.BASELINE,
        posture=posture,
    )
    active_sessions = connection.execute(
        text(_ACTIVE_NON_OWNER_SESSIONS_SQL),
        {
            "target_database_oid": target_database_oid,
            "owner_oid": owner_oid,
        },
    ).all()
    return classify_bootstrap_candidate(
        snapshot,
        caller=caller,
        restore_facts_absent=(
            facts.maintenance_gate is None
            and facts.restore_call_authority is None
            and facts.restore_completion is None
        ),
        has_active_non_owner_sessions=bool(active_sessions),
        object_grants_match=True,
    )


@contextmanager
def _record_connection_statements(connection: Connection) -> Iterator[list[str]]:
    """记录一个 focused entry 在既有 setup 之后执行的 SQL 文本。"""
    statements: list[str] = []

    def record(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(connection, "before_cursor_execute", record)


def _assert_only_read_statements(statements: Sequence[str]) -> None:
    """要求 admission 只执行 ``SELECT/WITH``，没有任何 mutation SQL。"""
    assert statements
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "WITH")) for statement in statements
    )


def _assert_live_role_bootstrap_rejected_without_writes(connection: Connection) -> None:
    """让 public role-bootstrap entry 消费 live verifier，并在 schema/runner 前拒绝。"""
    from ai_employee.infrastructure.db.database_access import DatabaseAccessInvariantError
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceContext,
        role_bootstrap_database,
    )

    events: list[str] = []

    class Target:
        def assert_role_bootstrap_allowed(self) -> None:
            events.append("admission")
            _live_bootstrap_candidate(
                connection,
                caller=BootstrapCaller.ROLE_BOOTSTRAP,
            )

        @contextmanager
        def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
            events.append("schema-enter")
            yield

        def run_typed_role_bootstrap(self) -> None:
            events.append("role-bootstrap-mutation")

    class Management:
        @contextmanager
        def acquire_target_lock(self) -> Iterator[Target]:
            events.append("target-enter")
            try:
                yield Target()
            finally:
                events.append("target-exit")

    class Context:
        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[Management]:
            events.append("management-enter")
            try:
                yield Management()
            finally:
                events.append("management-exit")

    before_entry = _capture_sync_transactional_state(connection)
    with (
        _record_connection_statements(connection) as statements,
        pytest.raises(
            DatabaseAccessInvariantError,
            match=r"^database access invariant violation$",
        ),
    ):
        role_bootstrap_database(cast(DatabaseMaintenanceContext, Context()))
    after_entry = _capture_sync_transactional_state(connection)

    assert events == [
        "management-enter",
        "target-enter",
        "admission",
        "target-exit",
        "management-exit",
    ]
    _assert_only_read_statements(statements)
    assert any("pg_auth_members" in statement for statement in statements)
    assert any("aclexplode" in statement for statement in statements)
    assert after_entry == before_entry


def _assert_live_candidate_migration_rejected_without_writes(
    connection: Connection,
) -> None:
    """让 public migrate entry 用 live verifier 证明 Candidate 不是 steady state。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceContext,
        DatabaseMaintenanceInvariantError,
        classify_database_admission,
        migrate_database,
        read_database_restore_facts,
    )

    events: list[str] = []

    class Target:
        def assert_migration_allowed(self) -> None:
            events.append("admission")
            candidate = _live_bootstrap_candidate(
                connection,
                caller=BootstrapCaller.ROLE_BOOTSTRAP,
            )
            facts = read_database_restore_facts(
                connection,
                target_database_oid=candidate.snapshot.target_database_oid,
            )
            # ``_live_bootstrap_candidate`` 已在同一 connection 完整验证 object grants；
            # 此处的 True 只是把该成功事实送入 steady classifier，不是测试豁免。
            classify_database_admission(
                facts=facts,
                access_snapshot=candidate.snapshot,
                object_grants_match=True,
                # Candidate 无 restore authority；该用例只证明 steady admission fail closed。
                expected_target_identity_digest="0" * 64,
            )

        @contextmanager
        def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
            events.append("schema-enter")
            yield

        def run_typed_migration(self) -> None:
            events.append("migration-mutation")

    class Management:
        @contextmanager
        def acquire_target_lock(self) -> Iterator[Target]:
            events.append("target-enter")
            try:
                yield Target()
            finally:
                events.append("target-exit")

    class Context:
        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[Management]:
            events.append("management-enter")
            try:
                yield Management()
            finally:
                events.append("management-exit")

    before_entry = _capture_sync_transactional_state(connection)
    with (
        _record_connection_statements(connection) as statements,
        pytest.raises(
            DatabaseMaintenanceInvariantError,
            match=r"^database maintenance invariant violation$",
        ),
    ):
        migrate_database(cast(DatabaseMaintenanceContext, Context()))
    after_entry = _capture_sync_transactional_state(connection)

    assert events == [
        "management-enter",
        "target-enter",
        "admission",
        "target-exit",
        "management-exit",
    ]
    _assert_only_read_statements(statements)
    assert any("pg_auth_members" in statement for statement in statements)
    assert any("aclexplode" in statement for statement in statements)
    assert after_entry == before_entry


def test_runtime_role_acl_and_object_grant_setup_rolls_back_atomically(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """证明 D 组 live setup 可由单一 transaction 回滚且无需 ``DROP ROLE``。

    本用例先从新连接证明 fixed role pair absent，再在一个显式 transaction 内同时执行
    ``CREATE/ALTER ROLE``、database ACL 与 object grant mutation。无论断言是否成功，
    唯一清理动作都是该 transaction 自身 rollback；随后用另一个新连接逐项复核全局
    roles、membership、ACL、object grants 与 restore facts 完全恢复。
    """
    _, target_url, target_name = _sync_lifecycle_urls(database_url)
    engine = create_engine(target_url, poolclass=NullPool)
    quoted_database = engine.dialect.identifier_preparer.quote_identifier(target_name)
    quoted_app_role = engine.dialect.identifier_preparer.quote_identifier(APP_RUNTIME_ROLE_NAME)
    quoted_retention_role = engine.dialect.identifier_preparer.quote_identifier(
        RETENTION_RUNTIME_ROLE_NAME
    )
    try:
        with engine.connect() as connection:
            before = _capture_sync_transactional_state(connection)
        assert before.runtime_roles == ()
        assert before.memberships == ()

        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                for quoted_role in (quoted_app_role, quoted_retention_role):
                    connection.execute(
                        text(
                            f"CREATE ROLE {quoted_role} LOGIN NOSUPERUSER INHERIT "
                            "NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS "
                            "CONNECTION LIMIT -1"
                        )
                    )
                connection.execute(
                    text(f"ALTER ROLE {quoted_app_role} SET statement_timeout TO '1s'")
                )
                connection.execute(
                    text(f"REVOKE ALL PRIVILEGES ON DATABASE {quoted_database} FROM PUBLIC")
                )
                connection.execute(
                    text(f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_app_role}")
                )
                connection.execute(
                    text(f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_retention_role}")
                )
                connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {quoted_app_role}"))
                during = _capture_sync_transactional_state(connection)
                assert {row[0] for row in during.runtime_roles} == {
                    APP_RUNTIME_ROLE_NAME,
                    RETENTION_RUNTIME_ROLE_NAME,
                }
                assert during.database_acl != before.database_acl
                assert during.object_grants != before.object_grants
                assert during.restore_facts == before.restore_facts
            finally:
                transaction.rollback()

        with engine.connect() as connection:
            after = _capture_sync_transactional_state(connection)
        assert after == before
        assert after.runtime_roles == ()
        assert after.memberships == ()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("profile", "roles_exist", "candidate_expected"),
    (
        pytest.param(
            DatabaseAclProfile.FRESH_DEFAULT,
            False,
            True,
            id="fresh-roles-absent",
        ),
        pytest.param(
            DatabaseAclProfile.FRESH_DEFAULT,
            True,
            True,
            id="fresh-roles-safe",
        ),
        pytest.param(
            DatabaseAclProfile.PRE_PROTOCOL_LEGACY,
            True,
            True,
            id="legacy-roles-safe",
        ),
        pytest.param(
            DatabaseAclProfile.BASELINE,
            True,
            False,
            id="baseline-roles-safe",
        ),
        pytest.param(
            DatabaseAclProfile.ACTIVE,
            True,
            False,
            id="active-roles-safe",
        ),
    ),
)
def test_live_database_acl_profiles_and_safe_candidate_shapes_are_exact(
    database_url: ValidatedDatabaseUrl,
    profile: DatabaseAclProfile,
    roles_exist: bool,
    candidate_expected: bool,
) -> None:
    """真实 PostgreSQL 17 必须返回四个完整 profile 与三种合法 Candidate 形状。"""
    _, target_url, target_name = _sync_lifecycle_urls(database_url)
    engine = create_engine(target_url, poolclass=NullPool)
    try:
        with _transactional_catalog_case(engine) as (connection, _):
            if roles_exist:
                _create_safe_runtime_roles(connection)
                _apply_current_runtime_object_grants(connection)
            _apply_database_profile_setup(
                connection,
                target_database_name=target_name,
                profile=profile,
            )
            identity = connection.execute(text(_DATABASE_IDENTITY_SQL)).mappings().one()
            target_database_oid = identity["target_database_oid"]
            if type(target_database_oid) is not int:
                raise AssertionError("live profile target OID must be an integer")
            snapshot = read_database_access_snapshot_sync(
                connection,
                target_database_oid=target_database_oid,
            )

            assert snapshot.acl_profile is profile
            assert snapshot.memberships == ()
            assert snapshot.acl_tuples == _expected_live_acl(
                profile=profile,
                owner_oid=snapshot.owner_oid,
                app_role_oid=(None if snapshot.app_role is None else snapshot.app_role.role_oid),
                retention_role_oid=(
                    None if snapshot.retention_role is None else snapshot.retention_role.role_oid
                ),
            )
            assert all(tuple_.is_grantable is False for tuple_ in snapshot.acl_tuples)
            owner_tuples = tuple(
                tuple_ for tuple_ in snapshot.acl_tuples if tuple_.grantee_oid == snapshot.owner_oid
            )
            assert len(owner_tuples) == 3
            assert all(tuple_.grantor_oid == snapshot.owner_oid for tuple_ in owner_tuples)
            assert all(tuple_.is_grantable is False for tuple_ in owner_tuples)

            if roles_exist:
                assert snapshot.app_role is not None
                assert snapshot.retention_role is not None
                _assert_safe_runtime_role(
                    snapshot.app_role,
                    role_name=APP_RUNTIME_ROLE_NAME,
                )
                _assert_safe_runtime_role(
                    snapshot.retention_role,
                    role_name=RETENTION_RUNTIME_ROLE_NAME,
                )
            else:
                assert snapshot.app_role is None
                assert snapshot.retention_role is None

            if candidate_expected:
                candidate = _live_bootstrap_candidate(
                    connection,
                    caller=BootstrapCaller.ROLE_BOOTSTRAP,
                )
                assert candidate.source_profile is profile
                assert candidate.roles_exist is roles_exist
                assert candidate.snapshot == snapshot
            else:
                verify_object_grants(
                    connection,
                    revision="20260809_0018",
                    phase=(
                        GrantPhase.ACTIVE
                        if profile is DatabaseAclProfile.ACTIVE
                        else GrantPhase.BASELINE
                    ),
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "profile",
    (
        pytest.param(DatabaseAclProfile.FRESH_DEFAULT, id="fresh-roles-absent"),
        pytest.param(DatabaseAclProfile.PRE_PROTOCOL_LEGACY, id="legacy-roles-safe"),
    ),
)
def test_live_candidate_direct_migration_rejects_without_catalog_mutation(
    database_url: ValidatedDatabaseUrl,
    profile: DatabaseAclProfile,
) -> None:
    """fresh/legacy Candidate 均须在 public migrate 的 schema/runner 前零写拒绝。

    每个参数都在唯一 PostgreSQL transaction 内构造真实 Candidate，并让 public
    ``migrate_database`` 消费 production restore/access/object-grant readers。用例退出时
    只 rollback 该 transaction，再由新连接逐项证明 fixed roles、ACL、Schema、业务行与
    restore facts 完全恢复；不使用 ``DROP ROLE`` 或其他补偿 SQL。
    """
    _, target_url, target_name = _sync_lifecycle_urls(database_url)
    engine = create_engine(target_url, poolclass=NullPool)
    try:
        with _transactional_catalog_case(engine) as (connection, _):
            if profile is DatabaseAclProfile.PRE_PROTOCOL_LEGACY:
                _create_safe_runtime_roles(connection)
                _apply_current_runtime_object_grants(connection)
            elif profile is not DatabaseAclProfile.FRESH_DEFAULT:
                raise AssertionError("direct migration matrix accepts only Candidate profiles")
            _apply_database_profile_setup(
                connection,
                target_database_name=target_name,
                profile=profile,
            )

            _assert_live_candidate_migration_rejected_without_writes(connection)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "source_profile",
    (
        pytest.param(DatabaseAclProfile.FRESH_DEFAULT, id="fresh-roles-absent"),
        pytest.param(DatabaseAclProfile.PRE_PROTOCOL_LEGACY, id="legacy-roles-safe"),
    ),
)
def test_live_candidate_to_baseline_focused_mutators_roll_back_atomically(
    database_url: ValidatedDatabaseUrl,
    source_profile: DatabaseAclProfile,
) -> None:
    """public Candidate→baseline 编排须调用真实 focused mutator 且整体可回滚。

    management/target/schema 使用 test-local 窄 lease 只记录 public orchestration 顺序；
    Candidate、三项 restore facts、database access、object grants 以及三个 focused mutator
    都使用 production 实现和同一个真实 owner connection。外层 transaction 由测试持有，
    Fake lease 不提交；函数返回后先证明已精确收敛为 ``pristine_idle`` 且 transaction 仍
    active，随后唯一 rollback 并由新连接证明 cluster/catalog/业务数据零残留。
    """
    from ai_employee.infrastructure.db.database_access import (
        apply_database_acl_mutation,
        apply_safe_runtime_roles,
    )
    from ai_employee.infrastructure.db.database_access import (
        bootstrap_candidate_to_baseline as plan_candidate_to_baseline,
    )
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseAdmissionState,
        DatabaseMaintenanceContext,
        DatabaseRestoreFacts,
        bootstrap_candidate_to_baseline,
        classify_database_admission,
        read_database_restore_facts,
    )

    _, target_url, target_name = _sync_lifecycle_urls(database_url)
    engine = create_engine(target_url, poolclass=NullPool)
    try:
        with _transactional_catalog_case(engine) as (connection, _):
            if source_profile is DatabaseAclProfile.PRE_PROTOCOL_LEGACY:
                _create_safe_runtime_roles(connection)
                _apply_current_runtime_object_grants(connection)
            elif source_profile is not DatabaseAclProfile.FRESH_DEFAULT:
                raise AssertionError("focused bootstrap accepts only Candidate profiles")
            _apply_database_profile_setup(
                connection,
                target_database_name=target_name,
                profile=source_profile,
            )
            candidate_state = _capture_sync_transactional_state(connection)
            events: list[str] = []
            observed_candidate: BootstrapCandidate | None = None

            def verify_pristine_idle() -> DatabaseAccessSnapshot:
                """在当前未提交 transaction 内重读并验证完整 steady posture。"""
                identity = connection.execute(text(_DATABASE_IDENTITY_SQL)).mappings().one()
                target_database_oid = identity["target_database_oid"]
                if type(target_database_oid) is not int:
                    raise AssertionError("bootstrap target OID must be an integer")
                facts = read_database_restore_facts(
                    connection,
                    target_database_oid=target_database_oid,
                )
                snapshot = read_database_access_snapshot_sync(
                    connection,
                    target_database_oid=target_database_oid,
                )
                verify_object_grants(
                    connection,
                    revision="20260809_0018",
                    phase=GrantPhase.BASELINE,
                )
                state = classify_database_admission(
                    facts=facts,
                    access_snapshot=snapshot,
                    object_grants_match=True,
                    # pristine 没有 authority；精确 target 绑定在真实 lease 的稳定路径另测。
                    expected_target_identity_digest="0" * 64,
                )
                assert facts == DatabaseRestoreFacts(None, None, None)
                assert state is DatabaseAdmissionState.PRISTINE_IDLE
                return snapshot

            class Transaction:
                def apply_safe_runtime_roles(self, candidate: BootstrapCandidate) -> None:
                    events.append("roles")
                    assert candidate is observed_candidate
                    assert (
                        _live_bootstrap_candidate(
                            connection,
                            caller=BootstrapCaller.ROLE_BOOTSTRAP,
                        )
                        == candidate
                    )
                    apply_safe_runtime_roles(
                        connection,
                        candidate,
                        app_password="synthetic-app-secret",
                        retention_password="synthetic-retention-secret",
                    )

                def apply_database_acl(self, profile: DatabaseAclProfile) -> None:
                    events.append("database-acl")
                    assert profile is DatabaseAclProfile.BASELINE
                    if observed_candidate is None:
                        raise AssertionError("candidate must be read before ACL mutation")
                    apply_database_acl_mutation(
                        connection,
                        target_database_name=target_name,
                        plan=plan_candidate_to_baseline(observed_candidate),
                    )

                def apply_object_grants(self, *, revision: str, phase: GrantPhase) -> None:
                    events.append("object-grants")
                    assert revision == "20260809_0018"
                    assert phase is GrantPhase.BASELINE
                    if observed_candidate is None:
                        raise AssertionError("candidate must be read before grant mutation")
                    apply_bootstrap_object_grants(
                        connection,
                        revision=revision,
                        phase=phase,
                        posture=(
                            BootstrapGrantPosture.CURRENT
                            if observed_candidate.roles_exist
                            else BootstrapGrantPosture.PRE_RUNTIME
                        ),
                    )

                def verify_pristine_idle_before_commit(self) -> None:
                    events.append("verify-before")
                    verify_pristine_idle()

            class Target:
                revision = "20260809_0018"

                def assert_restore_facts_absent(self) -> None:
                    events.append("facts")
                    identity = connection.execute(text(_DATABASE_IDENTITY_SQL)).mappings().one()
                    target_database_oid = identity["target_database_oid"]
                    if type(target_database_oid) is not int:
                        raise AssertionError("bootstrap target OID must be an integer")
                    assert read_database_restore_facts(
                        connection,
                        target_database_oid=target_database_oid,
                    ) == DatabaseRestoreFacts(None, None, None)

                def read_bootstrap_candidate(self) -> BootstrapCandidate:
                    nonlocal observed_candidate
                    events.append("candidate")
                    observed_candidate = _live_bootstrap_candidate(
                        connection,
                        caller=BootstrapCaller.ROLE_BOOTSTRAP,
                    )
                    assert observed_candidate.source_profile is source_profile
                    return observed_candidate

                @contextmanager
                def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
                    events.append("schema-enter")
                    try:
                        yield
                    finally:
                        events.append("schema-exit")

                @contextmanager
                def owner_transaction(self) -> Iterator[Transaction]:
                    events.append("transaction-enter")
                    try:
                        yield Transaction()
                    finally:
                        events.append("transaction-exit")

                def verify_pristine_idle_after_commit(self) -> None:
                    events.append("verify-after")
                    verify_pristine_idle()

            class Management:
                @contextmanager
                def acquire_target_lock(self) -> Iterator[Target]:
                    events.append("target-enter")
                    try:
                        yield Target()
                    finally:
                        events.append("target-exit")

            class Context:
                @contextmanager
                def acquire_management_lifecycle_lock(self) -> Iterator[Management]:
                    events.append("management-enter")
                    try:
                        yield Management()
                    finally:
                        events.append("management-exit")

            bootstrap_candidate_to_baseline(cast(DatabaseMaintenanceContext, Context()))

            assert connection.in_transaction() is True
            final_snapshot = verify_pristine_idle()
            final_state = _capture_sync_transactional_state(connection)
            assert final_snapshot.acl_profile is DatabaseAclProfile.BASELINE
            assert final_snapshot.app_role is not None
            assert final_snapshot.retention_role is not None
            _assert_safe_runtime_role(
                final_snapshot.app_role,
                role_name=APP_RUNTIME_ROLE_NAME,
            )
            _assert_safe_runtime_role(
                final_snapshot.retention_role,
                role_name=RETENTION_RUNTIME_ROLE_NAME,
            )
            assert final_snapshot.memberships == ()
            assert final_state.database_acl != candidate_state.database_acl
            assert final_state.restore_facts == candidate_state.restore_facts
            assert final_state.alembic_versions == candidate_state.alembic_versions
            assert final_state.schema_catalog == candidate_state.schema_catalog
            assert final_state.business_rows == candidate_state.business_rows
            assert events == [
                "management-enter",
                "target-enter",
                "facts",
                "candidate",
                "schema-enter",
                "transaction-enter",
                "roles",
                "database-acl",
                "object-grants",
                "verify-before",
                "transaction-exit",
                "verify-after",
                "schema-exit",
                "target-exit",
                "management-exit",
            ]
    finally:
        engine.dispose()


def test_live_steady_grant_reassertions_are_idempotent_and_roll_back_atomically(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """真实 PostgreSQL 上重复 CONNECT/object GRANT 不改变 exact baseline，且整体可回滚。"""
    from ai_employee.infrastructure.db.database_access import (
        apply_baseline_connect_reassertion,
    )

    _, target_url, target_name = _sync_lifecycle_urls(database_url)
    engine = create_engine(target_url, poolclass=NullPool)
    try:
        with _transactional_catalog_case(engine) as (connection, _):
            _create_safe_runtime_roles(connection)
            _apply_current_runtime_object_grants(connection)
            _apply_database_profile_setup(
                connection,
                target_database_name=target_name,
                profile=DatabaseAclProfile.BASELINE,
            )
            identity = connection.execute(text(_DATABASE_IDENTITY_SQL)).mappings().one()
            target_database_oid = identity["target_database_oid"]
            if type(target_database_oid) is not int:
                raise AssertionError("steady reassert target OID must be an integer")
            baseline_snapshot = read_database_access_snapshot_sync(
                connection,
                target_database_oid=target_database_oid,
            )
            verify_object_grants(
                connection,
                revision="20260809_0018",
                phase=GrantPhase.BASELINE,
            )
            before_reassert = _capture_sync_transactional_state(connection)

            for _ in range(2):
                apply_baseline_connect_reassertion(
                    connection,
                    target_database_name=target_name,
                    snapshot=baseline_snapshot,
                )
                apply_bootstrap_object_grants(
                    connection,
                    revision="20260809_0018",
                    phase=GrantPhase.BASELINE,
                    posture=BootstrapGrantPosture.CURRENT,
                )
                observed_snapshot = read_database_access_snapshot_sync(
                    connection,
                    target_database_oid=target_database_oid,
                )
                verify_object_grants(
                    connection,
                    revision="20260809_0018",
                    phase=GrantPhase.BASELINE,
                )
                assert observed_snapshot == baseline_snapshot
                assert _capture_sync_transactional_state(connection) == before_reassert

            assert connection.in_transaction() is True
    finally:
        engine.dispose()


_UNSAFE_ROLE_ALTERATIONS = (
    pytest.param("NOLOGIN", id="no-login"),
    pytest.param("NOINHERIT", id="no-inherit"),
    pytest.param("SUPERUSER", id="superuser"),
    pytest.param("CREATEDB", id="createdb"),
    pytest.param("CREATEROLE", id="createrole"),
    pytest.param("REPLICATION", id="replication"),
    pytest.param("BYPASSRLS", id="bypassrls"),
    pytest.param("CONNECTION LIMIT 0", id="connection-limit"),
    pytest.param("VALID UNTIL '2030-01-01T00:00:00Z'", id="valid-until"),
    pytest.param("SET statement_timeout TO '1s'", id="role-config"),
)


@pytest.mark.parametrize(
    "role_name",
    (
        pytest.param(APP_RUNTIME_ROLE_NAME, id="app"),
        pytest.param(RETENTION_RUNTIME_ROLE_NAME, id="retention"),
    ),
)
@pytest.mark.parametrize("alteration", _UNSAFE_ROLE_ALTERATIONS)
def test_live_unsafe_runtime_role_entry_rejects_without_catalog_mutation(
    database_url: ValidatedDatabaseUrl,
    role_name: str,
    alteration: str,
) -> None:
    """两 fixed roles 的每种 unsafe attribute 都在 live verifier 后零写拒绝。"""
    _, target_url, _ = _sync_lifecycle_urls(database_url)
    engine = create_engine(target_url, poolclass=NullPool)
    try:
        with _transactional_catalog_case(engine) as (connection, _):
            _create_safe_runtime_roles(connection)
            _apply_current_runtime_object_grants(connection)
            quoted_role = _quoted_runtime_role(connection, role_name)
            connection.execute(text(f"ALTER ROLE {quoted_role} {alteration}"))
            _assert_live_role_bootstrap_rejected_without_writes(connection)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "present_role_name",
    (
        pytest.param(APP_RUNTIME_ROLE_NAME, id="app-only"),
        pytest.param(RETENTION_RUNTIME_ROLE_NAME, id="retention-only"),
    ),
)
def test_live_partial_runtime_role_pair_rejects_without_catalog_mutation(
    database_url: ValidatedDatabaseUrl,
    present_role_name: str,
) -> None:
    """fresh profile 只存在一个 fixed role 时必须在 entry mutation 前拒绝。"""
    _, target_url, _ = _sync_lifecycle_urls(database_url)
    engine = create_engine(target_url, poolclass=NullPool)
    try:
        with _transactional_catalog_case(engine) as (connection, _):
            _create_safe_runtime_roles(connection, role_names=(present_role_name,))
            _assert_live_role_bootstrap_rejected_without_writes(connection)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("granted_role", "member_role"),
    (
        pytest.param(
            APP_RUNTIME_ROLE_NAME,
            RETENTION_RUNTIME_ROLE_NAME,
            id="app-roleid-retention-member",
        ),
        pytest.param(
            RETENTION_RUNTIME_ROLE_NAME,
            APP_RUNTIME_ROLE_NAME,
            id="retention-roleid-app-member",
        ),
    ),
)
def test_live_bidirectional_membership_rejects_without_catalog_mutation(
    database_url: ValidatedDatabaseUrl,
    granted_role: str,
    member_role: str,
) -> None:
    """app/retention 作为 roleid 或 member 的双向 membership 都必须零写拒绝。"""
    _, target_url, _ = _sync_lifecycle_urls(database_url)
    engine = create_engine(target_url, poolclass=NullPool)
    try:
        with _transactional_catalog_case(engine) as (connection, _):
            _create_safe_runtime_roles(connection)
            _apply_current_runtime_object_grants(connection)
            connection.execute(
                text(
                    f"GRANT {_quoted_runtime_role(connection, granted_role)} "
                    f"TO {_quoted_runtime_role(connection, member_role)}"
                )
            )
            _assert_live_role_bootstrap_rejected_without_writes(connection)
    finally:
        engine.dispose()


def _current_revision(engine: Engine) -> str:
    """读取本轮 live lock 测试前后的唯一 Alembic revision。"""
    with engine.connect() as connection:
        value = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    if type(value) is not str:
        raise AssertionError("alembic revision must be text")
    return value


def _live_target_identity(management_engine: Engine, target_name: str) -> object:
    """由独立 management session 读取并派生 lock probe 使用的目标 identity。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        derive_database_target_identity,
        signed_system_identifier_to_uint64,
    )

    with management_engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT control.system_identifier, database.datname "
                    "FROM pg_control_system() AS control "
                    "JOIN pg_database AS database ON database.datname = :target_database_name"
                ),
                {"target_database_name": target_name},
            )
            .mappings()
            .one()
        )
    system_identifier = signed_system_identifier_to_uint64(row["system_identifier"])
    observed_name = row["datname"]
    if type(observed_name) is not str or observed_name != target_name:
        raise AssertionError("management catalog target name mismatch")
    return derive_database_target_identity(
        system_identifier_ascii=str(system_identifier).encode("ascii"),
        database_name_utf8=observed_name.encode("utf-8"),
        target_name_verified=True,
    )


def _probe_session_lock(
    engine: Engine,
    *,
    lock_parameters: dict[str, int],
    try_sql: str,
    unlock_sql: str,
) -> bool:
    """在独立 session 尝试并立即释放 advisory lock，只返回是否成功取得。"""
    with engine.connect() as connection:
        acquired = connection.execute(text(try_sql), lock_parameters).scalar_one()
        if type(acquired) is not bool:
            raise AssertionError("advisory lock probe must return bool")
        if acquired:
            released = connection.execute(text(unlock_sql), lock_parameters).scalar_one()
            if released is not True:
                raise AssertionError("advisory lock probe failed to release its lock")
        connection.commit()
    return acquired


@contextmanager
def _hold_real_advisory_lock(
    engine: Engine,
    *,
    lock_parameters: dict[str, int],
    try_sql: str,
    unlock_sql: str,
) -> Iterator[None]:
    """用独立真实 PostgreSQL session 持有一把 advisory lock。

    Args:
        engine: management 或 target 同步 Engine；必须创建独立物理 session。
        lock_parameters: 仅含已由 production identity/固定 schema 常量派生的整数键。
        try_sql: 对应单 bigint 或双 int32 namespace 的非阻塞加锁 SQL。
        unlock_sql: 与加锁 namespace 精确匹配的释放 SQL。

    Raises:
        AssertionError: 锁无法取得、返回类型异常或 finally 无法精确释放。

    独立 holder 只提交 advisory-lock SELECT 产生的读事务；session-level lock 持续到
    ``finally`` 显式释放。测试不依赖进程退出或连接池回收清理锁。
    """
    with engine.connect() as connection:
        acquired = connection.execute(text(try_sql), lock_parameters).scalar_one()
        if acquired is not True:
            raise AssertionError("real PostgreSQL advisory lock holder could not acquire lock")
        connection.commit()
        try:
            yield
        finally:
            released = connection.execute(text(unlock_sql), lock_parameters).scalar_one()
            if released is not True:
                raise AssertionError("real PostgreSQL advisory lock holder could not release lock")
            connection.commit()


@pytest.mark.parametrize(
    ("entry_name", "blocked_stage"),
    tuple(
        pytest.param(entry_name, blocked_stage, id=f"{entry_name}-{blocked_stage}-held")
        for entry_name in ("migrate", "role-bootstrap", "reset")
        for blocked_stage in ("management", "target", "schema")
    ),
)
def test_public_entries_reject_real_postgresql_held_lock_without_writes(
    database_url: ValidatedDatabaseUrl,
    monkeypatch: pytest.MonkeyPatch,
    entry_name: str,
    blocked_stage: str,
) -> None:
    """三个 public entry 在真实三层锁竞争下都必须保持完整数据库状态零写。

    当前 Task 13 目标是 roles-absent Candidate。``role-bootstrap`` 先经过唯一
    public wrapper 执行 production Candidate 只读 admission，随后直接竞争真实
    schema lock，不创建角色或写入授权。``migrate`` 的 schema 参数则使用只读
    Candidate admission hybrid，只为到达同一 schema-lock acquisition。reset 仍执行
    production confirm 与 target→schema admission；所有 mutation method 另外替换为
    fail-fast canary，防止测试设置错误时误触 DROP/CREATE、角色创建、grant
    或 migration runner。

    前后快照覆盖 revision、database ACL、fixed roles/membership、对象授权、restore facts、
    schema、全部业务表（含 ``audit_events``）行数与 content digest。holder 释放后再从新连接
    复核快照及两 fixed roles 均为 absent，证明没有依赖连接 rollback 之外的补偿清理。
    """
    from ai_employee.infrastructure.db.database_maintenance import (
        SCHEMA_LIFECYCLE_LOCK,
        DatabaseMaintenanceContext,
        DatabaseMaintenanceInvariantError,
        DatabaseTargetIdentity,
        ProtectedResetPolicy,
        SqlAlchemyDatabaseMaintenanceContext,
        _SqlAlchemyManagementLifecycleLease,
        _SqlAlchemyTargetMaintenanceLease,
        migrate_database,
        reset_then_migrate,
        role_bootstrap_database,
    )

    management_url, target_url, target_name = _sync_lifecycle_urls(database_url)
    management_engine = create_engine(
        management_url,
        poolclass=NullPool,
        hide_parameters=True,
    )
    target_engine = create_engine(target_url, poolclass=NullPool, hide_parameters=True)
    mutation_events: list[str] = []
    role_bootstrap_calls: list[str] = []
    original_migration_admission = _SqlAlchemyTargetMaintenanceLease.assert_migration_allowed

    def observed_role_bootstrap(context: DatabaseMaintenanceContext) -> None:
        """记录 held-lock 矩阵是否经过唯一 standalone public role-bootstrap 入口。"""
        role_bootstrap_calls.append("role-bootstrap")
        role_bootstrap_database(context)

    def guarded_migration_admission(self: _SqlAlchemyTargetMaintenanceLease) -> None:
        """仅在 migrate/schema 参数用真实 Candidate readers 到达锁竞争点。"""
        if entry_name == "migrate" and blocked_stage == "schema":
            self.assert_restore_facts_absent()
            candidate = self.read_bootstrap_candidate()
            if candidate.roles_exist or candidate.source_profile not in {
                DatabaseAclProfile.FRESH_DEFAULT,
                DatabaseAclProfile.PRE_PROTOCOL_LEGACY,
            }:
                raise AssertionError("Task 13 target must remain a roles-absent Candidate")
            return
        original_migration_admission(self)

    def reject_mutation(*_args: object, **_kwargs: object) -> None:
        """把任何越过 held-lock 边界的 mutation method 变成可见测试失败。"""
        mutation_events.append("mutation-boundary")
        raise AssertionError("held-lock public entry reached a mutation boundary")

    monkeypatch.setattr(
        _SqlAlchemyTargetMaintenanceLease,
        "assert_migration_allowed",
        guarded_migration_admission,
    )
    monkeypatch.setattr(
        _SqlAlchemyTargetMaintenanceLease,
        "owner_transaction",
        reject_mutation,
    )
    monkeypatch.setattr(
        _SqlAlchemyTargetMaintenanceLease,
        "run_typed_migration",
        reject_mutation,
    )
    monkeypatch.setattr(
        _SqlAlchemyTargetMaintenanceLease,
        "run_typed_role_bootstrap",
        reject_mutation,
    )
    monkeypatch.setattr(
        _SqlAlchemyManagementLifecycleLease,
        "drop_and_create_target",
        reject_mutation,
    )
    monkeypatch.setattr(
        _SqlAlchemyManagementLifecycleLease,
        "bootstrap_new_target",
        reject_mutation,
    )
    monkeypatch.setattr(
        _SqlAlchemyManagementLifecycleLease,
        "run_typed_migration",
        reject_mutation,
    )

    try:
        identity = cast(
            DatabaseTargetIdentity,
            _live_target_identity(management_engine, target_name),
        )
        with target_engine.connect() as before_connection:
            before = _capture_sync_transactional_state(before_connection)
        assert before.runtime_roles == ()
        assert before.memberships == ()

        context = SqlAlchemyDatabaseMaintenanceContext(
            management_engine=management_engine,
            target_engine=target_engine,
            target_database_name=target_name,
            bootstrap_caller=(
                BootstrapCaller.DB_RESET_POST_CREATE
                if entry_name == "reset"
                else BootstrapCaller.ROLE_BOOTSTRAP
            ),
            app_password="synthetic-app-secret",
            retention_password="synthetic-retention-secret",
            migration_runner=reject_mutation,
            published_authority=PUBLISHED_AUTHORITY,
            reset_policy=(
                ProtectedResetPolicy(app_env="test", confirmed_database_name=target_name)
                if entry_name == "reset"
                else None
            ),
        )
        entrypoint = {
            "migrate": migrate_database,
            "role-bootstrap": observed_role_bootstrap,
            "reset": reset_then_migrate,
        }[entry_name]

        if blocked_stage == "management":
            holder_engine = management_engine
            lock_parameters = {"lock_key": identity.lifecycle_lock_key}
            try_sql = "SELECT pg_try_advisory_lock(:lock_key)"
            unlock_sql = "SELECT pg_advisory_unlock(:lock_key)"
        elif blocked_stage == "target":
            holder_engine = target_engine
            lock_parameters = {"lock_key": identity.target_lock_key}
            try_sql = "SELECT pg_try_advisory_lock(:lock_key)"
            unlock_sql = "SELECT pg_advisory_unlock(:lock_key)"
        else:
            holder_engine = target_engine
            lock_parameters = {
                "lock_class": SCHEMA_LIFECYCLE_LOCK[0],
                "lock_object": SCHEMA_LIFECYCLE_LOCK[1],
            }
            try_sql = "SELECT pg_try_advisory_lock(:lock_class, :lock_object)"
            unlock_sql = "SELECT pg_advisory_unlock(:lock_class, :lock_object)"

        with _hold_real_advisory_lock(
            holder_engine,
            lock_parameters=lock_parameters,
            try_sql=try_sql,
            unlock_sql=unlock_sql,
        ):
            assert not _probe_session_lock(
                holder_engine,
                lock_parameters=lock_parameters,
                try_sql=try_sql,
                unlock_sql=unlock_sql,
            )
            with pytest.raises(
                DatabaseMaintenanceInvariantError,
                match=r"^database maintenance invariant violation$",
            ):
                entrypoint(cast(DatabaseMaintenanceContext, context))
            with target_engine.connect() as held_after_connection:
                held_after = _capture_sync_transactional_state(held_after_connection)
            assert held_after == before
            assert held_after.runtime_roles == ()
            assert held_after.memberships == ()
            assert mutation_events == []

        with target_engine.connect() as released_after_connection:
            released_after = _capture_sync_transactional_state(released_after_connection)
        assert released_after == before
        assert released_after.runtime_roles == ()
        assert released_after.memberships == ()
        assert mutation_events == []
        assert role_bootstrap_calls == (
            ["role-bootstrap"] if entry_name == "role-bootstrap" else []
        )
    finally:
        target_engine.dispose()
        management_engine.dispose()


def test_sqlalchemy_context_holds_and_releases_management_target_schema_locks(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """真实 session 必须按 management→target→schema 持锁，并按逆序完整释放。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        SCHEMA_LIFECYCLE_LOCK,
        DatabaseTargetIdentity,
        SqlAlchemyDatabaseMaintenanceContext,
    )

    management_url, target_url, target_name = _sync_lifecycle_urls(database_url)
    management_engine = create_engine(
        management_url,
        poolclass=NullPool,
        hide_parameters=True,
    )
    target_engine = create_engine(target_url, poolclass=NullPool, hide_parameters=True)
    try:
        identity = cast(
            DatabaseTargetIdentity, _live_target_identity(management_engine, target_name)
        )
        before_revision = _current_revision(target_engine)
        context = SqlAlchemyDatabaseMaintenanceContext(
            management_engine=management_engine,
            target_engine=target_engine,
            target_database_name=target_name,
            bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
            app_password="synthetic-app-secret",
            retention_password="synthetic-retention-secret",
            migration_runner=cast(Callable[[Connection], None], lambda connection: None),
            published_authority=PUBLISHED_AUTHORITY,
        )
        lifecycle_parameters = {"lock_key": identity.lifecycle_lock_key}
        target_parameters = {"lock_key": identity.target_lock_key}
        schema_parameters = {
            "lock_class": SCHEMA_LIFECYCLE_LOCK[0],
            "lock_object": SCHEMA_LIFECYCLE_LOCK[1],
        }

        with context.acquire_management_lifecycle_lock() as management:
            assert not _probe_session_lock(
                management_engine,
                lock_parameters=lifecycle_parameters,
                try_sql="SELECT pg_try_advisory_lock(:lock_key)",
                unlock_sql="SELECT pg_advisory_unlock(:lock_key)",
            )
            with management.acquire_target_lock() as target:
                assert not _probe_session_lock(
                    target_engine,
                    lock_parameters=target_parameters,
                    try_sql="SELECT pg_try_advisory_lock(:lock_key)",
                    unlock_sql="SELECT pg_advisory_unlock(:lock_key)",
                )
                with target.acquire_schema_lifecycle_lock():
                    assert not _probe_session_lock(
                        target_engine,
                        lock_parameters=schema_parameters,
                        try_sql=("SELECT pg_try_advisory_lock(:lock_class, :lock_object)"),
                        unlock_sql="SELECT pg_advisory_unlock(:lock_class, :lock_object)",
                    )
                assert _probe_session_lock(
                    target_engine,
                    lock_parameters=schema_parameters,
                    try_sql="SELECT pg_try_advisory_lock(:lock_class, :lock_object)",
                    unlock_sql="SELECT pg_advisory_unlock(:lock_class, :lock_object)",
                )
            assert _probe_session_lock(
                target_engine,
                lock_parameters=target_parameters,
                try_sql="SELECT pg_try_advisory_lock(:lock_key)",
                unlock_sql="SELECT pg_advisory_unlock(:lock_key)",
            )
        assert _probe_session_lock(
            management_engine,
            lock_parameters=lifecycle_parameters,
            try_sql="SELECT pg_try_advisory_lock(:lock_key)",
            unlock_sql="SELECT pg_advisory_unlock(:lock_key)",
        )
        assert _current_revision(target_engine) == before_revision == "20260809_0018"
    finally:
        target_engine.dispose()
        management_engine.dispose()


def test_runtime_role_dynamic_sql_failure_never_exposes_bound_password(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """真实 PL/pgSQL dynamic SQL 失败不得把 password literal 带入错误 CONTEXT。"""
    from uuid import uuid4

    from sqlalchemy.exc import SQLAlchemyError

    import ai_employee.infrastructure.db.database_access as database_access_module

    _, target_url, _ = _sync_lifecycle_urls(database_url)
    engine = create_engine(target_url, poolclass=NullPool, hide_parameters=True)
    marker = f"synthetic-runtime-{uuid4().hex}"
    leaked = False
    parameters_hidden = False
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                role_exists = connection.execute(
                    text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
                    {"role_name": APP_RUNTIME_ROLE_NAME},
                ).scalar_one()
                connection.execute(text(database_access_module._RUNTIME_ROLE_MUTATOR_SQL))
                try:
                    connection.execute(
                        text(database_access_module._APPLY_RUNTIME_ROLE_SQL),
                        {
                            "role_name": APP_RUNTIME_ROLE_NAME,
                            "role_password": marker,
                            # 已存在则 CREATE 触发 duplicate；缺失则 ALTER 触发 missing。
                            "create_role": role_exists is True,
                        },
                    )
                except SQLAlchemyError as error:
                    error_text = str(error)
                    error_repr = repr(error)
                    leaked = marker in error_text or marker in error_repr
                    parameters_hidden = "SQL parameters hidden" in error_text
                else:  # pragma: no cover - 两种 catalog 分支都必须确定失败。
                    raise AssertionError("synthetic runtime-role mutation did not fail")
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

    assert not leaked, "server error context exposed a bound runtime-role Secret"
    assert parameters_hidden, "SQLAlchemy did not hide runtime-role bind parameters"


def test_disposable_cleanup_lock_rejects_competing_management_session(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """test-only advisory lease 必须跨 session 互斥，并在 holder close 后可重新取得。"""
    from uuid import uuid4

    from ai_employee.infrastructure.db.database_url import validate_test_database_url
    from tests.integration.disposable_database import (
        DISPOSABLE_DATABASE_CLEANUP_LOCK,
        managed_disposable_database_cleanup,
    )

    validated = validate_test_database_url(database_url)
    management_url = validated.maintenance_url().set(drivername="postgresql+psycopg")
    holder_engine = create_engine(
        management_url,
        poolclass=NullPool,
        hide_parameters=True,
    )
    competitor_engine = create_engine(
        management_url,
        poolclass=NullPool,
        hide_parameters=True,
    )
    database_name = f"ai_employee_mig_{uuid4().hex}_test"
    parameters = {
        "lock_class": DISPOSABLE_DATABASE_CLEANUP_LOCK[0],
        "lock_object": DISPOSABLE_DATABASE_CLEANUP_LOCK[1],
    }
    try:
        with (
            managed_disposable_database_cleanup(
                holder_engine,
                database_name=database_name,
            ),
            competitor_engine.connect() as connection,
        ):
            assert (
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_class, :lock_object)"),
                    parameters,
                ).scalar_one()
                is False
            )

        with competitor_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_class, :lock_object)"),
                    parameters,
                ).scalar_one()
                is True
            )
            assert (
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_class, :lock_object)"),
                    parameters,
                ).scalar_one()
                is True
            )
    finally:
        competitor_engine.dispose()
        holder_engine.dispose()


def test_sqlalchemy_context_rejects_live_candidate_before_typed_migration(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """当前 0018 fresh/roles-absent Candidate 必须在 schema lock 与 runner 首写前拒绝。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        SqlAlchemyDatabaseMaintenanceContext,
        migrate_database,
    )

    management_url, target_url, target_name = _sync_lifecycle_urls(database_url)
    management_engine = create_engine(
        management_url,
        poolclass=NullPool,
        hide_parameters=True,
    )
    target_engine = create_engine(target_url, poolclass=NullPool, hide_parameters=True)
    migration_connections: list[Connection] = []
    try:
        before_revision = _current_revision(target_engine)
        context = SqlAlchemyDatabaseMaintenanceContext(
            management_engine=management_engine,
            target_engine=target_engine,
            target_database_name=target_name,
            bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
            app_password="synthetic-app-secret",
            retention_password="synthetic-retention-secret",
            migration_runner=migration_connections.append,
            published_authority=PUBLISHED_AUTHORITY,
        )

        with pytest.raises(
            DatabaseMaintenanceInvariantError,
            match=r"^database maintenance invariant violation$",
        ):
            migrate_database(context)

        assert migration_connections == []
        assert _current_revision(target_engine) == before_revision == "20260809_0018"
    finally:
        target_engine.dispose()
        management_engine.dispose()


def test_absent_target_reset_creates_bootstraps_and_migrates_uuid_disposable_database(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """在标准安全测试端点验证 absent reset，并完整清理本轮 cluster-wide 角色。

    ``database_url`` 已经通过标准 loopback、显式端口、``_test`` 后缀和无 query/fragment
    校验；测试只在同端点创建 UUID 随机数据库。PostgreSQL role 是 cluster-wide 对象，
    因此首写前必须证明两个 fixed runtime role 均 absent，任一预存角色都会零写失败，
    不能轮换未知密码或删除既有角色。``finally`` 先删除 exact UUID 数据库，再证明仅本轮
    创建的角色没有 session、membership、role setting、database ownership/ACL 或
    ``pg_shdepend``，最后精确删除并复核 database/role 都为零。依赖异常时拒绝盲删并
    明确失败，保留现场供外层 disposable 测试基础设施处置。
    """
    from functools import partial
    from uuid import uuid4

    from ai_employee.infrastructure.db.alembic import run_alembic_upgrade_on_connection
    from ai_employee.infrastructure.db.database_access import (
        read_database_access_snapshot_sync,
    )
    from ai_employee.infrastructure.db.database_grants import GrantPhase, verify_object_grants
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseRestoreFacts,
        ProtectedResetPolicy,
        SqlAlchemyDatabaseMaintenanceContext,
        read_database_restore_facts,
        reset_then_migrate,
    )
    from ai_employee.infrastructure.db.database_url import validate_test_database_url
    from tests.integration.disposable_database import (
        DisposableDatabaseCleanupError,
        bind_disposable_database_cleanup,
        managed_disposable_database_cleanup,
    )

    validated = validate_test_database_url(database_url)
    database_name = f"ai_employee_reset_{uuid4().hex}_test"
    management_url = validated.maintenance_url().set(drivername="postgresql+psycopg")
    target_url = validated.for_database(database_name).set(drivername="postgresql+psycopg")
    management_engine = create_engine(
        management_url,
        poolclass=NullPool,
        hide_parameters=True,
    )
    backend_root = Path(__file__).resolve().parents[3]

    try:
        with managed_disposable_database_cleanup(
            management_engine,
            database_name=database_name,
        ) as cleanup:
            try:
                cleanup.assert_initial_absence()
            except DisposableDatabaseCleanupError:
                pytest.fail(
                    "BLOCKED: dedicated lifecycle target or fixed runtime roles already exist"
                )

            target_engine = create_engine(
                target_url,
                poolclass=NullPool,
                hide_parameters=True,
            )
            try:
                reset_then_migrate(
                    bind_disposable_database_cleanup(
                        SqlAlchemyDatabaseMaintenanceContext(
                            management_engine=management_engine,
                            target_engine=target_engine,
                            target_database_name=database_name,
                            bootstrap_caller=BootstrapCaller.DB_RESET_POST_CREATE,
                            # 固定合成密码与 migration fixture 一致，避免在共享测试 cluster 中引入
                            # 第二套 runtime-role credential 事实。
                            app_password="app-role-integration-password",
                            retention_password="retention-role-integration-password",
                            migration_runner=partial(
                                run_alembic_upgrade_on_connection,
                                config_path=backend_root / "alembic.ini",
                            ),
                            published_authority=PUBLISHED_AUTHORITY,
                            reset_policy=ProtectedResetPolicy(
                                app_env="test",
                                confirmed_database_name=database_name,
                            ),
                        ),
                        cleanup,
                    )
                )

                with target_engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    target_database_oid = connection.execute(
                        text("SELECT oid FROM pg_database WHERE datname = current_database()")
                    ).scalar_one()
                    if type(target_database_oid) is not int:
                        raise AssertionError("disposable target database OID must be an integer")
                    access_snapshot = read_database_access_snapshot_sync(
                        connection,
                        target_database_oid=target_database_oid,
                    )
                    restore_facts = read_database_restore_facts(
                        connection,
                        target_database_oid=target_database_oid,
                    )
                    verify_object_grants(
                        connection,
                        revision=PUBLISHED_AUTHORITY.head_revision,
                        phase=GrantPhase.BASELINE,
                    )

                assert revision == PUBLISHED_AUTHORITY.head_revision
                assert access_snapshot.acl_profile is DatabaseAclProfile.BASELINE
                assert access_snapshot.app_role is not None
                assert access_snapshot.retention_role is not None
                assert restore_facts == DatabaseRestoreFacts(None, None, None)
            finally:
                # cleanup 的首个 DROP 前必须关闭所有 target session/Engine。
                target_engine.dispose()
    finally:
        management_engine.dispose()


def test_disposable_reset_migration_failure_cleans_created_database_and_roles(
    database_url: ValidatedDatabaseUrl,
) -> None:
    """bootstrap 后 migration runner 失败仍按阶段 OID provenance 精确清理全部对象。"""
    from uuid import uuid4

    from ai_employee.infrastructure.db.alembic import OnlineMigrationAuthority
    from ai_employee.infrastructure.db.database_maintenance import (
        ProtectedResetPolicy,
        SqlAlchemyDatabaseMaintenanceContext,
        reset_then_migrate,
    )
    from ai_employee.infrastructure.db.database_url import validate_test_database_url
    from tests.integration.disposable_database import (
        DISPOSABLE_DATABASE_CLEANUP_LOCK,
        DisposableDatabaseCleanupError,
        bind_disposable_database_cleanup,
        managed_disposable_database_cleanup,
    )

    validated = validate_test_database_url(database_url)
    database_name = f"ai_employee_reset_{uuid4().hex}_test"
    management_url = validated.maintenance_url().set(drivername="postgresql+psycopg")
    target_url = validated.for_database(database_name).set(drivername="postgresql+psycopg")
    management_engine = create_engine(
        management_url,
        poolclass=NullPool,
        hide_parameters=True,
    )
    target_engine = create_engine(target_url, poolclass=NullPool, hide_parameters=True)

    def fail_after_bootstrap(authority: OnlineMigrationAuthority) -> None:
        """证明 runner 已取得真实 target connection 后制造可辨识的 migration 异常。"""
        assert (
            authority.connection.execute(text("SELECT current_database()")).scalar_one()
            == database_name
        )
        raise RuntimeError("synthetic migration failure")

    try:
        with (
            pytest.raises(RuntimeError, match=r"^synthetic migration failure$"),
            managed_disposable_database_cleanup(
                management_engine,
                database_name=database_name,
            ) as cleanup,
        ):
            try:
                cleanup.assert_initial_absence()
            except DisposableDatabaseCleanupError:
                pytest.fail(
                    "BLOCKED: dedicated lifecycle target or fixed runtime roles already exist"
                )
            try:
                reset_then_migrate(
                    bind_disposable_database_cleanup(
                        SqlAlchemyDatabaseMaintenanceContext(
                            management_engine=management_engine,
                            target_engine=target_engine,
                            target_database_name=database_name,
                            bootstrap_caller=BootstrapCaller.DB_RESET_POST_CREATE,
                            app_password="app-role-integration-password",
                            retention_password="retention-role-integration-password",
                            migration_runner=fail_after_bootstrap,
                            published_authority=PUBLISHED_AUTHORITY,
                            reset_policy=ProtectedResetPolicy(
                                app_env="test",
                                confirmed_database_name=database_name,
                            ),
                        ),
                        cleanup,
                    )
                )
            finally:
                # 让 migration runner 的 target session/Engine 在 cleanup 首个 DROP 前关闭。
                target_engine.dispose()

        with management_engine.connect() as connection:
            database_residual = connection.execute(
                text("SELECT count(*) FROM pg_database WHERE datname = :database_name"),
                {"database_name": database_name},
            ).scalar_one()
            role_residual = connection.execute(
                text("SELECT count(*) FROM pg_roles WHERE rolname = ANY(:role_names)"),
                {"role_names": [APP_RUNTIME_ROLE_NAME, RETENTION_RUNTIME_ROLE_NAME]},
            ).scalar_one()
            cleanup_lock_residual = connection.execute(
                text(
                    """SELECT count(*)
                    FROM pg_locks AS held_lock
                    WHERE held_lock.locktype = 'advisory'
                      AND held_lock.classid = :lock_class
                      AND held_lock.objid = :lock_object
                      AND held_lock.objsubid = 2
                      AND held_lock.granted"""
                ),
                {
                    "lock_class": DISPOSABLE_DATABASE_CLEANUP_LOCK[0],
                    "lock_object": DISPOSABLE_DATABASE_CLEANUP_LOCK[1],
                },
            ).scalar_one()
        assert database_residual == 0
        assert role_residual == 0
        assert cleanup_lock_residual == 0
    finally:
        target_engine.dispose()
        management_engine.dispose()
