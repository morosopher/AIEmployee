"""冻结 protected reset 的 post-create provenance 与 absent-target lifecycle authority。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine

import ai_employee.infrastructure.db.database_maintenance as maintenance_module
from ai_employee.infrastructure.db.alembic import (
    PublishedAlembicAuthority,
    load_published_alembic_authority,
)
from ai_employee.infrastructure.db.database_access import (
    APP_RUNTIME_ROLE_NAME,
    RETENTION_RUNTIME_ROLE_NAME,
    BootstrapCaller,
    DatabaseAccessSnapshot,
    DatabaseAclProfile,
    DatabaseAclTuple,
    RuntimeRoleSnapshot,
)
from ai_employee.infrastructure.db.database_grants import BootstrapGrantPosture, GrantPhase


class _SafeEngineStub:
    """为不触发 Engine I/O 的 reset 单元测试声明参数脱敏能力。"""

    hide_parameters = True


_SAFE_ENGINE_STUB = cast(Engine, _SafeEngineStub())


def _published_authority() -> PublishedAlembicAuthority:
    """从当前 backend migration scripts 读取测试使用的实际发布权威。"""
    backend_root = Path(__file__).resolve().parents[4]
    return load_published_alembic_authority(Config(backend_root / "alembic.ini"))


def _safe_role(name: str, oid: int) -> RuntimeRoleSnapshot:
    """构造与 PostgreSQL 17 runtime role 冻结姿态一致的合成角色。"""
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


def _fresh_roles_exist_snapshot() -> DatabaseAccessSnapshot:
    """构造新库 default ACL + 两个已存在 safe cluster roles 的合法 Candidate。"""
    owner_oid = 101
    app_oid = 201
    retention_oid = 202
    return DatabaseAccessSnapshot(
        target_database_oid=401,
        owner_oid=owner_oid,
        acl_profile=DatabaseAclProfile.FRESH_DEFAULT,
        acl_tuples=tuple(
            sorted(
                (
                    DatabaseAclTuple(owner_oid, owner_oid, "CREATE", False),
                    DatabaseAclTuple(owner_oid, owner_oid, "CONNECT", False),
                    DatabaseAclTuple(owner_oid, owner_oid, "TEMPORARY", False),
                    DatabaseAclTuple(0, owner_oid, "CONNECT", False),
                    DatabaseAclTuple(0, owner_oid, "TEMPORARY", False),
                )
            )
        ),
        app_role=_safe_role(APP_RUNTIME_ROLE_NAME, app_oid),
        retention_role=_safe_role(RETENTION_RUNTIME_ROLE_NAME, retention_oid),
        memberships=(),
    )


def test_context_rejects_engines_that_can_render_bound_runtime_secrets() -> None:
    """非 CLI 调用也不得让不安全 Engine 到达 runtime-role password mutation。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        SqlAlchemyDatabaseMaintenanceContext,
    )

    unsafe_engine = create_engine("sqlite://", hide_parameters=False)
    safe_engine = create_engine("sqlite://", hide_parameters=True)
    try:
        with pytest.raises(
            DatabaseMaintenanceInvariantError,
            match=r"^database maintenance invariant violation$",
        ):
            SqlAlchemyDatabaseMaintenanceContext(
                management_engine=unsafe_engine,
                target_engine=safe_engine,
                target_database_name="synthetic_test",
                bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
                app_password="synthetic-app-secret",
                retention_password="synthetic-retention-secret",
                migration_runner=lambda connection: None,
                published_authority=_published_authority(),
            )
    finally:
        safe_engine.dispose()
        unsafe_engine.dispose()


class _NoTransactionConnection:
    """提供纯读 lease 测试所需的最小 transaction 状态。"""

    def in_transaction(self) -> bool:
        return False

    def commit(self) -> None:
        raise AssertionError("no synthetic read transaction is open")

    def rollback(self) -> None:
        raise AssertionError("no synthetic read transaction is open")


def test_post_create_roles_exist_still_require_pre_runtime_object_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CREATE 后 cluster roles 已存在时，provenance 仍必须验证/应用 pre-runtime grants。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseRestoreFacts,
        ProtectedResetPolicy,
        SqlAlchemyDatabaseMaintenanceContext,
        _DatabaseTargetCatalog,
        _PostCreateTargetProvenance,
        _SqlAlchemyTargetMaintenanceLease,
        derive_database_target_identity,
    )

    identity = derive_database_target_identity(
        system_identifier_ascii=b"1",
        database_name_utf8=b"synthetic_reset_test",
        target_name_verified=True,
    )
    target = _DatabaseTargetCatalog(
        identity=identity,
        database_oid=401,
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name="synthetic_reset_test",
    )
    context = SqlAlchemyDatabaseMaintenanceContext(
        management_engine=_SAFE_ENGINE_STUB,
        target_engine=_SAFE_ENGINE_STUB,
        target_database_name=target.database_name,
        bootstrap_caller=BootstrapCaller.DB_RESET_POST_CREATE,
        app_password="synthetic-app-secret",
        retention_password="synthetic-retention-secret",
        migration_runner=lambda connection: None,
        published_authority=_published_authority(),
        reset_policy=ProtectedResetPolicy(
            app_env="test",
            confirmed_database_name=target.database_name,
        ),
    )
    connection = cast(Connection, _NoTransactionConnection())
    observed_postures: list[BootstrapGrantPosture] = []

    monkeypatch.setattr(
        maintenance_module,
        "_read_target_catalog",
        lambda connection, *, expected: expected,
    )
    monkeypatch.setattr(
        maintenance_module,
        "_read_current_revision",
        lambda connection, *, authority: "base",
    )
    monkeypatch.setattr(
        maintenance_module,
        "read_database_restore_facts",
        lambda connection, *, target_database_oid: DatabaseRestoreFacts(None, None, None),
    )
    monkeypatch.setattr(
        maintenance_module,
        "read_database_access_snapshot_sync",
        lambda connection, *, target_database_oid: _fresh_roles_exist_snapshot(),
    )
    monkeypatch.setattr(
        maintenance_module,
        "_has_active_non_owner_sessions",
        lambda connection, *, target: False,
    )

    def fake_verify_bootstrap_grants(
        connection: Connection,
        *,
        revision: str,
        phase: GrantPhase,
        posture: BootstrapGrantPosture,
    ) -> object:
        assert revision == "base"
        assert phase is GrantPhase.BASELINE
        observed_postures.append(posture)
        return object()

    monkeypatch.setattr(
        maintenance_module,
        "verify_bootstrap_object_grants",
        fake_verify_bootstrap_grants,
    )
    lease = _SqlAlchemyTargetMaintenanceLease(
        context=context,
        connection=connection,
        target=target,
        revision="base",
        post_create_provenance=_PostCreateTargetProvenance(
            target=target,
            lifecycle_lock_key=identity.lifecycle_lock_key,
        ),
    )

    candidate = lease.read_bootstrap_candidate()

    assert candidate.roles_exist is True
    assert observed_postures == [BootstrapGrantPosture.PRE_RUNTIME]


class _FakeManagementConnection:
    """记录 absent-target reset 的 DDL，且不模拟任何 target session。"""

    def __init__(self) -> None:
        sqlite_engine = create_engine("sqlite://")
        self.dialect = sqlite_engine.dialect
        sqlite_engine.dispose()
        self.created = False
        self.ddl_sql: list[str] = []

    def execute(self, statement: object, parameters: object = None) -> object:
        del parameters
        sql = str(statement)
        self.ddl_sql.append(sql)
        if sql.startswith("CREATE DATABASE"):
            self.created = True
        return object()

    def execution_options(self, **options: object) -> _FakeManagementConnection:
        assert options == {"isolation_level": "AUTOCOMMIT"}
        return self

    def in_transaction(self) -> bool:
        return False

    def commit(self) -> None:
        raise AssertionError("autocommit DDL must not call commit")

    def rollback(self) -> None:
        raise AssertionError("no synthetic transaction is open")


class _FakeConnect:
    """把一个固定 fake management connection 暴露为 Engine context manager。"""

    def __init__(self, connection: _FakeManagementConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _FakeManagementConnection:
        return self.connection

    def __exit__(self, *args: object) -> None:
        del args


class _FakeManagementEngine:
    """记录整个 reset 是否只打开一个 live management lease。"""

    hide_parameters = True

    def __init__(self, connection: _FakeManagementConnection) -> None:
        self.connection = connection
        self.connect_count = 0

    def connect(self) -> _FakeConnect:
        self.connect_count += 1
        return _FakeConnect(self.connection)


def test_absent_target_reuses_confirmed_identity_key_and_same_management_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """absent reset 只能用 exact-confirmed bytes 建锁，并在同一 management lease 内 CREATE。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        ProtectedResetPolicy,
        SqlAlchemyDatabaseMaintenanceContext,
        _AbsentDatabaseTargetCatalog,
        _DatabaseTargetCatalog,
        derive_database_target_identity,
    )

    database_name = "synthetic_absent_reset_test"
    identity = derive_database_target_identity(
        system_identifier_ascii=b"1",
        database_name_utf8=database_name.encode("utf-8"),
        target_name_verified=True,
    )
    absent = _AbsentDatabaseTargetCatalog(
        identity=identity,
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name=database_name,
    )
    created = _DatabaseTargetCatalog(
        identity=identity,
        database_oid=402,
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name=database_name,
    )
    connection = _FakeManagementConnection()
    engine = _FakeManagementEngine(connection)
    catalog_calls: list[tuple[bool, object]] = []
    lock_events: list[tuple[str, int]] = []

    def fake_catalog_read(
        observed_connection: object,
        *,
        target_database_name: str,
        allow_absent: bool,
    ) -> object:
        assert observed_connection is connection
        assert target_database_name == database_name
        catalog_calls.append((allow_absent, observed_connection))
        return created if connection.created else absent

    monkeypatch.setattr(maintenance_module, "_read_management_target_catalog", fake_catalog_read)
    monkeypatch.setattr(
        maintenance_module,
        "_acquire_single_session_lock",
        lambda observed_connection, *, lock_key: lock_events.append(("acquire", lock_key)),
    )
    monkeypatch.setattr(
        maintenance_module,
        "_release_single_session_lock",
        lambda observed_connection, *, lock_key: lock_events.append(("release", lock_key)),
    )
    context = SqlAlchemyDatabaseMaintenanceContext(
        management_engine=cast(Engine, engine),
        target_engine=_SAFE_ENGINE_STUB,
        target_database_name=database_name,
        bootstrap_caller=BootstrapCaller.DB_RESET_POST_CREATE,
        app_password="synthetic-app-secret",
        retention_password="synthetic-retention-secret",
        migration_runner=lambda target_connection: None,
        published_authority=_published_authority(),
        reset_policy=ProtectedResetPolicy(
            app_env="test",
            confirmed_database_name=database_name,
        ),
    )

    with context.acquire_management_lifecycle_lock() as management:
        management.confirm_exact_non_production_target()
        management.assert_reset_allowed()
        management.drop_and_create_target()
        assert management._connection is connection
        assert management._target is created

    assert engine.connect_count == 1
    assert catalog_calls == [(True, connection), (True, connection), (False, connection)]
    assert lock_events == [
        ("acquire", identity.lifecycle_lock_key),
        ("release", identity.lifecycle_lock_key),
    ]
    assert connection.ddl_sql == [
        'CREATE DATABASE "synthetic_absent_reset_test" OWNER "synthetic_owner"'
    ]


def test_successful_post_create_bootstrap_consumes_provenance_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功 bootstrap 后同一 CREATE provenance 不得再次取得 target mutation lease。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        ProtectedResetPolicy,
        SqlAlchemyDatabaseMaintenanceContext,
        _AbsentDatabaseTargetCatalog,
        _DatabaseTargetCatalog,
        _SqlAlchemyManagementLifecycleLease,
        derive_database_target_identity,
    )

    class FakeTransaction:
        """记录一次完整 candidate→baseline 调用，但不执行真实 mutation。"""

        def apply_safe_runtime_roles(self, candidate: object) -> None:
            assert candidate is reset_candidate

        def apply_database_acl(self, profile: DatabaseAclProfile) -> None:
            assert profile is DatabaseAclProfile.BASELINE

        def apply_object_grants(self, *, revision: str, phase: GrantPhase) -> None:
            assert revision == "base"
            assert phase is GrantPhase.BASELINE

        def verify_pristine_idle_before_commit(self) -> None:
            return None

    class FakeTarget:
        """提供 post-create bootstrap 编排所需的最小 typed target lease。"""

        revision = "base"

        def assert_restore_facts_absent(self) -> None:
            return None

        def read_bootstrap_candidate(self) -> object:
            return reset_candidate

        @contextmanager
        def acquire_schema_lifecycle_lock(self) -> Iterator[None]:
            yield

        @contextmanager
        def owner_transaction(self) -> Iterator[FakeTransaction]:
            yield FakeTransaction()

        def verify_pristine_idle_after_commit(self) -> None:
            return None

    database_name = "synthetic_absent_reset_test"
    identity = derive_database_target_identity(
        system_identifier_ascii=b"1",
        database_name_utf8=database_name.encode("utf-8"),
        target_name_verified=True,
    )
    absent = _AbsentDatabaseTargetCatalog(
        identity=identity,
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name=database_name,
    )
    created = _DatabaseTargetCatalog(
        identity=identity,
        database_oid=402,
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name=database_name,
    )
    connection = _FakeManagementConnection()
    engine = _FakeManagementEngine(connection)
    reset_candidate = object()
    target_lock_calls = 0

    monkeypatch.setattr(
        maintenance_module,
        "_read_management_target_catalog",
        lambda observed_connection, *, target_database_name, allow_absent: (
            created if connection.created else absent
        ),
    )
    monkeypatch.setattr(
        maintenance_module,
        "_acquire_single_session_lock",
        lambda observed_connection, *, lock_key: None,
    )
    monkeypatch.setattr(
        maintenance_module,
        "_release_single_session_lock",
        lambda observed_connection, *, lock_key: None,
    )

    @contextmanager
    def fake_acquire_target_lock(
        lease: _SqlAlchemyManagementLifecycleLease,
    ) -> Iterator[FakeTarget]:
        nonlocal target_lock_calls
        target_lock_calls += 1
        yield FakeTarget()

    monkeypatch.setattr(
        _SqlAlchemyManagementLifecycleLease,
        "acquire_target_lock",
        fake_acquire_target_lock,
    )
    context = SqlAlchemyDatabaseMaintenanceContext(
        management_engine=cast(Engine, engine),
        target_engine=_SAFE_ENGINE_STUB,
        target_database_name=database_name,
        bootstrap_caller=BootstrapCaller.DB_RESET_POST_CREATE,
        app_password="synthetic-app-secret",
        retention_password="synthetic-retention-secret",
        migration_runner=lambda target_connection: None,
        published_authority=_published_authority(),
        reset_policy=ProtectedResetPolicy(
            app_env="test",
            confirmed_database_name=database_name,
        ),
    )

    with context.acquire_management_lifecycle_lock() as management:
        management.confirm_exact_non_production_target()
        management.assert_reset_allowed()
        management.drop_and_create_target()
        management.bootstrap_new_target()
        with pytest.raises(
            DatabaseMaintenanceInvariantError,
            match=r"^database maintenance invariant violation$",
        ):
            management.bootstrap_new_target()

    assert target_lock_calls == 1


@pytest.mark.parametrize(
    "operation",
    (
        pytest.param(maintenance_module.migrate_database, id="migrate"),
        pytest.param(maintenance_module.role_bootstrap_database, id="role-bootstrap"),
    ),
)
def test_ordinary_lifecycle_rejects_absent_target_before_lock_or_write(
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable[[object], None],
) -> None:
    """只有 protected reset 可解释 absent；普通入口不得从 DSN 猜名或取得 mutation authority。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        SqlAlchemyDatabaseMaintenanceContext,
        _AbsentDatabaseTargetCatalog,
        derive_database_target_identity,
    )

    database_name = "synthetic_absent_reset_test"
    absent = _AbsentDatabaseTargetCatalog(
        identity=derive_database_target_identity(
            system_identifier_ascii=b"1",
            database_name_utf8=database_name.encode("utf-8"),
            target_name_verified=True,
        ),
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name=database_name,
    )
    connection = _FakeManagementConnection()
    engine = _FakeManagementEngine(connection)
    lock_calls: list[int] = []
    runner_calls: list[Connection] = []
    monkeypatch.setattr(
        maintenance_module,
        "_read_management_target_catalog",
        lambda observed_connection, *, target_database_name, allow_absent: absent,
    )
    monkeypatch.setattr(
        maintenance_module,
        "_acquire_single_session_lock",
        lambda observed_connection, *, lock_key: lock_calls.append(lock_key),
    )
    context = SqlAlchemyDatabaseMaintenanceContext(
        management_engine=cast(Engine, engine),
        target_engine=_SAFE_ENGINE_STUB,
        target_database_name=database_name,
        bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
        app_password="synthetic-app-secret",
        retention_password="synthetic-retention-secret",
        migration_runner=runner_calls.append,
        published_authority=_published_authority(),
    )

    with pytest.raises(
        DatabaseMaintenanceInvariantError,
        match=r"^database maintenance invariant violation$",
    ):
        operation(context)

    assert engine.connect_count == 1
    assert lock_calls == []
    assert runner_calls == []
    assert connection.ddl_sql == []
