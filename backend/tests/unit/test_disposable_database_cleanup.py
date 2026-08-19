"""冻结 disposable PostgreSQL 测试目标的不可替换 cleanup provenance。"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.infrastructure.db.database_access import (
    APP_RUNTIME_ROLE_NAME,
    RETENTION_RUNTIME_ROLE_NAME,
)
from ai_employee.infrastructure.db.database_maintenance import (
    DatabaseMaintenanceContext,
    reset_then_migrate,
)
from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl as DatabaseUrlValue,
)
from ai_employee.infrastructure.db.database_url import (
    ValidatedTestDatabaseUrl,
    validate_test_database_url,
)
from tests.integration import disposable_database as disposable_database_module
from tests.integration.disposable_database import (
    DISPOSABLE_DATABASE_CLEANUP_LOCK,
    INTEGRATION_SUITE_LOCK,
    DisposableDatabaseCleanupError,
    DisposableDatabaseCleanupLease,
    IntegrationSuiteLockLease,
    managed_disposable_database_cleanup,
    managed_integration_suite_lock,
)
from tests.integration.integration_suite import (
    IntegrationSuiteLease,
    PytestInvocation,
    orchestrate_integration_tests,
)

_DATABASE_NAME = "ai_employee_mig_0123456789abcdef0123456789abcdef_test"
_SUITE_DATABASE_NAME = "ai_employee_suite_0123456789abcdef0123456789abcdef_test"
_ANCHOR_URL = DatabaseUrlValue(
    "postgresql+asyncpg://owner:synthetic-anchor-secret@localhost:5432/ai_employee_test"
)
_REGULAR_URL = DatabaseUrlValue(
    "postgresql+asyncpg://owner:synthetic-regular-secret@localhost:5432/"
    "ai_employee_suite_0123456789abcdef0123456789abcdef_test"
)


class _FakeResult:
    """实现 cleanup helper 使用的最小 SQLAlchemy Result 表面。"""

    def __init__(
        self,
        *,
        scalar: object | None = None,
        rows: tuple[Mapping[str, object], ...] = (),
    ) -> None:
        self._scalar = scalar
        self._rows = rows

    def scalar_one(self) -> object:
        """返回测试预设的唯一 scalar。"""
        return self._scalar

    def mappings(self) -> _FakeResult:
        """保持自身以支持 ``mappings().all()``。"""
        return self

    def all(self) -> list[Mapping[str, object]]:
        """返回当前 catalog 查询的独立行列表。"""
        return list(self._rows)


def _safe_role(role_name: str, role_oid: int) -> dict[str, object]:
    """构造与 frozen runtime-role posture 完全一致的 catalog 行。"""
    return {
        "role_name": role_name,
        "role_oid": role_oid,
        "can_login": True,
        "inherits": True,
        "is_superuser": False,
        "can_create_database": False,
        "can_create_role": False,
        "can_replicate": False,
        "bypasses_rls": False,
        "connection_limit": -1,
        "valid_until": None,
        "config": None,
    }


class _FakeIdentifierPreparer:
    """按 PostgreSQL 双引号规则引用固定测试标识符。"""

    def quote_identifier(self, value: str) -> str:
        """返回不做 case-fold 的双引号标识符。"""
        return f'"{value.replace(chr(34), chr(34) * 2)}"'


class _FakeDialect:
    """只暴露 cleanup manager 需要的 identifier preparer。"""

    identifier_preparer = _FakeIdentifierPreparer()


class _FakeConnection:
    """以可变 catalog 状态模拟同一 live management session。"""

    dialect = _FakeDialect()

    def __init__(self) -> None:
        self.closed = False
        self.invalidated = False
        self.backend_pid = 7711
        self.system_identifier = 72623859790382856
        self.management_database_name = "postgres"
        self.lock_held = False
        self.unlock_result = True
        self.unlock_error: SQLAlchemyError | None = None
        self.close_error: SQLAlchemyError | None = None
        self.ownership_error: SQLAlchemyError | None = None
        self.database_rows_error: SQLAlchemyError | None = None
        self.role_rows_error: SQLAlchemyError | None = None
        self.database_session_error: SQLAlchemyError | None = None
        self.database_oid: int | None = None
        self.database_name = _DATABASE_NAME
        self.next_database_oid = 401
        self.roles: dict[str, dict[str, object]] = {}
        self.dependencies = {
            "database_sessions": 0,
            "active_sessions": 0,
            "memberships": 0,
            "role_settings": 0,
            "database_ownership": 0,
            "database_acl": 0,
            "shared_dependencies": 0,
        }
        self.statements: list[str] = []
        self.mutations: list[str] = []
        self.lock_requests: list[tuple[object, object]] = []
        self.dependency_queries: set[str] = set()
        self.drop_database_hook: Callable[[], None] | None = None

    def execution_options(self, **options: object) -> _FakeConnection:
        """验证 manager 始终把同一 session 切到 AUTOCOMMIT。"""
        assert options == {"isolation_level": "AUTOCOMMIT"}
        return self

    def in_transaction(self) -> bool:
        """测试 management session 不保留隐式 catalog transaction。"""
        return False

    def execute(
        self,
        statement: object,
        parameters: Mapping[str, object] | None = None,
    ) -> _FakeResult:
        """按 SQL 形状返回 catalog state，并记录所有 destructive statement。"""
        sql = str(statement)
        self.statements.append(sql)
        if "pg_backend_pid() AS backend_pid" in sql:
            return _FakeResult(
                rows=(
                    {
                        "backend_pid": self.backend_pid,
                        "system_identifier": self.system_identifier,
                        "management_database_name": self.management_database_name,
                    },
                )
            )
        if "pg_try_advisory_lock" in sql:
            assert parameters is not None
            self.lock_requests.append((parameters["lock_class"], parameters["lock_object"]))
            self.lock_held = True
            return _FakeResult(scalar=True)
        if "FROM pg_locks AS held_lock" in sql:
            if self.ownership_error is not None:
                raise self.ownership_error
            return _FakeResult(scalar=1 if self.lock_held else 0)
        if "pg_advisory_unlock" in sql:
            if self.unlock_error is not None:
                raise self.unlock_error
            released = self.lock_held
            self.lock_held = False
            return _FakeResult(scalar=self.unlock_result if released else False)
        if "SELECT rolcreatedb" in sql:
            return _FakeResult(scalar=True)
        if "AS target_database_oid" in sql and "FROM pg_database AS target_database" in sql:
            if self.database_rows_error is not None:
                raise self.database_rows_error
            rows: tuple[Mapping[str, object], ...] = ()
            if self.database_oid is not None:
                rows = (
                    {
                        "target_database_oid": self.database_oid,
                        "target_database_name": self.database_name,
                    },
                )
            return _FakeResult(rows=rows)
        if "FROM pg_roles AS runtime_role" in sql:
            if self.role_rows_error is not None:
                raise self.role_rows_error
            return _FakeResult(rows=tuple(self.roles[name] for name in sorted(self.roles)))
        if "activity.datid = :target_database_oid" in sql:
            if self.database_session_error is not None:
                raise self.database_session_error
            self.dependency_queries.add("database_sessions")
            return _FakeResult(scalar=self.dependencies["database_sessions"])
        if "activity.usesysid = ANY(:role_oids)" in sql:
            self.dependency_queries.add("active_sessions")
            return _FakeResult(scalar=self.dependencies["active_sessions"])
        if "FROM pg_auth_members AS membership" in sql:
            self.dependency_queries.add("memberships")
            return _FakeResult(scalar=self.dependencies["memberships"])
        if "FROM pg_db_role_setting AS role_setting" in sql:
            self.dependency_queries.add("role_settings")
            return _FakeResult(scalar=self.dependencies["role_settings"])
        if "target_database.datdba = ANY(:role_oids)" in sql:
            self.dependency_queries.add("database_ownership")
            return _FakeResult(scalar=self.dependencies["database_ownership"])
        if "CROSS JOIN LATERAL aclexplode" in sql:
            self.dependency_queries.add("database_acl")
            return _FakeResult(scalar=self.dependencies["database_acl"])
        if "FROM pg_shdepend AS dependency" in sql:
            self.dependency_queries.add("shared_dependencies")
            return _FakeResult(scalar=self.dependencies["shared_dependencies"])
        if sql.startswith("CREATE DATABASE "):
            self.mutations.append(sql)
            self.database_oid = self.next_database_oid
            return _FakeResult()
        if sql.startswith("DROP DATABASE "):
            self.mutations.append(sql)
            if self.drop_database_hook is not None:
                self.drop_database_hook()
            self.database_oid = None
            return _FakeResult()
        if sql.startswith("DROP ROLE "):
            self.mutations.append(sql)
            quoted_name = sql.removeprefix("DROP ROLE ").strip()
            role_name = quoted_name[1:-1].replace('""', '"')
            self.roles.pop(role_name, None)
            return _FakeResult()
        raise AssertionError(f"unexpected cleanup SQL: {sql}")

    def close(self) -> None:
        """记录 manager 已显式关闭 fake session。"""
        # PostgreSQL session advisory lock 会随连接关闭而释放；Fake 必须保留同一语义。
        self.lock_held = False
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _FakeEngine:
    """始终返回同一 fake management connection。"""

    hide_parameters = True
    dialect = _FakeDialect()

    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> _FakeConnection:
        """返回需要由 manager 显式 close 的同一 session。"""
        return self.connection


class _ManagedSuiteResources:
    """让 orchestrator 直接使用真实 managed suite context 与 Fake connection。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def acquire_suite_lock(
        self,
        anchor: ValidatedTestDatabaseUrl,
    ) -> AbstractContextManager[IntegrationSuiteLockLease]:
        """返回待测的真实 suite context manager。"""
        assert anchor.value == _ANCHOR_URL
        return managed_integration_suite_lock(self._engine)

    def capture_anchor(self, anchor: ValidatedTestDatabaseUrl) -> object:
        """返回稳定合成 anchor 快照。"""
        assert anchor.value == _ANCHOR_URL
        return "unchanged"

    @contextmanager
    def provision_regular_database(
        self,
        anchor: ValidatedTestDatabaseUrl,
    ) -> Iterator[DatabaseUrlValue]:
        """提供不执行真实 DDL 的合成 regular URL。"""
        assert anchor.value == _ANCHOR_URL
        yield _REGULAR_URL


def _acquire(connection: _FakeConnection) -> DisposableDatabaseCleanupLease:
    """取得固定 advisory lock 并返回 unit-test cleanup lease。"""
    return DisposableDatabaseCleanupLease.acquire(
        connection=cast(Connection, connection),
        database_name=_DATABASE_NAME,
        quote_identifier=connection.dialect.identifier_preparer.quote_identifier,
    )


def _record_database(lease: DisposableDatabaseCleanupLease, connection: _FakeConnection) -> None:
    """在初始双 absent 后冻结本轮 database OID。"""
    lease.assert_initial_absence()
    connection.database_oid = 401
    lease.record_created_database()


def _record_complete_provenance(
    lease: DisposableDatabaseCleanupLease,
    connection: _FakeConnection,
) -> None:
    """冻结 database 与两个 safe role 的 exact name/OID provenance。"""
    _record_database(lease, connection)
    connection.roles = {
        APP_RUNTIME_ROLE_NAME: _safe_role(APP_RUNTIME_ROLE_NAME, 501),
        RETENTION_RUNTIME_ROLE_NAME: _safe_role(RETENTION_RUNTIME_ROLE_NAME, 502),
    }
    lease.record_created_runtime_roles()


@pytest.mark.parametrize(
    "role_rows",
    (
        pytest.param(
            {APP_RUNTIME_ROLE_NAME: _safe_role(APP_RUNTIME_ROLE_NAME, 501)},
            id="missing-one-role",
        ),
        pytest.param({}, id="zero-role-rows"),
        pytest.param(
            {
                APP_RUNTIME_ROLE_NAME: _safe_role(APP_RUNTIME_ROLE_NAME, 501),
                RETENTION_RUNTIME_ROLE_NAME: {
                    **_safe_role(RETENTION_RUNTIME_ROLE_NAME, 502),
                    "is_superuser": True,
                },
            },
            id="unsafe-role-posture",
        ),
    ),
)
def test_cleanup_rejects_incomplete_role_provenance_before_first_drop(
    role_rows: dict[str, dict[str, object]],
) -> None:
    """缺一或零角色都不得被 ``all([])`` 当成安全，更不得按后来名称盲删。"""
    connection = _FakeConnection()
    lease = _acquire(connection)
    try:
        _record_database(lease, connection)
        connection.roles = role_rows
        with pytest.raises(DisposableDatabaseCleanupError):
            lease.record_created_runtime_roles()

        # 即使随后出现同名 safe roles，没有记录过 OID 的 lease 也没有删除 authority。
        connection.roles = {
            APP_RUNTIME_ROLE_NAME: _safe_role(APP_RUNTIME_ROLE_NAME, 601),
            RETENTION_RUNTIME_ROLE_NAME: _safe_role(RETENTION_RUNTIME_ROLE_NAME, 602),
        }
        with pytest.raises(DisposableDatabaseCleanupError):
            lease.cleanup()
        assert connection.mutations == []
    finally:
        lease.release()


def test_cleanup_rejects_role_oid_swap_before_first_drop() -> None:
    """同名角色被替换或 OID 对调时不得删除 database 或任何角色。"""
    connection = _FakeConnection()
    lease = _acquire(connection)
    try:
        _record_complete_provenance(lease, connection)
        connection.roles = {
            APP_RUNTIME_ROLE_NAME: _safe_role(APP_RUNTIME_ROLE_NAME, 502),
            RETENTION_RUNTIME_ROLE_NAME: _safe_role(RETENTION_RUNTIME_ROLE_NAME, 501),
        }
        with pytest.raises(DisposableDatabaseCleanupError):
            lease.cleanup()
        assert connection.mutations == []
    finally:
        lease.release()


def test_cleanup_rejects_database_drop_recreate_oid_mismatch_before_first_drop() -> None:
    """UUID 名称相同但 database OID 改变时不得删除后来创建的对象。"""
    connection = _FakeConnection()
    lease = _acquire(connection)
    try:
        _record_complete_provenance(lease, connection)
        connection.database_oid = 999
        with pytest.raises(DisposableDatabaseCleanupError):
            lease.cleanup()
        assert connection.mutations == []
    finally:
        lease.release()


def test_cleanup_rejects_cluster_identity_mismatch_before_first_drop() -> None:
    """management endpoint 指向不同 PostgreSQL cluster 时必须在首个 DROP 前拒绝。"""
    connection = _FakeConnection()
    lease = _acquire(connection)
    try:
        _record_complete_provenance(lease, connection)
        connection.system_identifier += 1
        with pytest.raises(DisposableDatabaseCleanupError):
            lease.cleanup()
        assert connection.mutations == []
    finally:
        lease.release()


@pytest.mark.parametrize("lost_fact", ("lock", "session"))
def test_cleanup_rejects_lock_or_session_loss_before_first_drop(lost_fact: str) -> None:
    """cleanup authority 不能脱离最初取得 advisory lock 的 live backend session。"""
    connection = _FakeConnection()
    lease = _acquire(connection)
    try:
        _record_complete_provenance(lease, connection)
        if lost_fact == "lock":
            connection.lock_held = False
        else:
            connection.closed = True
        with pytest.raises(DisposableDatabaseCleanupError):
            lease.cleanup()
        assert connection.mutations == []
    finally:
        connection.closed = False
        lease.release()


def test_exact_provenance_executes_only_fixed_non_cascade_cleanup_sql() -> None:
    """只有完整 provenance 才能先删 exact database、审计零依赖，再删两个角色。"""
    connection = _FakeConnection()
    lease = _acquire(connection)
    try:
        _record_complete_provenance(lease, connection)
        lease.cleanup()

        assert connection.mutations == [
            f'DROP DATABASE "{_DATABASE_NAME}"',
            f'DROP ROLE "{APP_RUNTIME_ROLE_NAME}"',
            f'DROP ROLE "{RETENTION_RUNTIME_ROLE_NAME}"',
        ]
        assert all("CASCADE" not in statement for statement in connection.mutations)
        assert connection.dependency_queries == {
            "database_sessions",
            "active_sessions",
            "memberships",
            "role_settings",
            "database_ownership",
            "database_acl",
            "shared_dependencies",
        }
        assert connection.database_oid is None
        assert connection.roles == {}
    finally:
        lease.release()


def test_cleanup_rechecks_role_dependencies_after_database_drop() -> None:
    """database 删除后任一角色依赖都必须阻止后续 ``DROP ROLE``。"""
    connection = _FakeConnection()
    lease = _acquire(connection)
    try:
        _record_complete_provenance(lease, connection)
        connection.dependencies["memberships"] = 1
        with pytest.raises(DisposableDatabaseCleanupError):
            lease.cleanup()
        assert connection.mutations == [f'DROP DATABASE "{_DATABASE_NAME}"']
    finally:
        lease.release()


def test_managed_cleanup_always_releases_lock_and_connection_on_exception() -> None:
    """调用体异常也必须显式 unlock/close，且无 provenance 时不得尝试 DROP。"""
    connection = _FakeConnection()
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(RuntimeError, match="synthetic setup failure"),
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as lease,
    ):
        lease.assert_initial_absence()
        raise RuntimeError("synthetic setup failure")

    assert connection.lock_held is False
    assert connection.closed is True
    assert connection.mutations == []
    assert any("pg_advisory_unlock" in statement for statement in connection.statements)


def test_managed_cleanup_release_and_close_errors_do_not_mask_active_body_error() -> None:
    """body 已失败时 unlock/close DBAPI 细节不得覆盖或泄漏原始异常。"""
    connection = _FakeConnection()
    connection.unlock_error = SQLAlchemyError("synthetic unlock secret")
    connection.close_error = SQLAlchemyError("synthetic close secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(RuntimeError, match=r"^synthetic body failure$") as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as lease,
    ):
        lease.assert_initial_absence()
        raise RuntimeError("synthetic body failure")

    assert "synthetic unlock secret" not in str(caught.value)
    assert "synthetic close secret" not in str(caught.value)
    assert connection.closed is True
    assert connection.mutations == []


def test_acquire_guard_maps_ownership_driver_error_and_releases_resources() -> None:
    """acquire 后 ownership 读取失败必须稳定脱敏，并尽力释放已取得的锁与连接。"""
    connection = _FakeConnection()
    connection.ownership_error = SQLAlchemyError("synthetic ownership secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ),
    ):
        raise AssertionError("body must not run after acquire guard failure")

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(caught.value))
    assert "synthetic ownership secret" not in formatted
    assert connection.lock_held is False
    assert connection.closed is True
    assert connection.mutations == []


def test_suite_database_name_is_accepted_by_the_closed_cleanup_namespace() -> None:
    """regular phase 的 UUID 名称必须进入同一窄 cleanup allowlist。"""
    connection = _FakeConnection()
    connection.database_name = _SUITE_DATABASE_NAME
    lease = DisposableDatabaseCleanupLease.acquire(
        connection=cast(Connection, connection),
        database_name=_SUITE_DATABASE_NAME,
        quote_identifier=connection.dialect.identifier_preparer.quote_identifier,
    )
    try:
        lease.assert_initial_absence()
    finally:
        lease.release()


def test_database_only_setup_failure_drops_owned_database_when_roles_remain_absent() -> None:
    """typed bootstrap 前失败时可按已冻结 DB OID 清理，但绝不猜测或删除角色。"""
    connection = _FakeConnection()
    lease = _acquire(connection)
    try:
        lease.assert_initial_absence()
        assert lease.create_database() is True

        lease.cleanup()

        assert connection.mutations == [
            f'CREATE DATABASE "{_DATABASE_NAME}"',
            f'DROP DATABASE "{_DATABASE_NAME}"',
        ]
        assert connection.roles == {}
    finally:
        lease.release()


def test_optional_stage_role_recorder_keeps_database_only_authority_when_roles_absent() -> None:
    """bootstrap 在角色事务前失败时，zero rows 不是角色删除权限，仍可只清理数据库。"""
    connection = _FakeConnection()
    lease = _acquire(connection)
    try:
        lease.assert_initial_absence()
        connection.database_oid = 401
        lease.record_created_database_after_create()

        assert lease.record_created_runtime_roles_if_safe() is False
        lease.cleanup()

        assert connection.mutations == [f'DROP DATABASE "{_DATABASE_NAME}"']
        assert connection.roles == {}
    finally:
        lease.release()


def test_database_stage_recorder_maps_driver_error_without_cleanup_authority() -> None:
    """CREATE 后 catalog 读取失败必须稳定拒绝，且不得推测 database 删除权限。"""
    connection = _FakeConnection()
    engine = cast(Engine, _FakeEngine(connection))

    class FailingManagementLease:
        """模拟 CREATE 已提交、database stage recorder 随后读取失败。"""

        def confirm_exact_non_production_target(self) -> None:
            return None

        def assert_reset_allowed(self) -> None:
            return None

        def drop_and_create_target(self) -> None:
            connection.database_oid = 401
            connection.database_rows_error = SQLAlchemyError("synthetic database recorder secret")

        def bootstrap_new_target(self) -> None:
            raise AssertionError("bootstrap must not run after database recorder failure")

        def run_typed_migration(self) -> None:
            raise AssertionError("migration must not run after database recorder failure")

    class FailingContext:
        """提供 production reset 编排所需的 management lease。"""

        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[FailingManagementLease]:
            yield FailingManagementLease()

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as cleanup,
    ):
        cleanup.assert_initial_absence()
        reset_then_migrate(
            disposable_database_module.bind_disposable_database_cleanup(
                cast(DatabaseMaintenanceContext, FailingContext()),
                cleanup,
            )
        )

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(caught.value))
    assert "synthetic database recorder secret" not in formatted
    assert connection.mutations == []


def test_bootstrap_role_stage_driver_error_preserves_original_error_chain() -> None:
    """角色 recorder 驱动失败不得替换 bootstrap 原因或授予角色删除权限。"""
    connection = _FakeConnection()
    engine = cast(Engine, _FakeEngine(connection))

    class FailingManagementLease:
        """CREATE 后让 role catalog 读取失败，再抛出原始 bootstrap 异常。"""

        def confirm_exact_non_production_target(self) -> None:
            return None

        def assert_reset_allowed(self) -> None:
            return None

        def drop_and_create_target(self) -> None:
            connection.database_oid = 401

        def bootstrap_new_target(self) -> None:
            connection.role_rows_error = SQLAlchemyError("synthetic role recorder secret")
            raise RuntimeError("synthetic bootstrap failure")

        def run_typed_migration(self) -> None:
            raise AssertionError("migration must not run after bootstrap failure")

    class FailingContext:
        """提供 production reset 编排所需的 management lease。"""

        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[FailingManagementLease]:
            yield FailingManagementLease()

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as cleanup,
    ):
        cleanup.assert_initial_absence()
        reset_then_migrate(
            disposable_database_module.bind_disposable_database_cleanup(
                cast(DatabaseMaintenanceContext, FailingContext()),
                cleanup,
            )
        )

    proxy_error = caught.value.__cause__
    assert isinstance(proxy_error, DisposableDatabaseCleanupError)
    assert isinstance(proxy_error.__cause__, RuntimeError)
    assert str(proxy_error.__cause__) == "synthetic bootstrap failure"
    formatted = "".join(traceback.format_exception(caught.value))
    assert "synthetic role recorder secret" not in formatted
    assert connection.mutations == []


def test_bootstrap_role_guard_driver_error_preserves_original_error_chain() -> None:
    """bootstrap 后 ownership 读取失败必须保留原异常链且不产生角色删除权限。"""
    connection = _FakeConnection()
    engine = cast(Engine, _FakeEngine(connection))

    class FailingManagementLease:
        """CREATE 后让 role recorder 的 guard 失败，并保留原 bootstrap 异常。"""

        def confirm_exact_non_production_target(self) -> None:
            return None

        def assert_reset_allowed(self) -> None:
            return None

        def drop_and_create_target(self) -> None:
            connection.database_oid = 401

        def bootstrap_new_target(self) -> None:
            connection.ownership_error = SQLAlchemyError("synthetic ownership secret")
            raise RuntimeError("synthetic bootstrap failure")

        def run_typed_migration(self) -> None:
            raise AssertionError("migration must not run after bootstrap failure")

    class FailingContext:
        """提供 production reset 编排所需的 management lease。"""

        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[FailingManagementLease]:
            yield FailingManagementLease()

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as cleanup,
    ):
        cleanup.assert_initial_absence()
        reset_then_migrate(
            disposable_database_module.bind_disposable_database_cleanup(
                cast(DatabaseMaintenanceContext, FailingContext()),
                cleanup,
            )
        )

    proxy_error = caught.value.__cause__
    assert isinstance(proxy_error, DisposableDatabaseCleanupError)
    assert isinstance(proxy_error.__cause__, RuntimeError)
    assert str(proxy_error.__cause__) == "synthetic bootstrap failure"
    formatted = "".join(traceback.format_exception(caught.value))
    assert "synthetic ownership secret" not in formatted
    assert connection.mutations == []


@pytest.mark.parametrize("roles_committed", (False, True), ids=("before-roles", "after-roles"))
def test_disposable_reset_records_stage_provenance_before_bootstrap_error(
    roles_committed: bool,
) -> None:
    """test-only context decorator 必须保持 production 编排并冻结阶段 provenance。"""
    connection = _FakeConnection()
    engine = cast(Engine, _FakeEngine(connection))
    calls: list[str] = []

    class FailingManagementLease:
        """模拟 CREATE 已成功、bootstrap 在角色 commit 前后失败的 production lease。"""

        def confirm_exact_non_production_target(self) -> None:
            calls.append("confirm")

        def assert_reset_allowed(self) -> None:
            calls.append("assert-reset")

        def drop_and_create_target(self) -> None:
            calls.append("drop-create")
            connection.database_oid = 401

        def bootstrap_new_target(self) -> None:
            calls.append("bootstrap")
            if roles_committed:
                connection.roles = {
                    APP_RUNTIME_ROLE_NAME: _safe_role(APP_RUNTIME_ROLE_NAME, 501),
                    RETENTION_RUNTIME_ROLE_NAME: _safe_role(
                        RETENTION_RUNTIME_ROLE_NAME,
                        502,
                    ),
                }
            raise RuntimeError("synthetic bootstrap failure")

        def run_typed_migration(self) -> None:
            raise AssertionError("migration must not run after bootstrap failure")

    class FailingContext:
        """只提供 production reset 编排会消费的 management context manager。"""

        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[FailingManagementLease]:
            calls.append("acquire-management")
            yield FailingManagementLease()

    with (
        pytest.raises(RuntimeError, match=r"^synthetic bootstrap failure$"),
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as cleanup,
    ):
        cleanup.assert_initial_absence()
        reset_then_migrate(
            disposable_database_module.bind_disposable_database_cleanup(
                cast(DatabaseMaintenanceContext, FailingContext()),
                cleanup,
            )
        )

    assert calls == [
        "acquire-management",
        "confirm",
        "assert-reset",
        "drop-create",
        "bootstrap",
    ]
    expected = [f'DROP DATABASE "{_DATABASE_NAME}"']
    if roles_committed:
        expected.extend(
            (
                f'DROP ROLE "{APP_RUNTIME_ROLE_NAME}"',
                f'DROP ROLE "{RETENTION_RUNTIME_ROLE_NAME}"',
            )
        )
    assert connection.mutations == expected


@pytest.mark.parametrize("role_failure", ("partial", "unsafe"))
def test_disposable_reset_chains_bootstrap_error_when_role_recording_is_unsafe(
    role_failure: str,
) -> None:
    """角色 catalog 不可安全认领时抛稳定 invariant，并保留原 bootstrap cause。"""
    connection = _FakeConnection()
    engine = cast(Engine, _FakeEngine(connection))

    class FailingManagementLease:
        """在 CREATE 后制造 partial 或 unsafe role catalog，再抛原始异常。"""

        def confirm_exact_non_production_target(self) -> None:
            return None

        def assert_reset_allowed(self) -> None:
            return None

        def drop_and_create_target(self) -> None:
            connection.database_oid = 401

        def bootstrap_new_target(self) -> None:
            role = _safe_role(APP_RUNTIME_ROLE_NAME, 501)
            if role_failure == "unsafe":
                role["is_superuser"] = True
            connection.roles = {APP_RUNTIME_ROLE_NAME: role}
            if role_failure == "unsafe":
                connection.roles[RETENTION_RUNTIME_ROLE_NAME] = _safe_role(
                    RETENTION_RUNTIME_ROLE_NAME,
                    502,
                )
            raise RuntimeError("synthetic bootstrap failure")

        def run_typed_migration(self) -> None:
            raise AssertionError("migration must not run after bootstrap failure")

    class FailingContext:
        """提供 production reset 所需的单一 management lease。"""

        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[FailingManagementLease]:
            yield FailingManagementLease()

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as cleanup,
    ):
        cleanup.assert_initial_absence()
        reset_then_migrate(
            disposable_database_module.bind_disposable_database_cleanup(
                cast(DatabaseMaintenanceContext, FailingContext()),
                cleanup,
            )
        )

    proxy_error = caught.value.__cause__
    assert isinstance(proxy_error, DisposableDatabaseCleanupError)
    assert str(proxy_error) == "disposable database cleanup provenance violation"
    assert isinstance(proxy_error.__cause__, RuntimeError)
    assert str(proxy_error.__cause__) == "synthetic bootstrap failure"
    assert connection.mutations == []


def test_cleanup_unlock_and_close_failures_preserve_full_bootstrap_error_chain() -> None:
    """cleanup 拒绝、unlock 与 close 同时失败也必须保留完整稳定异常链且不泄密。"""
    connection = _FakeConnection()
    connection.unlock_error = SQLAlchemyError("synthetic unlock secret")
    connection.close_error = SQLAlchemyError("synthetic close secret")
    engine = cast(Engine, _FakeEngine(connection))

    class FailingManagementLease:
        """CREATE 后留下 partial role，并以原 bootstrap RuntimeError 结束。"""

        def confirm_exact_non_production_target(self) -> None:
            return None

        def assert_reset_allowed(self) -> None:
            return None

        def drop_and_create_target(self) -> None:
            connection.database_oid = 401

        def bootstrap_new_target(self) -> None:
            connection.roles = {APP_RUNTIME_ROLE_NAME: _safe_role(APP_RUNTIME_ROLE_NAME, 501)}
            raise RuntimeError("synthetic bootstrap failure")

        def run_typed_migration(self) -> None:
            raise AssertionError("migration must not run after bootstrap failure")

    class FailingContext:
        """提供 production reset 所需的单一 management lease。"""

        @contextmanager
        def acquire_management_lifecycle_lock(self) -> Iterator[FailingManagementLease]:
            yield FailingManagementLease()

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as cleanup,
    ):
        cleanup.assert_initial_absence()
        reset_then_migrate(
            disposable_database_module.bind_disposable_database_cleanup(
                cast(DatabaseMaintenanceContext, FailingContext()),
                cleanup,
            )
        )

    proxy_error = caught.value.__cause__
    assert isinstance(proxy_error, DisposableDatabaseCleanupError)
    assert isinstance(proxy_error.__cause__, RuntimeError)
    assert str(proxy_error.__cause__) == "synthetic bootstrap failure"
    assert "synthetic unlock secret" not in repr(caught.value)
    assert "synthetic close secret" not in repr(caught.value)
    assert connection.closed is True
    assert connection.mutations == []


def test_active_body_maps_cleanup_driver_error_without_masking_body() -> None:
    """cleanup 驱动异常必须变成稳定 invariant，并以原 body 异常为 direct cause。"""
    connection = _FakeConnection()
    connection.database_session_error = SQLAlchemyError("synthetic cleanup secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as lease,
    ):
        _record_complete_provenance(lease, connection)
        raise RuntimeError("synthetic body failure")

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "synthetic body failure"
    assert "synthetic cleanup secret" not in repr(caught.value)
    assert connection.mutations == []


def test_normal_body_maps_cleanup_driver_error_to_stable_invariant() -> None:
    """正常 body 的 cleanup 驱动异常必须 fail closed，且不得泄露 DBAPI 文本。"""
    connection = _FakeConnection()
    connection.database_session_error = SQLAlchemyError("synthetic cleanup secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as lease,
    ):
        _record_complete_provenance(lease, connection)

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "synthetic cleanup secret" not in repr(caught.value)
    assert connection.mutations == []


def test_normal_cleanup_invariant_is_not_replaced_by_close_error() -> None:
    """正常 body 的 cleanup invariant 必须优先于随后发生的 close 驱动异常。"""
    connection = _FakeConnection()
    connection.dependencies["database_sessions"] = 1
    connection.close_error = SQLAlchemyError("synthetic close secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as lease,
    ):
        _record_complete_provenance(lease, connection)

    assert caught.value.__context__ is None
    formatted = "".join(traceback.format_exception(caught.value))
    assert "synthetic close secret" not in formatted
    assert connection.mutations == []


def test_normal_cleanup_driver_invariant_is_not_replaced_by_close_error() -> None:
    """cleanup 驱动映射后的 invariant 必须保持主导，close 只能尽力收尾。"""
    connection = _FakeConnection()
    cleanup_error = SQLAlchemyError("synthetic cleanup secret")
    connection.database_session_error = cleanup_error
    connection.close_error = SQLAlchemyError("synthetic close secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as lease,
    ):
        _record_complete_provenance(lease, connection)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is cleanup_error
    assert caught.value.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(caught.value))
    assert "synthetic cleanup secret" not in formatted
    assert "synthetic close secret" not in formatted
    assert connection.mutations == []


def test_normal_close_only_error_maps_to_stable_invariant() -> None:
    """body 与 cleanup 均成功时，close 驱动失败仍须给出固定且脱敏的不变量。"""
    connection = _FakeConnection()
    close_error = SQLAlchemyError("synthetic close secret")
    connection.close_error = close_error
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_disposable_database_cleanup(
            engine,
            database_name=_DATABASE_NAME,
        ) as lease,
    ):
        lease.assert_initial_absence()

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is close_error
    assert caught.value.__suppress_context__ is True
    formatted = "".join(traceback.format_exception(caught.value))
    assert "synthetic close secret" not in formatted
    assert connection.mutations == []


def test_suite_global_lock_uses_distinct_key_and_releases_on_exception() -> None:
    """parent suite lease 不得占用 child fixture 使用的 destructive role lock。"""
    connection = _FakeConnection()
    engine = cast(Engine, _FakeEngine(connection))

    assert INTEGRATION_SUITE_LOCK != DISPOSABLE_DATABASE_CLEANUP_LOCK
    with (
        pytest.raises(RuntimeError, match="synthetic suite failure"),
        managed_integration_suite_lock(engine),
    ):
        assert connection.lock_held is True
        raise RuntimeError("synthetic suite failure")

    assert connection.lock_requests == [INTEGRATION_SUITE_LOCK]
    assert connection.lock_held is False
    assert connection.closed is True
    assert connection.mutations == []


@pytest.mark.parametrize(
    "drift",
    ("backend_pid", "system_identifier", "ownership"),
)
def test_suite_lock_lease_verify_rejects_session_cluster_or_ownership_drift(
    drift: str,
) -> None:
    """任一 live provenance 漂移都必须在 phase guard 处 fail closed。"""
    connection = _FakeConnection()
    engine = cast(Engine, _FakeEngine(connection))

    with managed_integration_suite_lock(engine) as lease:
        original_backend_pid = connection.backend_pid
        original_system_identifier = connection.system_identifier
        if drift == "backend_pid":
            connection.backend_pid += 1
        elif drift == "system_identifier":
            connection.system_identifier += 1
        else:
            connection.lock_held = False

        with pytest.raises(DisposableDatabaseCleanupError):
            lease.verify()

        # 还原 Fake catalog，允许 context 的严格成功释放单独验证 unlock ACK。
        connection.backend_pid = original_backend_pid
        connection.system_identifier = original_system_identifier
        connection.lock_held = True


@pytest.mark.parametrize(
    "drift",
    ("backend_pid", "system_identifier", "ownership"),
)
def test_suite_lock_success_exit_rechecks_live_provenance(drift: str) -> None:
    """即使 body 未显式调用 guard，成功退出也必须先重验 live lease 再 unlock。"""
    connection = _FakeConnection()
    engine = cast(Engine, _FakeEngine(connection))

    with pytest.raises(DisposableDatabaseCleanupError), managed_integration_suite_lock(engine):
        if drift == "backend_pid":
            connection.backend_pid += 1
        elif drift == "system_identifier":
            connection.system_identifier += 1
        else:
            connection.lock_held = False

    assert connection.closed is True
    assert connection.lock_held is False


def test_suite_lock_success_exit_rejects_false_unlock_acknowledgement() -> None:
    """成功 body 必须精确收到 ``pg_advisory_unlock() = true``。"""
    connection = _FakeConnection()
    connection.unlock_result = False
    engine = cast(Engine, _FakeEngine(connection))

    with pytest.raises(DisposableDatabaseCleanupError), managed_integration_suite_lock(engine):
        pass

    assert connection.closed is True
    assert connection.lock_held is False


def test_suite_lock_success_exit_rejects_unlock_sqlalchemy_error() -> None:
    """正常 body 的 unlock DBAPI 异常必须收敛为稳定、非泄密的基础设施错误。"""
    connection = _FakeConnection()
    connection.unlock_error = SQLAlchemyError("synthetic unlock secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ),
        managed_integration_suite_lock(engine),
    ):
        pass

    assert connection.closed is True
    assert connection.lock_held is False


def test_suite_lock_release_error_does_not_mask_body_exception() -> None:
    """body 已失败时只安全 close，释放异常不得覆盖原始业务异常。"""
    connection = _FakeConnection()
    connection.unlock_error = SQLAlchemyError("synthetic unlock secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(RuntimeError, match=r"^synthetic body failure$"),
        managed_integration_suite_lock(engine),
    ):
        raise RuntimeError("synthetic body failure")

    assert connection.closed is True
    assert connection.lock_held is False


def test_suite_lock_close_error_does_not_mask_body_exception() -> None:
    """body RuntimeError 为主异常；close SQLAlchemyError 文本不得覆盖或泄漏。"""
    connection = _FakeConnection()
    connection.close_error = SQLAlchemyError("synthetic close secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(RuntimeError, match=r"^synthetic body failure$") as caught,
        managed_integration_suite_lock(engine),
    ):
        raise RuntimeError("synthetic body failure")

    assert "synthetic close secret" not in str(caught.value)
    assert connection.closed is True


def test_suite_lock_close_error_does_not_mask_lease_verify_error() -> None:
    """active lease continuity invariant 必须优先于 close DBAPI 细节。"""
    connection = _FakeConnection()
    connection.close_error = SQLAlchemyError("synthetic close secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_integration_suite_lock(engine) as lease,
    ):
        connection.backend_pid += 1
        lease.verify()

    assert "synthetic close secret" not in str(caught.value)
    assert connection.closed is True


def test_suite_lock_close_error_does_not_mask_strict_release_error() -> None:
    """strict unlock 已产生稳定 invariant 时，后续 close 失败不得替换它。"""
    connection = _FakeConnection()
    connection.unlock_result = False
    connection.close_error = SQLAlchemyError("synthetic close secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_integration_suite_lock(engine),
    ):
        pass

    assert "synthetic close secret" not in str(caught.value)
    assert connection.closed is True


def test_suite_lock_normal_body_maps_close_error_to_stable_invariant() -> None:
    """仅 close 失败时不能静默成功，也不能暴露 DBAPI 文本。"""
    connection = _FakeConnection()
    connection.close_error = SQLAlchemyError("synthetic close secret")
    engine = cast(Engine, _FakeEngine(connection))

    with (
        pytest.raises(
            DisposableDatabaseCleanupError,
            match=r"^disposable database cleanup provenance violation$",
        ) as caught,
        managed_integration_suite_lock(engine),
    ):
        pass

    assert "synthetic close secret" not in str(caught.value)
    assert connection.closed is True


def test_suite_lock_close_error_does_not_rewrite_child_nonzero_exit_code() -> None:
    """orchestrator 的 typed child code 经过 close 失败路径后仍须原样返回。"""
    connection = _FakeConnection()
    connection.close_error = SQLAlchemyError("synthetic close secret")
    engine = cast(Engine, _FakeEngine(connection))

    def run_child(invocation: PytestInvocation, suite_lease: IntegrationSuiteLease) -> int:
        del invocation, suite_lease
        return 23

    result = orchestrate_integration_tests(
        anchor=validate_test_database_url(_ANCHOR_URL),
        base_environment={},
        resources=_ManagedSuiteResources(engine),
        run_child=run_child,
        python_executable="python",
        repository_root=Path("/repository"),
    )

    assert result == 23
    assert connection.closed is True
