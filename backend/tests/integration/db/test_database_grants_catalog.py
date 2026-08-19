"""以 PostgreSQL 17 只读 catalog 冻结四类对象 ACL 的原始 tuple 语义。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from ai_employee.infrastructure.db.alembic import (
    load_published_alembic_authority,
    run_alembic_upgrade_on_connection,
    set_alembic_database_url,
)
from ai_employee.infrastructure.db.database_access import (
    RETENTION_RUNTIME_ROLE_NAME,
    BootstrapCaller,
)
from ai_employee.infrastructure.db.database_grants import (
    COLUMN_GRANTS_SQL,
    SCHEMA_GRANTS_SQL,
    SEQUENCE_GRANTS_SQL,
    TABLE_GRANTS_SQL,
    CatalogObjectSnapshot,
    GrantPhase,
    ObjectGrantInvariantError,
    ObjectGrantTuple,
    ObjectKind,
    _expected_object_grants,
    _migration_grant_delta,
    _verify_exact_grant_multiset,
    read_object_grants,
    verify_object_grants,
)
from ai_employee.infrastructure.db.database_maintenance import (
    SqlAlchemyDatabaseMaintenanceContext,
    role_bootstrap_database,
)
from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl as ValidatedDatabaseUrl,
)
from ai_employee.infrastructure.db.database_url import validate_test_database_url
from tests.integration.alembic_commands import run_alembic_upgrade
from tests.integration.disposable_database import (
    DisposableDatabaseCleanupError,
    managed_disposable_database_cleanup,
)

HEAD_REVISION = "20260809_0018"
DESTINATION_REVISION = "20260809_0019"
PUBLIC_SCHEMA_OWNER = "pg_database_owner"
BACKEND_ROOT = Path(__file__).resolve().parents[3]
_APP_ROLE_PASSWORD = "app-role-integration-password"
_RETENTION_ROLE_PASSWORD = "retention-role-integration-password"

INDEPENDENT_CATALOG_SQL = """SELECT
    'schema'::text AS object_kind,
    namespace.nspname AS schema_name,
    namespace.nspname AS object_name,
    NULL::text AS column_name,
    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END AS grantee,
    pg_get_userbyid(acl.grantor) AS grantor,
    acl.privilege_type,
    acl.is_grantable
FROM pg_namespace AS namespace
CROSS JOIN LATERAL aclexplode(
    COALESCE(namespace.nspacl, acldefault('n', namespace.nspowner))
) AS acl
WHERE namespace.nspname = 'public'
UNION ALL
SELECT
    'table'::text AS object_kind,
    namespace.nspname AS schema_name,
    relation.relname AS object_name,
    NULL::text AS column_name,
    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END AS grantee,
    pg_get_userbyid(acl.grantor) AS grantor,
    acl.privilege_type,
    acl.is_grantable
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
CROSS JOIN LATERAL aclexplode(
    COALESCE(relation.relacl, acldefault('r', relation.relowner))
) AS acl
WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'p')
UNION ALL
SELECT
    'column'::text AS object_kind,
    namespace.nspname AS schema_name,
    relation.relname AS object_name,
    attribute.attname AS column_name,
    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END AS grantee,
    pg_get_userbyid(acl.grantor) AS grantor,
    acl.privilege_type,
    acl.is_grantable
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_attribute AS attribute
  ON attribute.attrelid = relation.oid
 AND attribute.attnum > 0
 AND NOT attribute.attisdropped
CROSS JOIN LATERAL aclexplode(
    COALESCE(attribute.attacl, acldefault('c', relation.relowner))
) AS acl
WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'p')
UNION ALL
SELECT
    'sequence'::text AS object_kind,
    namespace.nspname AS schema_name,
    relation.relname AS object_name,
    NULL::text AS column_name,
    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END AS grantee,
    pg_get_userbyid(acl.grantor) AS grantor,
    acl.privilege_type,
    acl.is_grantable
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
CROSS JOIN LATERAL aclexplode(
    COALESCE(relation.relacl, acldefault('s', relation.relowner))
) AS acl
WHERE namespace.nspname = 'public' AND relation.relkind = 'S'
ORDER BY object_kind, schema_name, object_name, column_name NULLS FIRST,
         grantee, grantor, privilege_type, is_grantable"""

CATALOG_FINGERPRINT_SQL = """SELECT jsonb_build_object(
    'revision', (SELECT jsonb_agg(version_num ORDER BY version_num) FROM alembic_version),
    'schemas', (
        SELECT jsonb_agg(
            jsonb_build_array(nspname, nspowner, COALESCE(nspacl::text, 'NULL')) ORDER BY nspname
        )
        FROM pg_namespace
        WHERE nspname = 'public'
    ),
    'relations', (
        SELECT jsonb_agg(
            jsonb_build_array(relkind::text, relname, relowner, COALESCE(relacl::text, 'NULL'))
            ORDER BY relkind::text, relname
        )
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'p', 'S')
    ),
    'column_acls', (
        SELECT COALESCE(
            jsonb_agg(
                jsonb_build_array(relation.relname, attribute.attname, attribute.attacl::text)
                ORDER BY relation.relname, attribute.attname
            ),
            '[]'::jsonb
        )
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_attribute AS attribute ON attribute.attrelid = relation.oid
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
          AND attribute.attacl IS NOT NULL
    ),
    'runtime_roles', (
        SELECT COALESCE(
            jsonb_agg(
                jsonb_build_array(
                    rolname, oid, rolcanlogin, rolinherit, rolsuper, rolcreatedb,
                    rolcreaterole, rolreplication, rolbypassrls, rolconnlimit,
                    rolvaliduntil, rolconfig
                ) ORDER BY rolname
            ),
            '[]'::jsonb
        )
        FROM pg_roles
        WHERE rolname IN ('ai_employee_app', 'ai_employee_retention')
    ),
    'restore_settings', (
        SELECT COALESCE(
            jsonb_agg(jsonb_build_array(setdatabase, setrole, setconfig) ORDER BY setdatabase, setrole),
            '[]'::jsonb
        )
        FROM pg_db_role_setting
        WHERE setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND EXISTS (
              SELECT 1
              FROM unnest(setconfig) AS setting
              WHERE setting LIKE 'ai_employee.maintenance_gate=%'
                 OR setting LIKE 'ai_employee.restore_call_authority=%'
                 OR setting LIKE 'ai_employee.restore_completion=%'
          )
    )
)"""


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """覆盖全局 autouse migration fixture，保证本模块不执行任何 Alembic 写入。"""
    yield


@pytest.fixture(autouse=True)
async def isolated_database() -> AsyncIterator[None]:
    """覆盖全局 TRUNCATE fixture；catalog reader 测试必须保持业务行逐字不变。"""
    yield


@pytest.fixture(scope="module")
def catalog_database_url(
    database_url: ValidatedDatabaseUrl,
) -> Iterator[ValidatedDatabaseUrl]:
    """创建带精确 0018 runtime grants 的 UUID disposable catalog 数据库。

    共享 Task 13 URL 只作为已经过 loopback、显式端口和 ``_test`` 校验的 management
    anchor；本 fixture 不迁移或授权该数据库。随机目标先由同一 cleanup management
    session 证明 database/两个 fixed roles 全部不存在，再执行 typed role-bootstrap，
    冻结创建后的 role provenance，并通过历史 Alembic typed helper 精确升级到 0018。
    模块结束时先关闭 target Engine，随后 cleanup lease 逐项复核并删除随机数据库和
    两个 fixed roles，保证后续 roles-absent lifecycle 测试看到未污染的 cluster posture。

    Args:
        database_url: 全局 fixture 提供且默认隐藏表示的本地测试 anchor URL。

    Yields:
        指向随机 0018 database 的 asyncpg URL；其 ``repr`` 不泄露连接事实。
    """
    validated = validate_test_database_url(database_url)
    database_name = f"ai_employee_mig_{uuid4().hex}_test"
    owner_async_url = validated.for_database(database_name)
    management_engine = create_engine(
        validated.maintenance_url().set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    published_authority = load_published_alembic_authority(
        Config(BACKEND_ROOT / "alembic.ini")
    )
    try:
        with managed_disposable_database_cleanup(
            management_engine,
            database_name=database_name,
        ) as cleanup:
            try:
                cleanup.assert_initial_absence()
            except DisposableDatabaseCleanupError:
                pytest.fail("BLOCKED: catalog target or fixed runtime roles already exist")
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
                        config_path=BACKEND_ROOT / "alembic.ini",
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
                        "catalog disposable bootstrap did not create safe runtime roles"
                    )

                config = Config(BACKEND_ROOT / "alembic.ini")
                set_alembic_database_url(
                    config,
                    owner_async_url.set(drivername="postgresql+psycopg").render_as_string(
                        hide_password=False
                    ),
                )
                run_alembic_upgrade(config, HEAD_REVISION)
                with target_engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    verify_object_grants(
                        connection,
                        revision=HEAD_REVISION,
                        phase=GrantPhase.BASELINE,
                    )
                if revision != HEAD_REVISION:
                    raise AssertionError("disposable catalog database did not reach revision 0018")

                yield ValidatedDatabaseUrl(
                    owner_async_url.render_as_string(hide_password=False)
                )
            finally:
                # cleanup 首个 DROP 前必须没有仍可持有 target session 的 Engine/pool。
                target_engine.dispose()
    finally:
        management_engine.dispose()


def _tuple_from_mapping(row: object) -> ObjectGrantTuple:
    """把独立 SQLAlchemy row mapping 转为稳定对象 ACL tuple。"""
    mapping = cast(dict[str, object], row)
    return ObjectGrantTuple(
        object_kind=ObjectKind(str(mapping["object_kind"])),
        schema_name=str(mapping["schema_name"]),
        object_name=str(mapping["object_name"]),
        column_name=None if mapping["column_name"] is None else str(mapping["column_name"]),
        grantee=str(mapping["grantee"]),
        grantor=str(mapping["grantor"]),
        privilege_type=str(mapping["privilege_type"]),
        is_grantable=bool(mapping["is_grantable"]),
    )


async def _read_snapshot(connection: AsyncConnection) -> CatalogObjectSnapshot:
    """通过 SQLAlchemy 官方 async→sync bridge 调用迁移期 canonical reader。"""
    return await connection.run_sync(
        lambda sync_connection: read_object_grants(
            sync_connection,
            revision=HEAD_REVISION,
            phase=GrantPhase.BASELINE,
        )
    )


async def _business_row_counts(connection: AsyncConnection) -> tuple[tuple[str, int], ...]:
    """只读取所有受管普通表的行数，证明 reader 前后没有业务 DML。"""
    names = tuple(
        (
            await connection.execute(
                text(
                    "SELECT relation.relname "
                    "FROM pg_class AS relation "
                    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'public' "
                    "AND relation.relkind IN ('r', 'p') "
                    "AND relation.relname <> 'alembic_version' "
                    "ORDER BY relation.relname"
                )
            )
        ).scalars()
    )
    result: list[tuple[str, int]] = []
    preparer = connection.engine.dialect.identifier_preparer
    for name in names:
        # 表名来自同一只读 pg_class 查询，并再次使用方言 quoting，绝不接收外部字符串。
        quoted_name = preparer.quote(str(name))
        count = await connection.scalar(text(f'SELECT count(*) FROM "public".{quoted_name}'))
        assert type(count) is int
        result.append((str(name), count))
    return tuple(result)


@pytest.mark.asyncio
async def test_reader_matches_independent_complete_sorted_catalog_multiset(
    catalog_database_url: ValidatedDatabaseUrl,
) -> None:
    """四个 reader 的并集必须与独立 raw catalog SQL 的完整排序 multiset 相同。"""
    engine = create_async_engine(catalog_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            snapshot = await _read_snapshot(connection)
            independent_rows = (
                (await connection.execute(text(INDEPENDENT_CATALOG_SQL))).mappings().all()
            )
            independent = tuple(sorted(_tuple_from_mapping(row) for row in independent_rows))
            await transaction.rollback()
    finally:
        await engine.dispose()

    assert snapshot.grants == independent
    assert snapshot.grants == tuple(sorted(snapshot.grants))
    assert len(set(snapshot.grants)) == len(snapshot.grants)


@pytest.mark.asyncio
async def test_postgresql17_owner_grantor_and_column_default_semantics(
    catalog_database_url: ValidatedDatabaseUrl,
) -> None:
    """public schema 与 relation owner 必须保留 PG17 原始 grantor/non-grantable 语义。"""
    engine = create_async_engine(catalog_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            server_version_num = await connection.scalar(
                text("SELECT current_setting('server_version_num')")
            )
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            owner = await connection.scalar(
                text(
                    "SELECT pg_get_userbyid(datdba) FROM pg_database "
                    "WHERE datname = current_database()"
                )
            )
            snapshot = await _read_snapshot(connection)
            await transaction.rollback()
    finally:
        await engine.dispose()

    assert type(server_version_num) is str
    assert 170000 <= int(server_version_num) < 180000
    assert revision == HEAD_REVISION
    assert type(owner) is str
    schema_owner = tuple(
        grant
        for grant in snapshot.grants
        if grant.object_kind is ObjectKind.SCHEMA and grant.grantee == PUBLIC_SCHEMA_OWNER
    )
    assert {(grant.privilege_type, grant.grantor) for grant in schema_owner} == {
        ("CREATE", PUBLIC_SCHEMA_OWNER),
        ("USAGE", PUBLIC_SCHEMA_OWNER),
    }
    assert (
        ObjectGrantTuple(
            ObjectKind.SCHEMA,
            "public",
            "public",
            None,
            "PUBLIC",
            PUBLIC_SCHEMA_OWNER,
            "USAGE",
            False,
        )
        in snapshot.grants
    )

    owner_table_grants = tuple(
        grant
        for grant in snapshot.grants
        if grant.object_kind is ObjectKind.TABLE and grant.grantee == owner
    )
    assert len(owner_table_grants) == 32 * 8
    assert {grant.privilege_type for grant in owner_table_grants} == {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
        "MAINTAIN",
    }
    assert all(
        grant.grantor == owner and grant.is_grantable is False for grant in owner_table_grants
    )

    owner_sequence_grants = tuple(
        grant
        for grant in snapshot.grants
        if grant.object_kind is ObjectKind.SEQUENCE and grant.grantee == owner
    )
    assert len(owner_sequence_grants) == 2 * 3
    assert {grant.privilege_type for grant in owner_sequence_grants} == {
        "SELECT",
        "UPDATE",
        "USAGE",
    }
    assert all(
        grant.grantor == owner and grant.is_grantable is False for grant in owner_sequence_grants
    )
    # ``acldefault('c', owner)`` 为空；表级 owner SELECT 等绝不能伪造成 owner 逐列 tuple。
    owner_column_grants = tuple(
        grant
        for grant in snapshot.grants
        if grant.object_kind is ObjectKind.COLUMN and grant.grantee == owner
    )
    assert owner_column_grants == ()

    # 0018 的 retention policy 刻意只授予 14 个 UPDATE column tuple。这里使用独立字面
    # 期望冻结 live PG17 的 raw ACL 事实，禁止调用 production 私有 registry 自证正确。
    retention_column_grants = tuple(
        grant
        for grant in snapshot.grants
        if grant.object_kind is ObjectKind.COLUMN and grant.grantee == RETENTION_RUNTIME_ROLE_NAME
    )
    assert len(retention_column_grants) == 14
    retention_column_tuples = {
        (
            grant.object_name,
            grant.column_name,
            grant.privilege_type,
            grant.grantor,
            grant.is_grantable,
        )
        for grant in retention_column_grants
    }
    assert retention_column_tuples == {
        ("email_messages", "body_ciphertext", "UPDATE", owner, False),
        ("email_messages", "body_key_version", "UPDATE", owner, False),
        ("email_messages", "body_nonce", "UPDATE", owner, False),
        ("sync_cursors", "cursor", "UPDATE", owner, False),
        ("sync_cursors", "last_attempt_at", "UPDATE", owner, False),
        ("sync_cursors", "last_error_code", "UPDATE", owner, False),
        ("sync_cursors", "last_success_at", "UPDATE", owner, False),
        ("users", "display_name", "UPDATE", owner, False),
        ("users", "email", "UPDATE", owner, False),
        ("users", "email_body_retention_days", "UPDATE", owner, False),
        ("users", "is_active", "UPDATE", owner, False),
        ("users", "password_hash", "UPDATE", owner, False),
        ("users", "source_metadata_retention_days", "UPDATE", owner, False),
        ("users", "workspace_history_retention_days", "UPDATE", owner, False),
    }


def test_four_catalog_queries_freeze_raw_sources_and_forbid_effective_permission_apis() -> None:
    """SQL 必须逐类使用 aclexplode/default ACL，不得展开角色继承或 information_schema。"""
    assert "COALESCE(namespace.nspacl, acldefault('n', namespace.nspowner))" in SCHEMA_GRANTS_SQL
    assert "COALESCE(relation.relacl, acldefault('r', relation.relowner))" in TABLE_GRANTS_SQL
    assert "COALESCE(relation.relacl, acldefault('s', relation.relowner))" in SEQUENCE_GRANTS_SQL
    assert "COALESCE(attribute.attacl, acldefault('c', relation.relowner))" in COLUMN_GRANTS_SQL
    assert "aclexplode" in SCHEMA_GRANTS_SQL
    assert "aclexplode" in TABLE_GRANTS_SQL
    assert "aclexplode" in SEQUENCE_GRANTS_SQL
    assert "aclexplode" in COLUMN_GRANTS_SQL

    sql = (
        f"{SCHEMA_GRANTS_SQL}\n{TABLE_GRANTS_SQL}\n{COLUMN_GRANTS_SQL}\n{SEQUENCE_GRANTS_SQL}"
    ).lower()
    for forbidden in (
        "information_schema",
        "has_schema_privilege",
        "has_table_privilege",
        "has_sequence_privilege",
        "pg_auth_members",
        "all tables",
        "all sequences",
        "security definer",
    ):
        assert forbidden not in sql


@pytest.mark.asyncio
async def test_complete_multiset_comparator_rejects_catalog_drift_variants(
    catalog_database_url: ValidatedDatabaseUrl,
) -> None:
    """真实 PG17 snapshot 的 duplicate/extra/missing/grantor/unknown/option 变体全部 fail closed。"""
    engine = create_async_engine(catalog_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            snapshot = await _read_snapshot(connection)
            await transaction.rollback()
    finally:
        await engine.dispose()

    expected = snapshot.grants
    variants = (
        expected[:-1],
        (*expected, expected[-1]),
        (*expected, replace(expected[-1], object_name="unexpected_object")),
        (replace(expected[0], grantor="unexpected_grantor"), *expected[1:]),
        (replace(expected[0], privilege_type="UNKNOWN"), *expected[1:]),
        (replace(expected[0], is_grantable=True), *expected[1:]),
    )
    for variant in variants:
        with pytest.raises(
            ObjectGrantInvariantError,
            match=r"^object grant invariant violation$",
        ):
            _verify_exact_grant_multiset(tuple(variant), expected)


def test_destination_0019_inventory_freezes_retention_sequence_narrowing() -> None:
    """最终 registry 只给 retention 审计 sequence USAGE，且 0018 delta 明确撤销另一条。"""
    inventory = _expected_object_grants(
        revision=DESTINATION_REVISION,
        phase=GrantPhase.BASELINE,
        relation_owner="synthetic_owner",
    )
    sequence_grants = {
        (grant.object_name, grant.privilege_type)
        for grant in inventory
        if grant.object_kind is ObjectKind.SEQUENCE and grant.grantee == RETENTION_RUNTIME_ROLE_NAME
    }
    assert sequence_grants == {("audit_events_id_seq", "USAGE")}

    delta = _migration_grant_delta(
        source_revision=HEAD_REVISION,
        destination_revision=DESTINATION_REVISION,
        phase=GrantPhase.BASELINE,
        relation_owner="synthetic_owner",
    )
    assert {
        (grant.object_name, grant.privilege_type)
        for grant in delta.grants_to_revoke
        if grant.object_kind is ObjectKind.SEQUENCE
    } == {("checkpoint_migrations_v_seq", "USAGE")}


def test_first_install_inventory_adds_only_exact_alembic_owner_acl() -> None:
    """base 没有 version table；首个 destination 把其完整 owner ACL 纳入 inventory。"""
    base = _expected_object_grants(
        revision="base",
        phase=GrantPhase.BASELINE,
        relation_owner="synthetic_owner",
    )
    first = _expected_object_grants(
        revision="20260730_0001",
        phase=GrantPhase.BASELINE,
        relation_owner="synthetic_owner",
    )
    assert not any(grant.object_name == "alembic_version" for grant in base)
    alembic_grants = tuple(grant for grant in first if grant.object_name == "alembic_version")
    assert {grant.grantee for grant in alembic_grants} == {"synthetic_owner"}
    assert {grant.privilege_type for grant in alembic_grants} == {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
        "MAINTAIN",
    }


@pytest.mark.asyncio
async def test_reader_leaves_schema_roles_acl_restore_facts_and_business_rows_unchanged(
    catalog_database_url: ValidatedDatabaseUrl,
) -> None:
    """disposable 0018 集成覆盖只允许 SELECT，并以 catalog/row-count 前后值证明零写。"""
    engine = create_async_engine(catalog_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            before_catalog = await connection.scalar(text(CATALOG_FINGERPRINT_SQL))
            before_counts = await _business_row_counts(connection)
            before_xid = await connection.scalar(text("SELECT txid_current_if_assigned()"))

            await _read_snapshot(connection)

            after_xid = await connection.scalar(text("SELECT txid_current_if_assigned()"))
            after_counts = await _business_row_counts(connection)
            after_catalog = await connection.scalar(text(CATALOG_FINGERPRINT_SQL))
            await transaction.rollback()
    finally:
        await engine.dispose()

    assert before_xid is None
    assert after_xid is None
    assert after_catalog == before_catalog
    assert after_counts == before_counts
