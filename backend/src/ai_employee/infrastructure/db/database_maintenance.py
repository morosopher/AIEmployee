"""编排数据库维护生命周期并冻结目标身份与 advisory lock 派生规则。

本模块只拥有跨入口共享的目标身份、锁顺序、restore authority 与生命周期编排；
database ACL、运行时角色和对象授权的 catalog SQL/完整 inventory 继续分别由
``database_access.py`` 与 ``database_grants.py`` 独占。Cycle 4 首先冻结无 I/O 的目标
身份边界，后续持锁 context 只能消费这些经过严格验证的不可变值。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import NoReturn, Protocol
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from ai_employee.infrastructure.db.alembic import (
    AlembicMigrationInvariantError,
    OnlineMigrationAuthority,
    PublishedAlembicAuthority,
    _issue_online_migration_authority,
    read_current_alembic_revision,
)
from ai_employee.infrastructure.db.database_access import (
    BootstrapCaller,
    BootstrapCandidate,
    DatabaseAccessSnapshot,
    DatabaseAclProfile,
    apply_baseline_connect_reassertion,
    apply_database_acl_mutation,
    apply_safe_runtime_roles,
    classify_bootstrap_candidate,
    read_database_access_snapshot_sync,
    rotate_safe_runtime_role_passwords,
    transition_active_to_baseline,
    transition_baseline_to_active,
)
from ai_employee.infrastructure.db.database_access import (
    bootstrap_candidate_to_baseline as plan_bootstrap_candidate_to_baseline,
)
from ai_employee.infrastructure.db.database_grants import (
    BootstrapGrantPosture,
    GrantPhase,
    apply_bootstrap_object_grants,
    verify_bootstrap_object_grants,
    verify_object_grants,
)

TARGET_IDENTITY_DOMAIN = b"ai_employee.restore_target.v1\0"
DATABASE_LIFECYCLE_LOCK_DOMAIN = b"ai_employee.database_lifecycle_lock.v1\0"
DATABASE_MAINTENANCE_LOCK_DOMAIN = b"ai_employee.database_maintenance_lock.v1\0"
SCHEMA_LIFECYCLE_LOCK = (20260806, 143)

DATABASE_RESTORE_FACTS_SQL = """WITH current_values AS (
    SELECT
        current_setting('ai_employee.maintenance_gate', true)
            AS current_maintenance_gate,
        current_setting('ai_employee.restore_call_authority', true)
            AS current_restore_call_authority,
        current_setting('ai_employee.restore_completion', true)
            AS current_restore_completion
), catalog_rows AS (
    SELECT setting.setrole, setting.setconfig
    FROM pg_db_role_setting AS setting
    WHERE setting.setdatabase = :target_database_oid
      AND EXISTS (
          SELECT 1
          FROM unnest(setting.setconfig) AS config(value)
          WHERE split_part(config.value, '=', 1) IN (
              'ai_employee.maintenance_gate',
              'ai_employee.restore_call_authority',
              'ai_employee.restore_completion'
          )
      )
)
SELECT
    catalog_rows.setrole,
    catalog_rows.setconfig,
    current_values.current_maintenance_gate,
    current_values.current_restore_call_authority,
    current_values.current_restore_completion
FROM current_values
LEFT JOIN catalog_rows ON true
ORDER BY catalog_rows.setrole NULLS FIRST, catalog_rows.setconfig::text NULLS FIRST"""

_MANAGEMENT_TARGET_CATALOG_SQL = """SELECT
    current_database() AS management_database_name,
    current_user AS current_role_name,
    session_role.oid AS current_role_oid,
    control.system_identifier,
    database.oid AS target_database_oid,
    database.datname AS target_database_name,
    database.datdba AS owner_oid,
    pg_get_userbyid(database.datdba) AS owner_role_name
FROM pg_control_system() AS control
JOIN pg_roles AS session_role ON session_role.rolname = current_user
LEFT JOIN pg_database AS database ON database.datname = :target_database_name"""

_TARGET_CATALOG_SQL = """SELECT
    current_database() AS target_database_name,
    current_user AS current_role_name,
    control.system_identifier,
    database.oid AS target_database_oid,
    database.datdba AS owner_oid,
    pg_get_userbyid(database.datdba) AS owner_role_name
FROM pg_control_system() AS control
JOIN pg_database AS database ON database.datname = current_database()"""

_ACTIVE_NON_OWNER_SESSIONS_SQL = """SELECT EXISTS (
    SELECT 1
    FROM pg_stat_activity AS activity
    WHERE activity.datid = :target_database_oid
      AND activity.backend_type = 'client backend'
      AND activity.pid <> pg_backend_pid()
      AND activity.usesysid IS DISTINCT FROM :owner_oid
) AS has_active_non_owner_sessions"""

_TARGET_HAS_SESSIONS_SQL = """SELECT EXISTS (
    SELECT 1
    FROM pg_stat_activity AS activity
    WHERE activity.datid = :target_database_oid
) AS has_target_sessions"""

_TRY_SINGLE_LOCK_SQL = "SELECT pg_try_advisory_lock(:lock_key) AS acquired"
_UNLOCK_SINGLE_LOCK_SQL = "SELECT pg_advisory_unlock(:lock_key) AS released"
_TRY_SCHEMA_LOCK_SQL = "SELECT pg_try_advisory_lock(:lock_class, :lock_object) AS acquired"
_UNLOCK_SCHEMA_LOCK_SQL = "SELECT pg_advisory_unlock(:lock_class, :lock_object) AS released"

_SIGNED_INT64_MIN = -(1 << 63)
_SIGNED_INT64_MAX = (1 << 63) - 1
_UINT64_MODULUS = 1 << 64
_UINT64_MAX = _UINT64_MODULUS - 1
_INVARIANT_ERROR_MESSAGE = "database maintenance invariant violation"
_RESTORE_FACT_KEYS = (
    "ai_employee.maintenance_gate",
    "ai_employee.restore_call_authority",
    "ai_employee.restore_completion",
)
_LOWER_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,128}$")
_CANONICAL_DECIMAL_PATTERN = re.compile(r"^(0|[1-9][0-9]*)$")
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_COMPLETION_AUTHORITY_DOMAIN = b"ai_employee.restore_completion_authority.v1\0"


class DatabaseMaintenanceInvariantError(RuntimeError):
    """表示维护目标、锁 authority 或生命周期事实违反冻结不变量。

    异常消息固定且不包含数据库名、连接 URL、Secret 或 catalog 内容，调用边界可安全
    映射成稳定错误码，而不会把具体部署事实带入日志。
    """


@dataclass(frozen=True, slots=True)
class DatabaseTargetIdentity:
    """保存目标数据库身份摘要与两把 target-scoped advisory lock key。

    Attributes:
        identity_bytes: domain、canonical system identifier 与 exact database name 的冻结 bytes。
        digest: ``identity_bytes`` 的原始 SHA-256，供后续域分离的 lock key 派生复用。
        digest_hex: 与 ``digest`` 相同内容的小写十六进制表示。
        lifecycle_lock_key: 管理库 session 使用的 cluster/target 生命周期锁 key。
        target_lock_key: 目标库 owner session 使用的维护锁 key。
    """

    identity_bytes: bytes
    digest: bytes
    digest_hex: str
    lifecycle_lock_key: int
    target_lock_key: int


@dataclass(frozen=True, slots=True, repr=False)
class ProtectedResetPolicy:
    """绑定一次 destructive reset 唯一允许的环境与逐字节数据库全名。

    policy 只能由 typed ``db-reset`` CLI 显式构造；普通 role-bootstrap/migrate context
    不接收该值。环境闭集固定为 ``development | test``，名称不做 trim、大小写折叠或
    Unicode normalize，也不存在环境变量、文件或公开 skip token 能跳过后续复核。

    Attributes:
        app_env: 调用方显式提供的非生产环境。
        confirmed_database_name: 操作者完整输入并确认的 exact database name。
    """

    app_env: str
    confirmed_database_name: str

    def __post_init__(self) -> None:
        """在建立任何 Engine/session 前收窄非生产环境与完整名称。"""
        if type(self.app_env) is not str or self.app_env not in {"development", "test"}:
            _fail()
        _validated_database_name(self.confirmed_database_name)


@dataclass(frozen=True, slots=True)
class _DatabaseTargetCatalog:
    """保存 management 与 target session 必须逐字段一致的目标 catalog。"""

    identity: DatabaseTargetIdentity
    database_oid: int
    owner_oid: int
    owner_role_name: str
    database_name: str


@dataclass(frozen=True, slots=True)
class _AbsentDatabaseTargetCatalog:
    """保存 protected reset 对 absent target 建锁所需的冻结 catalog 事实。

    该形状没有数据库 OID；identity 的名称 bytes 必须来自操作员 exact confirmation，
    owner 则来自同一 management session 的当前角色。普通 lifecycle 不得消费该类型。
    """

    identity: DatabaseTargetIdentity
    owner_oid: int
    owner_role_name: str
    database_name: str


@dataclass(frozen=True, slots=True)
class _PostCreateTargetProvenance:
    """绑定本次 CREATE 后重读得到的 target 与仍在持有的 lifecycle lock key。

    该值只携带由 management catalog 重新证明的不可变事实，不携带 Engine、Connection
    或可重复执行 callback。生产路径只在 CREATE 成功后构造，并由紧接的 bootstrap
    消费一次；字段校验阻止 target 与 lifecycle key 被交叉拼接。
    """

    target: _DatabaseTargetCatalog
    lifecycle_lock_key: int

    def __post_init__(self) -> None:
        """要求 provenance 的 lock key 与 created target identity 精确一致。"""
        if (
            type(self.target) is not _DatabaseTargetCatalog
            or type(self.lifecycle_lock_key) is not int
            or self.lifecycle_lock_key != self.target.identity.lifecycle_lock_key
        ):
            _fail()


_ManagementTargetCatalog = _DatabaseTargetCatalog | _AbsentDatabaseTargetCatalog


@dataclass(frozen=True, slots=True)
class DatabaseRestoreFacts:
    """保存目标 database-wide restore authority 的三个原始 canonical 值。"""

    maintenance_gate: str | None
    restore_call_authority: str | None
    restore_completion: str | None


class DatabaseAdmissionState(StrEnum):
    """表示 restore catalog 与 access posture 唯一允许的三个 steady state。"""

    PRISTINE_IDLE = "pristine_idle"
    ACTIVE = "active"
    COMPLETED_IDLE = "completed_idle"


class _RestoreCallPhase(StrEnum):
    """冻结 ``restore-call:v4`` parser 唯一接受的 phase 闭集。"""

    GATE_ESTABLISHED = "gate_established"
    RESTORE_BACKEND_STARTING = "restore_backend_starting"
    RESTORE_BACKEND_READY = "restore_backend_ready"
    RESTORE_STARTED = "restore_started"
    RESTORE_OUTCOME_UNKNOWN = "restore_outcome_unknown"
    RESTORE_NOT_APPLIED = "restore_not_applied"
    RESTORE_SUCCEEDED = "restore_succeeded"
    GRANTS_SUCCEEDED = "grants_succeeded"
    VERIFIED = "verified"
    REOPEN_COMMITTING = "reopen_committing"
    REOPEN_NOT_APPLIED = "reopen_not_applied"
    COMPLETED = "completed"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True, slots=True)
class _RestoreCallAuthority:
    """保存已通过 grammar、phase-shape 与 completion digest 校验的 call。

    ``fields`` 保留逐字节 canonical 序列，供 completed zero-slot proof 复算；其余字段
    只暴露 admission 需要的类型化绑定，避免调用方再次按裸索引解释 phase/ordinal。
    """

    fields: tuple[str, ...]
    attempt_uuid: str
    kind: str
    target_digest: str
    source_digest: str
    call_ordinal: int
    reopen_ordinal: int
    phase: _RestoreCallPhase


@dataclass(frozen=True, slots=True)
class _StableRoleBootstrapAdmission:
    """绑定 standalone role-bootstrap steady 分支的完整只读事实。"""

    revision: str
    state: DatabaseAdmissionState
    facts: DatabaseRestoreFacts
    access_snapshot: DatabaseAccessSnapshot


class OwnerBootstrapTransaction(Protocol):
    """描述 candidate→baseline 单一 owner transaction 的窄执行端口。"""

    def apply_safe_runtime_roles(self, candidate: BootstrapCandidate) -> None:
        """创建两角色或只轮换已安全角色的 Secret 驱动密码。"""

    def apply_database_acl(self, profile: DatabaseAclProfile) -> None:
        """通过 database-access mutator 应用唯一允许的完整 ACL profile。"""

    def apply_object_grants(self, *, revision: str, phase: GrantPhase) -> None:
        """通过 database-grants mutator 应用当前 revision/phase 精确 inventory。"""

    def verify_pristine_idle_before_commit(self) -> None:
        """在 commit 前重读三项 facts、access posture 与 object grants。"""


class TargetMaintenanceLease(Protocol):
    """描述持有 target maintenance lock 后允许的最小生命周期操作。"""

    revision: str

    def assert_restore_facts_absent(self) -> None:
        """要求三项 database-wide restore facts 精确 absent。"""

    def read_bootstrap_candidate(self) -> BootstrapCandidate:
        """在零写前返回仅供本次持锁调用使用的瞬态 Candidate。"""

    def acquire_schema_lifecycle_lock(self) -> AbstractContextManager[None]:
        """在 target lock 之后取得固定 schema-lifecycle exclusive lock。"""

    def owner_transaction(self) -> AbstractContextManager[OwnerBootstrapTransaction]:
        """开启 candidate 收敛所需的唯一显式 owner transaction。"""

    def verify_pristine_idle_after_commit(self) -> None:
        """commit 后仍持三把锁，用普通 steady parser 重验 ``pristine_idle``。"""

    def assert_reset_allowed_for_drop(self) -> None:
        """只允许 stable idle 目标进入后续 DROP/CREATE，不授予 migration runner 状态。"""

    def assert_migration_allowed(self) -> None:
        """只允许 ``pristine_idle`` 或 ``completed_idle``，拒绝 Candidate/active。"""

    def run_typed_migration(self) -> None:
        """在同一 target/schema lease 下执行普通 typed migration。"""

    def assert_role_bootstrap_allowed(self) -> None:
        """只接纳 standalone Candidate 或两个 stable idle state。"""

    def run_typed_role_bootstrap(self) -> None:
        """在同一 target/schema lease 下执行 Candidate 收敛或 steady 权限重申。"""


class ManagementLifecycleLease(Protocol):
    """描述固定管理库 session 持有 lifecycle lock 后的操作。"""

    def acquire_target_lock(self) -> AbstractContextManager[TargetMaintenanceLease]:
        """在 management lock 后取得目标库 maintenance lock。"""

    def confirm_exact_non_production_target(self) -> None:
        """复核非生产环境、精确目标与完整名称人工确认。"""

    def assert_reset_allowed(self) -> None:
        """在 drop/create 前拒绝 active、needs-attention 或 malformed authority。"""

    def drop_and_create_target(self) -> None:
        """在当前 live management lease 内删除并重建唯一目标。"""

    def bootstrap_new_target(self) -> None:
        """紧接 create 执行 reset 专用 candidate→baseline bootstrap。"""

    def run_typed_migration(self) -> None:
        """把同一 in-process management lease 交给普通 typed migration。"""


class DatabaseMaintenanceContext(Protocol):
    """描述所有 repository-owned database lifecycle 共用的根 context。"""

    def acquire_management_lifecycle_lock(
        self,
    ) -> AbstractContextManager[ManagementLifecycleLease]:
        """从固定 ``postgres`` 管理库取得 session-level lifecycle lock。"""


class _CatalogRow(Protocol):
    """描述 SQLAlchemy RowMapping 与测试 mapping 共用的最小读取接口。"""

    def __contains__(self, key: object) -> bool:
        """返回 mapping 是否含有指定列。"""

    def __getitem__(self, key: str) -> object:
        """按明确列名读取 catalog 值。"""


def _fail() -> NoReturn:
    """抛出唯一稳定且不含部署内容的维护不变量错误。"""
    raise DatabaseMaintenanceInvariantError(_INVARIANT_ERROR_MESSAGE)


def signed_system_identifier_to_uint64(value: object) -> int:
    """把 PostgreSQL signed ``bigint`` system identifier 恢复为原始 uint64。

    PostgreSQL SQL 边界只允许 signed int64。非负值保持不变，负值通过加 ``2**64``
    恢复 ``pg_control_system`` 的原始无符号位模式；bool 虽是 Python ``int`` 子类，仍
    必须拒绝，避免宽松类型把开关值变成 cluster identity。

    Args:
        value: PostgreSQL 驱动返回的候选 signed bigint。

    Returns:
        ``0..2**64-1`` 的普通 Python 整数；调用方还需按目标身份规则拒绝零值。

    Raises:
        DatabaseMaintenanceInvariantError: 输入不是非 bool signed int64。
    """
    if type(value) is not int or not _SIGNED_INT64_MIN <= value <= _SIGNED_INT64_MAX:
        _fail()
    return value if value >= 0 else value + _UINT64_MODULUS


def _canonical_system_identifier_ascii(value: object) -> bytes:
    """验证目标身份中的 unsigned decimal system identifier ASCII。"""
    if type(value) is not bytes or value == b"":
        _fail()
    if any(byte < ord("0") or byte > ord("9") for byte in value):
        _fail()
    if len(value) > 1 and value.startswith(b"0"):
        _fail()
    try:
        numeric_value = int(value.decode("ascii"), 10)
    except (UnicodeDecodeError, ValueError):
        _fail()
    if not 1 <= numeric_value <= _UINT64_MAX:
        _fail()
    if str(numeric_value).encode("ascii") != value:
        _fail()
    return value


def _exact_database_name_utf8(value: object) -> bytes:
    """验证 database name 是 1..63 bytes、无 NUL 的 exact UTF-8。"""
    if type(value) is not bytes or not 1 <= len(value) <= 63 or b"\0" in value:
        _fail()
    try:
        value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail()
    return value


def _signed_lock_key(domain: bytes, target_digest: bytes) -> int:
    """按域分离 SHA-256 的前八字节生成 signed big-endian advisory key。"""
    return int.from_bytes(sha256(domain + target_digest).digest()[:8], "big", signed=True)


def _required_mapping_value(row: _CatalogRow, key: str) -> object:
    """从 SQLAlchemy RowMapping/Fake mapping 读取必需列并拒绝未知容器。"""
    try:
        if key not in row:
            _fail()
        return row[key]
    except (KeyError, TypeError):
        _fail()


def _optional_exact_setting(row: _CatalogRow, key: str) -> str | None:
    """读取 ``current_setting(..., true)`` 的 exact text 或 absent ``NULL``。

    PostgreSQL driver 在该边界只允许返回 ``str | None``。拒绝 bool、bytes 等宽松
    表示，避免测试 Fake 或异常 driver value 绕过 catalog/current identity 比较。
    """
    value = _required_mapping_value(row, key)
    if value is None:
        return None
    if type(value) is not str:
        _fail()
    return value


def read_database_restore_facts(
    connection: Connection,
    *,
    target_database_oid: object,
) -> DatabaseRestoreFacts:
    """直接读取并规范化三个 database-wide restore catalog facts。

    reader 只接受目标数据库的 ``setrole=0`` key，并要求同一 SQL snapshot 返回的三个
    ``current_setting(..., true)`` 与 catalog 逐字节相同。role-specific override、重复
    key、无 catalog 时的 session/PGOPTIONS 注入、stale session、非 ``list[str]`` config
    或 PostgreSQL OID 越界都会在调用方执行任何 DDL/DML/授权前 fail closed。无关
    database setting 保持未解释，不会被复制到 authority 类型。

    Args:
        connection: 已持 target maintenance lock 的同步 SQLAlchemy owner connection。
        target_database_oid: 已从目标 catalog 验证的普通 PostgreSQL OID。

    Returns:
        三个字段各自为 absent ``None`` 或未经 normalize 的 exact value。

    Raises:
        DatabaseMaintenanceInvariantError: catalog/current 行类型、作用域、唯一性或
            exact identity 不合法。
    """
    if type(target_database_oid) is not int or not 1 <= target_database_oid <= (1 << 32) - 1:
        _fail()
    result = connection.execute(
        text(DATABASE_RESTORE_FACTS_SQL),
        {"target_database_oid": target_database_oid},
    )
    rows = result.mappings().all()
    if not rows:
        # SQL 必须通过 LEFT JOIN 在 catalog absent 时仍返回唯一 sentinel；零行意味着
        # query/Fake 契约已漂移，不能把未知状态宽松解释为 pristine。
        _fail()

    values: dict[str, str] = {}
    current_values: tuple[str | None, str | None, str | None] | None = None
    saw_catalog_sentinel = False
    for row in rows:
        row_current_values = (
            _optional_exact_setting(row, "current_maintenance_gate"),
            _optional_exact_setting(row, "current_restore_call_authority"),
            _optional_exact_setting(row, "current_restore_completion"),
        )
        if current_values is None:
            current_values = row_current_values
        elif current_values != row_current_values:
            # 一个 statement 的重复 catalog 行必须携带完全相同的 session observation；
            # Fake/driver 若给出矛盾 tuple，不能依赖行顺序选择其中一个。
            _fail()

        setrole = _required_mapping_value(row, "setrole")
        setconfig = _required_mapping_value(row, "setconfig")
        if setrole is None or setconfig is None:
            if (
                setrole is not None
                or setconfig is not None
                or len(rows) != 1
                or saw_catalog_sentinel
            ):
                _fail()
            saw_catalog_sentinel = True
            continue
        if type(setrole) is not int or type(setconfig) is not list:
            _fail()
        row_fact_count = 0
        for entry in setconfig:
            if type(entry) is not str:
                _fail()
            key, separator, value = entry.partition("=")
            if key not in _RESTORE_FACT_KEYS:
                continue
            row_fact_count += 1
            if setrole != 0 or separator != "=" or key in values:
                _fail()
            values[key] = value
        if row_fact_count == 0:
            # 生产 SQL 只返回包含至少一个 authority key 的 row；显式拒绝不满足该
            # predicate 的 Fake/异常结果，避免 unrelated row 被当成 absent authority。
            _fail()

    facts = DatabaseRestoreFacts(
        maintenance_gate=values.get(_RESTORE_FACT_KEYS[0]),
        restore_call_authority=values.get(_RESTORE_FACT_KEYS[1]),
        restore_completion=values.get(_RESTORE_FACT_KEYS[2]),
    )
    if current_values != (
        facts.maintenance_gate,
        facts.restore_call_authority,
        facts.restore_completion,
    ):
        _fail()
    return facts


def _canonical_uuid(value: str) -> None:
    """验证规范小写 UUID 文本，不接受 normalize 后相等的其他表示。"""
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        _fail()
    if str(parsed) != value:
        _fail()


def _canonical_digest(value: str, *, allow_dash: bool = False) -> None:
    """验证小写 SHA-256 文本；指定位置可接受单一 ``-`` sentinel。"""
    if allow_dash and value == "-":
        return
    if _LOWER_DIGEST_PATTERN.fullmatch(value) is None:
        _fail()


def _canonical_ordinal(value: str) -> int:
    """验证 ``0..999999`` 无前导零十进制 ordinal。"""
    if _CANONICAL_DECIMAL_PATTERN.fullmatch(value) is None:
        _fail()
    parsed = int(value, 10)
    if not 0 <= parsed <= 999999:
        _fail()
    return parsed


def _canonical_backend_pid(value: str) -> int | None:
    """验证 backend PID 为 absent ``-`` 或无前导零的正 int32。"""
    if value == "-":
        return None
    if _CANONICAL_DECIMAL_PATTERN.fullmatch(value) is None:
        _fail()
    parsed = int(value, 10)
    if not 1 <= parsed <= (1 << 31) - 1:
        _fail()
    return parsed


def _canonical_timestamp(value: str, *, allow_dash: bool = False) -> None:
    """验证数据库 UTC RFC3339 恰好六位微秒的 canonical 文本。"""
    if allow_dash and value == "-":
        return
    if _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        _fail()
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        _fail()


def _canonical_revision(value: str, *, allow_dash: bool = False) -> None:
    """验证 restore authority 中的 revision 或显式 absent sentinel。"""
    if allow_dash and value == "-":
        return
    if _REVISION_PATTERN.fullmatch(value) is None:
        _fail()


def _paired_presence(left: str, right: str) -> bool:
    """要求两个已完成字段级校验的值同时 absent 或同时 present。"""
    if (left == "-") is not (right == "-"):
        _fail()
    return left != "-"


def _validate_restore_call_phase_shape(
    *,
    phase: _RestoreCallPhase,
    call_ordinal: int,
    reopen_ordinal: int,
    call_started: bool,
    backend: bool,
    pre_restore: bool,
    observed: bool,
    completed: bool,
) -> None:
    """把 frozen phase graph 收窄为唯一允许的 ordinal/presence shape。

    这里只验证单条 authority 的合法形状；同 ordinal 字段不可改写、CAS predecessor 与
    ordinal+1 retry 等跨记录单调性属于后续 writer/CAS 层，不在本次 parser 范围内。
    ``needs_attention`` 只继承 starting、backend 或 reopen predecessor 的原 facts，
    不能制造 gate-only 或 succeeded-without-reopen 的新形状。
    """
    direct_observed = (
        call_ordinal == 0 and not call_started and not backend and not pre_restore and observed
    )
    real_observed = call_ordinal >= 1 and call_started and backend and pre_restore and observed
    starting_shape = (
        call_ordinal >= 1
        and reopen_ordinal == 0
        and call_started
        and not backend
        and pre_restore
        and not observed
        and not completed
    )
    backend_shape = (
        call_ordinal >= 1
        and reopen_ordinal == 0
        and call_started
        and backend
        and pre_restore
        and not observed
        and not completed
    )
    reopened_observed_shape = (
        reopen_ordinal >= 1 and (direct_observed or real_observed) and not completed
    )

    if phase is _RestoreCallPhase.GATE_ESTABLISHED:
        valid = (
            call_ordinal == 0
            and reopen_ordinal == 0
            and not call_started
            and not backend
            and not pre_restore
            and not observed
            and not completed
        )
    elif phase is _RestoreCallPhase.RESTORE_BACKEND_STARTING:
        valid = starting_shape
    elif phase in {
        _RestoreCallPhase.RESTORE_BACKEND_READY,
        _RestoreCallPhase.RESTORE_STARTED,
        _RestoreCallPhase.RESTORE_OUTCOME_UNKNOWN,
        _RestoreCallPhase.RESTORE_NOT_APPLIED,
    }:
        valid = backend_shape
    elif phase in {
        _RestoreCallPhase.RESTORE_SUCCEEDED,
        _RestoreCallPhase.GRANTS_SUCCEEDED,
        _RestoreCallPhase.VERIFIED,
    }:
        valid = reopen_ordinal == 0 and (direct_observed or real_observed) and not completed
    elif phase in {
        _RestoreCallPhase.REOPEN_COMMITTING,
        _RestoreCallPhase.REOPEN_NOT_APPLIED,
    }:
        valid = reopened_observed_shape
    elif phase is _RestoreCallPhase.COMPLETED:
        valid = reopen_ordinal >= 1 and (direct_observed or real_observed) and completed
    else:
        # ``needs_attention`` 只有三类合法 predecessor shape；枚举闭集保证这里不会
        # 静默接纳未来新增 phase。
        valid = phase is _RestoreCallPhase.NEEDS_ATTENTION and (
            starting_shape or backend_shape or reopened_observed_shape
        )
    if not valid:
        _fail()


def _parse_call_authority(value: str) -> _RestoreCallAuthority:
    """解析并验证完整 ``restore-call:v4`` grammar 与 phase-shape 矩阵。

    parser 不执行 catalog CAS 或 restore 写入，只把数据库 authority 收窄为可供三态
    admission 使用的不可变类型。``completed`` 在此处完成 zero-slot digest 复算，确保
    任一调用方都不能只凭 phase 文本绕过 completion authority 完整性。
    """
    if type(value) is not str:
        _fail()
    try:
        encoded = value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        _fail()
    if len(encoded) > 1133 or any(character in value for character in "\0\r\n"):
        _fail()
    if value.count("|") != 21:
        _fail()
    fields = value.split("|")
    if len(fields) != 22 or any(field == "" for field in fields):
        _fail()
    if fields[0] != "restore-call:v4":
        _fail()
    _canonical_uuid(fields[1])
    if fields[2] not in {"generic", "sealed_0018"}:
        _fail()
    for index in (3, 4, 5):
        _canonical_digest(fields[index])
    if fields[4] != fields[5]:
        _fail()
    _canonical_digest(fields[6], allow_dash=True)
    call_ordinal = _canonical_ordinal(fields[7])
    reopen_ordinal = _canonical_ordinal(fields[8])
    try:
        phase = _RestoreCallPhase(fields[9])
    except ValueError:
        _fail()
    _canonical_timestamp(fields[10])
    _canonical_timestamp(fields[11], allow_dash=True)
    backend_pid = _canonical_backend_pid(fields[12])
    _canonical_timestamp(fields[13], allow_dash=True)
    _canonical_revision(fields[14], allow_dash=True)
    _canonical_digest(fields[15], allow_dash=True)
    _canonical_revision(fields[16])
    _canonical_digest(fields[17])
    _canonical_revision(fields[18], allow_dash=True)
    _canonical_digest(fields[19], allow_dash=True)
    _canonical_timestamp(fields[20], allow_dash=True)
    _canonical_digest(fields[21], allow_dash=True)
    backend_present = _paired_presence(fields[12], fields[13])
    if backend_present is not (backend_pid is not None):
        _fail()
    pre_restore_present = _paired_presence(fields[14], fields[15])
    observed_present = _paired_presence(fields[18], fields[19])
    completed_present = _paired_presence(fields[20], fields[21])
    _validate_restore_call_phase_shape(
        phase=phase,
        call_ordinal=call_ordinal,
        reopen_ordinal=reopen_ordinal,
        call_started=fields[11] != "-",
        backend=backend_present,
        pre_restore=pre_restore_present,
        observed=observed_present,
        completed=completed_present,
    )
    if phase is _RestoreCallPhase.COMPLETED:
        zero_slot_fields = [*fields]
        zero_slot_fields[21] = "0" * 64
        expected_digest = sha256(
            _COMPLETION_AUTHORITY_DOMAIN + "|".join(zero_slot_fields).encode("ascii")
        ).hexdigest()
        if fields[21] != expected_digest:
            _fail()
    return _RestoreCallAuthority(
        fields=tuple(fields),
        attempt_uuid=fields[1],
        kind=fields[2],
        target_digest=fields[3],
        source_digest=fields[4],
        call_ordinal=call_ordinal,
        reopen_ordinal=reopen_ordinal,
        phase=phase,
    )


def _validate_active_authority(gate: str, call: str) -> _RestoreCallAuthority:
    """验证 active gate 与任一合法非-completed call phase 的 exact 绑定。"""
    if type(gate) is not str:
        _fail()
    try:
        gate_bytes = gate.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        _fail()
    gate_fields = gate.split(":")
    if len(gate_fields) != 6 or gate_fields[:2] != ["restore", "v1"]:
        _fail()
    _, _, attempt, kind, target_digest, source_digest = gate_fields
    _canonical_uuid(attempt)
    if kind not in {"generic", "sealed_0018"}:
        _fail()
    _canonical_digest(target_digest)
    _canonical_digest(source_digest)
    expected_length = 185 if kind == "generic" else 189
    if len(gate_bytes) != expected_length:
        _fail()

    authority = _parse_call_authority(call)
    if authority.attempt_uuid != attempt or authority.kind != kind:
        _fail()
    if authority.target_digest != target_digest or authority.source_digest != source_digest:
        _fail()
    if authority.phase is _RestoreCallPhase.COMPLETED:
        _fail()
    return authority


def _validate_completed_authority(
    call: str,
    completion: str,
) -> _RestoreCallAuthority:
    """验证 completed call 与 completion GUC 的 zero-slot 双向 digest 绑定。"""
    authority = _parse_call_authority(call)
    if authority.phase is not _RestoreCallPhase.COMPLETED:
        _fail()
    expected_digest = authority.fields[21]
    if type(completion) is not str or completion != f"restore_completion:v1:{expected_digest}":
        _fail()
    return authority


def classify_database_admission(
    *,
    facts: DatabaseRestoreFacts,
    access_snapshot: DatabaseAccessSnapshot,
    object_grants_match: object,
    expected_target_identity_digest: object,
) -> DatabaseAdmissionState:
    """按 restore facts、database access 与独立 object grants 返回唯一 steady state。

    Args:
        facts: 同一 target owner connection 直接读取的三个 database-wide facts。
        access_snapshot: canonical database ACL/runtime-role/membership snapshot。
        object_grants_match: phase-aware verifier 的精确 bool 结果。
        expected_target_identity_digest: 当前持锁 target identity 的 canonical digest。

    Returns:
        ``pristine_idle | active | completed_idle`` 三态之一。

    Raises:
        DatabaseMaintenanceInvariantError: Candidate、cross-posture、malformed/partial facts、
            unsafe roles/membership 或 object-grant drift。
    """
    if (
        type(facts) is not DatabaseRestoreFacts
        or object_grants_match is not True
        or type(expected_target_identity_digest) is not str
    ):
        _fail()
    _canonical_digest(expected_target_identity_digest)
    profile = access_snapshot.acl_profile
    if profile is DatabaseAclProfile.BASELINE:
        transition_baseline_to_active(access_snapshot)
    elif profile is DatabaseAclProfile.ACTIVE:
        transition_active_to_baseline(access_snapshot)
    else:
        _fail()

    gate = facts.maintenance_gate
    call = facts.restore_call_authority
    completion = facts.restore_completion
    if gate is None and call is None and completion is None:
        if profile is not DatabaseAclProfile.BASELINE:
            _fail()
        return DatabaseAdmissionState.PRISTINE_IDLE
    if gate is not None and call is not None and completion is None:
        if profile is not DatabaseAclProfile.ACTIVE:
            _fail()
        authority = _validate_active_authority(gate, call)
        if authority.target_digest != expected_target_identity_digest:
            _fail()
        return DatabaseAdmissionState.ACTIVE
    if gate is None and call is not None and completion is not None:
        if profile is not DatabaseAclProfile.BASELINE:
            _fail()
        authority = _validate_completed_authority(call, completion)
        if authority.target_digest != expected_target_identity_digest:
            _fail()
        return DatabaseAdmissionState.COMPLETED_IDLE
    _fail()


def assert_ordinary_migration_admission(state: DatabaseAdmissionState) -> None:
    """只允许普通 migration 消费两个 stable idle state。

    Args:
        state: 经 :func:`classify_database_admission` 返回的精确枚举。

    Raises:
        DatabaseMaintenanceInvariantError: 输入不是枚举，或为 active/needs-attention。
    """
    if type(state) is not DatabaseAdmissionState or state not in {
        DatabaseAdmissionState.PRISTINE_IDLE,
        DatabaseAdmissionState.COMPLETED_IDLE,
    }:
        _fail()


def derive_database_target_identity(
    *,
    system_identifier_ascii: object,
    database_name_utf8: object,
    target_name_verified: object,
) -> DatabaseTargetIdentity:
    """从 canonical cluster ID 与 exact database name 派生目标身份和锁 key。

    ``target_name_verified`` 只能由持锁 lifecycle adapter 在以下两种事实成立后传入：
    已存在目标名从 ``pg_database.datname`` 读取并与 ``current_database()`` 逐字匹配，或
    protected reset 已完成非生产校验与全名人工确认。函数不猜测 DSN/环境中的缺失目标。

    Args:
        system_identifier_ascii: ``1..2**64-1`` 的无符号十进制 ASCII bytes。
        database_name_utf8: 1..63 bytes、无 NUL 的 exact PostgreSQL database name。
        target_name_verified: 必须是精确 ``True``，表示名称已有 catalog/确认事实。

    Returns:
        可供 management/target lock acquisition 使用的不可变目标身份。

    Raises:
        DatabaseMaintenanceInvariantError: 任一输入类型、编码、范围或验证事实不合法。
    """
    if type(target_name_verified) is not bool or target_name_verified is not True:
        _fail()
    canonical_system_identifier = _canonical_system_identifier_ascii(system_identifier_ascii)
    exact_database_name = _exact_database_name_utf8(database_name_utf8)
    identity_bytes = (
        TARGET_IDENTITY_DOMAIN + canonical_system_identifier + b"\0" + exact_database_name
    )
    digest = sha256(identity_bytes).digest()
    return DatabaseTargetIdentity(
        identity_bytes=identity_bytes,
        digest=digest,
        digest_hex=digest.hex(),
        lifecycle_lock_key=_signed_lock_key(DATABASE_LIFECYCLE_LOCK_DOMAIN, digest),
        target_lock_key=_signed_lock_key(DATABASE_MAINTENANCE_LOCK_DOMAIN, digest),
    )


def _exact_catalog_text(value: object) -> str:
    """读取非空、无 NUL 的 exact catalog text。"""
    if type(value) is not str or value == "" or "\0" in value:
        _fail()
    return value


def _positive_oid(value: object) -> int:
    """把 PostgreSQL OID 收窄为非 bool uint32 正整数。"""
    if type(value) is not int or not 1 <= value <= (1 << 32) - 1:
        _fail()
    return value


def _exact_bool(value: object) -> bool:
    """读取 PostgreSQL bool，拒绝整数或宽松 truthy/falsy。"""
    if type(value) is not bool:
        _fail()
    return value


def _validated_database_name(value: object) -> str:
    """把 CLI/URL 目标名收窄到 identity 使用的 exact UTF-8 PostgreSQL name。"""
    database_name = _exact_catalog_text(value)
    try:
        encoded = database_name.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail()
    _exact_database_name_utf8(encoded)
    return database_name


def _validated_secret(value: object) -> str:
    """在创建 engine/session 前验证 Secret 形状但不记录其内容。"""
    secret = _exact_catalog_text(value)
    try:
        secret.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail()
    return secret


def _read_management_target_catalog(
    connection: Connection,
    *,
    target_database_name: str,
    allow_absent: bool,
) -> _ManagementTargetCatalog:
    """从固定 management session 读取 cluster、session owner 与目标 catalog。

    ``allow_absent`` 只能由 protected reset 在 exact confirmed name 与 context target
    逐字节相同时传入。目标不存在时仍从同一 management session 读取 cluster ID 与
    当前 owner role，使用已确认名称派生与 CREATE 后完全相同的 lifecycle key；普通
    migration/role-bootstrap 传入 ``False``，因此在取得 advisory lock 前失败。

    Args:
        connection: 固定连接到 ``postgres`` 的 owner management session。
        target_database_name: 已验证的 exact target name。
        allow_absent: 是否允许返回仅供 protected reset 使用的 absent 形状。

    Returns:
        已存在目标的完整 catalog，或受控 absent catalog。

    Raises:
        DatabaseMaintenanceInvariantError: session、owner、名称、OID 或 absent authority
            不满足冻结不变量。
    """
    if type(allow_absent) is not bool:
        _fail()
    validated_name = _validated_database_name(target_database_name)
    result = connection.execute(
        text(_MANAGEMENT_TARGET_CATALOG_SQL),
        {"target_database_name": validated_name},
    )
    rows = result.mappings().all()
    if len(rows) != 1:
        _fail()
    row = rows[0]
    management_database_name = _exact_catalog_text(
        _required_mapping_value(row, "management_database_name")
    )
    current_role_name = _exact_catalog_text(_required_mapping_value(row, "current_role_name"))
    current_role_oid = _positive_oid(_required_mapping_value(row, "current_role_oid"))
    if management_database_name != "postgres":
        _fail()
    system_identifier = signed_system_identifier_to_uint64(
        _required_mapping_value(row, "system_identifier")
    )

    target_database_oid = _required_mapping_value(row, "target_database_oid")
    target_database_name_value = _required_mapping_value(row, "target_database_name")
    owner_oid_value = _required_mapping_value(row, "owner_oid")
    owner_role_name_value = _required_mapping_value(row, "owner_role_name")
    target_values = (
        target_database_oid,
        target_database_name_value,
        owner_oid_value,
        owner_role_name_value,
    )
    if all(value is None for value in target_values):
        if not allow_absent:
            _fail()
        identity = derive_database_target_identity(
            system_identifier_ascii=str(system_identifier).encode("ascii"),
            database_name_utf8=validated_name.encode("utf-8"),
            target_name_verified=True,
        )
        return _AbsentDatabaseTargetCatalog(
            identity=identity,
            owner_oid=current_role_oid,
            owner_role_name=current_role_name,
            database_name=validated_name,
        )
    if any(value is None for value in target_values):
        _fail()

    observed_database_name = _exact_catalog_text(target_database_name_value)
    owner_oid = _positive_oid(owner_oid_value)
    owner_role_name = _exact_catalog_text(owner_role_name_value)
    if (
        observed_database_name != validated_name
        or current_role_name != owner_role_name
        or current_role_oid != owner_oid
    ):
        _fail()
    identity = derive_database_target_identity(
        system_identifier_ascii=str(system_identifier).encode("ascii"),
        database_name_utf8=observed_database_name.encode("utf-8"),
        target_name_verified=True,
    )
    return _DatabaseTargetCatalog(
        identity=identity,
        database_oid=_positive_oid(target_database_oid),
        owner_oid=owner_oid,
        owner_role_name=owner_role_name,
        database_name=observed_database_name,
    )


def _read_target_catalog(
    connection: Connection,
    *,
    expected: _DatabaseTargetCatalog,
) -> _DatabaseTargetCatalog:
    """从目标 owner session 重建 identity，并与 management catalog 逐字段比较。"""
    rows = connection.execute(text(_TARGET_CATALOG_SQL)).mappings().all()
    if len(rows) != 1:
        _fail()
    row = rows[0]
    database_name = _exact_catalog_text(_required_mapping_value(row, "target_database_name"))
    current_role_name = _exact_catalog_text(_required_mapping_value(row, "current_role_name"))
    owner_role_name = _exact_catalog_text(_required_mapping_value(row, "owner_role_name"))
    system_identifier = signed_system_identifier_to_uint64(
        _required_mapping_value(row, "system_identifier")
    )
    observed = _DatabaseTargetCatalog(
        identity=derive_database_target_identity(
            system_identifier_ascii=str(system_identifier).encode("ascii"),
            database_name_utf8=database_name.encode("utf-8"),
            target_name_verified=True,
        ),
        database_oid=_positive_oid(_required_mapping_value(row, "target_database_oid")),
        owner_oid=_positive_oid(_required_mapping_value(row, "owner_oid")),
        owner_role_name=owner_role_name,
        database_name=database_name,
    )
    if current_role_name != owner_role_name or observed != expected:
        _fail()
    return observed


def _read_current_revision(
    connection: Connection,
    *,
    authority: PublishedAlembicAuthority,
) -> str:
    """读取唯一 current revision，并把未发布/planned 标签转为维护域稳定拒绝。"""
    try:
        return read_current_alembic_revision(connection, authority=authority)
    except AlembicMigrationInvariantError as error:
        raise DatabaseMaintenanceInvariantError(_INVARIANT_ERROR_MESSAGE) from error


def _has_active_non_owner_sessions(
    connection: Connection,
    *,
    target: _DatabaseTargetCatalog,
) -> bool:
    """判断目标库是否存在当前 owner backend 之外的非 owner session。"""
    value = connection.execute(
        text(_ACTIVE_NON_OWNER_SESSIONS_SQL),
        {
            "target_database_oid": target.database_oid,
            "owner_oid": target.owner_oid,
        },
    ).scalar_one()
    return _exact_bool(value)


def _assert_no_target_sessions(
    connection: Connection,
    *,
    target: _DatabaseTargetCatalog,
) -> None:
    """在 management session 上证明 DROP 前目标数据库没有任何连接。

    该检查发生在旧 target/schema lease 已释放之后，并仍处于同一个 management
    lifecycle advisory lease 内。它拒绝所有 session，而不只拒绝非 owner session，避免
    ``DROP DATABASE`` 依赖竞态或隐式 force 行为。
    """
    value = connection.execute(
        text(_TARGET_HAS_SESSIONS_SQL),
        {"target_database_oid": target.database_oid},
    ).scalar_one()
    if _exact_bool(value):
        _rollback_open_transaction(connection)
        _fail()
    _commit_read_transaction(connection)


def _bootstrap_grant_posture(
    access_snapshot: DatabaseAccessSnapshot,
) -> BootstrapGrantPosture:
    """把双角色存在形状映射为 object-grant bootstrap 的唯一 typed posture。"""
    app_present = access_snapshot.app_role is not None
    retention_present = access_snapshot.retention_role is not None
    if not app_present and not retention_present:
        return BootstrapGrantPosture.PRE_RUNTIME
    if app_present and retention_present:
        return BootstrapGrantPosture.CURRENT
    _fail()


def _facts_absent(facts: DatabaseRestoreFacts) -> bool:
    """返回三个 database-wide restore fact 是否精确全部 absent。"""
    if type(facts) is not DatabaseRestoreFacts:
        _fail()
    return (
        facts.maintenance_gate is None
        and facts.restore_call_authority is None
        and facts.restore_completion is None
    )


def _commit_read_transaction(connection: Connection) -> None:
    """结束 catalog/autobegin 只读事务，同时保留 session-level advisory locks。"""
    if connection.in_transaction():
        connection.commit()


def _rollback_open_transaction(connection: Connection) -> None:
    """在 invariant/runner 异常后清理连接事务，session lock 仍保持到显式释放。"""
    if connection.in_transaction():
        connection.rollback()


def _acquire_single_session_lock(connection: Connection, *, lock_key: int) -> None:
    """非阻塞取得单 bigint session advisory lock；已被持有时立即拒绝。"""
    acquired = connection.execute(
        text(_TRY_SINGLE_LOCK_SQL),
        {"lock_key": lock_key},
    ).scalar_one()
    if type(acquired) is not bool:
        _rollback_open_transaction(connection)
        _fail()
    _commit_read_transaction(connection)
    if not acquired:
        _fail()


def _release_single_session_lock(connection: Connection, *, lock_key: int) -> None:
    """在关闭 session 前显式释放单 bigint advisory lock并验证唯一持有事实。"""
    _rollback_open_transaction(connection)
    released = connection.execute(
        text(_UNLOCK_SINGLE_LOCK_SQL),
        {"lock_key": lock_key},
    ).scalar_one()
    if type(released) is not bool:
        _rollback_open_transaction(connection)
        _fail()
    _commit_read_transaction(connection)
    if not released:
        _fail()


def _acquire_schema_session_lock(connection: Connection) -> None:
    """非阻塞取得固定双 int32 schema-lifecycle exclusive lock。"""
    parameters = {
        "lock_class": SCHEMA_LIFECYCLE_LOCK[0],
        "lock_object": SCHEMA_LIFECYCLE_LOCK[1],
    }
    acquired = connection.execute(text(_TRY_SCHEMA_LOCK_SQL), parameters).scalar_one()
    if type(acquired) is not bool:
        _rollback_open_transaction(connection)
        _fail()
    _commit_read_transaction(connection)
    if not acquired:
        _fail()


def _release_schema_session_lock(connection: Connection) -> None:
    """显式释放固定 schema-lifecycle lock并验证本 session 曾持有。"""
    _rollback_open_transaction(connection)
    parameters = {
        "lock_class": SCHEMA_LIFECYCLE_LOCK[0],
        "lock_object": SCHEMA_LIFECYCLE_LOCK[1],
    }
    released = connection.execute(text(_UNLOCK_SCHEMA_LOCK_SQL), parameters).scalar_one()
    if type(released) is not bool:
        _rollback_open_transaction(connection)
        _fail()
    _commit_read_transaction(connection)
    if not released:
        _fail()


class SqlAlchemyDatabaseMaintenanceContext:
    """用同步 SQLAlchemy/psycopg session 实现可信数据库生命周期端口。

    context 不创建或释放 Engine；调用方负责用 ``NullPool`` 或等价短生命周期配置构造
    固定 ``postgres`` management engine 与精确 target engine。两个 Engine 必须使用同一
    database owner credential。context 只消费已存在依赖，不引入新驱动或第二套 SQL。
    """

    def __init__(
        self,
        *,
        management_engine: Engine,
        target_engine: Engine,
        target_database_name: str,
        bootstrap_caller: BootstrapCaller,
        app_password: str | None,
        retention_password: str | None,
        migration_runner: Callable[[OnlineMigrationAuthority], None],
        published_authority: PublishedAlembicAuthority,
        reset_policy: ProtectedResetPolicy | None = None,
    ) -> None:
        """保存无敏感 repr 的连接工厂与窄 callback，并在任何 session 前验证输入。

        runtime-role 密码虽然始终作为 bind parameter 发送，但 SQLAlchemy 默认会在异常与
        engine 日志中渲染参数。所有入口都必须显式启用 ``hide_parameters``；context 在
        首次连接前再次验证，避免非 CLI 调用绕过这一 Secret 边界。
        """
        if (
            getattr(management_engine, "hide_parameters", None) is not True
            or getattr(target_engine, "hide_parameters", None) is not True
        ):
            _fail()
        if (
            type(bootstrap_caller) is not BootstrapCaller
            or not callable(migration_runner)
            or type(published_authority) is not PublishedAlembicAuthority
        ):
            _fail()
        if (app_password is None) is not (retention_password is None):
            _fail()
        if bootstrap_caller is BootstrapCaller.DB_RESET_POST_CREATE:
            if (
                type(reset_policy) is not ProtectedResetPolicy
                or app_password is None
                or retention_password is None
            ):
                _fail()
        elif reset_policy is not None:
            # 普通 role-bootstrap/migrate 不得因误传 policy 隐式获得 destructive capability。
            _fail()
        self._management_engine = management_engine
        self._target_engine = target_engine
        self._target_database_name = _validated_database_name(target_database_name)
        self._bootstrap_caller = bootstrap_caller
        self._app_password = None if app_password is None else _validated_secret(app_password)
        self._retention_password = (
            None if retention_password is None else _validated_secret(retention_password)
        )
        self._migration_runner = migration_runner
        self._published_authority = published_authority
        self._reset_policy = reset_policy

    def _allow_absent_target(self) -> bool:
        """只在 protected reset 的 policy 与 context 名称 exact 相同时开放 absent read。"""
        policy = self._reset_policy
        if (
            self._bootstrap_caller is not BootstrapCaller.DB_RESET_POST_CREATE
            or type(policy) is not ProtectedResetPolicy
        ):
            return False
        confirmed_name = _validated_database_name(policy.confirmed_database_name)
        return confirmed_name.encode("utf-8") == self._target_database_name.encode("utf-8")

    @contextmanager
    def acquire_management_lifecycle_lock(
        self,
    ) -> Iterator[ManagementLifecycleLease]:
        """在固定 management session 上 double-read 目标并持有 lifecycle lock。

        普通入口即使底层 reader 被替换并错误返回 absent 类型，也会在 advisory lock 前
        二次 fail closed；只有 exact-confirmed protected reset 可用 absent identity 建锁。
        """
        allow_absent = self._allow_absent_target()
        with self._management_engine.connect() as connection:
            discovered = _read_management_target_catalog(
                connection,
                target_database_name=self._target_database_name,
                allow_absent=allow_absent,
            )
            if type(discovered) is _AbsentDatabaseTargetCatalog and not allow_absent:
                _fail()
            if type(discovered) not in {
                _DatabaseTargetCatalog,
                _AbsentDatabaseTargetCatalog,
            }:
                _fail()
            _acquire_single_session_lock(
                connection,
                lock_key=discovered.identity.lifecycle_lock_key,
            )
            try:
                locked = _read_management_target_catalog(
                    connection,
                    target_database_name=self._target_database_name,
                    allow_absent=allow_absent,
                )
                if type(locked) is not type(discovered) or locked != discovered:
                    _fail()
                _commit_read_transaction(connection)
                yield _SqlAlchemyManagementLifecycleLease(
                    context=self,
                    connection=connection,
                    target=locked,
                )
            finally:
                _release_single_session_lock(
                    connection,
                    lock_key=discovered.identity.lifecycle_lock_key,
                )


class _SqlAlchemyManagementLifecycleLease:
    """保存 live management session 与已锁定 target identity。"""

    def __init__(
        self,
        *,
        context: SqlAlchemyDatabaseMaintenanceContext,
        connection: Connection,
        target: _ManagementTargetCatalog,
    ) -> None:
        self._context = context
        self._connection = connection
        self._target: _ManagementTargetCatalog = target
        self._exact_target_confirmed = False
        self._reset_admitted_target: _ManagementTargetCatalog | None = None
        self._post_create_provenance: _PostCreateTargetProvenance | None = None
        self._active_post_create_provenance: _PostCreateTargetProvenance | None = None

    @contextmanager
    def acquire_target_lock(self) -> Iterator[TargetMaintenanceLease]:
        """在 management lock 仍持有时取得同 identity 的 target session lock。"""
        target = self._target
        if type(target) is not _DatabaseTargetCatalog:
            _fail()
        provenance = self._active_post_create_provenance
        if provenance is not None and (
            provenance is not self._post_create_provenance
            or provenance.target != target
            or provenance.lifecycle_lock_key != target.identity.lifecycle_lock_key
        ):
            _fail()
        with self._context._target_engine.connect() as connection:
            _read_target_catalog(connection, expected=target)
            _acquire_single_session_lock(
                connection,
                lock_key=target.identity.target_lock_key,
            )
            try:
                _read_target_catalog(connection, expected=target)
                revision = _read_current_revision(
                    connection,
                    authority=self._context._published_authority,
                )
                _commit_read_transaction(connection)
                yield _SqlAlchemyTargetMaintenanceLease(
                    context=self._context,
                    connection=connection,
                    target=target,
                    revision=revision,
                    post_create_provenance=provenance,
                )
            finally:
                _release_single_session_lock(
                    connection,
                    lock_key=target.identity.target_lock_key,
                )

    @contextmanager
    def _acquire_post_create_target_lock(
        self,
        provenance: _PostCreateTargetProvenance,
    ) -> Iterator[TargetMaintenanceLease]:
        """仅在 bootstrap 调用栈内把一次 CREATE provenance 注入 target lease。"""
        if (
            type(provenance) is not _PostCreateTargetProvenance
            or provenance is not self._post_create_provenance
            or self._active_post_create_provenance is not None
            or type(self._target) is not _DatabaseTargetCatalog
            or provenance.target != self._target
        ):
            _fail()
        self._active_post_create_provenance = provenance
        try:
            with self.acquire_target_lock() as target:
                yield target
        finally:
            self._active_post_create_provenance = None

    def confirm_exact_non_production_target(self) -> None:
        """逐字节复核 policy 环境、确认名称与锁定 catalog 目标完全相同。"""
        policy = self._context._reset_policy
        if type(policy) is not ProtectedResetPolicy:
            _fail()
        confirmed_name = _validated_database_name(policy.confirmed_database_name)
        if policy.app_env not in {"development", "test"}:
            _fail()
        if confirmed_name.encode("utf-8") != self._target.database_name.encode(
            "utf-8"
        ) or confirmed_name.encode("utf-8") != self._context._target_database_name.encode("utf-8"):
            _fail()
        self._exact_target_confirmed = True

    def assert_reset_allowed(self) -> None:
        """完成首写前 reset admission；absent 只记录本次 exact-confirmed 事实。"""
        if (
            not self._exact_target_confirmed
            or self._reset_admitted_target is not None
            or self._post_create_provenance is not None
        ):
            _fail()
        if type(self._target) is _AbsentDatabaseTargetCatalog:
            self._reset_admitted_target = self._target
            return
        if type(self._target) is not _DatabaseTargetCatalog:
            _fail()
        # 显式嵌套用于冻结 target→schema 锁序，不能由样式规则折叠成不易审阅的一行。
        with self.acquire_target_lock() as target:  # noqa: SIM117
            with target.acquire_schema_lifecycle_lock():
                target.assert_reset_allowed_for_drop()
        self._reset_admitted_target = self._target

    def drop_and_create_target(self) -> None:
        """在同一 management lease 内对 existing 执行 DROP/CREATE、对 absent 仅 CREATE。"""
        policy = self._context._reset_policy
        if type(policy) is not ProtectedResetPolicy:
            _fail()
        admitted_target = self._reset_admitted_target
        if (
            not self._exact_target_confirmed
            or admitted_target is None
            or admitted_target != self._target
            or self._post_create_provenance is not None
            or policy.confirmed_database_name != self._target.database_name
        ):
            _fail()
        if type(admitted_target) is _DatabaseTargetCatalog:
            _assert_no_target_sessions(self._connection, target=admitted_target)
        elif type(admitted_target) is not _AbsentDatabaseTargetCatalog:
            _fail()

        preparer = self._connection.dialect.identifier_preparer
        quoted_database = preparer.quote_identifier(admitted_target.database_name)
        quoted_owner = preparer.quote_identifier(admitted_target.owner_role_name)
        autocommit_connection = self._connection.execution_options(isolation_level="AUTOCOMMIT")
        # destructive/create authority 在第一条 DDL 前一次性消费；异常不能在同一 lease 内盲重放。
        self._reset_admitted_target = None
        if type(admitted_target) is _DatabaseTargetCatalog:
            autocommit_connection.execute(text(f"DROP DATABASE {quoted_database}"))
        autocommit_connection.execute(
            text(f"CREATE DATABASE {quoted_database} OWNER {quoted_owner}")
        )

        # CREATE 成功不能仅凭原 DSN 猜测目标；必须从同一 management session 重读同名
        # catalog，并重新绑定 system ID、exact name、owner 与新 OID。
        created = _read_management_target_catalog(
            self._connection,
            target_database_name=admitted_target.database_name,
            allow_absent=False,
        )
        if (
            type(created) is not _DatabaseTargetCatalog
            or created.identity != admitted_target.identity
            or created.identity.lifecycle_lock_key != admitted_target.identity.lifecycle_lock_key
            or created.owner_oid != admitted_target.owner_oid
            or created.owner_role_name != admitted_target.owner_role_name
            or created.database_name != admitted_target.database_name
        ):
            _fail()
        self._target = created
        self._post_create_provenance = _PostCreateTargetProvenance(
            target=created,
            lifecycle_lock_key=admitted_target.identity.lifecycle_lock_key,
        )

    def bootstrap_new_target(self) -> None:
        """紧接 CREATE，以 reset-only caller 把新 Candidate 原子收敛为 baseline。"""
        provenance = self._post_create_provenance
        if (
            self._context._bootstrap_caller is not BootstrapCaller.DB_RESET_POST_CREATE
            or type(provenance) is not _PostCreateTargetProvenance
            or type(self._target) is not _DatabaseTargetCatalog
            or provenance.target != self._target
        ):
            _fail()
        with self._acquire_post_create_target_lock(provenance) as target:
            target.assert_restore_facts_absent()
            candidate = target.read_bootstrap_candidate()
            with target.acquire_schema_lifecycle_lock():
                with target.owner_transaction() as transaction:
                    transaction.apply_safe_runtime_roles(candidate)
                    transaction.apply_database_acl(DatabaseAclProfile.BASELINE)
                    transaction.apply_object_grants(
                        revision=target.revision,
                        phase=GrantPhase.BASELINE,
                    )
                    transaction.verify_pristine_idle_before_commit()
                target.verify_pristine_idle_after_commit()
        # 只有完整 bootstrap + commit 后复核成功才消费 provenance；后续 migration 使用普通 lease。
        self._post_create_provenance = None

    def run_typed_migration(self) -> None:
        """在同一 live management lease 下以新 target/schema session 执行普通 migration。"""
        if (
            self._post_create_provenance is not None
            or type(self._target) is not _DatabaseTargetCatalog
        ):
            _fail()
        with self.acquire_target_lock() as target:
            target.assert_migration_allowed()
            with target.acquire_schema_lifecycle_lock():
                target.run_typed_migration()


class _SqlAlchemyTargetMaintenanceLease:
    """保存 target owner session、revision 与 schema-lock/transaction 状态。"""

    def __init__(
        self,
        *,
        context: SqlAlchemyDatabaseMaintenanceContext,
        connection: Connection,
        target: _DatabaseTargetCatalog,
        revision: str,
        post_create_provenance: _PostCreateTargetProvenance | None = None,
    ) -> None:
        self._context = context
        self._connection = connection
        self._target = target
        if post_create_provenance is not None and (
            type(post_create_provenance) is not _PostCreateTargetProvenance
            or context._bootstrap_caller is not BootstrapCaller.DB_RESET_POST_CREATE
            or post_create_provenance.target != target
            or post_create_provenance.lifecycle_lock_key != target.identity.lifecycle_lock_key
        ):
            _fail()
        self._post_create_provenance = post_create_provenance
        if not context._published_authority.contains(revision):
            _fail()
        self.revision = revision
        self._schema_lock_held = False
        self._owner_transaction_active = False
        self._migration_admitted_revision: str | None = None
        self._role_bootstrap_admission: (
            BootstrapCandidate | _StableRoleBootstrapAdmission | None
        ) = None

    def _candidate_grant_posture(
        self,
        access_snapshot: DatabaseAccessSnapshot,
    ) -> BootstrapGrantPosture:
        """冻结 Candidate 的 object-grant posture，并让 CREATE provenance 优先于角色存在性。

        app/retention role 是 cluster-wide 对象，重建数据库时通常仍存在；它们不能证明新库
        已拥有 database-local object grants。只有本次 CREATE provenance 可把该合法形状
        收窄为 ``PRE_RUNTIME``，standalone role-bootstrap 继续使用 catalog 观察值。
        """
        observed = _bootstrap_grant_posture(access_snapshot)
        provenance = self._post_create_provenance
        if provenance is None:
            return observed
        if (
            provenance.target != self._target
            or provenance.lifecycle_lock_key != self._target.identity.lifecycle_lock_key
        ):
            _fail()
        return BootstrapGrantPosture.PRE_RUNTIME

    def _read_bootstrap_candidate(self) -> BootstrapCandidate:
        """在当前 target session 重读全部 candidate admission 事实。"""
        _read_target_catalog(self._connection, expected=self._target)
        revision = _read_current_revision(
            self._connection,
            authority=self._context._published_authority,
        )
        if revision != self.revision:
            _fail()
        facts = read_database_restore_facts(
            self._connection,
            target_database_oid=self._target.database_oid,
        )
        access_snapshot = read_database_access_snapshot_sync(
            self._connection,
            target_database_oid=self._target.database_oid,
        )
        has_non_owner_sessions = _has_active_non_owner_sessions(
            self._connection,
            target=self._target,
        )
        verify_bootstrap_object_grants(
            self._connection,
            revision=revision,
            phase=GrantPhase.BASELINE,
            posture=self._candidate_grant_posture(access_snapshot),
        )
        return classify_bootstrap_candidate(
            access_snapshot,
            caller=self._context._bootstrap_caller,
            restore_facts_absent=_facts_absent(facts),
            has_active_non_owner_sessions=has_non_owner_sessions,
            object_grants_match=True,
        )

    def _read_stable_admission(
        self, *, require_revision: str | None
    ) -> tuple[str, DatabaseAdmissionState]:
        """在当前 session 重读 steady access/facts/session/object-grant admission。"""
        _read_target_catalog(self._connection, expected=self._target)
        revision = _read_current_revision(
            self._connection,
            authority=self._context._published_authority,
        )
        if require_revision is not None and revision != require_revision:
            _fail()
        if _has_active_non_owner_sessions(self._connection, target=self._target):
            _fail()
        facts = read_database_restore_facts(
            self._connection,
            target_database_oid=self._target.database_oid,
        )
        access_snapshot = read_database_access_snapshot_sync(
            self._connection,
            target_database_oid=self._target.database_oid,
        )
        if access_snapshot.acl_profile is DatabaseAclProfile.BASELINE:
            phase = GrantPhase.BASELINE
        elif access_snapshot.acl_profile is DatabaseAclProfile.ACTIVE:
            phase = GrantPhase.ACTIVE
        else:
            _fail()
        verify_object_grants(
            self._connection,
            revision=revision,
            phase=phase,
        )
        return revision, classify_database_admission(
            facts=facts,
            access_snapshot=access_snapshot,
            object_grants_match=True,
            expected_target_identity_digest=self._target.identity.digest_hex,
        )

    def _read_role_bootstrap_admission(
        self,
    ) -> BootstrapCandidate | _StableRoleBootstrapAdmission:
        """从一次完整 catalog snapshot 区分 Candidate 与 steady idle role-bootstrap。"""
        if self._context._bootstrap_caller is not BootstrapCaller.ROLE_BOOTSTRAP:
            _fail()
        _read_target_catalog(self._connection, expected=self._target)
        revision = _read_current_revision(
            self._connection,
            authority=self._context._published_authority,
        )
        if revision != self.revision:
            _fail()
        if _has_active_non_owner_sessions(self._connection, target=self._target):
            _fail()
        facts = read_database_restore_facts(
            self._connection,
            target_database_oid=self._target.database_oid,
        )
        access_snapshot = read_database_access_snapshot_sync(
            self._connection,
            target_database_oid=self._target.database_oid,
        )
        if access_snapshot.acl_profile in {
            DatabaseAclProfile.FRESH_DEFAULT,
            DatabaseAclProfile.PRE_PROTOCOL_LEGACY,
        }:
            verify_bootstrap_object_grants(
                self._connection,
                revision=revision,
                phase=GrantPhase.BASELINE,
                posture=_bootstrap_grant_posture(access_snapshot),
            )
            return classify_bootstrap_candidate(
                access_snapshot,
                caller=BootstrapCaller.ROLE_BOOTSTRAP,
                restore_facts_absent=_facts_absent(facts),
                has_active_non_owner_sessions=False,
                object_grants_match=True,
            )
        if access_snapshot.acl_profile is not DatabaseAclProfile.BASELINE:
            _fail()
        verify_object_grants(
            self._connection,
            revision=revision,
            phase=GrantPhase.BASELINE,
        )
        state = classify_database_admission(
            facts=facts,
            access_snapshot=access_snapshot,
            object_grants_match=True,
            expected_target_identity_digest=self._target.identity.digest_hex,
        )
        assert_ordinary_migration_admission(state)
        return _StableRoleBootstrapAdmission(
            revision=revision,
            state=state,
            facts=facts,
            access_snapshot=access_snapshot,
        )

    def assert_restore_facts_absent(self) -> None:
        """在进入 schema lock 前要求三个 database-wide fact 精确 absent。"""
        try:
            facts = read_database_restore_facts(
                self._connection,
                target_database_oid=self._target.database_oid,
            )
            if not _facts_absent(facts):
                _fail()
        except BaseException:
            _rollback_open_transaction(self._connection)
            raise
        _commit_read_transaction(self._connection)

    def read_bootstrap_candidate(self) -> BootstrapCandidate:
        """读取并返回只供本次锁持有期使用的瞬态 Candidate。"""
        try:
            candidate = self._read_bootstrap_candidate()
        except BaseException:
            _rollback_open_transaction(self._connection)
            raise
        _commit_read_transaction(self._connection)
        return candidate

    @contextmanager
    def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
        """在 target lock 后取得固定 schema lock，禁止同 lease 重入。"""
        if self._schema_lock_held or self._owner_transaction_active:
            _fail()
        _acquire_schema_session_lock(self._connection)
        self._schema_lock_held = True
        try:
            yield
        finally:
            self._schema_lock_held = False
            _release_schema_session_lock(self._connection)

    @contextmanager
    def owner_transaction(self) -> Iterator[OwnerBootstrapTransaction]:
        """在三把 session lock 内开启唯一显式 owner transaction。"""
        if not self._schema_lock_held or self._owner_transaction_active:
            _fail()
        _commit_read_transaction(self._connection)
        self._owner_transaction_active = True
        try:
            with self._connection.begin():
                yield _SqlAlchemyOwnerBootstrapTransaction(target=self)
        finally:
            self._owner_transaction_active = False

    def verify_pristine_idle_after_commit(self) -> None:
        """commit ACK 后仍持三把锁，以新事务重验 exact pristine idle。"""
        try:
            revision, state = self._read_stable_admission(require_revision=self.revision)
            if revision != self.revision or state is not DatabaseAdmissionState.PRISTINE_IDLE:
                _fail()
        except BaseException:
            _rollback_open_transaction(self._connection)
            raise
        _commit_read_transaction(self._connection)

    def assert_reset_allowed_for_drop(self) -> None:
        """重验 stable idle reset admission，但不设置后续 migration runner 的授权状态。"""
        try:
            revision, state = self._read_stable_admission(require_revision=self.revision)
            assert_ordinary_migration_admission(state)
            if revision != self.revision:
                _fail()
        except BaseException:
            _rollback_open_transaction(self._connection)
            raise
        _commit_read_transaction(self._connection)

    def assert_migration_allowed(self) -> None:
        """在 schema lock/runner 前只接纳 exact pristine/completed idle。"""
        try:
            revision, state = self._read_stable_admission(require_revision=self.revision)
            assert_ordinary_migration_admission(state)
        except BaseException:
            _rollback_open_transaction(self._connection)
            raise
        _commit_read_transaction(self._connection)
        self._migration_admitted_revision = revision

    def assert_role_bootstrap_allowed(self) -> None:
        """在 schema lock 前冻结 Candidate 或 steady idle 的完整 typed admission。"""
        try:
            admission = self._read_role_bootstrap_admission()
        except BaseException:
            _rollback_open_transaction(self._connection)
            raise
        _commit_read_transaction(self._connection)
        self._role_bootstrap_admission = admission

    def run_typed_role_bootstrap(self) -> None:
        """在 schema lock 内收敛 Candidate，或重申 steady 密码与最小 grants。"""
        admission = self._role_bootstrap_admission
        if not self._schema_lock_held or admission is None:
            _fail()
        app_password = self._context._app_password
        retention_password = self._context._retention_password
        if type(app_password) is not str or type(retention_password) is not str:
            _fail()

        if type(admission) is BootstrapCandidate:
            with self.owner_transaction() as transaction:
                transaction.apply_safe_runtime_roles(admission)
                transaction.apply_database_acl(DatabaseAclProfile.BASELINE)
                transaction.apply_object_grants(
                    revision=self.revision,
                    phase=GrantPhase.BASELINE,
                )
                transaction.verify_pristine_idle_before_commit()
            self.verify_pristine_idle_after_commit()
            self._role_bootstrap_admission = None
            return

        if type(admission) is not _StableRoleBootstrapAdmission:
            _fail()
        try:
            _commit_read_transaction(self._connection)
            with self._connection.begin():
                observed = self._read_role_bootstrap_admission()
                if observed != admission or type(observed) is not _StableRoleBootstrapAdmission:
                    _fail()
                rotate_safe_runtime_role_passwords(
                    self._connection,
                    observed.access_snapshot,
                    app_password=app_password,
                    retention_password=retention_password,
                )
                apply_baseline_connect_reassertion(
                    self._connection,
                    target_database_name=self._target.database_name,
                    snapshot=observed.access_snapshot,
                )
                apply_bootstrap_object_grants(
                    self._connection,
                    revision=observed.revision,
                    phase=GrantPhase.BASELINE,
                    posture=BootstrapGrantPosture.CURRENT,
                )
                if self._read_role_bootstrap_admission() != admission:
                    _fail()
            if self._read_role_bootstrap_admission() != admission:
                _fail()
        except BaseException:
            _rollback_open_transaction(self._connection)
            raise
        _commit_read_transaction(self._connection)
        self._role_bootstrap_admission = None

    def _issue_online_migration_authority(
        self,
        *,
        expected_target_revision: str,
    ) -> OnlineMigrationAuthority:
        """在同一 schema-lock lease 内重验 admission 并发行唯一 typed token。

        source revision、target identity、published chain 与 baseline phase 都由
        ``alembic`` 私有 issuer 从本 concrete lease 派生；调用方只能选择
        已发布的精确 target revision。catalog 重读产生的只读事务在返回
        token 前结束，session-level 三锁仍持有。

        Args:
            expected_target_revision: 本次官方 Alembic 命令必须精确到达的 revision。

        Returns:
            绑定本 lease 的不可拆分 online migration authority。

        Raises:
            AlembicMigrationInvariantError: issuer 发现 lease/发布链/admission 不合法。
            DatabaseMaintenanceInvariantError: 调用时未持 schema lock 或未先冻结 admission。
        """
        if (
            not self._schema_lock_held
            or self._owner_transaction_active
            or self._migration_admitted_revision is None
        ):
            _fail()
        try:
            authority = _issue_online_migration_authority(
                lease=self,
                expected_target_revision=expected_target_revision,
            )
        except AlembicMigrationInvariantError as error:
            _rollback_open_transaction(self._connection)
            raise DatabaseMaintenanceInvariantError(_INVARIANT_ERROR_MESSAGE) from error
        except BaseException:
            _rollback_open_transaction(self._connection)
            raise
        _commit_read_transaction(self._connection)
        return authority

    def _verify_online_migration_result(
        self,
        authority: OnlineMigrationAuthority,
    ) -> None:
        """在释放三锁前核对 token 消费与精确最终 revision/posture。

        生产 head migration、历史 upgrade/downgrade 与 metadata check 共用该结果
        边界；因此 tests-local helper 不能在锁外自行解释成功。只有同一
        token 的 grant lifecycle 已完成，且新事务重读仍是 stable idle 和精确
        target revision，才会更新 lease 的 revision 并消费 admission。

        Args:
            authority: 本 lease 刚发行并交给官方 Alembic 命令的 token。

        Raises:
            DatabaseMaintenanceInvariantError: token 异连接/异目标、命令未完成或结果不合法。
        """
        if type(authority) is not OnlineMigrationAuthority:
            _fail()
        try:
            authority.__post_init__()
        except AlembicMigrationInvariantError as error:
            raise DatabaseMaintenanceInvariantError(_INVARIANT_ERROR_MESSAGE) from error

        admitted_revision = self._migration_admitted_revision
        if (
            not self._schema_lock_held
            or self._owner_transaction_active
            or admitted_revision is None
            or authority.connection is not self._connection
            or authority.published_authority is not self._context._published_authority
            or authority.expected_target_identity_digest != self._target.identity.digest_hex
            or authority.expected_current_revision != admitted_revision
            or authority.phase is not GrantPhase.BASELINE
            or authority.grant_lifecycle._finished is not True
            or self._connection.in_transaction()
        ):
            _fail()

        try:
            resulting_revision, resulting_state = self._read_stable_admission(
                require_revision=authority.expected_target_revision
            )
            assert_ordinary_migration_admission(resulting_state)
            if resulting_revision != authority.expected_target_revision:
                _fail()
        except BaseException:
            _rollback_open_transaction(self._connection)
            raise
        _commit_read_transaction(self._connection)
        self.revision = resulting_revision
        self._migration_admitted_revision = None

    def run_typed_migration(self) -> None:
        """在同一 target/schema session 上重验 admission、调用 runner并核对结果。"""
        if not self._schema_lock_held or self._migration_admitted_revision is None:
            _fail()
        try:
            online_authority = self._issue_online_migration_authority(
                expected_target_revision=self._context._published_authority.head_revision,
            )
            self._context._migration_runner(online_authority)
            self._verify_online_migration_result(online_authority)
        except BaseException:
            _rollback_open_transaction(self._connection)
            raise


class _SqlAlchemyOwnerBootstrapTransaction:
    """把三类 focused mutator 串为一个不可跳步的 owner transaction。"""

    def __init__(self, *, target: _SqlAlchemyTargetMaintenanceLease) -> None:
        self._target = target
        self._candidate: BootstrapCandidate | None = None
        self._plan_applied = False
        self._acl_applied = False
        self._grants_applied = False

    def apply_safe_runtime_roles(self, candidate: BootstrapCandidate) -> None:
        """重读 Candidate 后创建两角色或只轮换两个 safe role 密码。"""
        if self._candidate is not None:
            _fail()
        app_password = self._target._context._app_password
        retention_password = self._target._context._retention_password
        if type(app_password) is not str or type(retention_password) is not str:
            _fail()
        observed = self._target._read_bootstrap_candidate()
        if observed != candidate:
            _fail()
        plan_bootstrap_candidate_to_baseline(candidate)
        apply_safe_runtime_roles(
            self._target._connection,
            candidate,
            app_password=app_password,
            retention_password=retention_password,
        )
        self._candidate = candidate
        self._plan_applied = True

    def apply_database_acl(self, profile: DatabaseAclProfile) -> None:
        """只允许紧随角色 mutation 应用 exact candidate→baseline ACL。"""
        if (
            not self._plan_applied
            or self._acl_applied
            or self._candidate is None
            or profile is not DatabaseAclProfile.BASELINE
        ):
            _fail()
        plan = plan_bootstrap_candidate_to_baseline(self._candidate)
        apply_database_acl_mutation(
            self._target._connection,
            target_database_name=self._target._target.database_name,
            plan=plan,
        )
        self._acl_applied = True

    def apply_object_grants(self, *, revision: str, phase: GrantPhase) -> None:
        """按 target lease 冻结的 Candidate posture 应用 current revision baseline grants。"""
        if (
            not self._acl_applied
            or self._grants_applied
            or self._candidate is None
            or revision != self._target.revision
            or phase is not GrantPhase.BASELINE
        ):
            _fail()
        apply_bootstrap_object_grants(
            self._target._connection,
            revision=revision,
            phase=phase,
            posture=self._target._candidate_grant_posture(self._candidate.snapshot),
        )
        self._grants_applied = True

    def verify_pristine_idle_before_commit(self) -> None:
        """在 commit 前重读全部 steady 事实并要求 exact pristine idle。"""
        if not self._grants_applied:
            _fail()
        revision, state = self._target._read_stable_admission(
            require_revision=self._target.revision
        )
        if revision != self._target.revision or state is not DatabaseAdmissionState.PRISTINE_IDLE:
            _fail()


def bootstrap_candidate_to_baseline(context: DatabaseMaintenanceContext) -> None:
    """按固定三锁顺序把唯一合法 Candidate 原子收敛为 ``pristine_idle``。

    本函数只编排窄端口，不包含 ACL、角色、membership 或 object-grant SQL，也不复制
    revision inventory。所有 mutation 与 catalog 重读由 focused adapter 实现；异常自然
    穿透 context manager，使 owner transaction rollback 后再按逆序释放 schema、target、
    management session locks。

    Args:
        context: 已绑定单一 cluster/target 且能提供真实 session lease 的维护 context。

    Raises:
        DatabaseMaintenanceInvariantError: 任一 lock、Candidate、authority 或最终 posture
            不满足冻结闭集时，由具体 adapter 抛出。
    """
    # 显式嵌套用于把安全锁序直接呈现在代码结构中，不能合并为难以审阅的一行。
    with context.acquire_management_lifecycle_lock() as management:  # noqa: SIM117
        with management.acquire_target_lock() as target:
            target.assert_restore_facts_absent()
            candidate = target.read_bootstrap_candidate()
            with target.acquire_schema_lifecycle_lock():
                with target.owner_transaction() as transaction:
                    transaction.apply_safe_runtime_roles(candidate)
                    transaction.apply_database_acl(DatabaseAclProfile.BASELINE)
                    transaction.apply_object_grants(
                        revision=target.revision,
                        phase=GrantPhase.BASELINE,
                    )
                    transaction.verify_pristine_idle_before_commit()
                target.verify_pristine_idle_after_commit()


def migrate_database(context: DatabaseMaintenanceContext) -> None:
    """在固定 management→target→schema 锁序下执行普通 typed migration。

    admission 必须先于 schema lock 和 Alembic DDL/DML/version/grant callback；Candidate、
    active/needs-attention 或 malformed authority 因而在第一笔 mutation 前拒绝。

    Args:
        context: 已绑定唯一目标的维护 context。
    """
    # 显式嵌套用于冻结 management→target→schema 顺序。
    with context.acquire_management_lifecycle_lock() as management:  # noqa: SIM117
        with management.acquire_target_lock() as target:
            target.assert_migration_allowed()
            with target.acquire_schema_lifecycle_lock():
                target.run_typed_migration()


def role_bootstrap_database(context: DatabaseMaintenanceContext) -> None:
    """执行唯一 standalone role-bootstrap Candidate/steady typed lifecycle。

    Candidate 只能由 ``BootstrapCaller.ROLE_BOOTSTRAP`` 收敛；已经稳定的 pristine/completed
    idle 轮换两个已验证 safe role 密码，并逐条重申 baseline CONNECT 与当前 revision 的
    exact object grants。active/needs-attention、mixed/unsafe role 与任一 grant drift 均在
    首条 mutation 前拒绝。

    Args:
        context: 已绑定 standalone caller、双 Secret 与唯一 target 的维护 context。
    """
    with context.acquire_management_lifecycle_lock() as management:  # noqa: SIM117
        with management.acquire_target_lock() as target:
            target.assert_role_bootstrap_allowed()
            with target.acquire_schema_lifecycle_lock():
                target.run_typed_role_bootstrap()


def reset_then_migrate(context: DatabaseMaintenanceContext) -> None:
    """在一个 live management lease 内完成 protected reset 的完整生命周期。

    确认、durable authority 检查、drop/create、post-create bootstrap 与普通 migration
    不能交换顺序、拆成多个进程或通过环境/file skip token 伪造 lease。具体 destructive
    adapter 必须在每一步前再次复核精确目标；本编排函数不包含任何直接 DDL。

    Args:
        context: 已绑定非生产目标与本次 exact-name confirmation 的维护 context。
    """
    with context.acquire_management_lifecycle_lock() as management:
        management.confirm_exact_non_production_target()
        management.assert_reset_allowed()
        management.drop_and_create_target()
        management.bootstrap_new_target()
        management.run_typed_migration()
