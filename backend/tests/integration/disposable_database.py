"""提供 disposable PostgreSQL 集成测试目标的 provenance-bound 清理租约。

本模块只属于测试基础设施，不进入产品代码。四个会创建 cluster-wide fixed runtime
roles 的集成路径必须共享同一 test-only session advisory lock，并把最初确认 absent、
创建后 OID 与 teardown 时的精确 catalog 事实绑定到同一个 live management session。
任何 provenance、锁、session 或 cluster 漂移都在首个 ``DROP`` 前拒绝。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from time import monotonic, sleep
from typing import NoReturn, Self, cast

from sqlalchemy import Connection, Engine, text
from sqlalchemy.engine import Result
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.infrastructure.db.database_access import (
    APP_RUNTIME_ROLE_NAME,
    RETENTION_RUNTIME_ROLE_NAME,
)
from ai_employee.infrastructure.db.database_maintenance import (
    DatabaseMaintenanceContext,
    ManagementLifecycleLease,
    TargetMaintenanceLease,
)

DISPOSABLE_DATABASE_CLEANUP_LOCK = (20260812, 316)
INTEGRATION_SUITE_LOCK = (20260812, 317)

_DISPOSABLE_DATABASE_NAME = re.compile(
    r"^ai_employee_(?:mig|retention|reset|suite)_[0-9a-f]{32}_test$"
)
_ROLE_NAMES = (APP_RUNTIME_ROLE_NAME, RETENTION_RUNTIME_ROLE_NAME)
_ERROR_MESSAGE = "disposable database cleanup provenance violation"

_SESSION_IDENTITY_SQL = """SELECT
    pg_backend_pid() AS backend_pid,
    control.system_identifier AS system_identifier,
    current_database() AS management_database_name
FROM pg_control_system() AS control"""

_TRY_CLEANUP_LOCK_SQL = """SELECT pg_try_advisory_lock(
    :lock_class,
    :lock_object
) AS acquired"""

_CLEANUP_LOCK_OWNERSHIP_SQL = """SELECT count(*)
FROM pg_locks AS held_lock
WHERE held_lock.locktype = 'advisory'
  AND held_lock.pid = pg_backend_pid()
  AND held_lock.classid = :lock_class
  AND held_lock.objid = :lock_object
  AND held_lock.objsubid = 2
  AND held_lock.granted"""

_UNLOCK_CLEANUP_SQL = """SELECT pg_advisory_unlock(
    :lock_class,
    :lock_object
) AS released"""

_DATABASE_ROWS_SQL = """SELECT
    target_database.oid AS target_database_oid,
    target_database.datname AS target_database_name
FROM pg_database AS target_database
WHERE target_database.datname = :target_database_name
ORDER BY target_database.oid"""

_RUNTIME_ROLE_ROWS_SQL = """SELECT
    runtime_role.oid AS role_oid,
    runtime_role.rolname AS role_name,
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
WHERE runtime_role.rolname = ANY(:role_names)
ORDER BY runtime_role.rolname"""

_CURRENT_ROLE_CAN_CREATE_DATABASE_SQL = (
    "SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user"
)

_DATABASE_SESSION_COUNT_SQL = """SELECT
    count(*) AS database_sessions,
    count(*) FILTER (
        WHERE activity.backend_type = 'autovacuum worker'
    ) AS autovacuum_sessions
FROM pg_stat_activity AS activity
WHERE activity.datid = :target_database_oid"""

_DATABASE_BACKEND_TYPES_SQL = """SELECT activity.backend_type AS backend_type
FROM pg_stat_activity AS activity
WHERE activity.datid = :target_database_oid"""

_ROLE_SESSION_COUNT_SQL = """SELECT count(*)
FROM pg_stat_activity AS activity
WHERE activity.usesysid = ANY(:role_oids)"""

_ROLE_MEMBERSHIP_COUNT_SQL = """SELECT count(*)
FROM pg_auth_members AS membership
WHERE membership.roleid = ANY(:role_oids)
   OR membership.member = ANY(:role_oids)"""

_ROLE_SETTING_COUNT_SQL = """SELECT count(*)
FROM pg_db_role_setting AS role_setting
WHERE role_setting.setrole = ANY(:role_oids)"""

_ROLE_DATABASE_OWNERSHIP_COUNT_SQL = """SELECT count(*)
FROM pg_database AS target_database
WHERE target_database.datdba = ANY(:role_oids)"""

_ROLE_DATABASE_ACL_COUNT_SQL = """SELECT count(*)
FROM pg_database AS target_database
CROSS JOIN LATERAL aclexplode(
    COALESCE(target_database.datacl, acldefault('d', target_database.datdba))
) AS acl
WHERE acl.grantee = ANY(:role_oids)
   OR acl.grantor = ANY(:role_oids)"""

_ROLE_SHARED_DEPENDENCY_COUNT_SQL = """SELECT count(*)
FROM pg_shdepend AS dependency
WHERE dependency.refclassid = 'pg_authid'::regclass
  AND dependency.refobjid = ANY(:role_oids)"""


class DisposableDatabaseCleanupError(AssertionError):
    """表示 disposable cleanup authority 不再能证明对象来源。"""


def _fail() -> NoReturn:
    """抛出不含数据库名、OID 或 catalog 内容的稳定安全错误。"""
    raise DisposableDatabaseCleanupError(_ERROR_MESSAGE) from None


def _validated_database_name(value: object) -> str:
    """只接受四个已知 fixture 生成的 lowercase UUID 数据库名。"""
    if type(value) is not str or _DISPOSABLE_DATABASE_NAME.fullmatch(value) is None:
        _fail()
    return value


def _exact_mapping_rows(
    result: Result[tuple[object, ...]],
) -> tuple[Mapping[str, object], ...]:
    """把 SQLAlchemy mapping result 收窄为不可变行集合。"""
    try:
        rows = result.mappings().all()
    except (AttributeError, TypeError, ValueError):
        _fail()
    return tuple(cast(Mapping[str, object], dict(row)) for row in rows)


def _exact_scalar(result: Result[tuple[object, ...]]) -> object:
    """读取唯一 scalar；任何不完整 Result 表面都 fail closed。"""
    try:
        return result.scalar_one()
    except (AttributeError, TypeError, ValueError):
        _fail()


def _zero_count(value: object) -> None:
    """要求 catalog count 精确为整数零，禁止把 bool 当作 count。"""
    if type(value) is not int or value != 0:
        _fail()


@dataclass(frozen=True, slots=True)
class _SessionProvenance:
    """冻结最初取得 test-only advisory lock 的 management backend。"""

    backend_pid: int
    system_identifier: int


@dataclass(frozen=True, slots=True)
class _DatabaseProvenance:
    """冻结本轮 UUID database 的逐字名称与创建后 OID。"""

    database_name: str
    database_oid: int


@dataclass(frozen=True, order=True, slots=True)
class _RuntimeRoleProvenance:
    """冻结 fixed runtime role 的 OID 与完整安全姿态。"""

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
    valid_until: object | None
    config: object | None


def _read_session_provenance(connection: Connection) -> _SessionProvenance:
    """读取当前 backend、cluster 与固定 management database identity。"""
    if (
        getattr(connection, "closed", None) is not False
        or getattr(connection, "invalidated", None) is not False
    ):
        _fail()
    try:
        rows = _exact_mapping_rows(connection.execute(text(_SESSION_IDENTITY_SQL)))
    except DisposableDatabaseCleanupError:
        raise
    except SQLAlchemyError:
        # 驱动异常可能包含 endpoint/catalog 细节；测试边界只保留稳定错误。
        _fail()
    if len(rows) != 1:
        _fail()
    row = rows[0]
    backend_pid = row.get("backend_pid")
    system_identifier = row.get("system_identifier")
    management_database_name = row.get("management_database_name")
    if (
        type(backend_pid) is not int
        or backend_pid <= 0
        or type(system_identifier) is not int
        or system_identifier == 0
        or management_database_name != "postgres"
    ):
        _fail()
    return _SessionProvenance(
        backend_pid=backend_pid,
        system_identifier=system_identifier,
    )


def _read_database_rows(
    connection: Connection,
    *,
    database_name: str,
) -> tuple[Mapping[str, object], ...]:
    """按 exact name 读取零或一条 database catalog 行。"""
    try:
        return _exact_mapping_rows(
            connection.execute(
                text(_DATABASE_ROWS_SQL),
                {"target_database_name": database_name},
            )
        )
    except DisposableDatabaseCleanupError:
        raise
    except (SQLAlchemyError, TypeError, ValueError):
        # catalog/驱动细节可能包含 endpoint 或对象内容；stage recorder 只暴露固定不变量。
        _fail()


def _read_role_rows(connection: Connection) -> tuple[Mapping[str, object], ...]:
    """读取两个 fixed runtime role 的完整安全姿态。"""
    try:
        return _exact_mapping_rows(
            connection.execute(
                text(_RUNTIME_ROLE_ROWS_SQL),
                {"role_names": list(_ROLE_NAMES)},
            )
        )
    except DisposableDatabaseCleanupError:
        raise
    except (SQLAlchemyError, TypeError, ValueError):
        # 角色 recorder 失败时不得让驱动异常替换原 bootstrap cause 或产生删除权限。
        _fail()


def _parse_exact_runtime_roles(
    rows: tuple[Mapping[str, object], ...],
) -> tuple[_RuntimeRoleProvenance, ...]:
    """要求恰好两个名称/OID 唯一且安全姿态逐字段精确匹配的角色。"""
    if len(rows) != 2:
        _fail()
    parsed: list[_RuntimeRoleProvenance] = []
    for row in rows:
        role = _RuntimeRoleProvenance(
            role_name=row.get("role_name"),  # type: ignore[arg-type]
            role_oid=row.get("role_oid"),  # type: ignore[arg-type]
            can_login=row.get("can_login"),  # type: ignore[arg-type]
            inherits=row.get("inherits"),  # type: ignore[arg-type]
            is_superuser=row.get("is_superuser"),  # type: ignore[arg-type]
            can_create_database=row.get("can_create_database"),  # type: ignore[arg-type]
            can_create_role=row.get("can_create_role"),  # type: ignore[arg-type]
            can_replicate=row.get("can_replicate"),  # type: ignore[arg-type]
            bypasses_rls=row.get("bypasses_rls"),  # type: ignore[arg-type]
            connection_limit=row.get("connection_limit"),  # type: ignore[arg-type]
            valid_until=row.get("valid_until"),
            config=row.get("config"),
        )
        if (
            role.role_name not in _ROLE_NAMES
            or type(role.role_oid) is not int
            or role.role_oid <= 0
            or role.can_login is not True
            or role.inherits is not True
            or role.is_superuser is not False
            or role.can_create_database is not False
            or role.can_create_role is not False
            or role.can_replicate is not False
            or role.bypasses_rls is not False
            or type(role.connection_limit) is not int
            or role.connection_limit != -1
            or role.valid_until is not None
            or role.config is not None
        ):
            _fail()
        parsed.append(role)
    result = tuple(sorted(parsed))
    if (
        tuple(role.role_name for role in result) != _ROLE_NAMES
        or len({role.role_oid for role in result}) != 2
    ):
        _fail()
    return result


class DisposableDatabaseCleanupLease:
    """把 disposable 对象清理权绑定到同一 live management session。

    lease 自身不拥有 Engine；调用方必须最终调用 :meth:`release`，通常应通过
    :func:`managed_disposable_database_cleanup` 自动完成 cleanup、unlock 与 close。
    """

    def __init__(
        self,
        *,
        connection: Connection,
        database_name: str,
        quoted_database_name: str,
        quoted_role_names: Mapping[str, str],
        session: _SessionProvenance,
    ) -> None:
        self._connection = connection
        self._database_name = database_name
        self._quoted_database_name = quoted_database_name
        self._quoted_role_names = dict(quoted_role_names)
        self._session = session
        self._initial_absence_verified = False
        self._database: _DatabaseProvenance | None = None
        self._roles: tuple[_RuntimeRoleProvenance, ...] | None = None
        self._cleaned = False
        self._released = False

    @classmethod
    def acquire(
        cls,
        *,
        connection: Connection,
        database_name: str,
        quote_identifier: Callable[[str], str],
    ) -> Self:
        """先取得固定 test-only advisory lock，再发行 session-bound lease。

        Args:
            connection: 指向同一测试 cluster 固定 ``postgres`` 数据库的 live session。
            database_name: 四个已知 fixture 之一生成的 lowercase UUID 名称。
            quote_identifier: 当前 Engine dialect 的 PostgreSQL 标识符引用函数。

        Returns:
            已持固定 advisory lock、尚未读取角色 absence 的 cleanup lease。

        Raises:
            DisposableDatabaseCleanupError: session、名称、引用函数或锁不满足闭集。
        """
        validated_name = _validated_database_name(database_name)
        if not callable(quote_identifier):
            _fail()
        try:
            quoted_database_name = quote_identifier(validated_name)
            quoted_role_names = {
                role_name: quote_identifier(role_name) for role_name in _ROLE_NAMES
            }
        except (TypeError, ValueError):
            _fail()
        if type(quoted_database_name) is not str or any(
            type(value) is not str for value in quoted_role_names.values()
        ):
            _fail()
        session = _read_session_provenance(connection)
        parameters = {
            "lock_class": DISPOSABLE_DATABASE_CLEANUP_LOCK[0],
            "lock_object": DISPOSABLE_DATABASE_CLEANUP_LOCK[1],
        }
        acquired = False
        try:
            acquired_value = _exact_scalar(
                connection.execute(text(_TRY_CLEANUP_LOCK_SQL), parameters)
            )
            if acquired_value is not True:
                _fail()
            acquired = True
            lease = cls(
                connection=connection,
                database_name=validated_name,
                quoted_database_name=quoted_database_name,
                quoted_role_names=quoted_role_names,
                session=session,
            )
            lease._assert_guard()
            return lease
        except BaseException:
            if acquired:
                try:
                    connection.execute(text(_UNLOCK_CLEANUP_SQL), parameters)
                except SQLAlchemyError:
                    acquired = False
            raise

    @property
    def connection(self) -> Connection:
        """返回仍持 test-only lock 的同一 management session。"""
        return self._connection

    def _assert_guard(self) -> None:
        """重验 live backend、cluster identity 与 advisory lock 均未替换。"""
        if self._released:
            _fail()
        observed = _read_session_provenance(self._connection)
        if observed != self._session:
            _fail()
        try:
            lock_count = _exact_scalar(
                self._connection.execute(
                    text(_CLEANUP_LOCK_OWNERSHIP_SQL),
                    {
                        "lock_class": DISPOSABLE_DATABASE_CLEANUP_LOCK[0],
                        "lock_object": DISPOSABLE_DATABASE_CLEANUP_LOCK[1],
                    },
                )
            )
        except DisposableDatabaseCleanupError:
            raise
        except SQLAlchemyError:
            # ownership 驱动异常可能包含连接信息；guard 只能暴露固定 provenance invariant。
            _fail()
        if type(lock_count) is not int or lock_count != 1:
            _fail()

    def assert_initial_absence(self) -> None:
        """在锁内证明 UUID database 与两个 fixed roles 同时不存在。"""
        if self._initial_absence_verified:
            _fail()
        self._assert_guard()
        if _read_database_rows(self._connection, database_name=self._database_name):
            _fail()
        # 该查询严格位于 advisory lock 之后，是本轮最早的 role absence check。
        if _read_role_rows(self._connection):
            _fail()
        self._initial_absence_verified = True

    def create_database(self) -> bool:
        """创建 exact UUID database，并立即读取创建后 OID。

        Returns:
            当前 owner 具备 ``CREATEDB`` 并成功创建时返回 ``True``；权限不足返回
            ``False``，调用 fixture 应报告 BLOCKED。
        """
        if not self._initial_absence_verified or self._database is not None:
            _fail()
        self._assert_guard()
        if _read_database_rows(self._connection, database_name=self._database_name):
            _fail()
        if _read_role_rows(self._connection):
            _fail()
        can_create = _exact_scalar(
            self._connection.execute(text(_CURRENT_ROLE_CAN_CREATE_DATABASE_SQL))
        )
        if can_create is not True:
            return False
        self._connection.execute(text(f"CREATE DATABASE {self._quoted_database_name}"))
        self.record_created_database()
        return True

    def record_created_database(self) -> None:
        """冻结已由 helper 或 protected reset 创建的 exact database name/OID。"""
        if not self._initial_absence_verified or self._database is not None:
            _fail()
        self._assert_guard()
        rows = _read_database_rows(self._connection, database_name=self._database_name)
        if len(rows) != 1:
            _fail()
        database_oid = rows[0].get("target_database_oid")
        observed_name = rows[0].get("target_database_name")
        if (
            type(database_oid) is not int
            or database_oid <= 0
            or observed_name != self._database_name
        ):
            _fail()
        self._database = _DatabaseProvenance(
            database_name=self._database_name,
            database_oid=database_oid,
        )

    def record_created_database_after_create(self) -> None:
        """在 CREATE 已成功返回后立即读回并冻结数据库 provenance。

        目标缺失、重复记录或 name/OID 形状异常仍由
        :meth:`record_created_database` fail closed；调用者不能传入自报 OID。
        """
        self.record_created_database()

    def _verify_database(self) -> _DatabaseProvenance:
        """重读 database 行并要求 name/OID 与创建后 provenance 完全一致。"""
        expected = self._database
        if expected is None:
            _fail()
        rows = _read_database_rows(self._connection, database_name=self._database_name)
        if len(rows) != 1:
            _fail()
        observed_oid = rows[0].get("target_database_oid")
        observed_name = rows[0].get("target_database_name")
        if observed_oid != expected.database_oid or observed_name != expected.database_name:
            _fail()
        return expected

    def record_created_runtime_roles(self) -> None:
        """冻结恰好两个 fixed safe roles 的名称、OID 与完整 posture。"""
        if self._database is None or self._roles is not None:
            _fail()
        self._assert_guard()
        self._verify_database()
        self._roles = _parse_exact_runtime_roles(_read_role_rows(self._connection))

    def record_created_runtime_roles_if_safe(self) -> bool:
        """在 owner commit 后直接读回并冻结两个同时安全的 role provenance。

        本方法不接收角色名、OID 或 posture；所有事实都来自同一 cleanup management
        session。zero rows 表示 bootstrap 尚未创建角色，返回 ``False`` 且保留 database-only
        authority；partial/mixed/unsafe rows 继续由 canonical parser 拒绝，且失败不会给
        lease 增加任何角色删除权限。

        Returns:
            两个 exact safe roles 已冻结时返回 ``True``；两个角色都不存在时返回 ``False``。
        """
        if self._database is None or self._roles is not None:
            _fail()
        self._assert_guard()
        self._verify_database()
        rows = _read_role_rows(self._connection)
        if rows == ():
            return False
        self._roles = _parse_exact_runtime_roles(rows)
        return True

    def _verify_roles(self) -> tuple[_RuntimeRoleProvenance, ...]:
        """重读角色并要求 name/OID/posture 与冻结 provenance 完全一致。"""
        expected = self._roles
        if expected is None:
            _fail()
        observed = _parse_exact_runtime_roles(_read_role_rows(self._connection))
        if observed != expected:
            _fail()
        return expected

    def _assert_role_dependencies_absent(
        self,
        roles: tuple[_RuntimeRoleProvenance, ...],
    ) -> None:
        """database 删除后要求两个角色在整个 cluster 中没有任何依赖。"""
        role_oids = [role.role_oid for role in roles]
        queries = (
            _ROLE_SESSION_COUNT_SQL,
            _ROLE_MEMBERSHIP_COUNT_SQL,
            _ROLE_SETTING_COUNT_SQL,
            _ROLE_DATABASE_OWNERSHIP_COUNT_SQL,
            _ROLE_DATABASE_ACL_COUNT_SQL,
            _ROLE_SHARED_DEPENDENCY_COUNT_SQL,
        )
        for query in queries:
            value = _exact_scalar(self._connection.execute(text(query), {"role_oids": role_oids}))
            _zero_count(value)

    def _wait_for_autovacuum(self, *, deadline: float) -> None:
        """在最终清理检查前，最多等五秒让自有目标的 PostgreSQL vacuum 自行结束。

        每次只读观察都先重验原 management session、锁、cluster、database OID 和角色
        provenance。只允许 ``autovacuum worker`` 进入等待；客户端或未知类型立即拒绝。
        此准备不执行 DDL、不终止会话，也不消费或重试清理失败；返回后仍执行原始全量
        身份、角色和所有会话零计数检查，因此等待完成本身不产生任何删除 authority。

        Args:
            deadline: 整次清理准备共用的 monotonic 截止点，晚到 vacuum 不得重置预算。
        """
        while True:
            self._assert_guard()
            database = self._verify_database()
            if self._roles is None:
                if _read_role_rows(self._connection):
                    _fail()
            else:
                self._verify_roles()
            rows = _exact_mapping_rows(
                self._connection.execute(
                    text(_DATABASE_BACKEND_TYPES_SQL),
                    {"target_database_oid": database.database_oid},
                )
            )
            if monotonic() >= deadline:
                _fail()
            if not rows:
                return
            if any(row.get("backend_type") != "autovacuum worker" for row in rows):
                _fail()
            sleep(0.01)

    def cleanup(self) -> None:
        """按 provenance 顺序删除 database 与两个无依赖 fixed roles。

        先只读等待已识别的后台 vacuum 自行结束，再在首个 ``DROP`` 前一次性重验
        live session、lock、cluster、database OID 和两个 role OID/posture。最终全会话
        数与其中 vacuum 数来自同一条 SQL；若准备后又出现了且仅出现 vacuum，继续在
        原五秒预算内准备。客户端、未知类型及任何来源漂移立即拒绝，只有最终精确零
        会话才执行删除。database 删除后再重验 lock/cluster/role provenance 与六类
        cluster 依赖，最后只执行两个无 ``CASCADE`` 的固定 ``DROP ROLE``。
        """
        if self._cleaned:
            return
        if self._database is None and self._roles is None:
            # 初始 admission 或测试体在任何对象创建前失败时，没有删除 authority。
            return
        if self._database is None:
            _fail()

        deadline = monotonic() + 5
        while True:
            self._wait_for_autovacuum(deadline=deadline)

            # 准备观察与最终准入之间可能发生漂移；每次准备结束都重验完整来源，不能
            # 因为此前已看见空会话集合，就沿用旧 session、OID、锁或角色 posture。
            self._assert_guard()
            database = self._verify_database()
            if self._roles is None:
                # bootstrap 前或事务性失败后仅有 database authority，任何同名角色出现
                # 都缺少本轮角色来源证明，必须在首个 DROP 前拒绝。
                if _read_role_rows(self._connection):
                    _fail()
            else:
                self._verify_roles()
            rows = _exact_mapping_rows(
                self._connection.execute(
                    text(_DATABASE_SESSION_COUNT_SQL),
                    {"target_database_oid": database.database_oid},
                )
            )
            if len(rows) != 1 or set(rows[0]) != {"database_sessions", "autovacuum_sessions"}:
                _fail()
            database_sessions = rows[0]["database_sessions"]
            autovacuum_sessions = rows[0]["autovacuum_sessions"]
            if (
                type(database_sessions) is not int
                or type(autovacuum_sessions) is not int
                or database_sessions < 0
                or autovacuum_sessions < 0
                or autovacuum_sessions > database_sessions
                or monotonic() >= deadline
            ):
                _fail()
            if database_sessions > 0 and database_sessions == autovacuum_sessions:
                # PostgreSQL 可在上一次空观察后启动后台 worker。仅同一快照已精确证明
                # 全部都是 vacuum 才继续准备；不消费 _zero_count 或 DROP 的失败再重试。
                sleep(0.01)
                continue
            _zero_count(database_sessions)
            break

        self._connection.execute(text(f"DROP DATABASE {self._quoted_database_name}"))
        if _read_database_rows(self._connection, database_name=self._database_name):
            _fail()
        if self._roles is None:
            self._cleaned = True
            return

        self._assert_guard()
        roles = self._verify_roles()
        self._assert_role_dependencies_absent(roles)
        for role in roles:
            quoted_role = self._quoted_role_names.get(role.role_name)
            if type(quoted_role) is not str:
                _fail()
            self._connection.execute(text(f"DROP ROLE {quoted_role}"))
        if _read_role_rows(self._connection):
            _fail()
        self._cleaned = True

    def release(self) -> None:
        """尽力释放固定 advisory lock；无论结果如何都消费本 lease。"""
        if self._released:
            return
        self._released = True
        if (
            getattr(self._connection, "closed", None) is not False
            or getattr(self._connection, "invalidated", None) is not False
        ):
            return
        try:
            self._connection.execute(
                text(_UNLOCK_CLEANUP_SQL),
                {
                    "lock_class": DISPOSABLE_DATABASE_CLEANUP_LOCK[0],
                    "lock_object": DISPOSABLE_DATABASE_CLEANUP_LOCK[1],
                },
            )
        except SQLAlchemyError:
            # close session 仍会由 PostgreSQL 释放 session lock；不得因 unlock ACK 覆盖原异常。
            return


class _CleanupRecordingManagementLease:
    """原样委托 production management lease，并在两个安全阶段冻结 provenance。"""

    def __init__(
        self,
        *,
        wrapped: ManagementLifecycleLease,
        cleanup: DisposableDatabaseCleanupLease,
    ) -> None:
        self._wrapped = wrapped
        self._cleanup = cleanup

    def acquire_target_lock(self) -> AbstractContextManager[TargetMaintenanceLease]:
        """原样返回 underlying target-lock context；decorator 不介入其他 lifecycle。"""
        return self._wrapped.acquire_target_lock()

    def confirm_exact_non_production_target(self) -> None:
        """原样委托 production 精确目标确认。"""
        self._wrapped.confirm_exact_non_production_target()

    def assert_reset_allowed(self) -> None:
        """原样委托 production reset admission。"""
        self._wrapped.assert_reset_allowed()

    def drop_and_create_target(self) -> None:
        """CREATE 成功返回后，从 cleanup session 冻结 exact database OID。"""
        self._wrapped.drop_and_create_target()
        self._cleanup.record_created_database_after_create()

    def bootstrap_new_target(self) -> None:
        """bootstrap 结束或失败时，只冻结同一 session 可证明的 exact safe roles。"""
        try:
            self._wrapped.bootstrap_new_target()
        except BaseException as original_error:
            try:
                self._cleanup.record_created_runtime_roles_if_safe()
            except DisposableDatabaseCleanupError as cleanup_error:
                raise cleanup_error from original_error
            raise
        if self._cleanup.record_created_runtime_roles_if_safe() is not True:
            _fail()

    def run_typed_migration(self) -> None:
        """原样委托 production migration；此时 DB 与双角色 provenance 均已冻结。"""
        self._wrapped.run_typed_migration()


class _CleanupRecordingDatabaseMaintenanceContext:
    """只包装 management lease 的 test-owned ``DatabaseMaintenanceContext``。"""

    def __init__(
        self,
        *,
        wrapped: DatabaseMaintenanceContext,
        cleanup: DisposableDatabaseCleanupLease,
    ) -> None:
        self._wrapped = wrapped
        self._cleanup = cleanup

    @contextmanager
    def acquire_management_lifecycle_lock(
        self,
    ) -> Iterator[ManagementLifecycleLease]:
        """保持 underlying context manager 的 enter/exit 与异常传播语义。"""
        with self._wrapped.acquire_management_lifecycle_lock() as management:
            yield _CleanupRecordingManagementLease(
                wrapped=management,
                cleanup=self._cleanup,
            )


def bind_disposable_database_cleanup(
    context: DatabaseMaintenanceContext,
    cleanup: DisposableDatabaseCleanupLease,
) -> DatabaseMaintenanceContext:
    """为 production reset context 绑定 test-only 阶段 provenance recorder。

    Args:
        context: 仍由 production ``reset_then_migrate`` 消费的真实 typed context。
        cleanup: 已持独立 test-only cleanup lock 的 exact provenance lease。

    Returns:
        仅包装 management lease 方法的 typed context；不复制 lifecycle 编排或 SQL。
    """
    return _CleanupRecordingDatabaseMaintenanceContext(
        wrapped=context,
        cleanup=cleanup,
    )


@contextmanager
def managed_disposable_database_cleanup(
    management_engine: Engine,
    *,
    database_name: str,
) -> Iterator[DisposableDatabaseCleanupLease]:
    """在同一 Engine connection 上持锁、清理、unlock 并显式 close。

    Args:
        management_engine: 指向固定 ``postgres`` database 的 ``NullPool`` owner Engine。
        database_name: 本轮 UUID disposable database 名称。

    Yields:
        仍持 test-only advisory lock 的 provenance lease。
    """
    if getattr(management_engine, "hide_parameters", None) is not True:
        _fail()
    connection = management_engine.connect()
    lease: DisposableDatabaseCleanupLease | None = None
    active_error = False
    try:
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        lease = DisposableDatabaseCleanupLease.acquire(
            connection=connection,
            database_name=database_name,
            quote_identifier=management_engine.dialect.identifier_preparer.quote_identifier,
        )
        try:
            yield lease
        except BaseException as body_error:
            active_error = True
            try:
                lease.cleanup()
            except (DisposableDatabaseCleanupError, SQLAlchemyError, TypeError, ValueError):
                # body 已失败时 cleanup 仍必须 fail closed；新稳定 invariant 以完整 body
                # 异常为直接 cause，避免 finally 错误截断原链或泄露 DBAPI/catalog 细节。
                raise DisposableDatabaseCleanupError(_ERROR_MESSAGE) from body_error
            raise
        else:
            try:
                lease.cleanup()
            except DisposableDatabaseCleanupError:
                active_error = True
                raise
            except (SQLAlchemyError, TypeError, ValueError):
                active_error = True
                raise DisposableDatabaseCleanupError(_ERROR_MESSAGE) from None
    finally:
        if lease is not None:
            lease.release()
        try:
            connection.close()
        except SQLAlchemyError:
            if not active_error:
                _fail()


class IntegrationSuiteLockLease:
    """把完整 integration suite 的串行权绑定到同一 live management session。

    lease 冻结取得 advisory lock 时的 backend PID 与 PostgreSQL system identifier；parent
    orchestrator 必须在每个 phase 边界及 child watchdog 周期调用 :meth:`verify`。正常完成
    时 :meth:`release` 还要求 PostgreSQL 精确确认 unlock，异常路径则只做不覆盖原异常的
    best-effort unlock，最终依靠关闭同一 session 释放锁。
    """

    def __init__(
        self,
        *,
        connection: Connection,
        session: _SessionProvenance,
    ) -> None:
        self._connection = connection
        self._session = session
        self._released = False

    @classmethod
    def acquire(cls, *, connection: Connection) -> Self:
        """取得独立 suite-global lock，并发行可持续验证的 typed lease。

        Args:
            connection: 指向测试 cluster 固定 ``postgres`` database 的 live session。

        Returns:
            已冻结 backend、cluster 与 advisory lock ownership 的 suite lease。

        Raises:
            DisposableDatabaseCleanupError: session、cluster 或 lock 不满足闭集。
        """
        session = _read_session_provenance(connection)
        parameters = {
            "lock_class": INTEGRATION_SUITE_LOCK[0],
            "lock_object": INTEGRATION_SUITE_LOCK[1],
        }
        acquired = False
        try:
            try:
                acquired_value = _exact_scalar(
                    connection.execute(text(_TRY_CLEANUP_LOCK_SQL), parameters)
                )
            except DisposableDatabaseCleanupError:
                raise
            except SQLAlchemyError:
                _fail()
            if acquired_value is not True:
                _fail()
            acquired = True
            lease = cls(connection=connection, session=session)
            lease.verify()
            return lease
        except BaseException:
            if acquired and getattr(connection, "closed", None) is False:
                try:
                    connection.execute(text(_UNLOCK_CLEANUP_SQL), parameters)
                except SQLAlchemyError:
                    # acquire 异常仍由原稳定错误主导；close 会释放 session lock。
                    pass
            raise

    def verify(self) -> None:
        """重验同一 backend、cluster identity 与唯一 suite lock ownership。

        Raises:
            DisposableDatabaseCleanupError: lease 已消费或任一 live provenance 漂移。
        """
        if self._released:
            _fail()
        observed = _read_session_provenance(self._connection)
        if observed != self._session:
            _fail()
        try:
            lock_count = _exact_scalar(
                self._connection.execute(
                    text(_CLEANUP_LOCK_OWNERSHIP_SQL),
                    {
                        "lock_class": INTEGRATION_SUITE_LOCK[0],
                        "lock_object": INTEGRATION_SUITE_LOCK[1],
                    },
                )
            )
        except DisposableDatabaseCleanupError:
            raise
        except SQLAlchemyError:
            _fail()
        if type(lock_count) is not int or lock_count != 1:
            _fail()

    def release(self, *, suppress_errors: bool = False) -> None:
        """消费 lease，并按 body 结果选择严格或 best-effort unlock。

        Args:
            suppress_errors: ``True`` 仅用于 body 已抛异常的清理路径；此时任何 unlock
                失败都不得覆盖原异常，同一 connection 随后仍会被显式关闭。

        Raises:
            DisposableDatabaseCleanupError: 正常成功路径的 provenance 或 unlock ACK 异常。
        """
        if self._released:
            return
        if suppress_errors:
            self._released = True
            if getattr(self._connection, "closed", None) is False:
                try:
                    self._connection.execute(
                        text(_UNLOCK_CLEANUP_SQL),
                        {
                            "lock_class": INTEGRATION_SUITE_LOCK[0],
                            "lock_object": INTEGRATION_SUITE_LOCK[1],
                        },
                    )
                except SQLAlchemyError:
                    pass
            return

        self.verify()
        try:
            released = _exact_scalar(
                self._connection.execute(
                    text(_UNLOCK_CLEANUP_SQL),
                    {
                        "lock_class": INTEGRATION_SUITE_LOCK[0],
                        "lock_object": INTEGRATION_SUITE_LOCK[1],
                    },
                )
            )
        except DisposableDatabaseCleanupError:
            raise
        except SQLAlchemyError:
            _fail()
        if released is not True:
            _fail()
        self._released = True


@contextmanager
def managed_integration_suite_lock(
    management_engine: Engine,
) -> Iterator[IntegrationSuiteLockLease]:
    """用独立 session advisory lock 串行两个完整 integration suite。

    此锁与 disposable fixture 的 role cleanup lock 使用不同 key。parent orchestrator 在
    regular provisioning、child cleanup 和 lifecycle child 全程持有本锁，但不会占用
    child fixture 需要的 destructive lock，因此既阻止两个完整 suite 交错，也不会让
    lifecycle child 自锁死。

    Args:
        management_engine: 指向固定 ``postgres`` database、显式隐藏参数的同步 Engine。

    Yields:
        绑定同一 live management session、可跨 phase 持续验证的 suite lease。

    Raises:
        DisposableDatabaseCleanupError: Engine、session、cluster 或 lock ownership 不满足
            test-only 闭集，已有另一完整 suite 持锁，或正常 body/release 后无法安全关闭
            management session。active acquire/body/release 异常不会被 close DBAPI 文本覆盖。
    """
    if getattr(management_engine, "hide_parameters", None) is not True:
        _fail()
    connection = management_engine.connect()
    try:
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        lease = IntegrationSuiteLockLease.acquire(connection=connection)
    except BaseException:
        try:
            connection.close()
        except SQLAlchemyError:
            # acquire 是 active error；close DBAPI 细节不得替换或泄漏该错误。
            pass
        raise

    try:
        yield lease
    except BaseException:
        # ``suppress_errors=True`` 只执行内部已收敛 SQLAlchemyError 的 best-effort unlock；
        # 同一 session close 仍是最终释放手段，active body 错误始终保持主导。
        lease.release(suppress_errors=True)
        try:
            connection.close()
        except SQLAlchemyError:
            # body/_ChildExitCode/lease continuity error 必须保持主导。
            pass
        raise

    try:
        lease.release()
    except BaseException:
        try:
            connection.close()
        except SQLAlchemyError:
            # strict release 已给出稳定 invariant；close 不得覆盖。
            pass
        raise

    try:
        connection.close()
    except SQLAlchemyError:
        # 只有正常 body 且 strict release 成功时，close 失败才成为当前错误；仍统一收敛为
        # 不含 endpoint、session 或 DBAPI 文本的稳定测试基础设施 invariant。
        _fail()
