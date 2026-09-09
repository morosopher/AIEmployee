"""读取、验证并按冻结边界应用 PostgreSQL 对象级直接授权。

本模块独立于 database ACL、runtime role bootstrap 与 restore authority。它只读取
``public`` schema 下 schema/table/column/sequence 的原始 ACL tuple，并以一个显式的
``(revision, phase)`` registry 构造完整 expected inventory。角色尚未创建时，bootstrap
只允许从 lifecycle 已验证的当前 revision exact pre-runtime inventory 补齐该 revision 的
全部 runtime tuples；既有角色先验证同 revision 的完整 inventory，再逐 tuple 幂等重申。
迁移 delta 只能处理当前相邻 revision 新增/移除的对象，或 registry 明确允许的 policy
变化；普通漂移在任何 GRANT/REVOKE 前失败，绝不形成通用权限修复入口。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NoReturn, Protocol, overload

from sqlalchemy import Connection, text

from ai_employee.infrastructure.db.database_access import (
    APP_RUNTIME_ROLE_NAME,
    RETENTION_RUNTIME_ROLE_NAME,
)

PUBLIC_SCHEMA_NAME = "public"
PUBLIC_GRANTEE_NAME = "PUBLIC"
PUBLIC_SCHEMA_OWNER_NAME = "pg_database_owner"

SCHEMA_GRANTS_SQL = """SELECT
    'schema'::text AS object_kind,
    namespace.nspname AS schema_name,
    namespace.nspname AS object_name,
    NULL::text AS column_name,
    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END AS grantee,
    pg_get_userbyid(acl.grantor) AS grantor,
    acl.privilege_type,
    acl.is_grantable,
    pg_get_userbyid(namespace.nspowner) AS object_owner,
    pg_get_userbyid(database.datdba) AS database_owner
FROM pg_namespace AS namespace
JOIN pg_database AS database ON database.datname = current_database()
CROSS JOIN LATERAL aclexplode(
    COALESCE(namespace.nspacl, acldefault('n', namespace.nspowner))
) AS acl
WHERE namespace.nspname = :schema_name"""

TABLE_GRANTS_SQL = """SELECT
    'table'::text AS object_kind,
    namespace.nspname AS schema_name,
    relation.relname AS object_name,
    NULL::text AS column_name,
    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END AS grantee,
    pg_get_userbyid(acl.grantor) AS grantor,
    acl.privilege_type,
    acl.is_grantable,
    pg_get_userbyid(relation.relowner) AS object_owner,
    pg_get_userbyid(database.datdba) AS database_owner
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_database AS database ON database.datname = current_database()
CROSS JOIN LATERAL aclexplode(
    COALESCE(relation.relacl, acldefault('r', relation.relowner))
) AS acl
WHERE namespace.nspname = :schema_name
  AND relation.relkind IN ('r', 'p')"""

COLUMN_GRANTS_SQL = """SELECT
    'column'::text AS object_kind,
    namespace.nspname AS schema_name,
    relation.relname AS object_name,
    attribute.attname AS column_name,
    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END AS grantee,
    pg_get_userbyid(acl.grantor) AS grantor,
    acl.privilege_type,
    acl.is_grantable,
    pg_get_userbyid(relation.relowner) AS object_owner,
    pg_get_userbyid(database.datdba) AS database_owner
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_database AS database ON database.datname = current_database()
JOIN pg_attribute AS attribute
  ON attribute.attrelid = relation.oid
 AND attribute.attnum > 0
 AND NOT attribute.attisdropped
CROSS JOIN LATERAL aclexplode(
    COALESCE(attribute.attacl, acldefault('c', relation.relowner))
) AS acl
WHERE namespace.nspname = :schema_name
  AND relation.relkind IN ('r', 'p')"""

SEQUENCE_GRANTS_SQL = """SELECT
    'sequence'::text AS object_kind,
    namespace.nspname AS schema_name,
    relation.relname AS object_name,
    NULL::text AS column_name,
    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END AS grantee,
    pg_get_userbyid(acl.grantor) AS grantor,
    acl.privilege_type,
    acl.is_grantable,
    pg_get_userbyid(relation.relowner) AS object_owner,
    pg_get_userbyid(database.datdba) AS database_owner
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_database AS database ON database.datname = current_database()
CROSS JOIN LATERAL aclexplode(
    COALESCE(relation.relacl, acldefault('s', relation.relowner))
) AS acl
WHERE namespace.nspname = :schema_name
  AND relation.relkind = 'S'"""

MIGRATION_RESUME_COLUMNS_SQL = """SELECT
    column_info.column_name,
    column_info.udt_name,
    column_info.is_nullable = 'YES' AS is_nullable,
    column_info.column_default,
    column_info.is_identity = 'YES' AS is_identity,
    column_info.identity_generation,
    column_info.is_generated <> 'NEVER' AS is_generated,
    NULLIF(column_info.generation_expression, '') AS generation_expression
FROM information_schema.columns AS column_info
WHERE column_info.table_schema = :schema_name
  AND column_info.table_name = :table_name
ORDER BY column_info.ordinal_position"""

MIGRATION_RESUME_INDEXES_SQL = """SELECT
    table_info.relname AS table_name,
    index_class.relname AS index_name,
    index_info.indisvalid AS is_valid,
    index_info.indisunique AS is_unique,
    index_info.indisprimary AS is_primary,
    index_info.indpred IS NULL AS is_unpartial,
    index_info.indexprs IS NULL AS has_no_expressions,
    index_info.indnkeyatts AS key_attribute_count,
    index_info.indnatts AS attribute_count,
    ARRAY(
        SELECT CASE WHEN index_key.attnum = 0 THEN NULL
                    ELSE attribute.attname::text END
        FROM unnest(index_info.indkey) WITH ORDINALITY
            AS index_key(attnum, position)
        LEFT JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = index_info.indrelid
         AND attribute.attnum = index_key.attnum
        ORDER BY index_key.position
    ) AS index_columns
FROM pg_catalog.pg_index AS index_info
JOIN pg_catalog.pg_class AS index_class
  ON index_class.oid = index_info.indexrelid
JOIN pg_catalog.pg_class AS table_info
  ON table_info.oid = index_info.indrelid
JOIN pg_catalog.pg_namespace AS namespace_info
  ON namespace_info.oid = table_info.relnamespace
WHERE namespace_info.nspname = :schema_name
  AND table_info.relname = :table_name
ORDER BY index_class.relname"""

MIGRATION_RESUME_CONSTRAINTS_SQL = """SELECT
    table_info.relname AS table_name,
    constraint_info.conname AS constraint_name,
    constraint_info.contype::text AS constraint_type,
    constraint_info.convalidated AS is_valid,
    constraint_info.condeferrable AS is_deferrable,
    constraint_info.condeferred AS is_deferred,
    CASE WHEN constraint_info.contype = 'f'
         THEN constraint_info.confdeltype::text ELSE NULL END AS delete_action,
    ARRAY(
        SELECT attribute.attname::text
        FROM unnest(constraint_info.conkey) WITH ORDINALITY
            AS local_key(attnum, position)
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = constraint_info.conrelid
         AND attribute.attnum = local_key.attnum
        ORDER BY local_key.position
    ) AS local_columns,
    CASE WHEN constraint_info.contype = 'f'
         THEN target_table.relname ELSE NULL END AS target_table,
    CASE WHEN constraint_info.contype = 'f' THEN ARRAY(
        SELECT target_attribute.attname::text
        FROM unnest(constraint_info.confkey) WITH ORDINALITY
            AS target_key(attnum, position)
        JOIN pg_catalog.pg_attribute AS target_attribute
          ON target_attribute.attrelid = constraint_info.confrelid
         AND target_attribute.attnum = target_key.attnum
        ORDER BY target_key.position
    ) ELSE NULL END AS target_columns,
    CASE WHEN constraint_info.contype = 'c'
         THEN pg_catalog.pg_get_expr(
             constraint_info.conbin,
             constraint_info.conrelid,
             true
         ) ELSE NULL END AS check_expression
FROM pg_catalog.pg_constraint AS constraint_info
JOIN pg_catalog.pg_class AS table_info
  ON table_info.oid = constraint_info.conrelid
JOIN pg_catalog.pg_namespace AS namespace_info
  ON namespace_info.oid = table_info.relnamespace
LEFT JOIN pg_catalog.pg_class AS target_table
  ON target_table.oid = constraint_info.confrelid
WHERE namespace_info.nspname = :schema_name
  AND table_info.relname = :table_name
ORDER BY constraint_info.conname"""

_EMAIL_MESSAGE_0016_RESUME_STATE_SQL = """SELECT
    EXISTS (
        SELECT 1
        FROM public.email_messages AS message
        WHERE message.provider_updated_at IS NOT NULL
    ) AS has_unknown_provider_updated_at,
    EXISTS (
        SELECT 1
        FROM public.email_messages AS message
        LEFT JOIN public.email_threads AS thread ON thread.id = message.thread_id
        LEFT JOIN public.users AS message_user ON message_user.id = message.user_id
        LEFT JOIN public.users AS thread_user ON thread_user.id = thread.user_id
        LEFT JOIN public.oauth_connections AS owning_connection
          ON owning_connection.id = thread.connection_id
        WHERE thread.id IS NULL
           OR message_user.id IS NULL
           OR thread_user.id IS NULL
           OR owning_connection.id IS NULL
           OR message.user_id <> thread.user_id
           OR thread.user_id <> owning_connection.user_id
           OR (
               message.connection_id IS NOT NULL
               AND message.connection_id <> thread.connection_id
           )
    ) AS has_invalid_connection_projection"""

_EMAIL_MESSAGE_0017_RESUME_STATE_SQL = """SELECT
    EXISTS (
        SELECT 1
        FROM public.email_messages AS message
        LEFT JOIN public.email_threads AS thread ON thread.id = message.thread_id
        LEFT JOIN public.users AS message_user ON message_user.id = message.user_id
        LEFT JOIN public.users AS thread_user ON thread_user.id = thread.user_id
        LEFT JOIN public.oauth_connections AS owning_connection
          ON owning_connection.id = thread.connection_id
        WHERE thread.id IS NULL
           OR message_user.id IS NULL
           OR thread_user.id IS NULL
           OR owning_connection.id IS NULL
           OR message.user_id <> thread.user_id
           OR thread.user_id <> owning_connection.user_id
           OR (
               message.connection_id IS NOT NULL
               AND message.connection_id <> thread.connection_id
           )
    ) AS has_invalid_connection_projection"""

_INVARIANT_ERROR_MESSAGE = "object grant invariant violation"
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_PRIVILEGES = {
    "schema": frozenset({"CREATE", "USAGE"}),
    "table": frozenset(
        {
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
            "MAINTAIN",
        }
    ),
    "column": frozenset({"SELECT", "INSERT", "UPDATE", "REFERENCES"}),
    "sequence": frozenset({"SELECT", "UPDATE", "USAGE"}),
}
_OWNER_TABLE_PRIVILEGES = (
    "DELETE",
    "INSERT",
    "MAINTAIN",
    "REFERENCES",
    "SELECT",
    "TRIGGER",
    "TRUNCATE",
    "UPDATE",
)
_OWNER_SEQUENCE_PRIVILEGES = ("SELECT", "UPDATE", "USAGE")
_APP_TABLE_PRIVILEGES = ("DELETE", "INSERT", "SELECT", "UPDATE")


class GrantPhase(StrEnum):
    """表示 object-grant inventory 所属的数据库生命周期 phase。"""

    BASELINE = "baseline"
    ACTIVE = "active"


class BootstrapGrantPosture(StrEnum):
    """表示 bootstrap 前 database-local runtime grants 的两种合法完整形状。"""

    PRE_RUNTIME = "pre_runtime"
    CURRENT = "current"


class ObjectKind(StrEnum):
    """表示 canonical reader 支持的四类 PostgreSQL 对象。"""

    SCHEMA = "schema"
    TABLE = "table"
    COLUMN = "column"
    SEQUENCE = "sequence"


@dataclass(frozen=True, order=True, slots=True)
class ObjectGrantTuple:
    """保存一条未经 effective-permission 推断的原始对象 ACL 事实。"""

    object_kind: ObjectKind
    schema_name: str
    object_name: str
    column_name: str | None
    grantee: str
    grantor: str
    privilege_type: str
    is_grantable: bool


@dataclass(frozen=True, slots=True)
class CatalogObjectSnapshot:
    """保存一个 revision/phase 的完整排序对象 ACL multiset。"""

    revision: str
    phase: GrantPhase
    grants: tuple[ObjectGrantTuple, ...]


@dataclass(frozen=True, slots=True)
class _ResumeColumnCatalog:
    """保存 resume admission 所需的单列物理 catalog 事实。

    该投影只描述列类型、可空、默认值、identity 与 generated 属性，不承载任何业务值，
    也不提供 mutation SQL。0016 使用它区分精确 0015 fresh source 与唯一允许的两列
    nullable crash candidate。
    """

    column_name: str
    udt_name: str
    is_nullable: bool
    column_default: str | None
    is_identity: bool
    identity_generation: str | None
    is_generated: bool
    generation_expression: str | None


@dataclass(frozen=True, slots=True)
class _ResumeIndexCatalog:
    """保存 0017 并发索引 admission 所需的完整 pg_catalog 形状。

    ``is_valid`` 是失败的 ``CREATE INDEX CONCURRENTLY`` 允许留下的唯一可变字段；
    其余字段共同证明对象确实是本 migration 声明的普通、唯一、无 predicate、无
    expression、无 INCLUDE 且列顺序精确的索引。descriptor 只读取该事实，不执行
    ``DROP``、``CREATE`` 或任何 repair。
    """

    table_name: str
    index_name: str
    is_valid: bool
    is_unique: bool
    is_primary: bool
    is_unpartial: bool
    has_no_expressions: bool
    key_attribute_count: int
    attribute_count: int
    index_columns: tuple[str | None, ...]


@dataclass(frozen=True, slots=True)
class _ResumeConstraintCatalog:
    """保存受影响邮件表约束的完整、可比较 catalog 事实。"""

    table_name: str
    constraint_name: str
    constraint_type: str
    is_valid: bool
    is_deferrable: bool
    is_deferred: bool
    delete_action: str | None
    local_columns: tuple[str, ...]
    target_table: str | None
    target_columns: tuple[str, ...] | None
    check_expression: str | None


@dataclass(frozen=True, slots=True)
class MigrationGrantDelta:
    """保存一个相邻 migration step 唯一允许的直接授权变化。"""

    source_revision: str
    destination_revision: str
    phase: GrantPhase
    grants_to_revoke: tuple[ObjectGrantTuple, ...]
    grants_to_apply: tuple[ObjectGrantTuple, ...]


@dataclass(frozen=True, slots=True)
class _InventoryPolicy:
    """保存已完全展开但尚未绑定 relation owner 的 revision policy。"""

    tables: tuple[str, ...]
    sequences: tuple[str, ...]
    retention_table_privileges: tuple[tuple[str, tuple[str, ...]], ...]
    retention_update_columns: tuple[tuple[str, tuple[str, ...]], ...]
    retention_sequences: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CatalogRead:
    """在公开 snapshot 之外保存本次 catalog 证明的 database owner。"""

    snapshot: CatalogObjectSnapshot
    relation_owner: str


class ObjectGrantInvariantError(RuntimeError):
    """表示对象 ACL、revision 或 delta 违反冻结不变量。

    异常消息固定且不包含对象名、角色、grantor、revision 或数据库内容，调用边界可安全
    记录稳定错误码而不泄漏 catalog posture。
    """


class _CatalogRow(Protocol):
    """描述 SQLAlchemy RowMapping 与单元测试 mapping 的最小共同接口。"""

    def __contains__(self, key: object) -> bool:
        """返回是否包含指定稳定列名。"""

    def __getitem__(self, key: str) -> object:
        """按稳定列名读取第三方 catalog 值。"""


def _fail() -> NoReturn:
    """抛出唯一稳定且无 catalog 内容的 invariant error。"""
    raise ObjectGrantInvariantError(_INVARIANT_ERROR_MESSAGE)


def _required(row: _CatalogRow, key: str) -> object:
    """读取必需 catalog 列；缺列说明 reader 输入无法可信解释。"""
    if key not in row:
        _fail()
    return row[key]


def _exact_text(value: object) -> str:
    """读取非空、无 NUL 的原始 catalog text，拒绝隐式字符串转换。"""
    if type(value) is not str or value == "" or "\x00" in value:
        _fail()
    return value


def _optional_text(value: object) -> str | None:
    """读取 column name；只有非 column tuple 允许 ``NULL``。"""
    if value is None:
        return None
    return _exact_text(value)


def _exact_bool(value: object) -> bool:
    """读取 PostgreSQL bool，拒绝整数或宽松 truthy/falsy 表示。"""
    if type(value) is not bool:
        _fail()
    return value


def _exact_int(value: object) -> int:
    """读取 catalog 计数，拒绝 bool 或驱动宽松转换。"""
    if type(value) is not int:
        _fail()
    return value


@overload
def _exact_text_tuple(
    value: object,
    *,
    allow_null: Literal[False],
) -> tuple[str, ...]: ...


@overload
def _exact_text_tuple(
    value: object,
    *,
    allow_null: Literal[True],
) -> tuple[str | None, ...]: ...


def _exact_text_tuple(value: object, *, allow_null: bool) -> tuple[str | None, ...]:
    """把 PostgreSQL text 数组收窄为不可变列名 tuple。"""
    if not isinstance(value, (list, tuple)):
        _fail()
    result: list[str | None] = []
    for item in value:
        result.append(_optional_text(item) if allow_null else _exact_text(item))
    return tuple(result)


def _validated_identifier(value: object) -> str:
    """收窄由本模块写入 SQL 的冻结 identifier。"""
    text_value = _exact_text(value)
    if _IDENTIFIER_PATTERN.fullmatch(text_value) is None:
        _fail()
    return text_value


def _validated_revision(value: object) -> str:
    """验证 revision 标签只作为 registry key，不允许空值或隐式转换。"""
    return _exact_text(value)


def _validate_phase(value: object) -> GrantPhase:
    """要求调用方传入枚举实例，拒绝同值裸字符串绕过闭集。"""
    if type(value) is not GrantPhase:
        _fail()
    return value


def _validate_grant_tuple(grant: object) -> ObjectGrantTuple:
    """逐字段验证一条 canonical tuple，并按 object kind 检查 privilege。"""
    if type(grant) is not ObjectGrantTuple:
        _fail()
    if type(grant.object_kind) is not ObjectKind:
        _fail()
    schema_name = _exact_text(grant.schema_name)
    object_name = _exact_text(grant.object_name)
    column_name = _optional_text(grant.column_name)
    grantee = _exact_text(grant.grantee)
    grantor = _exact_text(grant.grantor)
    privilege_type = _exact_text(grant.privilege_type)
    is_grantable = _exact_bool(grant.is_grantable)
    if schema_name != PUBLIC_SCHEMA_NAME:
        _fail()
    if grant.object_kind is ObjectKind.SCHEMA:
        if object_name != schema_name or column_name is not None:
            _fail()
    elif grant.object_kind is ObjectKind.COLUMN:
        if column_name is None:
            _fail()
    elif column_name is not None:
        _fail()
    if privilege_type not in _ALLOWED_PRIVILEGES[grant.object_kind.value]:
        _fail()
    if is_grantable:
        _fail()
    return ObjectGrantTuple(
        object_kind=grant.object_kind,
        schema_name=schema_name,
        object_name=object_name,
        column_name=column_name,
        grantee=grantee,
        grantor=grantor,
        privilege_type=privilege_type,
        is_grantable=False,
    )


def _verify_exact_grant_multiset(
    actual: tuple[ObjectGrantTuple, ...],
    expected: tuple[ObjectGrantTuple, ...],
) -> None:
    """比较两个完整排序 multiset，并把重复 tuple 视为独立漂移。

    Args:
        actual: canonical reader 或调用方提供的全部实际 tuple。
        expected: 单一 revision/phase registry 生成的全部期望 tuple。

    Raises:
        ObjectGrantInvariantError: 类型、privilege、grant option、重复、排序后内容或数量
            任一不一致。
    """
    if type(actual) is not tuple or type(expected) is not tuple:
        _fail()
    validated_actual = tuple(_validate_grant_tuple(grant) for grant in actual)
    validated_expected = tuple(_validate_grant_tuple(grant) for grant in expected)
    if len(set(validated_actual)) != len(validated_actual):
        _fail()
    if len(set(validated_expected)) != len(validated_expected):
        _fail()
    if tuple(sorted(validated_actual)) != tuple(sorted(validated_expected)):
        _fail()


def _grant(
    kind: ObjectKind,
    object_name: str,
    grantee: str,
    grantor: str,
    privilege: str,
    *,
    column_name: str | None = None,
) -> ObjectGrantTuple:
    """构造 registry 内固定为 public/non-grantable 的 tuple。"""
    return ObjectGrantTuple(
        object_kind=kind,
        schema_name=PUBLIC_SCHEMA_NAME,
        object_name=object_name,
        column_name=column_name,
        grantee=grantee,
        grantor=grantor,
        privilege_type=privilege,
        is_grantable=False,
    )


_IDENTITY_TABLES = frozenset({"alembic_version", "users", "user_sessions"})
_TASK_TABLES = frozenset(
    {
        "approval_requests",
        "audit_events",
        "outbox_events",
        "task_runs",
        "task_steps",
        "tool_executions",
    }
)
_CHECKPOINT_TABLES = frozenset(
    {"checkpoint_blobs", "checkpoint_migrations", "checkpoint_writes", "checkpoints"}
)
_SOURCE_TABLES = frozenset(
    {
        "calendar_events",
        "email_analyses",
        "email_messages",
        "email_threads",
        "encrypted_credentials",
        "oauth_attempts",
        "oauth_connections",
        "sync_cursors",
    }
)
_WORKSPACE_TABLES = frozenset(
    {"conversations", "daily_brief_items", "daily_briefs", "llm_invocations", "messages"}
)
_CONNECTION_TABLES = frozenset({"connection_capabilities", "provider_calendars"})
_TRUSTED_ACTION_TABLES = frozenset(
    {
        "calendar_change_proposals",
        "calendar_change_snapshots",
        "mail_draft_versions",
        "mail_drafts",
    }
)
_ALL_TABLES = (
    _IDENTITY_TABLES
    | _TASK_TABLES
    | _CHECKPOINT_TABLES
    | _SOURCE_TABLES
    | _WORKSPACE_TABLES
    | _CONNECTION_TABLES
    | _TRUSTED_ACTION_TABLES
)

_REVISION_ORDER = (
    "base",
    "20260730_0001",
    "20260730_0002",
    "20260803_0003",
    "20260803_0004",
    "20260803_0005",
    "20260803_0006",
    "20260804_0007_google_sources",
    "20260804_0007",
    "20260804_0008",
    "20260730_0004",
    "20260804_0009",
    "20260804_0010",
    "20260806_0011",
    "20260806_0012",
    "20260806_0013",
    "20260807_0014",
    "20260808_0015",
    "20260808_0016",
    "20260808_0017",
    "20260809_0018",
    "20260809_0019",
)

_EMAIL_MESSAGES_0016_OWNED_COLUMNS = (
    _ResumeColumnCatalog(
        column_name="connection_id",
        udt_name="uuid",
        is_nullable=True,
        column_default=None,
        is_identity=False,
        identity_generation=None,
        is_generated=False,
        generation_expression=None,
    ),
    _ResumeColumnCatalog(
        column_name="provider_updated_at",
        udt_name="timestamptz",
        is_nullable=True,
        column_default=None,
        is_identity=False,
        identity_generation=None,
        is_generated=False,
        generation_expression=None,
    ),
)
_EMAIL_MESSAGES_0016_OWNED_COLUMN_NAMES = frozenset(
    column.column_name for column in _EMAIL_MESSAGES_0016_OWNED_COLUMNS
)


def _frozen_resume_column(
    column_name: str,
    udt_name: str,
    *,
    nullable: bool,
    default: str | None = None,
) -> _ResumeColumnCatalog:
    """构造代码内冻结的 source column 形状，不读取运行时业务值。"""
    return _ResumeColumnCatalog(
        column_name=column_name,
        udt_name=udt_name,
        is_nullable=nullable,
        column_default=default,
        is_identity=False,
        identity_generation=None,
        is_generated=False,
        generation_expression=None,
    )


# 0015 的 fresh source 不是只按列名识别：ordinal、类型、NULL/default、identity 和
# generated 属性共同构成 migration admission 的物理闭集。使用 tuple 保留
# information_schema.ordinal_position 顺序，避免同名重排列被误认为可安全 resume。
_EMAIL_MESSAGES_0015_SOURCE_COLUMNS = (
    _frozen_resume_column("user_id", "uuid", nullable=False),
    _frozen_resume_column("thread_id", "uuid", nullable=False),
    _frozen_resume_column("provider_message_id", "varchar", nullable=False),
    _frozen_resume_column("received_at", "timestamptz", nullable=False),
    _frozen_resume_column("sender", "jsonb", nullable=False),
    _frozen_resume_column("recipients", "jsonb", nullable=False),
    _frozen_resume_column("subject", "text", nullable=False),
    _frozen_resume_column("snippet", "text", nullable=False),
    _frozen_resume_column("body_ciphertext", "bytea", nullable=True),
    _frozen_resume_column("body_nonce", "bytea", nullable=True),
    _frozen_resume_column("body_key_version", "int4", nullable=True),
    _frozen_resume_column("labels", "jsonb", nullable=False),
    _frozen_resume_column("headers", "jsonb", nullable=False),
    _frozen_resume_column("provider_url", "text", nullable=False),
    _frozen_resume_column("id", "uuid", nullable=False),
    _frozen_resume_column("created_at", "timestamptz", nullable=False, default="now()"),
    _frozen_resume_column("updated_at", "timestamptz", nullable=False, default="now()"),
    _frozen_resume_column("internet_message_id", "varchar", nullable=True),
    _frozen_resume_column("provider_conversation_id", "varchar", nullable=True),
    _frozen_resume_column("sent_at", "timestamptz", nullable=True),
    _frozen_resume_column("mailbox_scope_key", "varchar", nullable=False),
)
_EMAIL_MESSAGES_0015_COLUMN_NAMES = frozenset(
    column.column_name for column in _EMAIL_MESSAGES_0015_SOURCE_COLUMNS
)


_EMAIL_MESSAGES_0016_SOURCE_COLUMNS = frozenset(
    {
        _frozen_resume_column("user_id", "uuid", nullable=False),
        _frozen_resume_column("thread_id", "uuid", nullable=False),
        _frozen_resume_column("provider_message_id", "varchar", nullable=False),
        _frozen_resume_column("received_at", "timestamptz", nullable=False),
        _frozen_resume_column("sender", "jsonb", nullable=False),
        _frozen_resume_column("recipients", "jsonb", nullable=False),
        _frozen_resume_column("subject", "text", nullable=False),
        _frozen_resume_column("snippet", "text", nullable=False),
        _frozen_resume_column("body_ciphertext", "bytea", nullable=True),
        _frozen_resume_column("body_nonce", "bytea", nullable=True),
        _frozen_resume_column("body_key_version", "int4", nullable=True),
        _frozen_resume_column("labels", "jsonb", nullable=False),
        _frozen_resume_column("headers", "jsonb", nullable=False),
        _frozen_resume_column("provider_url", "text", nullable=False),
        _frozen_resume_column("id", "uuid", nullable=False),
        _frozen_resume_column("created_at", "timestamptz", nullable=False, default="now()"),
        _frozen_resume_column("updated_at", "timestamptz", nullable=False, default="now()"),
        _frozen_resume_column("internet_message_id", "varchar", nullable=True),
        _frozen_resume_column("provider_conversation_id", "varchar", nullable=True),
        _frozen_resume_column("sent_at", "timestamptz", nullable=True),
        _frozen_resume_column("mailbox_scope_key", "varchar", nullable=False),
        _EMAIL_MESSAGES_0016_OWNED_COLUMNS[0],
        _EMAIL_MESSAGES_0016_OWNED_COLUMNS[1],
    }
)

_EMAIL_THREADS_0016_SOURCE_COLUMNS = frozenset(
    {
        _frozen_resume_column("user_id", "uuid", nullable=False),
        _frozen_resume_column("connection_id", "uuid", nullable=False),
        _frozen_resume_column("provider_thread_id", "varchar", nullable=False),
        _frozen_resume_column("subject", "text", nullable=False),
        _frozen_resume_column("participants", "jsonb", nullable=False),
        _frozen_resume_column("latest_message_at", "timestamptz", nullable=False),
        _frozen_resume_column("provider_url", "text", nullable=False),
        _frozen_resume_column("id", "uuid", nullable=False),
        _frozen_resume_column("created_at", "timestamptz", nullable=False, default="now()"),
        _frozen_resume_column("updated_at", "timestamptz", nullable=False, default="now()"),
        _frozen_resume_column("provider_updated_at", "timestamptz", nullable=True),
    }
)


def _frozen_resume_index(
    table_name: str,
    index_name: str,
    columns: tuple[str, ...],
    *,
    is_primary: bool,
) -> _ResumeIndexCatalog:
    """构造 source index 的不可变精确形状。"""
    return _ResumeIndexCatalog(
        table_name=table_name,
        index_name=index_name,
        is_valid=True,
        is_unique=True,
        is_primary=is_primary,
        is_unpartial=True,
        has_no_expressions=True,
        key_attribute_count=len(columns),
        attribute_count=len(columns),
        index_columns=columns,
    )


_EMAIL_0016_SOURCE_INDEXES = frozenset(
    {
        _frozen_resume_index("email_messages", "email_messages_pkey", ("id",), is_primary=True),
        _frozen_resume_index(
            "email_messages",
            "uq_email_messages_thread_provider_message",
            ("thread_id", "provider_message_id"),
            is_primary=False,
        ),
        _frozen_resume_index("email_threads", "email_threads_pkey", ("id",), is_primary=True),
        _frozen_resume_index(
            "email_threads",
            "uq_email_threads_connection_provider_thread",
            ("connection_id", "provider_thread_id"),
            is_primary=False,
        ),
    }
)


def _frozen_resume_constraint(
    table_name: str,
    constraint_name: str,
    constraint_type: str,
    local_columns: tuple[str, ...],
    *,
    delete_action: str | None = None,
    target_table: str | None = None,
    target_columns: tuple[str, ...] | None = None,
) -> _ResumeConstraintCatalog:
    """构造 source constraint 的完整形状；0017 不允许新增任何 constraint。"""
    return _ResumeConstraintCatalog(
        table_name=table_name,
        constraint_name=constraint_name,
        constraint_type=constraint_type,
        is_valid=True,
        is_deferrable=False,
        is_deferred=False,
        delete_action=delete_action,
        local_columns=local_columns,
        target_table=target_table,
        target_columns=target_columns,
        check_expression=None,
    )


_EMAIL_0016_SOURCE_CONSTRAINTS = frozenset(
    {
        _frozen_resume_constraint("email_messages", "email_messages_pkey", "p", ("id",)),
        _frozen_resume_constraint(
            "email_messages",
            "email_messages_thread_id_fkey",
            "f",
            ("thread_id",),
            delete_action="c",
            target_table="email_threads",
            target_columns=("id",),
        ),
        _frozen_resume_constraint(
            "email_messages",
            "email_messages_user_id_fkey",
            "f",
            ("user_id",),
            delete_action="r",
            target_table="users",
            target_columns=("id",),
        ),
        _frozen_resume_constraint(
            "email_messages",
            "uq_email_messages_thread_provider_message",
            "u",
            ("thread_id", "provider_message_id"),
        ),
        _frozen_resume_constraint(
            "email_threads",
            "email_threads_connection_id_fkey",
            "f",
            ("connection_id",),
            delete_action="c",
            target_table="oauth_connections",
            target_columns=("id",),
        ),
        _frozen_resume_constraint("email_threads", "email_threads_pkey", "p", ("id",)),
        _frozen_resume_constraint(
            "email_threads",
            "email_threads_user_id_fkey",
            "f",
            ("user_id",),
            delete_action="r",
            target_table="users",
            target_columns=("id",),
        ),
        _frozen_resume_constraint(
            "email_threads",
            "uq_email_threads_connection_provider_thread",
            "u",
            ("connection_id", "provider_thread_id"),
        ),
    }
)

_EMAIL_0017_MESSAGE_INDEX_NAME = "uq_email_messages_connection_provider_message"
_EMAIL_0017_THREAD_INDEX_NAME = "uq_email_threads_id_connection_user"
_EMAIL_0017_MESSAGE_INDEX_COLUMNS = ("connection_id", "provider_message_id")
_EMAIL_0017_THREAD_INDEX_COLUMNS = ("id", "connection_id", "user_id")

_CALENDAR_EVENTS_0017_SOURCE_COLUMNS = frozenset(
    {
        _frozen_resume_column("user_id", "uuid", nullable=False),
        _frozen_resume_column("connection_id", "uuid", nullable=False),
        _frozen_resume_column("provider_event_id", "varchar", nullable=False),
        _frozen_resume_column("calendar_id", "varchar", nullable=False),
        _frozen_resume_column("title", "text", nullable=False),
        _frozen_resume_column("description_ciphertext", "bytea", nullable=True),
        _frozen_resume_column("description_nonce", "bytea", nullable=True),
        _frozen_resume_column("description_key_version", "int4", nullable=True),
        _frozen_resume_column("location_ciphertext", "bytea", nullable=True),
        _frozen_resume_column("location_nonce", "bytea", nullable=True),
        _frozen_resume_column("location_key_version", "int4", nullable=True),
        _frozen_resume_column("starts_at", "timestamptz", nullable=True),
        _frozen_resume_column("ends_at", "timestamptz", nullable=True),
        _frozen_resume_column("all_day", "bool", nullable=False),
        _frozen_resume_column("transparency", "varchar", nullable=False),
        _frozen_resume_column("status", "varchar", nullable=False),
        _frozen_resume_column("timezone", "varchar", nullable=False),
        _frozen_resume_column("recurring_event_id", "varchar", nullable=True),
        _frozen_resume_column("etag", "varchar", nullable=True),
        _frozen_resume_column("provider_url", "text", nullable=False),
        _frozen_resume_column("id", "uuid", nullable=False),
        _frozen_resume_column("created_at", "timestamptz", nullable=False, default="now()"),
        _frozen_resume_column("updated_at", "timestamptz", nullable=False, default="now()"),
        _frozen_resume_column("provider_updated_at", "timestamptz", nullable=True),
        _frozen_resume_column("organizer", "jsonb", nullable=True),
        _frozen_resume_column("attendees", "jsonb", nullable=True),
        _frozen_resume_column("access_role", "varchar", nullable=True),
        _frozen_resume_column("can_edit", "bool", nullable=False, default="false"),
    }
)


def _frozen_resume_nonunique_index(
    table_name: str,
    index_name: str,
    columns: tuple[str, ...],
) -> _ResumeIndexCatalog:
    """构造 source 非唯一普通索引的精确形状。"""
    return _ResumeIndexCatalog(
        table_name=table_name,
        index_name=index_name,
        is_valid=True,
        is_unique=False,
        is_primary=False,
        is_unpartial=True,
        has_no_expressions=True,
        key_attribute_count=len(columns),
        attribute_count=len(columns),
        index_columns=columns,
    )


_CALENDAR_0017_SOURCE_INDEXES = frozenset(
    {
        _frozen_resume_index("calendar_events", "calendar_events_pkey", ("id",), is_primary=True),
        _frozen_resume_nonunique_index(
            "calendar_events",
            "ix_calendar_events_connection_starts",
            ("connection_id", "starts_at"),
        ),
        _frozen_resume_index(
            "calendar_events",
            "uq_calendar_events_connection_provider_event",
            ("connection_id", "provider_event_id"),
            is_primary=False,
        ),
    }
)

_CALENDAR_0017_SOURCE_CONSTRAINTS = frozenset(
    {
        _frozen_resume_constraint(
            "calendar_events",
            "calendar_events_connection_id_fkey",
            "f",
            ("connection_id",),
            delete_action="c",
            target_table="oauth_connections",
            target_columns=("id",),
        ),
        _frozen_resume_constraint("calendar_events", "calendar_events_pkey", "p", ("id",)),
        _frozen_resume_constraint(
            "calendar_events",
            "calendar_events_user_id_fkey",
            "f",
            ("user_id",),
            delete_action="r",
            target_table="users",
            target_columns=("id",),
        ),
        _frozen_resume_constraint(
            "calendar_events",
            "uq_calendar_events_connection_provider_event",
            "u",
            ("connection_id", "provider_event_id"),
        ),
    }
)

_CALENDAR_0018_INDEX_NAME = "uq_calendar_events_connection_calendar_provider_event"
_CALENDAR_0018_INDEX_COLUMNS = ("connection_id", "calendar_id", "provider_event_id")

_TABLES_BY_REVISION: dict[str, frozenset[str]] = {
    "base": frozenset(),
    "20260730_0001": _IDENTITY_TABLES,
    "20260730_0002": _IDENTITY_TABLES | _TASK_TABLES,
    "20260803_0003": _IDENTITY_TABLES | _TASK_TABLES,
    "20260803_0004": _IDENTITY_TABLES | _TASK_TABLES | _CHECKPOINT_TABLES,
    "20260803_0005": _IDENTITY_TABLES | _TASK_TABLES | _CHECKPOINT_TABLES,
    "20260803_0006": _IDENTITY_TABLES | _TASK_TABLES | _CHECKPOINT_TABLES,
    "20260804_0007_google_sources": (
        _IDENTITY_TABLES | _TASK_TABLES | _CHECKPOINT_TABLES | _SOURCE_TABLES
    ),
    "20260804_0007": _IDENTITY_TABLES | _TASK_TABLES | _CHECKPOINT_TABLES | _SOURCE_TABLES,
    "20260804_0008": _IDENTITY_TABLES | _TASK_TABLES | _CHECKPOINT_TABLES | _SOURCE_TABLES,
    "20260730_0004": (
        _IDENTITY_TABLES | _TASK_TABLES | _CHECKPOINT_TABLES | _SOURCE_TABLES | _WORKSPACE_TABLES
    ),
    "20260804_0009": (
        _IDENTITY_TABLES | _TASK_TABLES | _CHECKPOINT_TABLES | _SOURCE_TABLES | _WORKSPACE_TABLES
    ),
    "20260804_0010": (
        _IDENTITY_TABLES | _TASK_TABLES | _CHECKPOINT_TABLES | _SOURCE_TABLES | _WORKSPACE_TABLES
    ),
    "20260806_0011": (
        _IDENTITY_TABLES
        | _TASK_TABLES
        | _CHECKPOINT_TABLES
        | _SOURCE_TABLES
        | _WORKSPACE_TABLES
        | _CONNECTION_TABLES
    ),
    "20260806_0012": _ALL_TABLES,
    "20260806_0013": _ALL_TABLES,
    "20260807_0014": _ALL_TABLES,
    "20260808_0015": _ALL_TABLES,
    "20260808_0016": _ALL_TABLES,
    "20260808_0017": _ALL_TABLES,
    "20260809_0018": _ALL_TABLES,
    "20260809_0019": _ALL_TABLES,
}

_SEQUENCES_BY_REVISION: dict[str, frozenset[str]] = {
    revision: (
        frozenset()
        if revision in {"base", "20260730_0001"}
        else (
            frozenset({"audit_events_id_seq"})
            if revision in {"20260730_0002", "20260803_0003"}
            else frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"})
        )
    )
    for revision in _REVISION_ORDER
}

_PRE_RETENTION_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "approval_requests": ("DELETE", "SELECT"),
    "audit_events": ("DELETE", "INSERT", "SELECT"),
    "calendar_events": ("DELETE", "SELECT"),
    "conversations": ("DELETE", "SELECT"),
    "daily_brief_items": ("DELETE", "SELECT"),
    "daily_briefs": ("DELETE", "SELECT"),
    "email_analyses": ("DELETE", "SELECT"),
    "email_messages": ("DELETE", "SELECT"),
    "email_threads": ("DELETE", "SELECT"),
    "encrypted_credentials": ("DELETE", "SELECT"),
    "llm_invocations": ("DELETE", "SELECT"),
    "messages": ("DELETE", "SELECT"),
    "oauth_connections": ("DELETE", "SELECT"),
    "outbox_events": ("DELETE", "SELECT"),
    "sync_cursors": ("SELECT",),
    "task_runs": ("DELETE", "SELECT"),
    "task_steps": ("DELETE", "SELECT"),
    "tool_executions": ("DELETE", "SELECT"),
    "user_sessions": ("DELETE", "SELECT"),
    "users": ("SELECT",),
}

_FINAL_RETENTION_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "approval_requests": ("DELETE", "SELECT"),
    "audit_events": ("DELETE", "INSERT", "SELECT"),
    "calendar_change_proposals": ("DELETE", "SELECT"),
    "calendar_change_snapshots": ("DELETE", "SELECT"),
    "calendar_events": ("DELETE", "SELECT"),
    "connection_capabilities": ("DELETE", "SELECT"),
    "conversations": ("DELETE", "SELECT"),
    "daily_brief_items": ("DELETE", "SELECT"),
    "daily_briefs": ("DELETE", "SELECT"),
    "email_analyses": ("DELETE", "SELECT"),
    "email_messages": ("DELETE", "SELECT"),
    "email_threads": ("DELETE", "SELECT"),
    "encrypted_credentials": ("DELETE", "SELECT"),
    "llm_invocations": ("DELETE", "SELECT"),
    "mail_draft_versions": ("DELETE", "SELECT"),
    "mail_drafts": ("DELETE", "SELECT"),
    "messages": ("DELETE", "SELECT"),
    "oauth_attempts": ("DELETE", "SELECT"),
    "oauth_connections": ("DELETE", "SELECT"),
    "outbox_events": ("DELETE", "SELECT"),
    "provider_calendars": ("DELETE", "SELECT"),
    "sync_cursors": ("DELETE", "SELECT"),
    "task_runs": ("DELETE", "SELECT"),
    "task_steps": ("DELETE", "SELECT"),
    "tool_executions": ("DELETE", "SELECT"),
    "user_sessions": ("DELETE", "SELECT"),
    "users": ("SELECT",),
}

_FINAL_RETENTION_UPDATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "approval_requests": (
        "payload_ciphertext",
        "payload_key_version",
        "payload_nonce",
        "status",
    ),
    "calendar_change_proposals": ("status", "updated_at"),
    "calendar_change_snapshots": (
        "content_ciphertext",
        "content_key_version",
        "content_nonce",
    ),
    "calendar_events": (
        "description_aad_version",
        "description_ciphertext",
        "description_key_version",
        "description_nonce",
        "location_aad_version",
        "location_ciphertext",
        "location_key_version",
        "location_nonce",
        "updated_at",
    ),
    "email_messages": ("body_ciphertext", "body_key_version", "body_nonce", "updated_at"),
    "mail_draft_versions": ("body_ciphertext", "body_key_version", "body_nonce"),
    "mail_drafts": ("status", "updated_at"),
    "sync_cursors": ("cursor", "last_attempt_at", "last_error_code", "last_success_at"),
    "task_runs": (
        "approval_checkpoint_recovery_at",
        "error_code",
        "finished_at",
        "lease_expires_at",
        "lease_owner",
        "retry_recovery_at",
        "scheduled_for",
        "status",
        "updated_at",
    ),
    "tool_executions": ("error_code", "status"),
    "users": (
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
    ),
}

_BASE_USER_UPDATE_COLUMNS = ("display_name", "email", "is_active", "password_hash")
_RETENTION_DAY_COLUMNS = (
    "email_body_retention_days",
    "source_metadata_retention_days",
    "workspace_history_retention_days",
)
_SYNC_CURSOR_UPDATE_COLUMNS = (
    "cursor",
    "last_attempt_at",
    "last_error_code",
    "last_success_at",
)
_EMAIL_BODY_UPDATE_COLUMNS = ("body_ciphertext", "body_key_version", "body_nonce")


def _pre_retention_columns(revision: str, tables: frozenset[str]) -> dict[str, tuple[str, ...]]:
    """按历史列出现与 0010 policy 生效点构造 pre-0019 列授权。"""
    revision_index = _REVISION_ORDER.index(revision)
    result: dict[str, tuple[str, ...]] = {}
    if "users" in tables:
        user_columns = list(_BASE_USER_UPDATE_COLUMNS)
        if revision_index >= _REVISION_ORDER.index("20260730_0004"):
            user_columns.extend(_RETENTION_DAY_COLUMNS)
        result["users"] = tuple(sorted(user_columns))
    if "sync_cursors" in tables:
        result["sync_cursors"] = _SYNC_CURSOR_UPDATE_COLUMNS
    if revision_index >= _REVISION_ORDER.index("20260804_0010"):
        result["email_messages"] = _EMAIL_BODY_UPDATE_COLUMNS
    return result


def _build_inventory_policy(revision: str) -> _InventoryPolicy:
    """把一个 revision 的对象集合与 retention policy 完全冻结。"""
    tables = _TABLES_BY_REVISION[revision]
    sequences = _SEQUENCES_BY_REVISION[revision]
    retention_sequences: tuple[str, ...]
    if revision == "20260809_0019":
        retention_tables = {
            table: privileges
            for table, privileges in _FINAL_RETENTION_TABLE_PRIVILEGES.items()
            if table in tables
        }
        retention_columns = {
            table: columns
            for table, columns in _FINAL_RETENTION_UPDATE_COLUMNS.items()
            if table in tables
        }
        retention_sequences = ("audit_events_id_seq",)
    else:
        retention_tables = {
            table: privileges
            for table, privileges in _PRE_RETENTION_TABLE_PRIVILEGES.items()
            if table in tables
        }
        retention_columns = _pre_retention_columns(revision, tables)
        retention_sequences = tuple(sorted(sequences))
    return _InventoryPolicy(
        tables=tuple(sorted(tables)),
        sequences=tuple(sorted(sequences)),
        retention_table_privileges=tuple(
            (table, tuple(sorted(privileges)))
            for table, privileges in sorted(retention_tables.items())
        ),
        retention_update_columns=tuple(
            (table, tuple(sorted(columns))) for table, columns in sorted(retention_columns.items())
        ),
        retention_sequences=tuple(sorted(retention_sequences)),
    )


# baseline/active的表、列、序列目录同构，但权限包含独立验证的revision SELECT delta。
# 两个phase均使用独立registry key；任一key缺失都失败，禁止以baseline隐式替代active。
_INVENTORY_REGISTRY: dict[tuple[str, GrantPhase], _InventoryPolicy] = {
    (revision, phase): _build_inventory_policy(revision)
    for revision in _REVISION_ORDER
    for phase in (GrantPhase.BASELINE, GrantPhase.ACTIVE)
}

_EXPLICIT_POLICY_CHANGE_PAIRS = frozenset(
    {
        ("20260804_0008", "20260730_0004"),
        ("20260804_0009", "20260804_0010"),
        ("20260809_0018", "20260809_0019"),
    }
)


def _expected_object_grants(
    *,
    revision: str,
    phase: GrantPhase,
    relation_owner: str,
) -> tuple[ObjectGrantTuple, ...]:
    """从唯一 registry 构造一个 revision/phase 的完整直接授权 inventory。

    Args:
        revision: 精确 Alembic revision；首次安装迁移前使用 ``base``。
        phase: 显式 baseline 或 active key，不接受同值裸字符串。
        relation_owner: 当前数据库所有受管 relation 的实际 owner 角色名。

    Returns:
        已排序、无重复、所有 tuple 均 ``is_grantable=false`` 的完整 inventory。

    Raises:
        ObjectGrantInvariantError: revision/phase 未注册、owner 不是非空无 NUL 的 catalog
            文本，或 registry 内部出现未知/重复 tuple。
    """
    validated_revision = _validated_revision(revision)
    validated_phase = _validate_phase(phase)
    validated_owner = _exact_text(relation_owner)
    policy = _INVENTORY_REGISTRY.get((validated_revision, validated_phase))
    if policy is None:
        _fail()

    grants: list[ObjectGrantTuple] = [
        _grant(
            ObjectKind.SCHEMA,
            PUBLIC_SCHEMA_NAME,
            PUBLIC_GRANTEE_NAME,
            PUBLIC_SCHEMA_OWNER_NAME,
            "USAGE",
        ),
        _grant(
            ObjectKind.SCHEMA,
            PUBLIC_SCHEMA_NAME,
            PUBLIC_SCHEMA_OWNER_NAME,
            PUBLIC_SCHEMA_OWNER_NAME,
            "CREATE",
        ),
        _grant(
            ObjectKind.SCHEMA,
            PUBLIC_SCHEMA_NAME,
            PUBLIC_SCHEMA_OWNER_NAME,
            PUBLIC_SCHEMA_OWNER_NAME,
            "USAGE",
        ),
        _grant(
            ObjectKind.SCHEMA,
            PUBLIC_SCHEMA_NAME,
            APP_RUNTIME_ROLE_NAME,
            PUBLIC_SCHEMA_OWNER_NAME,
            "USAGE",
        ),
        _grant(
            ObjectKind.SCHEMA,
            PUBLIC_SCHEMA_NAME,
            RETENTION_RUNTIME_ROLE_NAME,
            PUBLIC_SCHEMA_OWNER_NAME,
            "USAGE",
        ),
    ]

    retention_tables = dict(policy.retention_table_privileges)
    retention_columns = dict(policy.retention_update_columns)
    for table_name in policy.tables:
        for privilege in _OWNER_TABLE_PRIVILEGES:
            grants.append(
                _grant(
                    ObjectKind.TABLE,
                    table_name,
                    validated_owner,
                    validated_owner,
                    privilege,
                )
            )
        if table_name != "alembic_version":
            app_privileges = (
                ("INSERT", "SELECT") if table_name == "audit_events" else _APP_TABLE_PRIVILEGES
            )
            for privilege in app_privileges:
                grants.append(
                    _grant(
                        ObjectKind.TABLE,
                        table_name,
                        APP_RUNTIME_ROLE_NAME,
                        validated_owner,
                        privilege,
                    )
                )
        elif validated_phase is GrantPhase.ACTIVE:
            # 只在恢复门禁内让SET ROLE app的READ ONLY verifier读取版本；普通baseline
            # 保持无此tuple，完成事务必须撤回这一唯一phase delta后才能恢复CONNECT。
            grants.append(
                _grant(
                    ObjectKind.TABLE, table_name, APP_RUNTIME_ROLE_NAME, validated_owner, "SELECT"
                )
            )
        for privilege in retention_tables.get(table_name, ()):
            grants.append(
                _grant(
                    ObjectKind.TABLE,
                    table_name,
                    RETENTION_RUNTIME_ROLE_NAME,
                    validated_owner,
                    privilege,
                )
            )
        for column_name in retention_columns.get(table_name, ()):
            grants.append(
                _grant(
                    ObjectKind.COLUMN,
                    table_name,
                    RETENTION_RUNTIME_ROLE_NAME,
                    validated_owner,
                    "UPDATE",
                    column_name=column_name,
                )
            )

    retention_sequences = set(policy.retention_sequences)
    for sequence_name in policy.sequences:
        for privilege in _OWNER_SEQUENCE_PRIVILEGES:
            grants.append(
                _grant(
                    ObjectKind.SEQUENCE,
                    sequence_name,
                    validated_owner,
                    validated_owner,
                    privilege,
                )
            )
        grants.append(
            _grant(
                ObjectKind.SEQUENCE,
                sequence_name,
                APP_RUNTIME_ROLE_NAME,
                validated_owner,
                "USAGE",
            )
        )
        if sequence_name in retention_sequences:
            grants.append(
                _grant(
                    ObjectKind.SEQUENCE,
                    sequence_name,
                    RETENTION_RUNTIME_ROLE_NAME,
                    validated_owner,
                    "USAGE",
                )
            )

    result = tuple(sorted(grants))
    _verify_exact_grant_multiset(result, result)
    return result


def _runtime_grant(grant: ObjectGrantTuple) -> bool:
    """返回 tuple 是否直接授予 app/retention，而不是 owner/default ACL。"""
    return grant.grantee in {APP_RUNTIME_ROLE_NAME, RETENTION_RUNTIME_ROLE_NAME}


def _object_identity(grant: ObjectGrantTuple) -> tuple[ObjectKind, str, str]:
    """返回用于识别 DDL 新增/移除对象的 kind/schema/name 三元组。"""
    kind = ObjectKind.TABLE if grant.object_kind is ObjectKind.COLUMN else grant.object_kind
    return kind, grant.schema_name, grant.object_name


def _migration_grant_delta(
    *,
    source_revision: str,
    destination_revision: str,
    phase: GrantPhase,
    relation_owner: str,
) -> MigrationGrantDelta:
    """构造相邻 published migration step 唯一允许执行的 runtime grant/revoke delta。

    新对象只应用 destination inventory 中该对象的 app/retention tuple；被 DDL 删除的对象
    不执行 REVOKE。既有对象上的权限变化只允许出现在三个显式 policy pair：新增用户保留
    列、0010 邮件正文抹除和 0019 最终 retention matrix。历史 migration 契约测试需要
    downgrade，因此相邻性允许双向；显式 policy authority 始终按规范正向 pair 判断，不能
    由调用方向创造第四种 policy transition。

    Args:
        source_revision: ``before`` snapshot 对应的精确 source revision。
        destination_revision: 当前 Alembic step 的精确 destination revision。
        phase: 与 snapshot/registry 相同的显式 phase。
        relation_owner: 当前数据库 relation owner 原始 catalog 文本，仅用于绑定 grantor。

    Returns:
        排序且只包含 runtime grantee 的冻结 delta。

    Raises:
        ObjectGrantInvariantError: revision 不相邻、phase/owner 非法，或 registry 在非显式
            正向 policy pair 上改变既有对象权限。
    """
    validated_source = _validated_revision(source_revision)
    validated_destination = _validated_revision(destination_revision)
    validated_phase = _validate_phase(phase)
    validated_owner = _exact_text(relation_owner)
    try:
        source_index = _REVISION_ORDER.index(validated_source)
        destination_index = _REVISION_ORDER.index(validated_destination)
    except ValueError:
        _fail()
    if abs(source_index - destination_index) != 1:
        _fail()

    source = set(
        _expected_object_grants(
            revision=validated_source,
            phase=validated_phase,
            relation_owner=validated_owner,
        )
    )
    destination = set(
        _expected_object_grants(
            revision=validated_destination,
            phase=validated_phase,
            relation_owner=validated_owner,
        )
    )
    source_objects = {_object_identity(grant) for grant in source}
    destination_objects = {_object_identity(grant) for grant in destination}
    created_objects = destination_objects - source_objects
    removed_objects = source_objects - destination_objects

    created_applies = {
        grant
        for grant in destination - source
        if _runtime_grant(grant) and _object_identity(grant) in created_objects
    }
    existing_applies = {
        grant
        for grant in destination - source
        if _runtime_grant(grant) and _object_identity(grant) not in created_objects
    }
    existing_revokes = {
        grant
        for grant in source - destination
        if _runtime_grant(grant) and _object_identity(grant) not in removed_objects
    }
    forward_pair = (
        (validated_source, validated_destination)
        if source_index < destination_index
        else (validated_destination, validated_source)
    )
    if (existing_applies or existing_revokes) and forward_pair not in _EXPLICIT_POLICY_CHANGE_PAIRS:
        _fail()
    if forward_pair in _EXPLICIT_POLICY_CHANGE_PAIRS and not (existing_applies or existing_revokes):
        _fail()
    grants_to_apply = tuple(sorted(created_applies | existing_applies))
    grants_to_revoke = tuple(sorted(existing_revokes))
    if any(not _runtime_grant(grant) for grant in (*grants_to_apply, *grants_to_revoke)):
        _fail()
    return MigrationGrantDelta(
        source_revision=validated_source,
        destination_revision=validated_destination,
        phase=validated_phase,
        grants_to_revoke=grants_to_revoke,
        grants_to_apply=grants_to_apply,
    )


def _parse_catalog_row(
    row: _CatalogRow, expected_kind: ObjectKind
) -> tuple[ObjectGrantTuple, str, str]:
    """把一条 raw catalog mapping 收窄为 tuple、object owner 与 database owner。"""
    kind_text = _exact_text(_required(row, "object_kind"))
    try:
        kind = ObjectKind(kind_text)
    except ValueError:
        _fail()
    if kind is not expected_kind:
        _fail()
    grant = _validate_grant_tuple(
        ObjectGrantTuple(
            object_kind=kind,
            schema_name=_exact_text(_required(row, "schema_name")),
            object_name=_exact_text(_required(row, "object_name")),
            column_name=_optional_text(_required(row, "column_name")),
            grantee=_exact_text(_required(row, "grantee")),
            grantor=_exact_text(_required(row, "grantor")),
            privilege_type=_exact_text(_required(row, "privilege_type")),
            is_grantable=_exact_bool(_required(row, "is_grantable")),
        )
    )
    object_owner = _exact_text(_required(row, "object_owner"))
    database_owner = _exact_text(_required(row, "database_owner"))
    if kind is ObjectKind.SCHEMA:
        if object_owner != PUBLIC_SCHEMA_OWNER_NAME:
            _fail()
    elif object_owner != database_owner:
        # 迁移与 ``pg_restore --no-owner`` 都要求受管 relation 属于当前数据库 owner；
        # 不能把一个自洽但被其他角色持有的 relation ACL 当成合法 inventory。
        _fail()
    return grant, object_owner, database_owner


def _read_catalog(
    connection: Connection,
    *,
    revision: str,
    phase: GrantPhase,
) -> _CatalogRead:
    """从同一同步 SQLAlchemy connection 读取四个 raw ACL source。"""
    validated_revision = _validated_revision(revision)
    validated_phase = _validate_phase(phase)
    parameters = {"schema_name": PUBLIC_SCHEMA_NAME}
    grants: list[ObjectGrantTuple] = []
    owners: set[str] = set()
    for sql, kind in (
        (SCHEMA_GRANTS_SQL, ObjectKind.SCHEMA),
        (TABLE_GRANTS_SQL, ObjectKind.TABLE),
        (COLUMN_GRANTS_SQL, ObjectKind.COLUMN),
        (SEQUENCE_GRANTS_SQL, ObjectKind.SEQUENCE),
    ):
        result = connection.execute(text(sql), parameters)
        rows = result.mappings().all()
        for row in rows:
            grant, _, database_owner = _parse_catalog_row(row, kind)
            grants.append(grant)
            owners.add(database_owner)
    if len(owners) != 1:
        _fail()
    sorted_grants = tuple(sorted(grants))
    if len(set(sorted_grants)) != len(sorted_grants):
        _fail()
    return _CatalogRead(
        snapshot=CatalogObjectSnapshot(
            revision=validated_revision,
            phase=validated_phase,
            grants=sorted_grants,
        ),
        relation_owner=owners.pop(),
    )


def read_object_grants(
    connection: Connection,
    *,
    revision: str,
    phase: GrantPhase,
) -> CatalogObjectSnapshot:
    """读取 schema/table/column/sequence 的完整 canonical ACL snapshot。

    函数只执行四条显式 ``pg_catalog`` SELECT；不使用 information_schema、effective
    permission API、角色继承、database ACL 或 restore facts，也不执行任何 mutation。

    Args:
        connection: Alembic 外层事务使用的同步 SQLAlchemy ``Connection``。
        revision: 仅用于给 snapshot 绑定当前 revision 标签。
        phase: 仅用于给 snapshot 绑定显式 lifecycle phase。

    Returns:
        已类型收窄、排序且无重复 tuple 的 snapshot。

    Raises:
        ObjectGrantInvariantError: raw 类型、owner、privilege、grant option 或重复 tuple
            任一不可信。
    """
    return _read_catalog(connection, revision=revision, phase=phase).snapshot


def verify_object_grants(
    connection: Connection,
    *,
    revision: str,
    phase: GrantPhase,
) -> CatalogObjectSnapshot:
    """读取并精确验证一个 destination revision/phase 的完整 inventory。

    Args:
        connection: 当前 migration/lifecycle 持有的同一同步连接。
        revision: 要验证的 destination revision。
        phase: 显式 baseline/active registry key。

    Returns:
        验证成功后供 callback chain 保存的新 snapshot。

    Raises:
        ObjectGrantInvariantError: 任一 tuple missing/extra/duplicate、grantor/grantee、
            privilege 或 grant option 不匹配。
    """
    catalog = _read_catalog(connection, revision=revision, phase=phase)
    expected = _expected_object_grants(
        revision=revision,
        phase=phase,
        relation_owner=catalog.relation_owner,
    )
    _verify_exact_grant_multiset(catalog.snapshot.grants, expected)
    return catalog.snapshot


def _parse_resume_column(row: _CatalogRow) -> _ResumeColumnCatalog:
    """把 information_schema 单列投影收窄为冻结的 resume catalog 类型。"""
    return _ResumeColumnCatalog(
        column_name=_exact_text(_required(row, "column_name")),
        udt_name=_exact_text(_required(row, "udt_name")),
        is_nullable=_exact_bool(_required(row, "is_nullable")),
        column_default=_optional_text(_required(row, "column_default")),
        is_identity=_exact_bool(_required(row, "is_identity")),
        identity_generation=_optional_text(_required(row, "identity_generation")),
        is_generated=_exact_bool(_required(row, "is_generated")),
        generation_expression=_optional_text(_required(row, "generation_expression")),
    )


def _read_resume_columns(
    connection: Connection,
    *,
    table_name: str,
) -> tuple[_ResumeColumnCatalog, ...]:
    """只读一个冻结表的完整活动列 catalog，并拒绝重复或非法行。

    Args:
        connection: Alembic 外层事务使用的同一同步连接。
        table_name: descriptor 固定的 public 表名，不接受任意 SQL 片段。

    Returns:
        按 catalog ordinal position 排列且列名唯一的完整投影。

    Raises:
        ObjectGrantInvariantError: 表名、字段类型或重复列事实无法可信解释。
    """
    validated_table = _validated_identifier(table_name)
    rows = connection.execute(
        text(MIGRATION_RESUME_COLUMNS_SQL),
        {"schema_name": PUBLIC_SCHEMA_NAME, "table_name": validated_table},
    ).mappings().all()
    columns = tuple(_parse_resume_column(row) for row in rows)
    if len({column.column_name for column in columns}) != len(columns):
        _fail()
    return columns


def _parse_resume_index(row: _CatalogRow) -> _ResumeIndexCatalog:
    """把 pg_catalog 索引行收窄为不可变形状，保留 expression 的 NULL 位置。"""
    return _ResumeIndexCatalog(
        table_name=_exact_text(_required(row, "table_name")),
        index_name=_exact_text(_required(row, "index_name")),
        is_valid=_exact_bool(_required(row, "is_valid")),
        is_unique=_exact_bool(_required(row, "is_unique")),
        is_primary=_exact_bool(_required(row, "is_primary")),
        is_unpartial=_exact_bool(_required(row, "is_unpartial")),
        has_no_expressions=_exact_bool(_required(row, "has_no_expressions")),
        key_attribute_count=_exact_int(_required(row, "key_attribute_count")),
        attribute_count=_exact_int(_required(row, "attribute_count")),
        index_columns=_exact_text_tuple(_required(row, "index_columns"), allow_null=True),
    )


def _read_resume_indexes(
    connection: Connection,
    *,
    table_names: tuple[str, ...],
) -> tuple[_ResumeIndexCatalog, ...]:
    """读取固定受影响表的全部索引 catalog，不接受调用方 SQL 标识符。"""
    indexes: list[_ResumeIndexCatalog] = []
    for table_name in table_names:
        validated_table = _validated_identifier(table_name)
        rows = connection.execute(
            text(MIGRATION_RESUME_INDEXES_SQL),
            {"schema_name": PUBLIC_SCHEMA_NAME, "table_name": validated_table},
        ).mappings().all()
        indexes.extend(_parse_resume_index(row) for row in rows)
    parsed = tuple(indexes)
    if len({index.index_name for index in parsed}) != len(parsed):
        _fail()
    return parsed


def _parse_resume_constraint(row: _CatalogRow) -> _ResumeConstraintCatalog:
    """把 pg_catalog 约束行收窄为完整本地/目标列形状。"""
    constraint_type = _exact_text(_required(row, "constraint_type"))
    if constraint_type not in {"p", "u", "f", "c", "x"}:
        _fail()
    target_table = _optional_text(_required(row, "target_table"))
    target_columns_value = _required(row, "target_columns")
    target_columns = (
        None
        if target_columns_value is None
        else tuple(
            _exact_text_tuple(target_columns_value, allow_null=False)
        )
    )
    return _ResumeConstraintCatalog(
        table_name=_exact_text(_required(row, "table_name")),
        constraint_name=_exact_text(_required(row, "constraint_name")),
        constraint_type=constraint_type,
        is_valid=_exact_bool(_required(row, "is_valid")),
        is_deferrable=_exact_bool(_required(row, "is_deferrable")),
        is_deferred=_exact_bool(_required(row, "is_deferred")),
        delete_action=_optional_text(_required(row, "delete_action")),
        local_columns=_exact_text_tuple(_required(row, "local_columns"), allow_null=False),
        target_table=target_table,
        target_columns=target_columns,
        check_expression=_optional_text(_required(row, "check_expression")),
    )


def _read_resume_constraints(
    connection: Connection,
    *,
    table_names: tuple[str, ...],
) -> tuple[_ResumeConstraintCatalog, ...]:
    """读取固定受影响表的全部约束 catalog，并拒绝重复名称。"""
    constraints: list[_ResumeConstraintCatalog] = []
    for table_name in table_names:
        validated_table = _validated_identifier(table_name)
        rows = connection.execute(
            text(MIGRATION_RESUME_CONSTRAINTS_SQL),
            {"schema_name": PUBLIC_SCHEMA_NAME, "table_name": validated_table},
        ).mappings().all()
        constraints.extend(_parse_resume_constraint(row) for row in rows)
    parsed = tuple(constraints)
    if len({constraint.constraint_name for constraint in parsed}) != len(parsed):
        _fail()
    return parsed


def _verify_0017_owned_index_shape(
    index: _ResumeIndexCatalog,
    *,
    table_name: str,
    index_name: str,
    columns: tuple[str, ...],
) -> None:
    """验证 0017 migration-owned index；valid/invalid 是唯一允许变化的字段。"""
    if (
        index.table_name != table_name
        or index.index_name != index_name
        or not index.is_unique
        or index.is_primary
        or not index.is_unpartial
        or not index.has_no_expressions
        or index.key_attribute_count != len(columns)
        or index.attribute_count != len(columns)
        or index.index_columns != columns
    ):
        _fail()


def _verify_0017_resume_catalog(connection: Connection) -> None:
    """验证 0016 source 加上 0017 可归因的精确索引中间态。

    该函数只读取两张受影响邮件表的完整 columns/indexes/constraints catalog。允许的
    extra 只有 message index 单独存在、message valid 后 thread index 存在，或这些
    精确索引的 invalid 版本；任何 unknown、提前 constraint、反向顺序或错误形状都
    fail closed。业务投影只允许原 bounded backfill 已写入的 owning connection 子集，
    剩余 NULL 继续交给 0017 原 migration。
    """
    table_names = ("email_messages", "email_threads")
    actual_columns = {
        table_name: frozenset(
            _read_resume_columns(connection, table_name=table_name)
        )
        for table_name in table_names
    }
    if actual_columns != {
        "email_messages": _EMAIL_MESSAGES_0016_SOURCE_COLUMNS,
        "email_threads": _EMAIL_THREADS_0016_SOURCE_COLUMNS,
    }:
        _fail()

    actual_constraints = frozenset(
        _read_resume_constraints(connection, table_names=table_names)
    )
    if actual_constraints != _EMAIL_0016_SOURCE_CONSTRAINTS:
        _fail()

    actual_indexes = {
        index.index_name: index
        for index in _read_resume_indexes(connection, table_names=table_names)
    }
    source_index_names = {
        index.index_name for index in _EMAIL_0016_SOURCE_INDEXES
    }
    if any(
        actual_indexes.get(index.index_name) != index
        for index in _EMAIL_0016_SOURCE_INDEXES
    ):
        _fail()
    extra_names = set(actual_indexes) - source_index_names
    allowed_names = {
        _EMAIL_0017_MESSAGE_INDEX_NAME,
        _EMAIL_0017_THREAD_INDEX_NAME,
    }
    if not extra_names <= allowed_names:
        _fail()
    message_index = actual_indexes.get(_EMAIL_0017_MESSAGE_INDEX_NAME)
    thread_index = actual_indexes.get(_EMAIL_0017_THREAD_INDEX_NAME)
    if message_index is None and thread_index is not None:
        _fail()
    if message_index is not None:
        _verify_0017_owned_index_shape(
            message_index,
            table_name="email_messages",
            index_name=_EMAIL_0017_MESSAGE_INDEX_NAME,
            columns=_EMAIL_0017_MESSAGE_INDEX_COLUMNS,
        )
    if thread_index is not None:
        if message_index is None or not message_index.is_valid:
            _fail()
        _verify_0017_owned_index_shape(
            thread_index,
            table_name="email_threads",
            index_name=_EMAIL_0017_THREAD_INDEX_NAME,
            columns=_EMAIL_0017_THREAD_INDEX_COLUMNS,
        )

    state = connection.execute(text(_EMAIL_MESSAGE_0017_RESUME_STATE_SQL)).mappings().one()
    if _exact_bool(_required(state, "has_invalid_connection_projection")):
        _fail()


def _verify_0018_resume_catalog(connection: Connection) -> None:
    """验证 0017 CalendarEvent source 与唯一三元索引 candidate 的闭集 catalog。

    0018 不执行业务行 DML，因此 admission 只比较完整列、索引和约束事实。允许的
    destination artifact 仅是固定名称、普通唯一、三列顺序精确的 standalone index，
    且 ``indisvalid`` 可为 true 或 false；不允许提前挂载 destination constraint。旧
    二元 identity、非唯一 starts 索引、主键及三个 source 约束都必须保持精确，任何
    unknown object 或 source 漂移均在 migration-local operation 前 fail closed。
    """
    actual_columns = frozenset(
        _read_resume_columns(connection, table_name="calendar_events")
    )
    if actual_columns != _CALENDAR_EVENTS_0017_SOURCE_COLUMNS:
        _fail()

    actual_constraints = frozenset(
        _read_resume_constraints(connection, table_names=("calendar_events",))
    )
    if actual_constraints != _CALENDAR_0017_SOURCE_CONSTRAINTS:
        _fail()

    actual_indexes = {
        index.index_name: index
        for index in _read_resume_indexes(
            connection,
            table_names=("calendar_events",),
        )
    }
    source_index_names = {
        index.index_name for index in _CALENDAR_0017_SOURCE_INDEXES
    }
    if any(
        actual_indexes.get(index.index_name) != index
        for index in _CALENDAR_0017_SOURCE_INDEXES
    ):
        _fail()
    extra_names = set(actual_indexes) - source_index_names
    if extra_names - {_CALENDAR_0018_INDEX_NAME}:
        _fail()
    candidate = actual_indexes.get(_CALENDAR_0018_INDEX_NAME)
    if candidate is not None:
        _verify_0017_owned_index_shape(
            candidate,
            table_name="calendar_events",
            index_name=_CALENDAR_0018_INDEX_NAME,
            columns=_CALENDAR_0018_INDEX_COLUMNS,
        )


def _verify_0016_resume_catalog(connection: Connection) -> None:
    """验证 0015 fresh source 或唯一允许的 0016 nullable crash candidate。

    Fresh source 必须仍是完整 0015 列集；candidate 的全部非 owned 列也必须按 ordinal
    与完整物理形状逐列等于 0015 source，同时一次性包含两个 migration-owned 列，且它们
    精确为无 default/identity/generated 的 nullable ``uuid`` 与 ``timestamptz``。只有
    完整 candidate 才读取业务投影：0016 从未写入 ``provider_updated_at``，故任一非
    NULL 值都不是可归因的迁移事实；已提交的 ``connection_id`` 子集则只能等于 owning
    thread 的连接。剩余 NULL 留给原 revision 固定批次处理，本函数不执行任何 DDL、
    DML、GRANT、REVOKE 或清理。
    """
    columns = _read_resume_columns(connection, table_name="email_messages")
    by_name = {column.column_name: column for column in columns}
    actual_names = frozenset(by_name)
    # fresh 0015 分支必须逐列、按 ordinal 精确匹配；只比较名称会把错形 source
    # （例如同名列被放宽为 nullable、改了 default 或重排）错误放行到 0016 DDL。
    if columns == _EMAIL_MESSAGES_0015_SOURCE_COLUMNS:
        return

    expected_candidate_names = (
        _EMAIL_MESSAGES_0015_COLUMN_NAMES | _EMAIL_MESSAGES_0016_OWNED_COLUMN_NAMES
    )
    if actual_names != expected_candidate_names:
        _fail()
    # owned expand 列会追加到原表；过滤它们后必须恢复逐 ordinal 的精确 0015 tuple，
    # 不能让同名但错 type/nullable/default/identity/generated 的 source 漂移进入 DML。
    source_columns = tuple(
        column
        for column in columns
        if column.column_name not in _EMAIL_MESSAGES_0016_OWNED_COLUMN_NAMES
    )
    if source_columns != _EMAIL_MESSAGES_0015_SOURCE_COLUMNS:
        _fail()
    for expected in _EMAIL_MESSAGES_0016_OWNED_COLUMNS:
        if by_name.get(expected.column_name) != expected:
            _fail()

    state = connection.execute(text(_EMAIL_MESSAGE_0016_RESUME_STATE_SQL)).mappings().one()
    if _exact_bool(_required(state, "has_unknown_provider_updated_at")):
        _fail()
    if _exact_bool(_required(state, "has_invalid_connection_projection")):
        _fail()


def verify_pre_migration_object_grants(
    connection: Connection,
    *,
    revision: str,
    destination_revision: str | None = None,
    phase: GrantPhase,
) -> CatalogObjectSnapshot:
    """在首个 migration DDL 前验证 source grants 与受限 resume candidate。

    ``base`` 只接受未出现 ``alembic_version`` 的精确首次安装 inventory；任何其他
    revision 都必须包含该表完整 owner ACL。``destination_revision`` 若存在，必须是冻结
    registry 中与 source 相邻的精确 step；当前只有 ``0015→0016`` 额外允许 migration
    自己可能留下的 closed candidate。无论 fresh/candidate，返回的 snapshot 都继续标记
    source revision，且实际 ACL 必须逐 tuple 等于 source inventory，绝不提前应用
    destination grants。

    Args:
        connection: 首个 migration DDL 前、外层事务持有的同步 SQLAlchemy 连接。
        revision: 要验证的唯一 source revision；首次安装迁移前使用 ``base``。
        destination_revision: lifecycle 从发布链解析出的相邻目标；``None`` 仅表示当前
            命令不移动 revision，不启用任何 resume descriptor。
        phase: source snapshot 对应的显式 baseline/active registry key。

    Returns:
        完整验证成功、可传给当前 step callback 的 source snapshot。

    Raises:
        ObjectGrantInvariantError: source tuple 任一 missing/extra/duplicate、owner/grantor、
            privilege 或 grant option 不匹配。
        sqlalchemy.exc.SQLAlchemyError: 四条只读 catalog 查询失败时不捕获、不包装，原样
            传播给外层事务。
    """
    validated_revision = _validated_revision(revision)
    validated_phase = _validate_phase(phase)
    validated_destination: str | None = None
    if destination_revision is not None:
        validated_destination = _validated_revision(destination_revision)
        try:
            source_index = _REVISION_ORDER.index(validated_revision)
            destination_index = _REVISION_ORDER.index(validated_destination)
        except ValueError:
            _fail()
        if abs(source_index - destination_index) != 1:
            _fail()

    # ACL 是所有 fresh/candidate 共同的第一层事实；candidate 新列没有直接 ACL，因此
    # 合法 crash catalog 仍必须逐 tuple 等于 source 0015 inventory。
    snapshot = verify_object_grants(
        connection,
        revision=validated_revision,
        phase=validated_phase,
    )
    if (validated_revision, validated_destination) == (
        "20260808_0015",
        "20260808_0016",
    ):
        _verify_0016_resume_catalog(connection)
    elif (validated_revision, validated_destination) == (
        "20260808_0016",
        "20260808_0017",
    ):
        _verify_0017_resume_catalog(connection)
    elif (validated_revision, validated_destination) == (
        "20260808_0017",
        "20260809_0018",
    ):
        _verify_0018_resume_catalog(connection)
    return snapshot


def _relation_owner_from_snapshot(snapshot: CatalogObjectSnapshot) -> str | None:
    """从已有 relation owner self-tuples 推导 source snapshot 的 owner。"""
    owners = {
        grant.grantee
        for grant in snapshot.grants
        if grant.object_kind in {ObjectKind.TABLE, ObjectKind.SEQUENCE}
        and grant.grantee == grant.grantor
        and grant.grantee
        not in {
            PUBLIC_GRANTEE_NAME,
            PUBLIC_SCHEMA_OWNER_NAME,
            APP_RUNTIME_ROLE_NAME,
            RETENTION_RUNTIME_ROLE_NAME,
        }
    }
    if not owners:
        return None
    if len(owners) != 1:
        _fail()
    return _exact_text(owners.pop())


def _expected_pre_delta_grants(
    *,
    destination: tuple[ObjectGrantTuple, ...],
    delta: MigrationGrantDelta,
) -> tuple[ObjectGrantTuple, ...]:
    """构造 DDL/version 行已完成而 grant delta 尚未应用的唯一 catalog shape。"""
    result = tuple(
        sorted((set(destination) - set(delta.grants_to_apply)) | set(delta.grants_to_revoke))
    )
    _verify_exact_grant_multiset(result, result)
    return result


def _quote_identifier(identifier: str) -> str:
    """引用 registry 控制的普通 PostgreSQL identifier。"""
    return f'"{_validated_identifier(identifier)}"'


def _render_mutation_sql(action: str, grant: ObjectGrantTuple) -> str:
    """把单一 runtime tuple 渲染为逐对象、逐权限 SQL。"""
    validated = _validate_grant_tuple(grant)
    if action not in {"GRANT", "REVOKE"}:
        _fail()
    if validated.grantee not in {APP_RUNTIME_ROLE_NAME, RETENTION_RUNTIME_ROLE_NAME}:
        _fail()
    privilege = validated.privilege_type
    schema = _quote_identifier(validated.schema_name)
    object_name = _quote_identifier(validated.object_name)
    grantee = _quote_identifier(validated.grantee)
    recipient_keyword = "TO" if action == "GRANT" else "FROM"
    if validated.object_kind is ObjectKind.SCHEMA:
        target = f"SCHEMA {schema}"
        privilege_clause = privilege
    elif validated.object_kind is ObjectKind.TABLE:
        target = f"TABLE {schema}.{object_name}"
        privilege_clause = privilege
    elif validated.object_kind is ObjectKind.SEQUENCE:
        target = f"SEQUENCE {schema}.{object_name}"
        privilege_clause = privilege
    else:
        if validated.column_name is None:
            _fail()
        target = f"TABLE {schema}.{object_name}"
        privilege_clause = f"{privilege} ({_quote_identifier(validated.column_name)})"
    return f"{action} {privilege_clause} ON {target} {recipient_keyword} {grantee}"


def _validate_bootstrap_request(
    *,
    revision: object,
    phase: object,
    posture: object,
) -> tuple[str, GrantPhase, BootstrapGrantPosture]:
    """在首条 catalog SQL 前收窄 bootstrap 唯一允许的 current-state 输入。"""
    validated_revision = _validated_revision(revision)
    validated_phase = _validate_phase(phase)
    if type(posture) is not BootstrapGrantPosture:
        _fail()
    if validated_phase is not GrantPhase.BASELINE:
        _fail()
    if (validated_revision, validated_phase) not in _INVENTORY_REGISTRY:
        # revision 必须来自冻结 registry；unknown 输入在读取 catalog 前拒绝，不能借
        # absent-role 分支生成调用方自选 destination policy。
        _fail()
    return validated_revision, validated_phase, posture


def _bootstrap_expected_pre_grants(
    expected: tuple[ObjectGrantTuple, ...],
    *,
    posture: BootstrapGrantPosture,
) -> tuple[ObjectGrantTuple, ...]:
    """返回角色创建前唯一允许的完整 inventory，而不是任意 missing-grant 子集。"""
    if posture is BootstrapGrantPosture.CURRENT:
        return expected
    if posture is not BootstrapGrantPosture.PRE_RUNTIME:
        _fail()
    return tuple(grant for grant in expected if not _runtime_grant(grant))


def _read_verified_bootstrap_grants(
    connection: Connection,
    *,
    revision: object,
    phase: object,
    posture: object,
) -> tuple[CatalogObjectSnapshot, tuple[ObjectGrantTuple, ...], BootstrapGrantPosture]:
    """重读并验证 bootstrap mutation 前的 exact current inventory。"""
    validated_revision, validated_phase, validated_posture = _validate_bootstrap_request(
        revision=revision,
        phase=phase,
        posture=posture,
    )
    catalog = _read_catalog(
        connection,
        revision=validated_revision,
        phase=validated_phase,
    )
    expected = _expected_object_grants(
        revision=validated_revision,
        phase=validated_phase,
        relation_owner=catalog.relation_owner,
    )
    expected_pre_grants = _bootstrap_expected_pre_grants(
        expected,
        posture=validated_posture,
    )
    _verify_exact_grant_multiset(catalog.snapshot.grants, expected_pre_grants)
    return catalog.snapshot, expected, validated_posture


def verify_bootstrap_object_grants(
    connection: Connection,
    *,
    revision: str,
    phase: GrantPhase,
    posture: BootstrapGrantPosture,
) -> CatalogObjectSnapshot:
    """验证 candidate 分类所需的 exact bootstrap object-grant posture。

    ``CURRENT`` 要求当前 revision 已精确匹配完整 baseline inventory；``PRE_RUNTIME``
    要求 catalog 精确等于同一 inventory 移除全部 app/retention tuple 后的形状。posture
    由可信 lifecycle caller 冻结，不能仅按 cluster-wide role 是否存在推断：数据库重建后
    两个角色通常仍存在，但新库的 database-local object grants 仍是 ``PRE_RUNTIME``。
    函数只读四个 canonical catalog source，不执行 GRANT/REVOKE，也不把 partial grant
    缺失解释成可修复状态。

    Args:
        connection: 已持 management/target lock 的同步 owner connection。
        revision: lifecycle 从当前 ``alembic_version`` 解析出的精确 revision。
        phase: bootstrap 只接受显式 ``GrantPhase.BASELINE``。
        posture: 当前 database-local runtime grants 是完整 current 或全 absent pre-runtime。

    Returns:
        与输入 revision/phase 绑定的实际 canonical snapshot；角色 absent 时其中不含任何
        app/retention tuple。

    Raises:
        ObjectGrantInvariantError: request、owner 或完整 tuple multiset 不满足唯一闭集。
    """
    snapshot, _, _ = _read_verified_bootstrap_grants(
        connection,
        revision=revision,
        phase=phase,
        posture=posture,
    )
    return snapshot


def apply_bootstrap_object_grants(
    connection: Connection,
    *,
    revision: str,
    phase: GrantPhase,
    posture: BootstrapGrantPosture,
) -> None:
    """在 owner transaction 内应用唯一允许的 bootstrap runtime-grant delta。

    本入口首先重读完整 catalog。``CURRENT`` 必须已经匹配当前 revision 的完整 baseline
    inventory；``PRE_RUNTIME`` 只允许同一 current revision 的 exact pre-runtime shape。
    两者都只按该 registry key 逐 tuple 授予全部 app/retention 直接权限，因此 steady
    role-bootstrap 可以重申 owner-granted、non-grantable grants，而 partial/extra 漂移仍在
    首条 SQL 前拒绝。posture 来自可信 lifecycle provenance，而不是宽松的角色存在性猜测。
    本入口不接收调用方 tuple、destination revision 或 repair plan，0018 lifecycle 因而
    不可能提前应用 0019 matrix。

    Args:
        connection: 与角色、database ACL mutation 共用的唯一 owner transaction 连接。
        revision: lifecycle 已验证的当前 revision，不是期望 destination。
        phase: bootstrap 唯一允许的 baseline phase。
        posture: lifecycle 按 caller/新库事实冻结的 database-local grant posture。

    Raises:
        ObjectGrantInvariantError: current inventory、revision/phase 或角色存在事实漂移。
        sqlalchemy.exc.SQLAlchemyError: 精确 GRANT 失败时原样向上传播，由外层统一回滚。
    """
    _, expected, _ = _read_verified_bootstrap_grants(
        connection,
        revision=revision,
        phase=phase,
        posture=posture,
    )

    # CURRENT 与 PRE_RUNTIME 都只重申当前 registry 的 runtime tuples；前置完整 inventory
    # 证明阻止该幂等动作退化为 partial drift repair，也禁止提前应用 destination policy。
    for grant in tuple(grant for grant in expected if _runtime_grant(grant)):
        connection.execute(text(_render_mutation_sql("GRANT", grant)))


def restore_inventory_objects(revision: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """返回同一权限registry冻结的恢复表/序列全集，fingerprint不得自行遗漏业务表。

    Args:
        revision: 当前镜像明确支持的已完成0018/0019版本；中间迁移态不是可恢复版本。

    Returns:
        稳定排序的表名与序列名，无供应商类型、数据值或用户可扩展对象。
    """
    if revision not in {"20260809_0018", "20260809_0019"}:
        _fail()
    policy = _INVENTORY_REGISTRY[(revision, GrantPhase.ACTIVE)]
    return policy.tables, policy.sequences


def verify_restore_pre_grants(connection: Connection, *, revision: str) -> CatalogObjectSnapshot:
    """验证真实no-privileges恢复后的完整对象形状及原public schema权限。

    这不是通用repair入口。调用方必须持同一management/target/schema恢复lease，且
    catalog已证明本次restore成功。PG17的普通dump不重建public schema，其原有两条
    runtime USAGE必须精确保留；被重建的table/column/sequence才没有runtime tuple。
    任何多余、部分grant、schema USAGE缺失或owner漂移在首条写入前拒绝。
    """
    restore_inventory_objects(revision)
    catalog = _read_catalog(connection, revision=revision, phase=GrantPhase.ACTIVE)
    expected = _expected_object_grants(
        revision=revision, phase=GrantPhase.ACTIVE, relation_owner=catalog.relation_owner
    )
    _verify_exact_grant_multiset(
        catalog.snapshot.grants,
        tuple(
            grant
            for grant in expected
            if not _runtime_grant(grant) or grant.object_kind is ObjectKind.SCHEMA
        ),
    )
    return catalog.snapshot


def apply_restore_object_grants(connection: Connection, *, revision: str) -> None:
    """在既有holder事务内验证no-privileges形状、逐tuple授予ACTIVE并完整复核。

    不调用bootstrap、不创建角色或连接、不授予CONNECT。任何失败由调用方事务整体
    回滚，catalog phase仍为restore_succeeded，只有新的有界调用才能重试。
    """
    verify_restore_pre_grants(connection, revision=revision)
    catalog = _read_catalog(connection, revision=revision, phase=GrantPhase.ACTIVE)
    expected = _expected_object_grants(
        revision=revision, phase=GrantPhase.ACTIVE, relation_owner=catalog.relation_owner
    )
    for grant in expected:
        if _runtime_grant(grant) and grant.object_kind is not ObjectKind.SCHEMA:
            connection.execute(text(_render_mutation_sql("GRANT", grant)))
    verify_object_grants(connection, revision=revision, phase=GrantPhase.ACTIVE)


def apply_restore_phase_grants(
    connection: Connection, *, revision: str, destination: GrantPhase
) -> None:
    """只在已验证完整inventory间应用唯一ACTIVE revision SELECT delta并复核。

    BASELINE→ACTIVE与gate/CONNECT撤销共用事务；ACTIVE→BASELINE与completion/audit/
    gate RESET共用事务。部分权限漂移不能借此修复，非法destination在任何SQL前拒绝。
    """
    restore_inventory_objects(revision)
    _validate_phase(destination)
    source = GrantPhase.BASELINE if destination is GrantPhase.ACTIVE else GrantPhase.ACTIVE
    catalog = _read_catalog(connection, revision=revision, phase=source)
    before = _expected_object_grants(
        revision=revision, phase=source, relation_owner=catalog.relation_owner
    )
    after = _expected_object_grants(
        revision=revision, phase=destination, relation_owner=catalog.relation_owner
    )
    _verify_exact_grant_multiset(catalog.snapshot.grants, before)
    for grant in sorted(set(before) - set(after)):
        connection.execute(text(_render_mutation_sql("REVOKE", grant)))
    for grant in sorted(set(after) - set(before)):
        connection.execute(text(_render_mutation_sql("GRANT", grant)))
    verify_object_grants(connection, revision=revision, phase=destination)


def apply_migration_grant_delta(
    connection: Connection,
    *,
    source_revision: str,
    destination_revision: str,
    phase: GrantPhase,
    before: CatalogObjectSnapshot,
) -> None:
    """验证 source 与 post-DDL shape 后应用当前相邻 step 的精确 delta。

    函数先验证 ``before`` 标签与完整 source inventory，再读取 DDL 已完成、policy delta
    尚未应用的 raw catalog。只有该 shape 精确等于“destination 减 grants_to_apply 加
    grants_to_revoke”时才逐 tuple 执行 SQL。unexpected object/grant、旧对象漂移、错误
    grantor、grant option 或非相邻 revision 都在首个 mutation 前失败。

    Args:
        connection: 与 Alembic revision operation 相同的同步连接。
        source_revision: 当前 step 的唯一 source revision。
        destination_revision: 当前 step 的唯一 destination revision。
        phase: 与 lifecycle/registry 相同的显式 phase。
        before: 首个 DDL 前由 verifier 返回的 source snapshot。

    Raises:
        ObjectGrantInvariantError: snapshot 标签/内容、post-DDL diff 或 delta registry 任一
            不匹配。SQL 错误由 SQLAlchemy 原样向上传播，由外层事务处理回滚。
    """
    validated_source = _validated_revision(source_revision)
    validated_destination = _validated_revision(destination_revision)
    validated_phase = _validate_phase(phase)
    if type(before) is not CatalogObjectSnapshot:
        _fail()
    if before.revision != validated_source or before.phase is not validated_phase:
        _fail()

    # owner 推导会读取 tuple 字段，因此先完整收窄容器、成员、字段与 duplicate；任何
    # malformed snapshot 都在 catalog SELECT 或 GRANT/REVOKE 之前变成稳定 typed error。
    _verify_exact_grant_multiset(before.grants, before.grants)
    owner = _relation_owner_from_snapshot(before)
    catalog: _CatalogRead | None = None
    if owner is None:
        # 只有精确 base inventory 没有 relation owner self-tuple；首步 DDL 后的新表提供
        # owner 事实。该读取仍是 SELECT，且任何 mismatch 都发生在 GRANT/REVOKE 前。
        catalog = _read_catalog(
            connection,
            revision=validated_destination,
            phase=validated_phase,
        )
        owner = catalog.relation_owner

    source_expected = _expected_object_grants(
        revision=validated_source,
        phase=validated_phase,
        relation_owner=owner,
    )
    _verify_exact_grant_multiset(before.grants, source_expected)
    delta = _migration_grant_delta(
        source_revision=validated_source,
        destination_revision=validated_destination,
        phase=validated_phase,
        relation_owner=owner,
    )
    if catalog is None:
        catalog = _read_catalog(
            connection,
            revision=validated_destination,
            phase=validated_phase,
        )
    if catalog.relation_owner != owner:
        _fail()
    destination_expected = _expected_object_grants(
        revision=validated_destination,
        phase=validated_phase,
        relation_owner=owner,
    )
    expected_pre_delta = _expected_pre_delta_grants(
        destination=destination_expected,
        delta=delta,
    )
    _verify_exact_grant_multiset(catalog.snapshot.grants, expected_pre_delta)

    for grant in delta.grants_to_revoke:
        connection.execute(text(_render_mutation_sql("REVOKE", grant)))
    for grant in delta.grants_to_apply:
        connection.execute(text(_render_mutation_sql("GRANT", grant)))
