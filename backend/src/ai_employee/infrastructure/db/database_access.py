"""读取、分类并按闭集计划修改 PostgreSQL database ACL 与运行时角色。

本模块是数据库级访问事实的唯一 canonical reader。异步业务边界与同步
maintenance/Alembic 边界复用相同 SQL 和 staged parser，把 PostgreSQL catalog 行立即
规范化为不可变类型，并用完整排序 multiset 区分四种冻结 ACL profile。四个纯 planning
入口只描述允许的闭集动作；同步 mutator 分别处理 runtime role 密码、candidate ACL 与
steady baseline CONNECT 重申。锁、事务、restore/session admission 和 object-grant inventory
仍由上层生命周期负责，不能以本模块的计划或 mutator 代替持锁重读。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NoReturn, Protocol, cast

from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection

APP_RUNTIME_ROLE_NAME = "ai_employee_app"
RETENTION_RUNTIME_ROLE_NAME = "ai_employee_retention"

DATABASE_ACL_SQL = """SELECT acl.grantee, acl.grantor, acl.privilege_type, acl.is_grantable
FROM pg_database AS db
CROSS JOIN LATERAL aclexplode(
    COALESCE(db.datacl, acldefault('d', db.datdba))
) AS acl
WHERE db.oid = :target_database_oid"""

_DATABASE_OWNER_SQL = """SELECT
    db.oid AS target_database_oid,
    db.datdba AS owner_oid
FROM pg_database AS db
WHERE db.oid = :target_database_oid"""

_RUNTIME_ROLES_SQL = """SELECT
    runtime_role.rolname AS role_name,
    runtime_role.oid AS role_oid,
    runtime_role.rolcanlogin AS can_login,
    runtime_role.rolinherit AS inherits,
    runtime_role.rolsuper AS is_superuser,
    runtime_role.rolcreatedb AS can_create_database,
    runtime_role.rolcreaterole AS can_create_role,
    runtime_role.rolreplication AS can_replicate,
    runtime_role.rolbypassrls AS bypasses_rls,
    runtime_role.rolconnlimit AS connection_limit,
    runtime_role.rolvaliduntil AS valid_until,
    runtime_role.rolconfig AS config
FROM pg_roles AS runtime_role
WHERE runtime_role.rolname IN (:app_role_name, :retention_role_name)
ORDER BY runtime_role.rolname"""

_ROLE_MEMBERSHIPS_SQL = """WITH runtime_roles AS (
    SELECT runtime_role.oid
    FROM pg_roles AS runtime_role
    WHERE runtime_role.rolname IN (:app_role_name, :retention_role_name)
)
SELECT
    membership.roleid AS role_oid,
    membership.member AS member_oid,
    membership.grantor AS grantor_oid,
    membership.admin_option AS admin_option
FROM pg_auth_members AS membership
WHERE membership.roleid IN (SELECT oid FROM runtime_roles)
   OR membership.member IN (SELECT oid FROM runtime_roles)
ORDER BY membership.roleid, membership.member, membership.grantor, membership.admin_option"""

_ALLOWED_DATABASE_PRIVILEGES = frozenset({"CREATE", "CONNECT", "TEMPORARY"})
_INVARIANT_ERROR_MESSAGE = "database access invariant violation"
_POSTGRESQL_OID_MAX = (1 << 32) - 1

_RUNTIME_ROLE_MUTATOR_SQL = """CREATE OR REPLACE FUNCTION pg_temp.ai_employee_apply_runtime_role(
    role_name text,
    role_password text,
    create_role boolean
) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    IF role_name NOT IN ('ai_employee_app', 'ai_employee_retention') THEN
        RAISE EXCEPTION 'database access invariant violation';
    END IF;
    BEGIN
        IF create_role THEN
            EXECUTE format(
                'CREATE ROLE %I LOGIN NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB '
                'NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1 PASSWORD %L',
                role_name,
                role_password
            );
        ELSE
            EXECUTE format('ALTER ROLE %I PASSWORD %L', role_name, role_password);
        END IF;
    EXCEPTION WHEN OTHERS THEN
        -- PostgreSQL 会把失败的 dynamic SQL 放入错误 CONTEXT；直接向上传播会泄漏
        -- PASSWORD literal。新异常只保留 SQLSTATE 分类，不携带原消息、DETAIL 或语句。
        RAISE EXCEPTION USING
            MESSAGE = 'database access invariant violation',
            ERRCODE = SQLSTATE;
    END;
END
$function$"""

_APPLY_RUNTIME_ROLE_SQL = """SELECT pg_temp.ai_employee_apply_runtime_role(
    :role_name,
    :role_password,
    :create_role
)"""


class DatabaseAclProfile(StrEnum):
    """表示完整 database ACL multiset 唯一对应的冻结 profile。"""

    FRESH_DEFAULT = "fresh_default"
    PRE_PROTOCOL_LEGACY = "pre_protocol_legacy"
    BASELINE = "baseline"
    ACTIVE = "active"


class BootstrapCaller(StrEnum):
    """表示唯一允许请求瞬态 bootstrap candidate 的生命周期入口。"""

    ROLE_BOOTSTRAP = "role_bootstrap"
    DB_RESET_POST_CREATE = "db_reset_post_create"


@dataclass(frozen=True, order=True, slots=True)
class DatabaseAclTuple:
    """保存 ``aclexplode`` 返回的一条未正规化 database ACL 事实。

    ``PUBLIC`` 以 ``grantee_oid=0`` 表示。owner 的隐式可授权能力不是 ACL tuple，
    因而 ``is_grantable`` 必须保留 catalog 原值，不能推断为 ``True``。
    """

    grantee_oid: int
    grantor_oid: int
    privilege_type: str
    is_grantable: bool


@dataclass(frozen=True, order=True, slots=True)
class RuntimeRoleSnapshot:
    """保存 app/retention 登录角色的全部安全相关公开属性。

    密码不属于 posture，reader 既不读取 ``rolpassword``，也不访问 ``pg_authid``。
    ``config`` 把 asyncpg 返回的 ``list[str] | None`` 立即规范化为不可变表示。
    """

    role_name: str
    role_oid: int
    can_login: bool
    inherits: bool
    is_superuser: bool
    can_create_database: bool
    can_create_role: bool
    can_replicate: bool
    bypasses_rls: bool
    connection_limit: int
    valid_until: datetime | None
    config: tuple[str, ...] | None


@dataclass(frozen=True, order=True, slots=True)
class RoleMembershipTuple:
    """保存涉及 app/retention 的一条双向 ``pg_auth_members`` 事实。"""

    role_oid: int
    member_oid: int
    grantor_oid: int
    admin_option: bool


@dataclass(frozen=True, slots=True)
class DatabaseAccessSnapshot:
    """保存一个目标数据库的完整 ACL、运行时角色与 membership 快照。"""

    target_database_oid: int
    owner_oid: int
    acl_profile: DatabaseAclProfile
    acl_tuples: tuple[DatabaseAclTuple, ...]
    app_role: RuntimeRoleSnapshot | None
    retention_role: RuntimeRoleSnapshot | None
    memberships: tuple[RoleMembershipTuple, ...]


@dataclass(frozen=True, slots=True)
class BootstrapCandidate:
    """表示当前持锁调用内可继续复核的瞬态 bootstrap 分类结果。

    该值不是 catalog authority。外部 restore/session/object-grant 证据不会被持久化到
    Candidate；Cycle 4 执行任何 mutation 前必须在锁内重新读取这些事实。
    """

    caller: BootstrapCaller
    source_profile: DatabaseAclProfile
    roles_exist: bool
    snapshot: DatabaseAccessSnapshot


class RuntimeRoleAction(StrEnum):
    """表示纯计划允许描述的完整双角色动作闭集。"""

    CREATE_BOTH_SAFE = "create_both_safe"
    ROTATE_BOTH_PASSWORDS = "rotate_both_passwords"
    NONE = "none"


class DatabaseAclAction(StrEnum):
    """表示纯计划允许描述的 database ACL 过渡或幂等重申动作闭集。"""

    CANDIDATE_TO_BASELINE = "candidate_to_baseline"
    BASELINE_TO_ACTIVE = "baseline_to_active"
    ACTIVE_TO_BASELINE = "active_to_baseline"
    BASELINE_CONNECT_REASSERT = "baseline_connect_reassert"


@dataclass(frozen=True, slots=True)
class DatabaseAccessMutationPlan:
    """描述一个已分类 posture 的最小未来动作，不授予执行权限。

    计划没有任意 grantee、privilege、SQL、连接、事务、锁或执行器字段，调用方不能把它
    当成已经完成 lifecycle admission 的证明。Cycle 4 及 restore higher layer 必须根据
    phase 在持锁 mutation 前重新验证 restore facts、session 与 object grants。
    """

    source_profile: DatabaseAclProfile
    target_profile: DatabaseAclProfile
    role_action: RuntimeRoleAction
    acl_action: DatabaseAclAction


class DatabaseAccessInvariantError(RuntimeError):
    """表示 database access catalog 或闭集输入违反冻结不变量。

    异常消息稳定且不包含数据库名、角色、OID、ACL 或其他 catalog 内容，适合跨边界记录
    稳定错误码而不泄露环境事实。
    """


class _CatalogRow(Protocol):
    """描述 SQLAlchemy RowMapping 与单元测试 mapping 共用的最小读取接口。"""

    def __contains__(self, key: object) -> bool:
        """返回 mapping 是否含有指定列。"""

    def __getitem__(self, key: str) -> object:
        """按明确列名读取第三方 catalog 值。"""


def _fail() -> NoReturn:
    """抛出唯一稳定、无内容的 database access invariant error。"""
    raise DatabaseAccessInvariantError(_INVARIANT_ERROR_MESSAGE)


def _required(row: _CatalogRow, key: str) -> object:
    """读取必需 catalog 列；缺列属于无法解析的外部事实。"""
    if key not in row:
        _fail()
    return row[key]


def _positive_int(value: object) -> int:
    """把普通 PostgreSQL OID 收窄为非 bool 的 ``1..2**32-1``。"""
    if type(value) is not int or not 1 <= value <= _POSTGRESQL_OID_MAX:
        _fail()
    return value


def _nonnegative_int(value: object) -> int:
    """把 PUBLIC-capable grantee OID 收窄为非 bool 的 ``0..2**32-1``。"""
    if type(value) is not int or not 0 <= value <= _POSTGRESQL_OID_MAX:
        _fail()
    return value


def _exact_int(value: object) -> int:
    """读取允许为 ``-1`` 的 PostgreSQL 整数属性，同时拒绝 bool。"""
    if type(value) is not int:
        _fail()
    return value


def _exact_bool(value: object) -> bool:
    """读取 PostgreSQL bool，拒绝整数或宽松 truthy/falsy 表示。"""
    if type(value) is not bool:
        _fail()
    return value


def _exact_text(value: object) -> str:
    """读取 PostgreSQL text，拒绝隐式字符串转换。"""
    if type(value) is not str:
        _fail()
    return value


def _optional_datetime(value: object) -> datetime | None:
    """读取 ``timestamptz | NULL``，未知驱动表示必须 fail closed。"""
    if value is None or isinstance(value, datetime):
        return value
    _fail()


def _normalize_role_config(value: object) -> tuple[str, ...] | None:
    """把 asyncpg 的 ``list[str] | None`` 规范化为不可变 role config。"""
    if value is None:
        return None
    if type(value) is not list:
        _fail()
    raw_items = cast(list[object], value)
    if not all(type(item) is str for item in raw_items):
        _fail()
    return tuple(cast(str, item) for item in raw_items)


def _owner_acl(owner_oid: int) -> tuple[DatabaseAclTuple, ...]:
    """返回所有 profile 共享的三条 owner/non-grantable ACL tuple。"""
    return (
        DatabaseAclTuple(owner_oid, owner_oid, "CREATE", False),
        DatabaseAclTuple(owner_oid, owner_oid, "CONNECT", False),
        DatabaseAclTuple(owner_oid, owner_oid, "TEMPORARY", False),
    )


def _expected_acl_profiles(
    *,
    owner_oid: int,
    app_role_oid: int | None,
    retention_role_oid: int | None,
) -> tuple[tuple[DatabaseAclProfile, tuple[DatabaseAclTuple, ...]], ...]:
    """按当前可解析角色 OID 构造四种唯一合法完整 multiset。"""
    owner_acl = _owner_acl(owner_oid)
    profiles: list[tuple[DatabaseAclProfile, tuple[DatabaseAclTuple, ...]]] = [
        (
            DatabaseAclProfile.FRESH_DEFAULT,
            tuple(
                sorted(
                    (
                        *owner_acl,
                        DatabaseAclTuple(0, owner_oid, "CONNECT", False),
                        DatabaseAclTuple(0, owner_oid, "TEMPORARY", False),
                    )
                )
            ),
        ),
        (DatabaseAclProfile.ACTIVE, tuple(sorted(owner_acl))),
    ]
    if app_role_oid is not None and retention_role_oid is not None:
        runtime_connect = (
            DatabaseAclTuple(app_role_oid, owner_oid, "CONNECT", False),
            DatabaseAclTuple(retention_role_oid, owner_oid, "CONNECT", False),
        )
        profiles.extend(
            (
                (
                    DatabaseAclProfile.PRE_PROTOCOL_LEGACY,
                    tuple(
                        sorted(
                            (
                                *owner_acl,
                                DatabaseAclTuple(0, owner_oid, "CONNECT", False),
                                DatabaseAclTuple(0, owner_oid, "TEMPORARY", False),
                                *runtime_connect,
                            )
                        )
                    ),
                ),
                (
                    DatabaseAclProfile.BASELINE,
                    tuple(sorted((*owner_acl, *runtime_connect))),
                ),
            )
        )
    return tuple(profiles)


def classify_database_acl_profile(
    *,
    owner_oid: int,
    app_role_oid: int | None,
    retention_role_oid: int | None,
    acl_tuples: tuple[DatabaseAclTuple, ...],
) -> DatabaseAclProfile:
    """按完整排序 multiset 返回唯一 database ACL profile。

    Args:
        owner_oid: 目标 ``pg_database.datdba`` 的精确 OID。
        app_role_oid: 精确角色名解析出的 app OID；角色缺失时为 ``None``。
        retention_role_oid: 精确角色名解析出的 retention OID；缺失时为 ``None``。
        acl_tuples: canonical query 返回的全部 ACL tuple，不是最低权限子集。

    Returns:
        与完整 multiset 唯一匹配的冻结 profile。

    Raises:
        DatabaseAccessInvariantError: tuple 缺失、额外、重复、未知、grantor/grant option
            错误，或角色 OID 无法形成唯一闭集。
    """
    validated_owner_oid = _positive_int(owner_oid)
    if app_role_oid is not None:
        app_role_oid = _positive_int(app_role_oid)
    if retention_role_oid is not None:
        retention_role_oid = _positive_int(retention_role_oid)
    present_runtime_oids = tuple(
        role_oid for role_oid in (app_role_oid, retention_role_oid) if role_oid is not None
    )
    if len({validated_owner_oid, *present_runtime_oids}) != 1 + len(present_runtime_oids):
        _fail()
    if type(acl_tuples) is not tuple:
        _fail()

    validated_tuples: list[DatabaseAclTuple] = []
    known_grantees = {0, validated_owner_oid, *present_runtime_oids}
    for acl_tuple in acl_tuples:
        if type(acl_tuple) is not DatabaseAclTuple:
            _fail()
        grantee_oid = _nonnegative_int(acl_tuple.grantee_oid)
        grantor_oid = _positive_int(acl_tuple.grantor_oid)
        privilege_type = _exact_text(acl_tuple.privilege_type)
        is_grantable = _exact_bool(acl_tuple.is_grantable)
        if privilege_type not in _ALLOWED_DATABASE_PRIVILEGES:
            _fail()
        if grantee_oid not in known_grantees:
            _fail()
        validated_tuples.append(
            DatabaseAclTuple(
                grantee_oid=grantee_oid,
                grantor_oid=grantor_oid,
                privilege_type=privilege_type,
                is_grantable=is_grantable,
            )
        )

    # 重复 tuple 也是完整 multiset 漂移，不能在 set 转换时被悄然折叠。
    if len(set(validated_tuples)) != len(validated_tuples):
        _fail()
    sorted_tuples = tuple(sorted(validated_tuples))
    matches = tuple(
        profile
        for profile, expected_tuples in _expected_acl_profiles(
            owner_oid=validated_owner_oid,
            app_role_oid=app_role_oid,
            retention_role_oid=retention_role_oid,
        )
        if sorted_tuples == expected_tuples
    )
    if len(matches) != 1:
        _fail()
    return matches[0]


def _parse_runtime_role(row: _CatalogRow) -> RuntimeRoleSnapshot:
    """把一条 ``pg_roles`` mapping 完整收窄为不可变 posture。"""
    return RuntimeRoleSnapshot(
        role_name=_exact_text(_required(row, "role_name")),
        role_oid=_positive_int(_required(row, "role_oid")),
        can_login=_exact_bool(_required(row, "can_login")),
        inherits=_exact_bool(_required(row, "inherits")),
        is_superuser=_exact_bool(_required(row, "is_superuser")),
        can_create_database=_exact_bool(_required(row, "can_create_database")),
        can_create_role=_exact_bool(_required(row, "can_create_role")),
        can_replicate=_exact_bool(_required(row, "can_replicate")),
        bypasses_rls=_exact_bool(_required(row, "bypasses_rls")),
        connection_limit=_exact_int(_required(row, "connection_limit")),
        valid_until=_optional_datetime(_required(row, "valid_until")),
        config=_normalize_role_config(_required(row, "config")),
    )


def _parse_membership(row: _CatalogRow) -> RoleMembershipTuple:
    """把一条 ``pg_auth_members`` mapping 完整收窄为不可变 tuple。"""
    return RoleMembershipTuple(
        role_oid=_positive_int(_required(row, "role_oid")),
        member_oid=_positive_int(_required(row, "member_oid")),
        grantor_oid=_positive_int(_required(row, "grantor_oid")),
        admin_option=_exact_bool(_required(row, "admin_option")),
    )


def _parse_acl_tuple(row: _CatalogRow) -> DatabaseAclTuple:
    """把 canonical ``aclexplode`` mapping 收窄为未经推断的 ACL tuple。"""
    return DatabaseAclTuple(
        grantee_oid=_nonnegative_int(_required(row, "grantee")),
        grantor_oid=_positive_int(_required(row, "grantor")),
        privilege_type=_exact_text(_required(row, "privilege_type")),
        is_grantable=_exact_bool(_required(row, "is_grantable")),
    )


def _owner_from_rows(
    target_database_oid: int,
    owner_rows: Sequence[_CatalogRow],
) -> int:
    """解析唯一 target/owner 行，使 reader 能在后续 SQL 前 fail closed。"""
    if len(owner_rows) != 1:
        _fail()
    owner_row = owner_rows[0]
    observed_target_oid = _positive_int(_required(owner_row, "target_database_oid"))
    owner_oid = _positive_int(_required(owner_row, "owner_oid"))
    if observed_target_oid != target_database_oid:
        _fail()
    return owner_oid


def _roles_from_rows(
    owner_oid: int,
    role_rows: Sequence[_CatalogRow],
) -> tuple[RuntimeRoleSnapshot | None, RuntimeRoleSnapshot | None]:
    """解析两角色公开 posture，并在 ACL 查询前拒绝重复/owner 冲突。"""
    roles_by_name: dict[str, RuntimeRoleSnapshot] = {}
    for role_row in role_rows:
        role = _parse_runtime_role(role_row)
        if role.role_name not in {APP_RUNTIME_ROLE_NAME, RETENTION_RUNTIME_ROLE_NAME}:
            _fail()
        if role.role_name in roles_by_name:
            _fail()
        roles_by_name[role.role_name] = role
    if len({role.role_oid for role in roles_by_name.values()}) != len(roles_by_name):
        _fail()

    app_role = roles_by_name.get(APP_RUNTIME_ROLE_NAME)
    retention_role = roles_by_name.get(RETENTION_RUNTIME_ROLE_NAME)
    for role in roles_by_name.values():
        if role.role_oid == owner_oid:
            _fail()
    return app_role, retention_role


def _acl_from_rows(
    *,
    owner_oid: int,
    app_role: RuntimeRoleSnapshot | None,
    retention_role: RuntimeRoleSnapshot | None,
    acl_rows: Sequence[_CatalogRow],
) -> tuple[DatabaseAclProfile, tuple[DatabaseAclTuple, ...]]:
    """解析完整 database ACL，并在 membership 查询前冻结唯一 profile。"""
    acl_tuples = tuple(sorted(_parse_acl_tuple(row) for row in acl_rows))
    acl_profile = classify_database_acl_profile(
        owner_oid=owner_oid,
        app_role_oid=None if app_role is None else app_role.role_oid,
        retention_role_oid=None if retention_role is None else retention_role.role_oid,
        acl_tuples=acl_tuples,
    )
    return acl_profile, acl_tuples


def _memberships_from_rows(
    *,
    app_role: RuntimeRoleSnapshot | None,
    retention_role: RuntimeRoleSnapshot | None,
    membership_rows: Sequence[_CatalogRow],
) -> tuple[RoleMembershipTuple, ...]:
    """解析涉及 runtime roles 的双向 membership 完整 multiset。"""
    memberships = tuple(sorted(_parse_membership(row) for row in membership_rows))
    if len(set(memberships)) != len(memberships):
        _fail()
    runtime_oids = {role.role_oid for role in (app_role, retention_role) if role is not None}
    if any(
        membership.role_oid not in runtime_oids and membership.member_oid not in runtime_oids
        for membership in memberships
    ):
        _fail()
    return memberships


def _snapshot_from_parts(
    *,
    target_database_oid: int,
    owner_oid: int,
    acl_profile: DatabaseAclProfile,
    acl_tuples: tuple[DatabaseAclTuple, ...],
    app_role: RuntimeRoleSnapshot | None,
    retention_role: RuntimeRoleSnapshot | None,
    memberships: tuple[RoleMembershipTuple, ...],
) -> DatabaseAccessSnapshot:
    """把两个 reader 共用的已验证阶段结果组合为不可变 snapshot。"""

    return DatabaseAccessSnapshot(
        target_database_oid=target_database_oid,
        owner_oid=owner_oid,
        acl_profile=acl_profile,
        acl_tuples=acl_tuples,
        app_role=app_role,
        retention_role=retention_role,
        memberships=memberships,
    )


async def read_database_access_snapshot(
    connection: AsyncConnection,
    *,
    target_database_oid: int,
) -> DatabaseAccessSnapshot:
    """从同一异步连接读取目标数据库的 canonical access snapshot。

    函数只调用 ``AsyncConnection.execute(text(SQL), dict_params)`` 并消费其 buffered
    ``Result``。它不显式调用 ``begin``/``commit``；若连接当前没有事务，SQLAlchemy
    ``execute`` 会 autobegin，事务生命周期仍由调用方管理。函数不读取密码、不执行
    DDL/DML，也不获取 lifecycle lock；所有 ACL 与 membership 都保留完整排序 multiset。

    Args:
        connection: 调用方持有的 SQLAlchemy 2 ``AsyncConnection``。
        target_database_oid: 已由 lifecycle 边界确定的目标 database OID。

    Returns:
        经过明确类型规范化且 ACL profile 已闭集分类的不可变快照。

    Raises:
        DatabaseAccessInvariantError: 目标/owner 不唯一、角色或 membership 重复、catalog
            类型未知，或完整 ACL 无法匹配四个冻结 profile。
    """
    validated_target_oid = _positive_int(target_database_oid)
    target_parameters = {"target_database_oid": validated_target_oid}
    role_parameters = {
        "app_role_name": APP_RUNTIME_ROLE_NAME,
        "retention_role_name": RETENTION_RUNTIME_ROLE_NAME,
    }
    owner_result = await connection.execute(text(_DATABASE_OWNER_SQL), target_parameters)
    owner_oid = _owner_from_rows(validated_target_oid, owner_result.mappings().all())
    role_result = await connection.execute(text(_RUNTIME_ROLES_SQL), role_parameters)
    app_role, retention_role = _roles_from_rows(owner_oid, role_result.mappings().all())
    acl_result = await connection.execute(text(DATABASE_ACL_SQL), target_parameters)
    acl_profile, acl_tuples = _acl_from_rows(
        owner_oid=owner_oid,
        app_role=app_role,
        retention_role=retention_role,
        acl_rows=acl_result.mappings().all(),
    )
    membership_result = await connection.execute(text(_ROLE_MEMBERSHIPS_SQL), role_parameters)
    memberships = _memberships_from_rows(
        app_role=app_role,
        retention_role=retention_role,
        membership_rows=membership_result.mappings().all(),
    )
    return _snapshot_from_parts(
        target_database_oid=validated_target_oid,
        owner_oid=owner_oid,
        acl_profile=acl_profile,
        acl_tuples=acl_tuples,
        app_role=app_role,
        retention_role=retention_role,
        memberships=memberships,
    )


def read_database_access_snapshot_sync(
    connection: Connection,
    *,
    target_database_oid: int,
) -> DatabaseAccessSnapshot:
    """从 maintenance/Alembic 同步连接读取与 async reader 完全相同的 snapshot。

    同步入口只为共享同一 Alembic owner connection 而存在；SQL 常量、行 parser、四 profile
    inventory 与 safe-role/membership 规则全部由本模块唯一实现，不能在 lifecycle 编排层
    复制。函数不执行 mutation，也不管理 transaction 或 advisory lock。

    Args:
        connection: 调用方已持 target lock 的同步 SQLAlchemy ``Connection``。
        target_database_oid: 已验证的目标 database OID。

    Returns:
        与 :func:`read_database_access_snapshot` 相同的不可变 snapshot。

    Raises:
        DatabaseAccessInvariantError: 输入或任一 catalog 事实不满足冻结闭集。
    """
    validated_target_oid = _positive_int(target_database_oid)
    target_parameters = {"target_database_oid": validated_target_oid}
    role_parameters = {
        "app_role_name": APP_RUNTIME_ROLE_NAME,
        "retention_role_name": RETENTION_RUNTIME_ROLE_NAME,
    }
    owner_result = connection.execute(text(_DATABASE_OWNER_SQL), target_parameters)
    owner_oid = _owner_from_rows(validated_target_oid, owner_result.mappings().all())
    role_result = connection.execute(text(_RUNTIME_ROLES_SQL), role_parameters)
    app_role, retention_role = _roles_from_rows(owner_oid, role_result.mappings().all())
    acl_result = connection.execute(text(DATABASE_ACL_SQL), target_parameters)
    acl_profile, acl_tuples = _acl_from_rows(
        owner_oid=owner_oid,
        app_role=app_role,
        retention_role=retention_role,
        acl_rows=acl_result.mappings().all(),
    )
    membership_result = connection.execute(text(_ROLE_MEMBERSHIPS_SQL), role_parameters)
    memberships = _memberships_from_rows(
        app_role=app_role,
        retention_role=retention_role,
        membership_rows=membership_result.mappings().all(),
    )
    return _snapshot_from_parts(
        target_database_oid=validated_target_oid,
        owner_oid=owner_oid,
        acl_profile=acl_profile,
        acl_tuples=acl_tuples,
        app_role=app_role,
        retention_role=retention_role,
        memberships=memberships,
    )


def _validate_role_identity(
    role: RuntimeRoleSnapshot,
    *,
    expected_name: str,
    owner_oid: int,
) -> None:
    """验证角色类型、精确名称、正 OID 与 owner 隔离。"""
    if type(role) is not RuntimeRoleSnapshot:
        _fail()
    if role.role_name != expected_name:
        _fail()
    role_oid = _positive_int(role.role_oid)
    if role_oid == owner_oid:
        _fail()


def _validate_safe_role(role: RuntimeRoleSnapshot) -> None:
    """验证单个 runtime role 精确满足冻结 safe posture。"""
    safe = (
        type(role.can_login) is bool
        and role.can_login is True
        and type(role.inherits) is bool
        and role.inherits is True
        and type(role.is_superuser) is bool
        and role.is_superuser is False
        and type(role.can_create_database) is bool
        and role.can_create_database is False
        and type(role.can_create_role) is bool
        and role.can_create_role is False
        and type(role.can_replicate) is bool
        and role.can_replicate is False
        and type(role.bypasses_rls) is bool
        and role.bypasses_rls is False
        and type(role.connection_limit) is int
        and role.connection_limit == -1
        and role.valid_until is None
        and role.config is None
    )
    if not safe:
        _fail()


def _validate_snapshot_acl(snapshot: DatabaseAccessSnapshot) -> DatabaseAclProfile:
    """重算 snapshot 的完整 ACL profile，并拒绝声明值或 canonical 顺序不一致。"""
    if type(snapshot) is not DatabaseAccessSnapshot:
        _fail()
    _positive_int(snapshot.target_database_oid)
    owner_oid = _positive_int(snapshot.owner_oid)
    if type(snapshot.acl_profile) is not DatabaseAclProfile:
        _fail()
    if type(snapshot.acl_tuples) is not tuple:
        _fail()
    if any(type(acl_tuple) is not DatabaseAclTuple for acl_tuple in snapshot.acl_tuples):
        _fail()

    app_role = snapshot.app_role
    retention_role = snapshot.retention_role
    if app_role is not None:
        _validate_role_identity(
            app_role,
            expected_name=APP_RUNTIME_ROLE_NAME,
            owner_oid=owner_oid,
        )
    if retention_role is not None:
        _validate_role_identity(
            retention_role,
            expected_name=RETENTION_RUNTIME_ROLE_NAME,
            owner_oid=owner_oid,
        )
    present_role_oids = tuple(
        role.role_oid for role in (app_role, retention_role) if role is not None
    )
    if len(set(present_role_oids)) != len(present_role_oids):
        _fail()

    actual_profile = classify_database_acl_profile(
        owner_oid=owner_oid,
        app_role_oid=None if app_role is None else app_role.role_oid,
        retention_role_oid=None if retention_role is None else retention_role.role_oid,
        acl_tuples=snapshot.acl_tuples,
    )
    # classifier 已逐字段收窄全部 tuple；只有此后排序才不会泄漏原生比较异常。
    if snapshot.acl_tuples != tuple(sorted(snapshot.acl_tuples)):
        _fail()
    if actual_profile is not snapshot.acl_profile:
        _fail()
    return actual_profile


def _safe_role_pair(
    snapshot: DatabaseAccessSnapshot,
) -> tuple[RuntimeRoleSnapshot, RuntimeRoleSnapshot]:
    """返回同时存在且安全的 app/retention pair；partial/mixed 立即拒绝。"""
    app_role = snapshot.app_role
    retention_role = snapshot.retention_role
    if app_role is None or retention_role is None:
        _fail()
    _validate_safe_role(app_role)
    _validate_safe_role(retention_role)
    return app_role, retention_role


def _require_zero_memberships(snapshot: DatabaseAccessSnapshot) -> None:
    """要求 canonical membership multiset 精确为空。"""
    if type(snapshot.memberships) is not tuple or snapshot.memberships != ():
        _fail()


def _classify_candidate_snapshot(
    snapshot: DatabaseAccessSnapshot,
) -> tuple[DatabaseAclProfile, bool]:
    """只根据 Candidate 内部快照重验合法 source profile 与 role pair。"""
    profile = _validate_snapshot_acl(snapshot)
    _require_zero_memberships(snapshot)
    if profile is DatabaseAclProfile.FRESH_DEFAULT:
        if snapshot.app_role is None and snapshot.retention_role is None:
            return profile, False
        _safe_role_pair(snapshot)
        return profile, True
    if profile is DatabaseAclProfile.PRE_PROTOCOL_LEGACY:
        _safe_role_pair(snapshot)
        return profile, True
    _fail()


def classify_bootstrap_candidate(
    snapshot: DatabaseAccessSnapshot,
    *,
    caller: BootstrapCaller,
    restore_facts_absent: bool,
    has_active_non_owner_sessions: bool,
    object_grants_match: bool,
) -> BootstrapCandidate:
    """在任何 mutation plan 前分类唯一合法的瞬态 bootstrap candidate。

    这是 Cycle 2 唯一接收外部闭集证据的边界。所有 bool 都要求精确 ``bool``；裸字符串
    caller、任一 restore fact、活动非 owner session 或 object-grant drift 均 fail closed。
    返回 Candidate 仍不是执行 authority，Cycle 4 必须在锁内重新读取这些事实。

    Args:
        snapshot: canonical reader 或合成测试提供的完整 access snapshot。
        caller: 仅允许 standalone role-bootstrap 或 reset post-create 枚举值。
        restore_facts_absent: 三项权威 restore fact 是否全部 absent。
        has_active_non_owner_sessions: 当前是否仍存在非 owner 活动 session。
        object_grants_match: phase-aware object-grant verifier 的闭集结果。

    Returns:
        仅含 source profile、角色存在形状与原始快照的瞬态 Candidate。

    Raises:
        DatabaseAccessInvariantError: caller/外部证据不安全，或 ACL、角色、membership
            不是三个冻结 candidate 形状之一。
    """
    if type(caller) is not BootstrapCaller:
        _fail()
    if (
        type(restore_facts_absent) is not bool
        or type(has_active_non_owner_sessions) is not bool
        or type(object_grants_match) is not bool
    ):
        _fail()
    if not restore_facts_absent or has_active_non_owner_sessions or not object_grants_match:
        _fail()
    source_profile, roles_exist = _classify_candidate_snapshot(snapshot)
    return BootstrapCandidate(
        caller=caller,
        source_profile=source_profile,
        roles_exist=roles_exist,
        snapshot=snapshot,
    )


def bootstrap_candidate_to_baseline(
    candidate: BootstrapCandidate,
) -> DatabaseAccessMutationPlan:
    """把已分类 Candidate 转为唯一 candidate→baseline 纯计划。

    函数会重验 Candidate 内部 snapshot、caller、source profile 与 ``roles_exist``，防止
    手工构造的矛盾 dataclass 直接产生计划。外部 restore/session/object-grant evidence
    不在 Candidate 中持久化，Cycle 4 必须在实际 mutation 前重新读取。

    Args:
        candidate: :func:`classify_bootstrap_candidate` 返回的瞬态分类结果。

    Returns:
        同时创建两个 safe role，或仅同时轮换两个既有 safe role 密码，并把 ACL 转为
        baseline 的闭集计划。

    Raises:
        DatabaseAccessInvariantError: Candidate 被伪造、内部事实已漂移或 source 不合法。
    """
    if type(candidate) is not BootstrapCandidate:
        _fail()
    if type(candidate.caller) is not BootstrapCaller:
        _fail()
    if type(candidate.source_profile) is not DatabaseAclProfile:
        _fail()
    if type(candidate.roles_exist) is not bool:
        _fail()
    source_profile, roles_exist = _classify_candidate_snapshot(candidate.snapshot)
    if source_profile is not candidate.source_profile or roles_exist is not candidate.roles_exist:
        _fail()
    role_action = (
        RuntimeRoleAction.ROTATE_BOTH_PASSWORDS
        if roles_exist
        else RuntimeRoleAction.CREATE_BOTH_SAFE
    )
    return DatabaseAccessMutationPlan(
        source_profile=source_profile,
        target_profile=DatabaseAclProfile.BASELINE,
        role_action=role_action,
        acl_action=DatabaseAclAction.CANDIDATE_TO_BASELINE,
    )


def _validated_role_password(value: object) -> str:
    """验证只会作为 bind parameter 发送的单个 runtime-role Secret。

    空值或 NUL 无法形成可接受的 PostgreSQL password literal；不可编码的孤立代理项也在
    首条 helper DDL 前拒绝。错误不包含 Secret 内容，调用方可安全记录稳定错误类型。
    """
    password = _exact_text(value)
    if not password or "\0" in password:
        _fail()
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        _fail()
    return password


def _validate_candidate_to_baseline_plan(
    plan: object,
) -> DatabaseAccessMutationPlan:
    """收窄 mutator 唯一接受的 candidate→baseline plan 形状。

    ``fresh_default`` 可对应两角色缺失时的同时创建，或两角色已安全时的同时轮换；
    ``pre_protocol_legacy`` 已含两角色，因此只允许轮换。任何 steady transition、裸字符串
    enum 或手工拼装的矛盾组合都必须在首条 GRANT/REVOKE 前拒绝。
    """
    if type(plan) is not DatabaseAccessMutationPlan:
        _fail()
    if (
        type(plan.source_profile) is not DatabaseAclProfile
        or type(plan.target_profile) is not DatabaseAclProfile
        or type(plan.role_action) is not RuntimeRoleAction
        or type(plan.acl_action) is not DatabaseAclAction
    ):
        _fail()
    if (
        plan.target_profile is not DatabaseAclProfile.BASELINE
        or plan.acl_action is not DatabaseAclAction.CANDIDATE_TO_BASELINE
    ):
        _fail()
    if plan.source_profile is DatabaseAclProfile.FRESH_DEFAULT:
        if plan.role_action not in {
            RuntimeRoleAction.CREATE_BOTH_SAFE,
            RuntimeRoleAction.ROTATE_BOTH_PASSWORDS,
        }:
            _fail()
    elif plan.source_profile is DatabaseAclProfile.PRE_PROTOCOL_LEGACY:
        if plan.role_action is not RuntimeRoleAction.ROTATE_BOTH_PASSWORDS:
            _fail()
    else:
        _fail()
    return plan


def apply_safe_runtime_roles(
    connection: Connection,
    candidate: BootstrapCandidate,
    *,
    app_password: str,
    retention_password: str,
) -> None:
    """在调用方 owner transaction 内同时创建或轮换两个 safe runtime role。

    函数首先通过纯 planner 重验 Candidate，再在任何 SQL 前验证两个 Secret。密码始终只
    作为 bind parameter 传给 ``pg_temp`` server-side helper；角色名来自冻结常量。新建
    路径逐项声明全部 safe attributes，既有路径只轮换密码，绝不把 unsafe role 修复成
    safe，也不自行提交或管理 lifecycle lock。

    Args:
        connection: 已持三把锁且处于唯一 owner transaction 的同步 PostgreSQL 连接。
        candidate: 当前持锁调用内刚分类、仍需再次重验的瞬态 Candidate。
        app_password: app runtime role 的非空、无 NUL Secret。
        retention_password: retention runtime role 的非空、无 NUL Secret。

    Raises:
        DatabaseAccessInvariantError: Candidate、计划或任一 Secret 不满足冻结闭集。
        sqlalchemy.exc.SQLAlchemyError: helper 或角色 mutation 失败时原样向上传播，由外层
            transaction 统一回滚，避免只留下一个角色的部分结果。
    """
    plan = _validate_candidate_to_baseline_plan(bootstrap_candidate_to_baseline(candidate))
    validated_app_password = _validated_role_password(app_password)
    validated_retention_password = _validated_role_password(retention_password)
    if plan.role_action is RuntimeRoleAction.CREATE_BOTH_SAFE:
        create_role = True
    elif plan.role_action is RuntimeRoleAction.ROTATE_BOTH_PASSWORDS:
        create_role = False
    else:
        _fail()

    # 临时 helper 固定 identifier 与属性闭集；Secret 只出现在后续 bind 参数中，绝不
    # 拼入客户端 SQL 文本。两个调用必须依赖外层同一 transaction 保持原子性。
    connection.execute(text(_RUNTIME_ROLE_MUTATOR_SQL))
    for role_name, role_password in (
        (APP_RUNTIME_ROLE_NAME, validated_app_password),
        (RETENTION_RUNTIME_ROLE_NAME, validated_retention_password),
    ):
        connection.execute(
            text(_APPLY_RUNTIME_ROLE_SQL),
            {
                "role_name": role_name,
                "role_password": role_password,
                "create_role": create_role,
            },
        )


def rotate_safe_runtime_role_passwords(
    connection: Connection,
    snapshot: DatabaseAccessSnapshot,
    *,
    app_password: str,
    retention_password: str,
) -> None:
    """在 exact baseline steady posture 上同时轮换两个既有角色密码。

    该入口只补足 standalone ``role-bootstrap`` 的幂等 steady-idle 分支，不接受
    Candidate、active posture 或任意修复计划。snapshot、双 safe role、zero membership 与
    两个 Secret 都在首条 helper SQL 前验证；角色属性、ACL 和 membership 均不会修改。

    Args:
        connection: 已持 management/target/schema locks 的唯一 owner transaction 连接。
        snapshot: 同一 transaction 内重读且精确为 baseline 的 access snapshot。
        app_password: app runtime role 的非空、无 NUL Secret。
        retention_password: retention runtime role 的非空、无 NUL Secret。

    Raises:
        DatabaseAccessInvariantError: steady posture 或任一 Secret 不满足冻结闭集。
        sqlalchemy.exc.SQLAlchemyError: helper/ALTER ROLE 失败时由外层 transaction 回滚。
    """
    _validate_steady_snapshot(snapshot, expected_profile=DatabaseAclProfile.BASELINE)
    validated_app_password = _validated_role_password(app_password)
    validated_retention_password = _validated_role_password(retention_password)

    connection.execute(text(_RUNTIME_ROLE_MUTATOR_SQL))
    for role_name, role_password in (
        (APP_RUNTIME_ROLE_NAME, validated_app_password),
        (RETENTION_RUNTIME_ROLE_NAME, validated_retention_password),
    ):
        connection.execute(
            text(_APPLY_RUNTIME_ROLE_SQL),
            {
                "role_name": role_name,
                "role_password": role_password,
                "create_role": False,
            },
        )


def _validated_database_name(value: object) -> str:
    """验证将由 PostgreSQL dialect 强制引用的 exact database name。"""
    database_name = _exact_text(value)
    if not database_name or "\0" in database_name:
        _fail()
    try:
        encoded_name = database_name.encode("utf-8")
    except UnicodeEncodeError:
        _fail()
    if len(encoded_name) > 63:
        _fail()
    return database_name


def apply_database_acl_mutation(
    connection: Connection,
    *,
    target_database_name: str,
    plan: DatabaseAccessMutationPlan,
) -> None:
    """按 exact candidate→baseline plan 应用三条固定 database ACL mutation。

    本入口不是通用 ACL repair API。它只撤销 ``PUBLIC`` 的全部 database privilege，并
    以 owner/non-grantable 语义分别授予 app 与 retention ``CONNECT``。计划与目标名在
    首条 SQL 前完整验证；目标数据库及两个固定角色都由 PostgreSQL dialect 强制引用，
    因而数据库名中的双引号不会改变 SQL 结构。

    Args:
        connection: 与角色 mutation、object grants 共用的 owner transaction 连接。
        target_database_name: 生命周期已验证且与 target identity 绑定的 exact 名称。
        plan: 纯 planner 返回的 candidate→baseline 闭集计划。

    Raises:
        DatabaseAccessInvariantError: 名称或 plan 不是唯一受支持的闭集形状。
        sqlalchemy.exc.SQLAlchemyError: 任一 ACL SQL 失败时原样向上传播，由外层回滚。
    """
    _validate_candidate_to_baseline_plan(plan)
    database_name = _validated_database_name(target_database_name)
    preparer = connection.dialect.identifier_preparer
    quoted_database = preparer.quote_identifier(database_name)
    quoted_app_role = preparer.quote_identifier(APP_RUNTIME_ROLE_NAME)
    quoted_retention_role = preparer.quote_identifier(RETENTION_RUNTIME_ROLE_NAME)

    statements = (
        f"REVOKE ALL PRIVILEGES ON DATABASE {quoted_database} FROM PUBLIC",
        f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_app_role}",
        f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_retention_role}",
    )
    for statement in statements:
        connection.execute(text(statement))


def _validate_steady_snapshot(
    snapshot: DatabaseAccessSnapshot,
    *,
    expected_profile: DatabaseAclProfile,
) -> None:
    """验证 steady transition 的 exact ACL、两 safe role 与 zero membership。"""
    actual_profile = _validate_snapshot_acl(snapshot)
    if actual_profile is not expected_profile:
        _fail()
    _safe_role_pair(snapshot)
    _require_zero_memberships(snapshot)


def plan_baseline_connect_reassertion(
    snapshot: DatabaseAccessSnapshot,
) -> DatabaseAccessMutationPlan:
    """为 exact baseline steady posture 返回唯一 CONNECT 幂等重申计划。

    该计划不是 drift repair：完整 ACL 必须已经精确包含 owner 与两个 runtime role 的
    non-grantable tuples，两个角色必须安全且无任意方向 membership。计划只表达 baseline
    自环，不能撤销 ``PUBLIC``、改变 role 属性或进入 active/restore transition。

    Args:
        snapshot: 同一 owner transaction 内重读的完整 database access snapshot。

    Returns:
        source/target 均为 baseline、角色动作固定为 ``NONE`` 的窄自环计划。

    Raises:
        DatabaseAccessInvariantError: ACL、角色或 membership 不是 exact baseline posture。
    """
    _validate_steady_snapshot(snapshot, expected_profile=DatabaseAclProfile.BASELINE)
    return DatabaseAccessMutationPlan(
        source_profile=DatabaseAclProfile.BASELINE,
        target_profile=DatabaseAclProfile.BASELINE,
        role_action=RuntimeRoleAction.NONE,
        acl_action=DatabaseAclAction.BASELINE_CONNECT_REASSERT,
    )


def apply_baseline_connect_reassertion(
    connection: Connection,
    *,
    target_database_name: str,
    snapshot: DatabaseAccessSnapshot,
) -> None:
    """在 exact baseline 上逐条幂等重申 app/retention ``CONNECT``。

    函数先通过纯 planner 完整验证 snapshot，再验证并引用 exact database name；因此
    active、unsafe role、membership 或任意 ACL 漂移都会在首条 SQL 前拒绝。它只发送
    两条 owner 执行的 ``GRANT CONNECT``，不撤销 ``PUBLIC``、不使用 ``ALL``，也不修改
    role、default privilege 或 restore authority。

    Args:
        connection: 已持 management/target/schema locks 的 owner transaction 连接。
        target_database_name: 与当前 target identity 绑定的逐字数据库名。
        snapshot: 当前事务内已重读的 exact baseline access snapshot。

    Raises:
        DatabaseAccessInvariantError: 名称或 steady snapshot 不满足冻结闭集。
        sqlalchemy.exc.SQLAlchemyError: 任一精确 GRANT 失败时由外层事务整体回滚。
    """
    plan = plan_baseline_connect_reassertion(snapshot)
    if (
        plan.source_profile is not DatabaseAclProfile.BASELINE
        or plan.target_profile is not DatabaseAclProfile.BASELINE
        or plan.role_action is not RuntimeRoleAction.NONE
        or plan.acl_action is not DatabaseAclAction.BASELINE_CONNECT_REASSERT
    ):
        _fail()
    database_name = _validated_database_name(target_database_name)
    preparer = connection.dialect.identifier_preparer
    quoted_database = preparer.quote_identifier(database_name)
    for role_name in (APP_RUNTIME_ROLE_NAME, RETENTION_RUNTIME_ROLE_NAME):
        quoted_role = preparer.quote_identifier(role_name)
        connection.execute(text(f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_role}"))


def transition_baseline_to_active(
    snapshot: DatabaseAccessSnapshot,
) -> DatabaseAccessMutationPlan:
    """为 exact baseline posture 返回唯一 baseline→active ACL 计划。

    本函数只验证 access snapshot，不接收 restore facts、session 或 object-grant bool；这些
    phase-aware authority 属于后续 lifecycle。计划仅表示移除 app/retention CONNECT，owner
    三条 tuple 保持不变，绝不执行 SQL。

    Args:
        snapshot: 声明且重算后均精确为 baseline 的完整 access snapshot。

    Returns:
        不修改 role 属性、只执行闭集 baseline→active ACL 动作的纯计划。

    Raises:
        DatabaseAccessInvariantError: ACL、角色或 membership 不是 exact baseline posture。
    """
    _validate_steady_snapshot(snapshot, expected_profile=DatabaseAclProfile.BASELINE)
    return DatabaseAccessMutationPlan(
        source_profile=DatabaseAclProfile.BASELINE,
        target_profile=DatabaseAclProfile.ACTIVE,
        role_action=RuntimeRoleAction.NONE,
        acl_action=DatabaseAclAction.BASELINE_TO_ACTIVE,
    )


def transition_active_to_baseline(
    snapshot: DatabaseAccessSnapshot,
) -> DatabaseAccessMutationPlan:
    """为 exact active posture 返回唯一 active→baseline ACL 计划。

    matching restore 正常会携带 active restore facts，因此本函数不能硬编码 facts absent；
    higher layer 必须完成 phase-aware admission。本计划只表示以 owner/non-grantable 方式恢复
    app/retention CONNECT，owner tuple 保持不变，绝不执行 SQL。

    Args:
        snapshot: 声明且重算后均精确为 active 的完整 access snapshot。

    Returns:
        不修改 role 属性、只执行闭集 active→baseline ACL 动作的纯计划。

    Raises:
        DatabaseAccessInvariantError: ACL、角色或 membership 不是 exact active posture。
    """
    _validate_steady_snapshot(snapshot, expected_profile=DatabaseAclProfile.ACTIVE)
    return DatabaseAccessMutationPlan(
        source_profile=DatabaseAclProfile.ACTIVE,
        target_profile=DatabaseAclProfile.BASELINE,
        role_action=RuntimeRoleAction.NONE,
        acl_action=DatabaseAclAction.ACTIVE_TO_BASELINE,
    )
