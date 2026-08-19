"""冻结 Alembic 发布权威、同连接 grant lifecycle 与维护入口 fail-closed 边界。"""

from __future__ import annotations

import runpy
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import cast

import alembic as alembic_package
import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event

import ai_employee.infrastructure.db.alembic as alembic_module
from ai_employee.infrastructure.db.database_access import BootstrapCaller
from ai_employee.infrastructure.db.database_grants import CatalogObjectSnapshot, GrantPhase


class _SafeEngineStub:
    """为只验证 concrete lease 的单元测试声明参数脱敏能力。"""

    hide_parameters = True


_SAFE_ENGINE_STUB = cast(Engine, _SafeEngineStub())


def _backend_root() -> Path:
    """返回当前测试文件对应的 backend 根目录，避免依赖进程工作目录。"""
    return Path(__file__).resolve().parents[4]


def _issue_synthetic_live_authority(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connection: Connection,
    published: alembic_module.PublishedAlembicAuthority,
    system_identifier_ascii: bytes = b"1",
    current_revision: str = "base",
    expected_target_revision: str | None = None,
) -> alembic_module.OnlineMigrationAuthority:
    """从 exact concrete lease 构造不执行 PostgreSQL I/O 的 unit-test token。

    这些 env/token 单测只验证 capability 绑定，不重复集成测试的 catalog
    SQL。因此测试使用真实 ``_SqlAlchemyTargetMaintenanceLease`` 与同步
    SQLAlchemy ``Connection``，仅把 stable-admission reader 替换为类型精确的已验
    ``pristine_idle`` 结果。生产代码不因测试增加任何构造器或 waiver。

    Args:
        monkeypatch: 仅替换当前 lease 的 catalog reader。
        connection: env/token 测试全程绑定的同一同步 Connection。
        published: 从当前 migration scripts 派生的发布链。
        system_identifier_ascii: 用于生成相同或不同 target identity 的合成向量。
        current_revision: 本 lease 已冻结的 source revision。
        expected_target_revision: 精确 target；缺省时使用实际已发布 head。

    Returns:
        由 concrete lease 私有方法发行的 identity-bound token。
    """
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseAdmissionState,
        SqlAlchemyDatabaseMaintenanceContext,
        _DatabaseTargetCatalog,
        _SqlAlchemyTargetMaintenanceLease,
        derive_database_target_identity,
    )

    target_name = "synthetic_target"
    context = SqlAlchemyDatabaseMaintenanceContext(
        management_engine=_SAFE_ENGINE_STUB,
        target_engine=_SAFE_ENGINE_STUB,
        target_database_name=target_name,
        bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
        app_password=None,
        retention_password=None,
        migration_runner=lambda _authority: None,
        published_authority=published,
    )
    target = _DatabaseTargetCatalog(
        identity=derive_database_target_identity(
            system_identifier_ascii=system_identifier_ascii,
            database_name_utf8=target_name.encode("utf-8"),
            target_name_verified=True,
        ),
        database_oid=401,
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name=target_name,
    )
    lease = _SqlAlchemyTargetMaintenanceLease(
        context=context,
        connection=connection,
        target=target,
        revision=current_revision,
    )
    lease._schema_lock_held = True
    lease._migration_admitted_revision = current_revision

    def read_stable_admission(
        *,
        require_revision: str | None,
    ) -> tuple[str, DatabaseAdmissionState]:
        """返回与本 lease 冻结 source 一致的 stable baseline admission。"""
        assert require_revision == current_revision
        return current_revision, DatabaseAdmissionState.PRISTINE_IDLE

    monkeypatch.setattr(lease, "_read_stable_admission", read_stable_admission)
    return lease._issue_online_migration_authority(
        expected_target_revision=(
            published.head_revision
            if expected_target_revision is None
            else expected_target_revision
        )
    )


def test_published_authority_comes_only_from_current_script_chain() -> None:
    """发布权威必须只来自实际 migration 脚本链。

    0019 发布后必须成为唯一 head；grant inventory 或调用方标签仍不能把一个没有对应
    脚本的合成 revision 补进发布链。
    """
    authority = alembic_module.load_published_alembic_authority(
        Config(_backend_root() / "alembic.ini")
    )

    assert authority.head_revision == "20260809_0019"
    assert authority.revisions[0] == "base"
    assert authority.revisions[-1] == authority.head_revision
    assert authority.contains("base")
    assert authority.contains("20260809_0018")
    assert authority.contains("20260809_0019")
    assert not authority.contains("20991231_9999_synthetic_unpublished")


def test_online_connection_binding_rejects_missing_or_non_sqlalchemy_connection() -> None:
    """online env 只能消费调用方注入的同步 Connection，不得用 URL 暗建第二个 Engine。"""
    config = Config()
    with pytest.raises(
        alembic_module.AlembicMigrationInvariantError,
        match=r"^alembic migration invariant violation$",
    ):
        alembic_module.require_external_connection(config)

    config.attributes["connection"] = object()
    with pytest.raises(
        alembic_module.AlembicMigrationInvariantError,
        match=r"^alembic migration invariant violation$",
    ):
        alembic_module.require_external_connection(config)


def test_bare_connection_cannot_issue_online_migration_authority() -> None:
    """裸 Connection 即使携带完整 caller claims 也不得铸造 migration authority。

    token 必须来自已持有 management→target→schema 三锁且完成 stable
    admission 的 concrete maintenance lease。本回归保留旧入口的全部自报字段，
    以证明 issuer 不能因 claims 看似完整而跳过 live lease 证明。
    """
    config = Config(_backend_root() / "alembic.ini")
    published = alembic_module.load_published_alembic_authority(config)
    engine = create_engine("sqlite://")

    try:
        with (
            engine.connect() as connection,
            pytest.raises(
                alembic_module.AlembicMigrationInvariantError,
                match=r"^alembic migration invariant violation$",
            ),
        ):
            alembic_module._issue_online_migration_authority(
                connection=connection,
                published_authority=published,
                expected_target_identity_digest="a" * 64,
                expected_current_revision="base",
                expected_target_revision=published.head_revision,
                phase=GrantPhase.BASELINE,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "invalid_binding",
    (
        pytest.param("connection-only", id="connection-only"),
        pytest.param("missing-published-authority", id="missing-published-authority"),
        pytest.param("missing-expected-target", id="missing-expected-target"),
        pytest.param("missing-expected-revision", id="missing-expected-revision"),
        pytest.param("forged-token", id="forged-token"),
        pytest.param("different-target-identity", id="different-target-identity"),
        pytest.param("foreign-grant-lifecycle", id="foreign-grant-lifecycle"),
    ),
)
def test_online_env_rejects_incomplete_or_forged_authority_before_context_or_sql(
    monkeypatch: pytest.MonkeyPatch,
    invalid_binding: str,
) -> None:
    """online env 缺任一 identity-bound authority 事实时必须在 configure 前零写拒绝。

    测试使用 SQLite 仅作为可观察的同步 ``Connection``，不会执行 PostgreSQL lifecycle。
    当前若 env.py 仍从 ScriptDirectory/默认 head 自行补 authority，就会先调用
    ``context.configure`` 并尝试 catalog SQL；这两者都属于本回归要捕获的真实 RED。
    """
    config = Config()
    config.set_main_option("script_location", str(_backend_root() / "migrations"))
    authority = alembic_module.load_published_alembic_authority(config)
    engine = create_engine("sqlite://")
    context_events: list[str] = []
    sql_statements: list[str] = []

    fake_context = ModuleType("alembic.context")
    fake_context.config = config  # type: ignore[attr-defined]
    fake_context.is_offline_mode = lambda: False  # type: ignore[attr-defined]

    def configure(**kwargs: object) -> None:
        del kwargs
        context_events.append("configure")

    class _Transaction:
        def __enter__(self) -> None:
            context_events.append("begin")

        def __exit__(self, *args: object) -> None:
            del args
            context_events.append("end")

    fake_context.configure = configure  # type: ignore[attr-defined]
    fake_context.begin_transaction = _Transaction  # type: ignore[attr-defined]
    fake_context.run_migrations = lambda: context_events.append("run")  # type: ignore[attr-defined]

    monkeypatch.setattr(alembic_package, "context", fake_context)
    monkeypatch.setitem(sys.modules, "alembic.context", fake_context)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    try:
        with engine.connect() as connection:
            token = _issue_synthetic_live_authority(
                monkeypatch,
                connection=connection,
                published=authority,
            )
            complete_shape: dict[str, object] = {
                "connection": connection,
                "published_authority": authority,
                "expected_published_head_revision": authority.head_revision,
                "expected_target_identity_digest": token.expected_target_identity_digest,
                "expected_current_revision": "base",
                "expected_target_revision": authority.head_revision,
                "online_migration_authority": token,
                "migration_grant_lifecycle": token.grant_lifecycle,
            }
            if invalid_binding == "connection-only":
                complete_shape = {"connection": connection}
            elif invalid_binding == "missing-published-authority":
                complete_shape.pop("published_authority")
            elif invalid_binding == "missing-expected-target":
                complete_shape.pop("expected_target_identity_digest")
            elif invalid_binding == "missing-expected-revision":
                complete_shape.pop("expected_current_revision")
            elif invalid_binding == "forged-token":
                complete_shape["online_migration_authority"] = object()
            elif invalid_binding == "different-target-identity":
                complete_shape["online_migration_authority"] = _issue_synthetic_live_authority(
                    monkeypatch,
                    connection=connection,
                    published=authority,
                    system_identifier_ascii=b"2",
                )
            elif invalid_binding == "foreign-grant-lifecycle":
                complete_shape["migration_grant_lifecycle"] = _issue_synthetic_live_authority(
                    monkeypatch,
                    connection=connection,
                    published=authority,
                ).grant_lifecycle
            else:
                raise AssertionError("unknown invalid online migration binding")
            config.attributes.update(complete_shape)

            event.listen(
                connection,
                "before_cursor_execute",
                lambda *_args: sql_statements.append("sql"),
            )
            with pytest.raises(
                alembic_module.AlembicMigrationInvariantError,
                match=r"^alembic migration invariant violation$",
            ):
                runpy.run_path(
                    str(_backend_root() / "migrations" / "env.py"),
                    run_name=f"__alembic_env_{invalid_binding.replace('-', '_')}__",
                )
    finally:
        engine.dispose()

    assert context_events == []
    assert sql_statements == []


def test_online_env_accepts_only_one_complete_identity_bound_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整 token 必须把 Connection、发布 head、target/current revision 与 lifecycle 绑为一体。"""
    config = Config()
    config.set_main_option("script_location", str(_backend_root() / "migrations"))
    published = alembic_module.load_published_alembic_authority(config)
    engine = create_engine("sqlite://")
    context_events: list[str] = []

    fake_context = ModuleType("alembic.context")
    fake_context.config = config  # type: ignore[attr-defined]
    fake_context.is_offline_mode = lambda: False  # type: ignore[attr-defined]

    def configure(**kwargs: object) -> None:
        context_events.append("configure")
        assert kwargs["connection"] is config.attributes["connection"]
        assert (
            kwargs["on_version_apply"]
            == config.attributes["migration_grant_lifecycle"].on_version_apply
        )

    class _Transaction:
        def __enter__(self) -> None:
            context_events.append("begin")

        def __exit__(self, *args: object) -> None:
            del args
            context_events.append("end")

    fake_context.configure = configure  # type: ignore[attr-defined]
    fake_context.begin_transaction = _Transaction  # type: ignore[attr-defined]
    fake_context.run_migrations = lambda: context_events.append("run")  # type: ignore[attr-defined]
    monkeypatch.setattr(alembic_package, "context", fake_context)
    monkeypatch.setitem(sys.modules, "alembic.context", fake_context)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    monkeypatch.setattr(
        alembic_module.MigrationGrantLifecycle,
        "verify_before_migrations",
        lambda self: context_events.append("verify-before"),
    )
    monkeypatch.setattr(
        alembic_module.MigrationGrantLifecycle,
        "verify_after_migrations",
        lambda self: context_events.append("verify-after"),
    )

    try:
        with engine.connect() as connection:
            token = _issue_synthetic_live_authority(
                monkeypatch,
                connection=connection,
                published=published,
            )
            alembic_module.bind_alembic_connection(config, token)
            runpy.run_path(
                str(_backend_root() / "migrations" / "env.py"),
                run_name="__alembic_env_complete_authority__",
            )
    finally:
        engine.dispose()

    assert context_events == [
        "configure",
        "begin",
        "verify-before",
        "run",
        "verify-after",
        "end",
    ]


def test_alembic_env_keeps_preexisting_http_loggers_enabled_and_scrubbed() -> None:
    """嵌入式迁移不得禁用既有 HTTP logger 或绕过其父子 handler 脱敏边界。

    Alembic ``fileConfig`` 默认会禁用配置文件未列出的既有 logger。测试在隔离子进程中
    运行真实 ``migrations/env.py``，避免其 root handler 配置污染 pytest，同时证明迁移
    前已安装的 HTTPCore 父子 handler 在迁移后仍各收到一次固定安全事件。
    """
    script = textwrap.dedent(
        f"""
        import logging
        import runpy
        import sys
        from contextlib import nullcontext
        from types import ModuleType

        import alembic as alembic_package
        from alembic.config import Config

        from ai_employee.infrastructure.observability.logging import configure_http_client_logging


        class RecordingHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.messages = []

            def emit(self, record):
                self.messages.append(record.getMessage())


        config = Config({str(_backend_root() / "alembic.ini")!r})
        fake_context = ModuleType("alembic.context")
        fake_context.config = config
        fake_context.is_offline_mode = lambda: True
        fake_context.configure = lambda **_kwargs: None
        fake_context.begin_transaction = nullcontext
        fake_context.run_migrations = lambda: None
        alembic_package.context = fake_context
        sys.modules["alembic.context"] = fake_context

        parent = logging.getLogger("httpcore")
        child = logging.getLogger("httpcore.http11")
        parent_sink = RecordingHandler()
        child_sink = RecordingHandler()
        parent.addHandler(parent_sink)
        child.addHandler(child_sink)
        child.setLevel(logging.INFO)
        child.disabled = False
        child.propagate = True
        configure_http_client_logging()

        runpy.run_path({str(_backend_root() / "migrations" / "env.py")!r})

        if child.disabled:
            raise SystemExit(17)
        child.warning("Authorization: synthetic-regression-secret")
        if parent_sink.messages != ["http_client_event"]:
            raise SystemExit(18)
        if child_sink.messages != ["http_client_event"]:
            raise SystemExit(19)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_backend_root().parent,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, (
        f"embedded Alembic logging changed the HTTP logging boundary: exit={completed.returncode}"
    )


@dataclass(slots=True)
class _SyntheticMigrationContext:
    """提供 callback 唯一允许读取的同一 Connection 事实。"""

    connection: Connection


@dataclass(slots=True)
class _SyntheticMigrationStep:
    """提供符合生产 Protocol 可写属性的 Alembic 线性升级投影。"""

    is_upgrade: bool
    is_stamp: bool
    source_revision_ids: tuple[str, ...]
    destination_revision_ids: tuple[str, ...]


def test_grant_lifecycle_runs_initial_step_and_final_checks_on_same_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """grant precheck、delta、destination verify 与 final head 必须严格有序且同连接。"""
    authority = alembic_module.PublishedAlembicAuthority(
        revisions=("base", "20260808_0017", "20260809_0018")
    )
    engine = create_engine("sqlite://")
    events: list[tuple[str, Connection, str]] = []
    revisions = iter(("20260808_0017", "20260809_0018"))

    def fake_read_revision(
        connection: Connection,
        *,
        authority: alembic_module.PublishedAlembicAuthority,
    ) -> str:
        observed = next(revisions)
        assert authority.head_revision == "20260809_0018"
        events.append(("read", connection, observed))
        return observed

    def fake_precheck(
        connection: Connection,
        *,
        revision: str,
        destination_revision: str | None,
        phase: GrantPhase,
    ) -> CatalogObjectSnapshot:
        assert phase is GrantPhase.BASELINE
        assert destination_revision == "20260809_0018"
        events.append(("precheck", connection, f"{revision}->{destination_revision}"))
        return CatalogObjectSnapshot(revision=revision, phase=phase, grants=())

    def fake_delta(
        connection: Connection,
        *,
        source_revision: str,
        destination_revision: str,
        phase: GrantPhase,
        before: CatalogObjectSnapshot,
    ) -> None:
        assert phase is GrantPhase.BASELINE
        assert before.revision == source_revision
        events.append(("delta", connection, f"{source_revision}->{destination_revision}"))

    def fake_destination_verify(
        connection: Connection,
        *,
        revision: str,
        phase: GrantPhase,
    ) -> CatalogObjectSnapshot:
        assert phase is GrantPhase.BASELINE
        events.append(("destination", connection, revision))
        return CatalogObjectSnapshot(revision=revision, phase=phase, grants=())

    monkeypatch.setattr(alembic_module, "read_current_alembic_revision", fake_read_revision)
    monkeypatch.setattr(
        alembic_module,
        "verify_pre_migration_object_grants",
        fake_precheck,
    )
    monkeypatch.setattr(alembic_module, "apply_migration_grant_delta", fake_delta)
    monkeypatch.setattr(alembic_module, "verify_object_grants", fake_destination_verify)

    try:
        with engine.connect() as connection:
            lifecycle = alembic_module.MigrationGrantLifecycle(
                connection=connection,
                authority=authority,
                expected_target_revision="20260809_0018",
                phase=GrantPhase.BASELINE,
            )
            lifecycle.verify_before_migrations()
            lifecycle.on_version_apply(
                ctx=_SyntheticMigrationContext(connection),
                step=_SyntheticMigrationStep(
                    is_upgrade=True,
                    is_stamp=False,
                    source_revision_ids=("20260808_0017",),
                    destination_revision_ids=("20260809_0018",),
                ),
                heads={"20260809_0018"},
                run_args={},
            )
            lifecycle.verify_after_migrations()

            assert [event[0] for event in events] == [
                "read",
                "precheck",
                "delta",
                "destination",
                "read",
            ]
            assert all(event[1] is connection for event in events)
    finally:
        engine.dispose()


def test_grant_lifecycle_rejects_real_reverse_step_before_mutation_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 callback 必须在 delta/完整验证前拒绝真实相邻 reverse step。"""
    authority = alembic_module.PublishedAlembicAuthority(
        revisions=("base", "20260808_0017", "20260809_0018")
    )
    engine = create_engine("sqlite://")
    mutation_helpers: list[str] = []

    def fake_read_revision(
        connection: Connection,
        *,
        authority: alembic_module.PublishedAlembicAuthority,
    ) -> str:
        """把 lifecycle source 固定在 0018，构造真实 0018→0017 reverse。"""
        del connection
        assert authority.revisions == ("base", "20260808_0017", "20260809_0018")
        return "20260809_0018"

    def fake_precheck(
        connection: Connection,
        *,
        revision: str,
        destination_revision: str | None,
        phase: GrantPhase,
    ) -> CatalogObjectSnapshot:
        """返回已验证 source snapshot，使 RED 只暴露 callback 方向校验缺口。"""
        del connection
        assert revision == "20260809_0018"
        assert destination_revision == "20260808_0017"
        assert phase is GrantPhase.BASELINE
        return CatalogObjectSnapshot(revision=revision, phase=phase, grants=())

    def record_delta(
        connection: Connection,
        *,
        source_revision: str,
        destination_revision: str,
        phase: GrantPhase,
        before: CatalogObjectSnapshot,
    ) -> None:
        """记录 reverse 是否错误到达首个 grant mutation helper。"""
        del connection, phase, before
        mutation_helpers.append(f"delta:{source_revision}->{destination_revision}")

    def record_destination_verify(
        connection: Connection,
        *,
        revision: str,
        phase: GrantPhase,
    ) -> CatalogObjectSnapshot:
        """记录 reverse 是否继续刷新 destination snapshot/state。"""
        del connection
        mutation_helpers.append(f"verify:{revision}")
        return CatalogObjectSnapshot(revision=revision, phase=phase, grants=())

    monkeypatch.setattr(alembic_module, "read_current_alembic_revision", fake_read_revision)
    monkeypatch.setattr(
        alembic_module,
        "verify_pre_migration_object_grants",
        fake_precheck,
    )
    monkeypatch.setattr(alembic_module, "apply_migration_grant_delta", record_delta)
    monkeypatch.setattr(alembic_module, "verify_object_grants", record_destination_verify)

    caught: BaseException | None = None
    try:
        with engine.connect() as connection:
            lifecycle = alembic_module.MigrationGrantLifecycle(
                connection=connection,
                authority=authority,
                expected_current_revision="20260809_0018",
                expected_target_revision="20260808_0017",
                phase=GrantPhase.BASELINE,
            )
            lifecycle.verify_before_migrations()
            try:
                lifecycle.on_version_apply(
                    ctx=_SyntheticMigrationContext(connection),
                    step=_SyntheticMigrationStep(
                        is_upgrade=False,
                        is_stamp=False,
                        source_revision_ids=("20260809_0018",),
                        destination_revision_ids=("20260808_0017",),
                    ),
                    heads={"20260808_0017"},
                    run_args={},
                )
            except BaseException as error:  # noqa: BLE001 - RED 同时记录精确异常与越界调用。
                caught = error
    finally:
        engine.dispose()

    assert mutation_helpers == []
    assert isinstance(caught, alembic_module.AlembicMigrationInvariantError)


def test_maintenance_rejects_unknown_revision_before_runner_or_mutation() -> None:
    """格式合法但未发布的 revision 必须在 runner 或 mutation 前按脚本链拒绝。"""
    from ai_employee.infrastructure.db.alembic import OnlineMigrationAuthority
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        SqlAlchemyDatabaseMaintenanceContext,
        _DatabaseTargetCatalog,
        _SqlAlchemyTargetMaintenanceLease,
        derive_database_target_identity,
    )

    authority = alembic_module.load_published_alembic_authority(
        Config(_backend_root() / "alembic.ini")
    )
    unpublished_revision = "20991231_9999_synthetic_unpublished"
    assert not authority.contains(unpublished_revision)
    runner_authorities: list[OnlineMigrationAuthority] = []

    def record_runner(authority: OnlineMigrationAuthority) -> None:
        """记录 typed runner 是否越过未知 revision 的前置拒绝。"""
        runner_authorities.append(authority)

    context = SqlAlchemyDatabaseMaintenanceContext(
        management_engine=_SAFE_ENGINE_STUB,
        target_engine=_SAFE_ENGINE_STUB,
        target_database_name="synthetic_target",
        bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
        app_password=None,
        retention_password=None,
        migration_runner=record_runner,
        published_authority=authority,
    )
    target = _DatabaseTargetCatalog(
        identity=derive_database_target_identity(
            system_identifier_ascii=b"1",
            database_name_utf8=b"synthetic_target",
            target_name_verified=True,
        ),
        database_oid=401,
        owner_oid=101,
        owner_role_name="synthetic_owner",
        database_name="synthetic_target",
    )

    engine = create_engine("sqlite://", hide_parameters=True)
    try:
        with (
            engine.connect() as connection,
            pytest.raises(
                DatabaseMaintenanceInvariantError,
                match=r"^database maintenance invariant violation$",
            ),
        ):
            _SqlAlchemyTargetMaintenanceLease(
                context=context,
                connection=connection,
                target=target,
                revision=unpublished_revision,
            )
    finally:
        engine.dispose()

    assert runner_authorities == []


def test_historical_migration_tests_use_shared_external_connection_helper() -> None:
    """历史 upgrade/downgrade/stamp/check 必须持真实三锁 lease，不得裸连接发 token。"""
    integration_root = _backend_root() / "tests" / "integration"
    helper = integration_root / "alembic_commands.py"
    helper_source = helper.read_text(encoding="utf-8")
    assert "bind_alembic_connection(" in helper_source
    assert "SqlAlchemyDatabaseMaintenanceContext(" in helper_source
    assert "acquire_management_lifecycle_lock()" in helper_source
    assert "acquire_target_lock()" in helper_source
    assert "assert_migration_allowed()" in helper_source
    assert "acquire_schema_lifecycle_lock()" in helper_source
    assert "._issue_online_migration_authority(" in helper_source
    assert "._verify_online_migration_result(" in helper_source
    assert "from ai_employee.infrastructure.db.alembic import (\n    _issue" not in helper_source
    assert "command.upgrade(" in helper_source
    assert "command.downgrade(" in helper_source
    assert "command.stamp(" in helper_source
    assert "runner=command.check" in helper_source

    for relative_path in (
        Path("db/test_migrations.py"),
        Path("m2/test_connection_source_schema.py"),
        Path("m2/test_trusted_action_schema.py"),
    ):
        source = (integration_root / relative_path).read_text(encoding="utf-8")
        assert "command.upgrade(" not in source
        assert "command.downgrade(" not in source
        assert "command.stamp(" not in source
        assert "command.check(" not in source


def test_empty_migration_database_bootstraps_exact_base_posture_before_alembic() -> None:
    """首次安装测试库必须先走 typed role-bootstrap，不能让 env.py 修复 Candidate。"""
    conftest_source = (_backend_root() / "tests" / "integration" / "conftest.py").read_text(
        encoding="utf-8"
    )
    bootstrap_start = conftest_source.index("def _bootstrap_empty_migration_database(")
    bootstrap_end = conftest_source.index("@pytest.fixture", bootstrap_start)
    bootstrap_source = conftest_source[bootstrap_start:bootstrap_end]
    assert "role_bootstrap_database(" in bootstrap_source
    assert "SqlAlchemyDatabaseMaintenanceContext(" in bootstrap_source
    assert "published_authority=" in bootstrap_source

    fixture_start = conftest_source.index("def empty_migration_database(")
    fixture_end = conftest_source.index("async def _truncate_application_tables(")
    fixture_source = conftest_source[fixture_start:fixture_end]
    assert "managed_disposable_database_cleanup(" in fixture_source
    assert "cleanup.assert_initial_absence()" in fixture_source
    assert "cleanup.create_database()" in fixture_source
    bootstrap_call = fixture_source.index("_bootstrap_empty_migration_database(")
    yield_position = fixture_source.index("yield temporary_url")
    assert bootstrap_call < yield_position

    # role-bootstrap 可能在 candidate transaction 提交后才由 post-commit verifier 报错；
    # fixture 必须在同一 cleanup lease 上读回 safe roles，不能只在正常返回后晚到记录。
    assert "except BaseException as original_error" in bootstrap_source
    assert "cleanup.record_created_runtime_roles_if_safe()" in bootstrap_source
    assert "except DisposableDatabaseCleanupError as cleanup_error" in bootstrap_source
    assert "raise cleanup_error from original_error" in bootstrap_source
    assert "cleanup:" in bootstrap_source
    assert "management_engine:" in bootstrap_source
    assert "target_engine.dispose()" in bootstrap_source
    assert "management_engine.dispose()" not in bootstrap_source
