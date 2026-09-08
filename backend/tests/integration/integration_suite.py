"""以两个独立 pytest 进程编排完整 integration suite。

普通 integration/contract/eval 测试需要稳定 schema 与 fixed runtime roles；迁移、维护、
retention 和两个空库契约则要求同一测试 cluster 的 fixed roles absent。两类状态不能在同一
pytest session 可靠共存，因此本模块只在测试侧建立显式进程边界：parent 为 regular phase
创建并拥有 UUID disposable database，清理完成后才让 lifecycle phase 使用原 Task 13 anchor。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from ai_employee.infrastructure.db.alembic import (
    load_published_alembic_authority,
    run_alembic_upgrade_on_connection,
)
from ai_employee.infrastructure.db.database_access import (
    APP_RUNTIME_ROLE_NAME,
    RETENTION_RUNTIME_ROLE_NAME,
    BootstrapCaller,
)
from ai_employee.infrastructure.db.database_maintenance import (
    SqlAlchemyDatabaseMaintenanceContext,
    migrate_database,
    role_bootstrap_database,
)
from ai_employee.infrastructure.db.database_url import (
    InvalidTestDatabaseUrl,
    TestDatabaseUrl,
    ValidatedTestDatabaseUrl,
    validate_test_database_url,
)
from ai_employee.infrastructure.db.session import (
    ManagedAsyncSessionMaker,
    TaskEventNotificationPublisher,
    build_session_factory,
)
from tests.integration.disposable_database import (
    DisposableDatabaseCleanupError,
    IntegrationSuiteLockLease,
    managed_disposable_database_cleanup,
    managed_integration_suite_lock,
)

IntegrationPhase = Literal["regular", "lifecycle"]

REGULAR_TEST_ROOTS = (
    "backend/tests/integration",
    "backend/tests/contract",
    "backend/tests/evals",
)
LIFECYCLE_FILE_TARGETS = (
    "backend/tests/integration/db/test_migrations.py",
    # 该模块的 module fixture 会创建 cluster-wide fixed runtime roles；必须与其他
    # roles-absent lifecycle 文件一起在第二阶段运行，不能被 regular UUID 数据库阶段收集。
    "backend/tests/integration/db/test_database_grants_catalog.py",
    "backend/tests/integration/operations/test_database_maintenance_gate.py",
    "backend/tests/integration/retention/test_role_permissions.py",
    # 0019 rollout 必须使用由 empty_migration_database 创建的真正 0018 target；
    # regular head 阶段已有 cluster roles，不能把准入拒绝误当作业务 RED。
    "backend/tests/integration/operations/test_calendar_aad_0019_preflight.py",
    "backend/tests/integration/operations/test_calendar_aad_0019_recovery.py",
    "backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py",
)
LIFECYCLE_NODE_TARGETS = (
    (
        "backend/tests/integration/m2/test_trusted_action_schema.py::"
        "TestEmptyTrustedActionMigration::"
        "test_database_contains_named_aead_nonce_counter_and_manual_resolution_checks"
    ),
    (
        "backend/tests/integration/m2/test_connection_source_schema.py::"
        "TestEmptyConnectionSourceMigration::"
        "test_m1_google_rows_are_backfilled_without_inventing_source_facts"
    ),
)

_APP_ROLE_PASSWORD = "app-role-integration-password"
_RETENTION_ROLE_PASSWORD = "retention-role-integration-password"
_CHILD_LOCK_VERIFY_INTERVAL_SECONDS = 0.25
_CHILD_TERMINATE_TIMEOUT_SECONDS = 5.0
_CHILD_KILL_TIMEOUT_SECONDS = 5.0
_CHILD_CLEANUP_ERROR_MESSAGE = "integration child cleanup could not be confirmed"
_LIBPQ_TARGET_OVERRIDES = (
    "PGHOST",
    "PGHOSTADDR",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGSERVICE",
    "PGSERVICEFILE",
)

_ANCHOR_IDENTITY_SQL = """SELECT
    control.system_identifier AS system_identifier,
    target_database.oid AS database_oid,
    target_database.datname AS database_name,
    target_database.datdba AS owner_oid,
    target_database.datacl AS database_acl
FROM pg_database AS target_database
CROSS JOIN pg_control_system() AS control
WHERE target_database.datname = current_database()"""

_ANCHOR_SETTINGS_SQL = """SELECT
    role_setting.setrole,
    role_setting.setconfig
FROM pg_db_role_setting AS role_setting
WHERE role_setting.setdatabase = (
    SELECT target_database.oid
    FROM pg_database AS target_database
    WHERE target_database.datname = current_database()
)
ORDER BY role_setting.setrole"""

_ANCHOR_RUNTIME_ROLES_SQL = """SELECT
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
ORDER BY runtime_role.rolname"""

_ANCHOR_MEMBERSHIPS_SQL = """WITH runtime_roles AS (
    SELECT runtime_role.oid
    FROM pg_roles AS runtime_role
    WHERE runtime_role.rolname IN (:app_role_name, :retention_role_name)
)
SELECT
    membership.roleid,
    membership.member,
    membership.grantor,
    membership.admin_option
FROM pg_auth_members AS membership
WHERE membership.roleid IN (SELECT oid FROM runtime_roles)
   OR membership.member IN (SELECT oid FROM runtime_roles)
ORDER BY membership.roleid, membership.member, membership.grantor, membership.admin_option"""

_PUBLIC_RELATIONS_SQL = """SELECT
    relation.relkind,
    relation.relname,
    relation.relpersistence,
    relation.relrowsecurity,
    relation.relforcerowsecurity,
    relation.relreplident,
    relation.reloptions,
    relation.relacl,
    pg_get_partkeydef(relation.oid)
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
  AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
ORDER BY relation.relkind, relation.relname"""

_PUBLIC_COLUMNS_SQL = """SELECT
    relation.relname,
    column_value.attnum,
    column_value.attname,
    format_type(column_value.atttypid, column_value.atttypmod),
    column_value.attnotnull,
    column_value.attidentity,
    column_value.attgenerated,
    pg_get_expr(default_value.adbin, default_value.adrelid)
FROM pg_attribute AS column_value
JOIN pg_class AS relation ON relation.oid = column_value.attrelid
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
LEFT JOIN pg_attrdef AS default_value
    ON default_value.adrelid = column_value.attrelid
   AND default_value.adnum = column_value.attnum
WHERE namespace.nspname = 'public'
  AND column_value.attnum > 0
  AND NOT column_value.attisdropped
ORDER BY relation.relname, column_value.attnum"""

_PUBLIC_CONSTRAINTS_SQL = """SELECT
    relation.relname,
    constraint_value.conname,
    constraint_value.contype,
    constraint_value.condeferrable,
    constraint_value.condeferred,
    constraint_value.convalidated,
    pg_get_constraintdef(constraint_value.oid, true)
FROM pg_constraint AS constraint_value
JOIN pg_class AS relation ON relation.oid = constraint_value.conrelid
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
ORDER BY relation.relname, constraint_value.conname"""

_PUBLIC_INDEXES_SQL = """SELECT
    table_value.relname,
    index_value.relname,
    pg_get_indexdef(index_value.oid)
FROM pg_index AS index_catalog
JOIN pg_class AS table_value ON table_value.oid = index_catalog.indrelid
JOIN pg_class AS index_value ON index_value.oid = index_catalog.indexrelid
JOIN pg_namespace AS namespace ON namespace.oid = table_value.relnamespace
WHERE namespace.nspname = 'public'
ORDER BY table_value.relname, index_value.relname"""

_PUBLIC_VIEWS_SQL = """SELECT
    relation.relkind,
    relation.relname,
    pg_get_viewdef(relation.oid, true)
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
  AND relation.relkind IN ('v', 'm')
ORDER BY relation.relkind, relation.relname"""

_PUBLIC_FUNCTIONS_SQL = """SELECT
    function_value.proname,
    pg_get_function_identity_arguments(function_value.oid),
    pg_get_functiondef(function_value.oid)
FROM pg_proc AS function_value
JOIN pg_namespace AS namespace ON namespace.oid = function_value.pronamespace
WHERE namespace.nspname = 'public'
ORDER BY function_value.proname, pg_get_function_identity_arguments(function_value.oid)"""

_PUBLIC_TRIGGERS_SQL = """SELECT
    relation.relname,
    trigger_value.tgname,
    pg_get_triggerdef(trigger_value.oid, true)
FROM pg_trigger AS trigger_value
JOIN pg_class AS relation ON relation.oid = trigger_value.tgrelid
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
  AND NOT trigger_value.tgisinternal
ORDER BY relation.relname, trigger_value.tgname"""

_PUBLIC_POLICIES_SQL = """SELECT
    policy_value.schemaname,
    policy_value.tablename,
    policy_value.policyname,
    policy_value.permissive,
    policy_value.roles,
    policy_value.cmd,
    policy_value.qual,
    policy_value.with_check
FROM pg_policies AS policy_value
WHERE policy_value.schemaname = 'public'
ORDER BY policy_value.tablename, policy_value.policyname"""

_PUBLIC_SEQUENCES_SQL = """SELECT
    sequence_value.sequencename,
    sequence_value.data_type,
    sequence_value.start_value,
    sequence_value.min_value,
    sequence_value.max_value,
    sequence_value.increment_by,
    sequence_value.cycle,
    sequence_value.cache_size,
    sequence_value.last_value
FROM pg_sequences AS sequence_value
WHERE sequence_value.schemaname = 'public'
ORDER BY sequence_value.sequencename"""

_PUBLIC_TABLE_NAMES_SQL = """SELECT relation.relname
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
  AND relation.relkind IN ('r', 'p')
ORDER BY relation.relname"""

_PUBLIC_CATALOG_QUERIES = (
    ("relation", _PUBLIC_RELATIONS_SQL),
    ("column", _PUBLIC_COLUMNS_SQL),
    ("constraint", _PUBLIC_CONSTRAINTS_SQL),
    ("index", _PUBLIC_INDEXES_SQL),
    ("view", _PUBLIC_VIEWS_SQL),
    ("function", _PUBLIC_FUNCTIONS_SQL),
    ("trigger", _PUBLIC_TRIGGERS_SQL),
    ("policy", _PUBLIC_POLICIES_SQL),
    ("sequence", _PUBLIC_SEQUENCES_SQL),
)


class IntegrationSuiteInvariantError(RuntimeError):
    """表示测试编排的隔离、provenance 或 anchor 不变量被破坏。"""


@dataclass(frozen=True, slots=True)
class PytestInvocation:
    """保存一个不在 repr 中暴露环境变量的 child pytest 调用。"""

    phase: IntegrationPhase
    argv: tuple[str, ...]
    environment: Mapping[str, str] = field(repr=False)
    cwd: Path


@dataclass(frozen=True, slots=True)
class AnchorSnapshot:
    """保存 Task 13 anchor 的 content-free 逻辑 catalog 与数据指纹。"""

    system_identifier: int
    database_oid: int
    database_name: str
    owner_oid: int
    database_acl: tuple[str, ...] | None
    database_settings: tuple[tuple[object, ...], ...]
    runtime_roles: tuple[tuple[object, ...], ...]
    memberships: tuple[tuple[object, ...], ...]
    public_catalog: tuple[tuple[object, ...], ...]
    table_summaries: tuple[tuple[str, int, str], ...]


class IntegrationSuiteResources(Protocol):
    """抽象 parent 所需的 PostgreSQL lease、snapshot 与 provisioning。"""

    def acquire_suite_lock(
        self,
        anchor: ValidatedTestDatabaseUrl,
    ) -> AbstractContextManager[IntegrationSuiteLease]:
        """返回覆盖两个 child phase 的 suite-global lease。"""

    def capture_anchor(self, anchor: ValidatedTestDatabaseUrl) -> object:
        """返回可做精确相等比较的 content-free anchor 快照。"""

    def provision_regular_database(
        self,
        anchor: ValidatedTestDatabaseUrl,
    ) -> AbstractContextManager[TestDatabaseUrl]:
        """返回 parent-owned、退出时按 provenance 清理的 regular URL。"""


class IntegrationSuiteLease(Protocol):
    """定义 orchestrator 与 child watchdog 所需的最小连续性 guard。"""

    def verify(self) -> None:
        """重验同一 management backend、cluster 与 suite lock ownership。"""


ChildRunner = Callable[[PytestInvocation, IntegrationSuiteLease], int]


class _ChildExitCode(Exception):
    """仅在 context 内携带 child code，绝不包含 invocation 或 environment。"""

    def __init__(self, return_code: int) -> None:
        super().__init__("integration child returned nonzero")
        self.return_code = return_code


class ManagedSessionFactoryRegistry:
    """追踪单个 integration 测试创建的异步会话工厂并统一释放。"""

    def __init__(self) -> None:
        """初始化空 registry；只登记经 ``build`` 创建且由当前测试拥有的 factory。"""
        self._factories: list[ManagedAsyncSessionMaker] = []

    def build(
        self,
        database_url: str,
        *,
        task_event_publisher: TaskEventNotificationPublisher | None = None,
    ) -> ManagedAsyncSessionMaker:
        """按生产 helper 构造 factory，并在返回前登记其 pool 所有权。"""
        factory = build_session_factory(
            database_url,
            task_event_publisher=task_event_publisher,
        )
        self._factories.append(factory)
        return factory

    async def dispose_all(self) -> None:
        """逆序尝试释放全部已登记 pool，并在最后传播释放异常。

        factory 在调用 ``dispose`` 前先从 registry 弹出，因此单个释放失败不会阻断其余
        pool，也不会让重试误触已经尝试过的对象。普通异常全部收集；一个异常保留原类型，
        多个异常以 content-free ``ExceptionGroup`` 同时上报。取消与进程控制类
        ``BaseException`` 不在这里吞并。
        """
        errors: list[Exception] = []
        while self._factories:
            factory = self._factories.pop()
            try:
                await factory.dispose()
            except Exception as error:  # noqa: BLE001
                # dispose 是第三方 Engine 边界，必须保留未知普通异常的原类型，同时继续
                # 释放当前测试拥有的其他 pool，避免后置 TRUNCATE 被遗留连接阻塞。
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("managed session factory disposal failed", errors)


@asynccontextmanager
async def managed_session_factory_registry() -> AsyncIterator[ManagedSessionFactoryRegistry]:
    """在测试体正常返回或抛异常时都释放该测试创建的全部异步 pool。"""
    registry = ManagedSessionFactoryRegistry()
    try:
        yield registry
    finally:
        await registry.dispose_all()


def _raise_cycle5_regular_finalization_errors(
    *,
    primary_error: BaseException | None,
    finalization_errors: Sequence[Exception],
) -> None:
    """在所有后置复核结束后传播 Cycle 5 原异常与稳定次级错误。

    Args:
        primary_error: provision enter/body/exit 的原始异常；没有异常时为 ``None``。
        finalization_errors: 已脱敏的 lease/anchor 后置复核错误。

    Raises:
        BaseException: 只有原始异常时保留其对象与 traceback 重抛。
        Exception: 只有一个后置错误时直接抛出。
        ExceptionGroup: 多个后置错误同时存在时完整上报；若另有原始异常，则原始异常
            作为 direct cause 保留，而不会把后置驱动文本带入错误树。
    """
    errors = tuple(finalization_errors)
    if primary_error is None:
        if not errors:
            return
        if len(errors) == 1:
            raise errors[0]
        raise ExceptionGroup("Cycle 5 regular database finalization failed", list(errors))
    if not errors:
        raise primary_error.with_traceback(primary_error.__traceback__)
    raise ExceptionGroup(
        "Cycle 5 regular database finalization failed",
        list(errors),
    ) from primary_error


@contextmanager
def managed_cycle5_regular_database(
    *,
    anchor: ValidatedTestDatabaseUrl,
    resources: IntegrationSuiteResources,
) -> Iterator[TestDatabaseUrl]:
    """在 cleanup 后始终复核 suite lease 与 anchor，再传播 provision 结果。

    provision context 的 ``__enter__``、测试体或 ``__exit__`` 异常先暂存；只有其 cleanup
    已完成后，才重新验证同一 suite lease 并读取第二份 anchor snapshot。lease/anchor
    驱动异常统一收敛为固定消息，anchor 内容绝不进入错误文本。原 provision 异常单独
    发生时原样重抛；若同时存在后置失败，则通过 cause + ``ExceptionGroup`` 保留全部
    稳定事实。

    Args:
        anchor: 已验证的 roles-absent Task 13 anchor。
        resources: 提供 suite lease、anchor snapshot 与 regular provision context 的端口。

    Yields:
        已完成 bootstrap/migration 的 disposable regular database URL。
    """
    with resources.acquire_suite_lock(anchor) as suite_lease:
        anchor_before = resources.capture_anchor(anchor)
        primary_error: BaseException | None = None
        try:
            with resources.provision_regular_database(anchor) as regular_url:
                suite_lease.verify()
                try:
                    yield regular_url
                finally:
                    # 先在 provision cleanup 前确认 fixture/test 未丢失 suite lease；该
                    # 异常同样由外层暂存，cleanup 仍会执行。
                    suite_lease.verify()
        except BaseException as error:  # noqa: BLE001
            # 这是唯一的 cleanup 编排边界：即使测试控制流或进程控制类
            # BaseException 发生，也必须先完成 lease/anchor 复核，随后按原
            # 对象与 traceback 重抛，绝不吞并或转换该异常。
            primary_error = error

        finalization_errors: list[Exception] = []
        try:
            suite_lease.verify()
        except Exception:  # noqa: BLE001 - 后置驱动错误必须收敛为稳定且脱敏的不变量。
            finalization_errors.append(
                IntegrationSuiteInvariantError(
                    "Cycle 5 suite lease verification failed"
                )
            )
        try:
            anchor_after = resources.capture_anchor(anchor)
        except Exception:  # noqa: BLE001 - anchor 驱动错误不得泄露 endpoint/catalog 文本。
            finalization_errors.append(
                IntegrationSuiteInvariantError("Cycle 5 anchor verification failed")
            )
        else:
            if anchor_after != anchor_before:
                finalization_errors.append(
                    IntegrationSuiteInvariantError("Cycle 5 anchor verification failed")
                )
        _raise_cycle5_regular_finalization_errors(
            primary_error=primary_error,
            finalization_errors=finalization_errors,
        )


def build_regular_pytest_arguments() -> tuple[str, ...]:
    """构造普通 phase 的唯一 pytest 选择集合。"""
    ignored = tuple(f"--ignore={target}" for target in LIFECYCLE_FILE_TARGETS)
    # pytest 由 ``backend/pyproject.toml`` 把 rootdir 固定为 backend；``--deselect`` 匹配
    # collection nodeid 而非 shell 输入路径，因此必须使用 rootdir-relative ``tests/...``。
    deselected = tuple(
        f"--deselect={target.removeprefix('backend/')}" for target in LIFECYCLE_NODE_TARGETS
    )
    return (*REGULAR_TEST_ROOTS, *ignored, *deselected, "-q")


def build_lifecycle_pytest_arguments() -> tuple[str, ...]:
    """构造 roles-absent lifecycle phase 的完整冻结选择集合。"""
    return (*LIFECYCLE_FILE_TARGETS, *LIFECYCLE_NODE_TARGETS, "-q")


def _checkpoint_database_url(database_url: TestDatabaseUrl) -> str:
    """把已验证 asyncpg URL 结构化转换为 checkpointer 使用的 psycopg URL。"""
    validated = validate_test_database_url(database_url)
    return validated.parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _phase_environment(
    base_environment: Mapping[str, str],
    *,
    database_url: TestDatabaseUrl,
) -> dict[str, str]:
    """复制 parent 环境并强制所有数据库入口指向当前 phase 的精确 URL。"""
    environment = dict(base_environment)
    for variable_name in _LIBPQ_TARGET_OVERRIDES:
        environment.pop(variable_name, None)
    environment["TEST_DATABASE_URL"] = database_url
    environment["DATABASE_URL"] = database_url
    environment["CHECKPOINT_DATABASE_URL"] = _checkpoint_database_url(database_url)
    return environment


def _build_invocation(
    *,
    phase: IntegrationPhase,
    database_url: TestDatabaseUrl,
    base_environment: Mapping[str, str],
    python_executable: str,
    repository_root: Path,
) -> PytestInvocation:
    """构造 list argv child 调用，Secret 只存在于隐藏的 environment 字段。"""
    arguments = (
        build_regular_pytest_arguments()
        if phase == "regular"
        else build_lifecycle_pytest_arguments()
    )
    return PytestInvocation(
        phase=phase,
        argv=(python_executable, "-m", "pytest", *arguments),
        environment=_phase_environment(base_environment, database_url=database_url),
        cwd=repository_root,
    )


def orchestrate_integration_tests(
    *,
    anchor: ValidatedTestDatabaseUrl,
    base_environment: Mapping[str, str],
    resources: IntegrationSuiteResources,
    run_child: ChildRunner,
    python_executable: str,
    repository_root: Path,
) -> int:
    """按 regular cleanup → lifecycle 的固定顺序执行两个 pytest session。

    Args:
        anchor: 经过 Task 13 test URL validator 的原始 management anchor。
        base_environment: 复制给 child 的 parent 环境；三个数据库 URL 会被精确覆盖。
        resources: 提供 suite lock、anchor snapshot 与 disposable regular DB 的测试侧端口。
        run_child: 接受 list argv invocation 与同线程 suite lease 的 child 执行器。
        python_executable: 当前 uv 环境的 Python，可保证两个 child 使用相同 lockfile 环境。
        repository_root: child pytest 的固定工作目录。

    Returns:
        第一个失败 child 的原始退出码；两个 phase 均成功时返回零。

    Raises:
        IntegrationSuiteInvariantError: regular cleanup 后 anchor 快照发生任何变化。
        Exception: provisioning、cleanup 或 child runner 的基础设施异常原样向上传递；所有
            已进入的 context manager 仍先执行 finally。
    """
    try:
        with resources.acquire_suite_lock(anchor) as suite_lease:
            before = resources.capture_anchor(anchor)
            regular_code: int | None = None
            regular_error: Exception | None = None
            try:
                with resources.provision_regular_database(anchor) as regular_url:
                    suite_lease.verify()
                    regular_code = run_child(
                        _build_invocation(
                            phase="regular",
                            database_url=regular_url,
                            base_environment=base_environment,
                            python_executable=python_executable,
                            repository_root=repository_root,
                        ),
                        suite_lease,
                    )
                    suite_lease.verify()
            except Exception as error:  # noqa: BLE001
                # child、watchdog 或 provenance cleanup 的异常都先暂存；只有退出资源
                # context 并完成 anchor 复核后，才允许向上传播原始异常。这里是测试编排的
                # 最外层异常边界，故意保留任意 child/infrastructure Exception 的原类型。
                regular_error = error

            # disposable cleanup 可能跨多个 cluster catalog 操作；结束后必须先确认 suite
            # lease 连续，再读取 anchor 并决定是否允许进入 roles-absent phase。
            suite_lease.verify()
            after = resources.capture_anchor(anchor)
            if after != before:
                invariant_error = IntegrationSuiteInvariantError(
                    "integration anchor changed during regular phase"
                )
                if regular_error is not None:
                    raise invariant_error from regular_error
                raise invariant_error
            if regular_error is not None:
                raise regular_error
            if regular_code is None:
                raise IntegrationSuiteInvariantError(
                    "integration regular child result missing after cleanup"
                )
            if regular_code != 0:
                # 让 managed suite context 明确走异常释放路径，避免 unlock 异常改写
                # pytest 的原始非零 code；context 外立即恢复该 code。
                raise _ChildExitCode(regular_code)

            suite_lease.verify()
            lifecycle_code = run_child(
                _build_invocation(
                    phase="lifecycle",
                    database_url=anchor.value,
                    base_environment=base_environment,
                    python_executable=python_executable,
                    repository_root=repository_root,
                ),
                suite_lease,
            )
            suite_lease.verify()
            if lifecycle_code != 0:
                raise _ChildExitCode(lifecycle_code)
            return 0
    except _ChildExitCode as failure:
        return failure.return_code


def _freeze_catalog_value(value: object) -> object:
    """把驱动返回的 list/mapping/buffer 规范化为可稳定比较的不可变值。"""
    if isinstance(value, list):
        return tuple(_freeze_catalog_value(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze_catalog_value(item)) for key, item in value.items()))
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return value


def _freeze_rows(
    rows: Sequence[Sequence[object]],
) -> tuple[tuple[object, ...], ...]:
    """冻结 SQLAlchemy ``Result.all()`` 的 catalog 行。"""
    return tuple(tuple(_freeze_catalog_value(value) for value in row) for row in rows)


def _normalize_database_acl(value: object) -> tuple[str, ...] | None:
    """把 psycopg ACL array 收窄为排序后的不可变字符串集合。"""
    if value is None:
        return None
    if not isinstance(value, list) or not all(type(item) is str for item in value):
        raise IntegrationSuiteInvariantError("integration anchor ACL is malformed")
    return tuple(sorted(value))


def _capture_public_catalog(connection: Connection) -> tuple[tuple[object, ...], ...]:
    """读取 public schema 的逻辑定义，不包含物理页、统计或用户正文。"""
    rows: list[tuple[object, ...]] = []
    for category, sql in _PUBLIC_CATALOG_QUERIES:
        result_rows = connection.execute(text(sql)).all()
        rows.extend((category, *tuple(row)) for row in result_rows)
    return _freeze_rows(rows)


def _capture_table_summaries(connection: Connection) -> tuple[tuple[str, int, str], ...]:
    """只返回 public 表的 row count 与内容 digest，绝不把真实行带回 Python。"""
    table_names = connection.execute(text(_PUBLIC_TABLE_NAMES_SQL)).scalars().all()
    preparer = connection.dialect.identifier_preparer
    summaries: list[tuple[str, int, str]] = []
    for table_name in table_names:
        if type(table_name) is not str:
            raise IntegrationSuiteInvariantError("integration anchor table name is malformed")
        quoted_table = preparer.quote_identifier(table_name)
        row = (
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
        row_count = row["row_count"]
        content_digest = row["content_digest"]
        if type(row_count) is not int or type(content_digest) is not str:
            raise IntegrationSuiteInvariantError("integration anchor table summary is malformed")
        summaries.append((table_name, row_count, content_digest))
    return tuple(summaries)


class SqlAlchemyIntegrationSuiteResources:
    """用真实 typed lifecycle 与 provenance helper 实现 parent 测试资源。"""

    def __init__(self, repository_root: Path) -> None:
        self._repository_root = repository_root
        self._backend_root = repository_root / "backend"
        self._published_authority = load_published_alembic_authority(
            Config(self._backend_root / "alembic.ini")
        )

    @staticmethod
    def _engine(url: URL) -> Engine:
        """构造不复用 session 且显式隐藏参数的同步测试 Engine。"""
        return create_engine(
            url.set(drivername="postgresql+psycopg"),
            poolclass=NullPool,
            hide_parameters=True,
        )

    @contextmanager
    def acquire_suite_lock(
        self,
        anchor: ValidatedTestDatabaseUrl,
    ) -> Iterator[IntegrationSuiteLockLease]:
        """在独立 management session 中持有 suite-global advisory lease。"""
        management_engine = self._engine(anchor.maintenance_url())
        try:
            with managed_integration_suite_lock(management_engine) as lease:
                yield lease
        finally:
            management_engine.dispose()

    def capture_anchor(self, anchor: ValidatedTestDatabaseUrl) -> AnchorSnapshot:
        """从一个 target session 读取 anchor 的逻辑 catalog 与 content-free 数据指纹。"""
        engine = self._engine(anchor.parsed)
        parameters = {
            "app_role_name": APP_RUNTIME_ROLE_NAME,
            "retention_role_name": RETENTION_RUNTIME_ROLE_NAME,
        }
        try:
            with engine.connect() as connection:
                identity = connection.execute(text(_ANCHOR_IDENTITY_SQL)).mappings().one()
                system_identifier = identity["system_identifier"]
                database_oid = identity["database_oid"]
                database_name = identity["database_name"]
                owner_oid = identity["owner_oid"]
                if (
                    type(system_identifier) is not int
                    or system_identifier == 0
                    or type(database_oid) is not int
                    or database_oid <= 0
                    or database_name != anchor.database_name
                    or type(owner_oid) is not int
                    or owner_oid <= 0
                ):
                    raise IntegrationSuiteInvariantError("integration anchor identity is malformed")
                database_settings = _freeze_rows(
                    connection.execute(text(_ANCHOR_SETTINGS_SQL)).all()
                )
                runtime_roles = _freeze_rows(
                    connection.execute(text(_ANCHOR_RUNTIME_ROLES_SQL), parameters).all()
                )
                memberships = _freeze_rows(
                    connection.execute(text(_ANCHOR_MEMBERSHIPS_SQL), parameters).all()
                )
                return AnchorSnapshot(
                    system_identifier=system_identifier,
                    database_oid=database_oid,
                    database_name=database_name,
                    owner_oid=owner_oid,
                    database_acl=_normalize_database_acl(identity["database_acl"]),
                    database_settings=database_settings,
                    runtime_roles=runtime_roles,
                    memberships=memberships,
                    public_catalog=_capture_public_catalog(connection),
                    table_summaries=_capture_table_summaries(connection),
                )
        finally:
            engine.dispose()

    @contextmanager
    def provision_regular_database(
        self,
        anchor: ValidatedTestDatabaseUrl,
    ) -> Iterator[TestDatabaseUrl]:
        """创建并初始化 regular UUID DB，退出时精确删除本轮 DB 与 roles。"""
        database_name = f"ai_employee_suite_{uuid4().hex}_test"
        regular_url = anchor.for_database(database_name)
        management_engine = self._engine(anchor.maintenance_url())
        try:
            with managed_disposable_database_cleanup(
                management_engine,
                database_name=database_name,
            ) as cleanup:
                try:
                    cleanup.assert_initial_absence()
                except DisposableDatabaseCleanupError:
                    raise IntegrationSuiteInvariantError(
                        "integration suite requires absent fixed runtime roles"
                    ) from None
                if not cleanup.create_database():
                    raise IntegrationSuiteInvariantError("integration suite owner lacks CREATEDB")

                target_engine = self._engine(regular_url)
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
                            config_path=self._backend_root / "alembic.ini",
                        ),
                        published_authority=self._published_authority,
                    )
                    try:
                        role_bootstrap_database(context)
                    except BaseException as original_error:
                        # role-bootstrap 的数据库事务可能已提交，只是 post-commit verifier
                        # 随后失败。此时只能从仍持 cleanup lease 的 management session
                        # 读回恰好两个 safe role 并冻结 OID/posture；partial/unsafe catalog
                        # 继续 fail closed，绝不根据异常阶段猜测角色来源。
                        try:
                            cleanup.record_created_runtime_roles_if_safe()
                        except DisposableDatabaseCleanupError as cleanup_error:
                            raise cleanup_error from original_error
                        raise
                    if cleanup.record_created_runtime_roles_if_safe() is not True:
                        raise IntegrationSuiteInvariantError(
                            "integration suite runtime roles were not recorded"
                        )
                    migrate_database(context)
                    yield TestDatabaseUrl(regular_url.render_as_string(hide_password=False))
                finally:
                    # parent 必须先关闭 target pool，cleanup 才能证明 session count 为零。
                    target_engine.dispose()
        finally:
            management_engine.dispose()


def _terminate_pytest_child(process: subprocess.Popen[bytes]) -> None:
    """以有界 terminate→kill 顺序回收 child，且只接受可证明的退出状态。

    Raises:
        IntegrationSuiteInvariantError: 所有有界手段结束后仍不能由 ``wait`` 或可靠
            ``poll`` 证明 child 已退出。错误消息固定且不包含 argv、environment 或 PID。
    """
    try:
        if process.poll() is not None:
            return
    except OSError:
        # 初始状态读取失败不代表 child 已退出，仍继续发送终止信号并等待。
        pass
    try:
        process.terminate()
    except OSError:
        # terminate 竞争或系统错误后仍必须 wait；禁止把信号错误当作已回收证明。
        pass
    try:
        process.wait(timeout=_CHILD_TERMINATE_TIMEOUT_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        # kill 失败同样不能提前返回；最终 wait/poll 仍可能证明进程已自行退出。
        pass
    try:
        process.wait(timeout=_CHILD_KILL_TIMEOUT_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if process.poll() is not None:
            return
    except OSError:
        pass
    raise IntegrationSuiteInvariantError(_CHILD_CLEANUP_ERROR_MESSAGE)


def run_pytest_child(
    invocation: PytestInvocation,
    suite_lease: IntegrationSuiteLease,
) -> int:
    """用同线程 Popen watchdog 持续验证 suite lease 并返回原始 child code。

    Args:
        invocation: 冻结 list argv、工作目录与隐藏环境的 pytest 调用。
        suite_lease: 与 parent management session 绑定的连续性 guard。

    Returns:
        已确认 child 退出后的原始进程状态码。

    Raises:
        IntegrationSuiteInvariantError: suite lease 失效，或失效后无法证明 child 已回收。
        BaseException: child watchdog 遇到的其他 active error；若 child 无法回收，则由稳定
            cleanup invariant 作为主异常并通过 cause 保留该错误。
    """
    suite_lease.verify()
    process = subprocess.Popen(
        list(invocation.argv),
        cwd=invocation.cwd,
        env=dict(invocation.environment),
    )
    try:
        while True:
            try:
                return_code = process.wait(timeout=_CHILD_LOCK_VERIFY_INTERVAL_SECONDS)
            except subprocess.TimeoutExpired:
                suite_lease.verify()
                continue
            suite_lease.verify()
            return return_code
    except BaseException as original_error:
        try:
            _terminate_pytest_child(process)
        except IntegrationSuiteInvariantError as cleanup_error:
            # cleanup invariant 为当前安全事实，原 lease/body 异常通过显式 cause 保留；两者
            # 都是固定消息，绝不串入 invocation 或隐藏 environment。
            raise cleanup_error from original_error
        raise


def main() -> int:
    """读取唯一 anchor 环境变量并执行完整两阶段 integration suite。"""
    raw_anchor = os.environ.get("TEST_DATABASE_URL")
    if raw_anchor is None:
        print("integration tests require TEST_DATABASE_URL", file=sys.stderr)
        return 2
    try:
        anchor = validate_test_database_url(raw_anchor)
    except InvalidTestDatabaseUrl as error:
        # validator 仅返回静态原因；原 DSN 从不进入 argv、repr 或错误输出。
        print(str(error), file=sys.stderr)
        return 2

    repository_root = Path(__file__).resolve().parents[3]
    try:
        return orchestrate_integration_tests(
            anchor=anchor,
            base_environment=os.environ,
            resources=SqlAlchemyIntegrationSuiteResources(repository_root),
            run_child=run_pytest_child,
            python_executable=sys.executable,
            repository_root=repository_root,
        )
    except Exception:  # noqa: BLE001 - CLI 必须把未知 DBAPI 细节收敛为稳定错误。
        # DBAPI/SQLAlchemy 异常可能携带 endpoint 或 catalog 细节；CLI 只输出稳定状态。
        print("integration test orchestration failed", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - root script 是标准入口。
    raise SystemExit(main())
