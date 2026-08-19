"""冻结 typed 数据库生命周期 CLI 的参数、Secret 与同连接迁移边界。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine


class _SafeEngineStub:
    """为不打开 lifecycle session 的单元测试声明参数脱敏能力。"""

    hide_parameters = True


_SAFE_ENGINE_STUB = cast(Engine, _SafeEngineStub())


def _write_secret(path: Path, value: str = "synthetic-secret") -> Path:
    """创建只供当前测试使用的普通 UTF-8 Secret 文件。"""
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_cli_exposes_only_three_typed_subcommands_and_safe_argument_allowlists(
    tmp_path: Path,
) -> None:
    """公开 parser 不得出现 repair/stamp/downgrade、明文密码或 reset waiver。"""
    from ai_employee.cli.database_maintenance import (
        DbResetArguments,
        MigrateArguments,
        RoleBootstrapArguments,
        _parse_arguments,
    )

    owner = _write_secret(tmp_path / "owner")
    app = _write_secret(tmp_path / "app")
    retention = _write_secret(tmp_path / "retention")

    role_bootstrap = _parse_arguments(
        [
            "role-bootstrap",
            "--owner-password-file",
            str(owner),
            "--app-password-file",
            str(app),
            "--retention-password-file",
            str(retention),
        ]
    )
    assert isinstance(role_bootstrap, RoleBootstrapArguments)

    migrate = _parse_arguments(["migrate", "--owner-password-file", str(owner)])
    assert isinstance(migrate, MigrateArguments)

    reset = _parse_arguments(
        [
            "db-reset",
            "--owner-password-file",
            str(owner),
            "--app-password-file",
            str(app),
            "--retention-password-file",
            str(retention),
            "--app-env",
            "test",
            "--confirmed-database-name",
            "synthetic_test",
        ]
    )
    assert isinstance(reset, DbResetArguments)

    forbidden_argv = (
        ["repair"],
        ["stamp"],
        ["downgrade"],
        ["role-bootstrap", "--owner-password", "plaintext"],
        ["migrate", "--revision", "20260809_0018"],
        ["db-reset", "--skip-confirmation"],
        ["db-reset", "--waiver-file", str(tmp_path / "waiver")],
        ["db-reset", "--allow-production"],
    )
    for arguments in forbidden_argv:
        with pytest.raises(SystemExit):
            _parse_arguments(arguments)


@pytest.mark.parametrize("content", ["", "\n", " \t\r\n", "has\0nul"])
def test_secret_file_reader_rejects_empty_whitespace_or_nul(
    tmp_path: Path,
    content: str,
) -> None:
    """Secret 内容闭集拒绝空白与 NUL，异常不得回显原始内容。"""
    from ai_employee.cli.database_maintenance import SecretFileError, _read_secret_file

    path = _write_secret(tmp_path / "secret", content)
    with pytest.raises(SecretFileError, match=r"^database Secret file is invalid$"):
        _read_secret_file(path)


def test_secret_file_reader_rejects_non_file_symlink_and_unsafe_permissions(
    tmp_path: Path,
) -> None:
    """Secret 路径必须是不可执行、不可被组/其他用户写入的普通非 symlink 文件。"""
    from ai_employee.cli.database_maintenance import SecretFileError, _read_secret_file

    directory = tmp_path / "directory"
    directory.mkdir()
    target = _write_secret(tmp_path / "target")
    symlink = tmp_path / "link"
    symlink.symlink_to(target)
    unsafe_mode = _write_secret(tmp_path / "unsafe-mode")
    unsafe_mode.chmod(0o622)
    unreadable_mode = _write_secret(tmp_path / "unreadable-mode")
    unreadable_mode.chmod(0o200)

    for path in (directory, symlink, unsafe_mode, unreadable_mode):
        with pytest.raises(SecretFileError, match=r"^database Secret file is invalid$"):
            _read_secret_file(path)


def test_secret_value_repr_and_cli_failure_output_never_include_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secret 的 repr 与 CLI 静态错误消息都不得包含文件内容。"""
    import ai_employee.cli.database_maintenance as cli_module

    marker = "synthetic-secret-marker"
    owner = _write_secret(tmp_path / "owner", marker)
    secret = cli_module._read_secret_file(owner)
    assert marker not in repr(secret)

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://synthetic_owner@localhost:5432/synthetic_test",
    )

    @contextmanager
    def fail_open(*args: object, **kwargs: object) -> Iterator[object]:
        del args, kwargs
        raise RuntimeError(marker)
        yield object()  # pragma: no cover - 仅满足 generator contextmanager 类型。

    monkeypatch.setattr(cli_module, "_open_maintenance_context", fail_open)
    assert cli_module.main(["migrate", "--owner-password-file", str(owner)]) == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert marker not in combined


def test_cli_dispatches_typed_lifecycle_and_reset_policy_without_candidate_migrate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三个子命令只调用共享 typed orchestration；db-reset 必须携带 exact policy。"""
    import ai_employee.cli.database_maintenance as cli_module
    from ai_employee.infrastructure.db.database_maintenance import ProtectedResetPolicy

    owner = _write_secret(tmp_path / "owner")
    app = _write_secret(tmp_path / "app")
    retention = _write_secret(tmp_path / "retention")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://synthetic_owner@localhost:5432/synthetic_test",
    )

    contexts: list[tuple[object, object]] = []
    lifecycle_calls: list[tuple[str, object]] = []

    @contextmanager
    def fake_open(arguments: object, endpoint: object) -> Iterator[object]:
        context = object()
        contexts.append((arguments, endpoint))
        yield context

    monkeypatch.setattr(cli_module, "_open_maintenance_context", fake_open)
    monkeypatch.setattr(
        cli_module,
        "role_bootstrap_database",
        lambda context: lifecycle_calls.append(("role-bootstrap", context)),
    )
    monkeypatch.setattr(
        cli_module,
        "migrate_database",
        lambda context: lifecycle_calls.append(("migrate", context)),
    )
    monkeypatch.setattr(
        cli_module,
        "reset_then_migrate",
        lambda context: lifecycle_calls.append(("db-reset", context)),
    )

    assert (
        cli_module.main(
            [
                "role-bootstrap",
                "--owner-password-file",
                str(owner),
                "--app-password-file",
                str(app),
                "--retention-password-file",
                str(retention),
            ]
        )
        == 0
    )
    assert cli_module.main(["migrate", "--owner-password-file", str(owner)]) == 0
    assert (
        cli_module.main(
            [
                "db-reset",
                "--owner-password-file",
                str(owner),
                "--app-password-file",
                str(app),
                "--retention-password-file",
                str(retention),
                "--app-env",
                "test",
                "--confirmed-database-name",
                "synthetic_test",
            ]
        )
        == 0
    )

    assert [name for name, _ in lifecycle_calls] == ["role-bootstrap", "migrate", "db-reset"]
    reset_arguments = cast(cli_module.DbResetArguments, contexts[2][0])
    assert reset_arguments.reset_policy == ProtectedResetPolicy(
        app_env="test",
        confirmed_database_name="synthetic_test",
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "",
        "postgresql+asyncpg://synthetic_owner:plaintext@localhost:5432/synthetic_test",
        "postgresql+asyncpg://synthetic_owner@localhost:5432/synthetic_test?host=elsewhere",
        "postgresql+asyncpg://synthetic_owner@localhost:5432/synthetic_test#fragment",
        "mysql://synthetic_owner@localhost:3306/synthetic_test",
    ],
)
def test_database_endpoint_reuses_passwordless_database_url_and_rejects_overrides(
    database_url: str,
) -> None:
    """唯一 URL authority 必须 passwordless、无 query/fragment，拒绝第二套端点覆盖。"""
    from ai_employee.cli.database_maintenance import (
        DatabaseEndpointError,
        _load_database_endpoint,
    )

    with pytest.raises(DatabaseEndpointError, match=r"^database endpoint is invalid$"):
        _load_database_endpoint({"DATABASE_URL": database_url})


@pytest.mark.parametrize(
    "database_name",
    [
        '报价"库_test',
        "Café_test",
        "Cafe\N{COMBINING ACUTE ACCENT}_test",
    ],
)
def test_database_endpoint_preserves_exact_valid_utf8_database_name(
    database_name: str,
) -> None:
    """数据库名允许 quoted UTF-8，并且不得 normalization、case-fold 或截断。"""
    from pydantic import SecretStr

    from ai_employee.cli.database_maintenance import _load_database_endpoint

    endpoint = _load_database_endpoint(
        {"DATABASE_URL": ("postgresql+asyncpg://synthetic_owner@localhost:5432/" + database_name)}
    )

    assert endpoint.database_name == database_name
    assert (
        endpoint.owner_url(SecretStr("synthetic-secret"), database_name=database_name).database
        == database_name
    )


@pytest.mark.parametrize(
    ("database_path", "expected_database_name"),
    [
        (
            "%E6%8A%A5%E4%BB%B7%22%E5%BA%93_test",
            '报价"库_test',
        ),
        (
            "Cafe%CC%81%2Fquote%3Fprice%23_test",
            "Cafe\N{COMBINING ACUTE ACCENT}/quote?price#_test",
        ),
        ("literal%25percent_test", "literal%percent_test"),
        ("literal%252F_test", "literal%2F_test"),
    ],
)
def test_database_endpoint_decodes_canonical_url_path_to_exact_database_name(
    database_path: str,
    expected_database_name: str,
) -> None:
    """URL 编码只是传输形式，typed CLI 必须恢复同一个 exact quoted 数据库名。"""
    from ai_employee.cli.database_maintenance import _load_database_endpoint

    endpoint = _load_database_endpoint(
        {"DATABASE_URL": ("postgresql+psycopg://synthetic_owner@localhost:5432/" + database_path)}
    )

    assert endpoint.database_name == expected_database_name


def test_database_endpoint_decodes_valid_owner_percent_triplet_once() -> None:
    """合法 owner 编码只解码一次，并继续受 simple identifier 约束。"""
    from ai_employee.cli.database_maintenance import _load_database_endpoint

    endpoint = _load_database_endpoint(
        {"DATABASE_URL": ("postgresql+psycopg://synthetic%5Fowner@localhost:5432/synthetic_test")}
    )

    assert endpoint.owner_role == "synthetic_owner"


@pytest.mark.parametrize(
    ("component", "malformed_value"),
    [
        ("username", "synthetic_owner%"),
        ("username", "synthetic%4owner"),
        ("username", "synthetic_owner%gG"),
        ("database", "synthetic_test%"),
        ("database", "synthetic%4_test"),
        ("database", "synthetic_%GG_test"),
    ],
)
def test_database_endpoint_rejects_malformed_percent_triplet_before_endpoint_creation(
    component: str,
    malformed_value: str,
) -> None:
    """username/database 的每个 percent 必须组成完整两位 ASCII hex triplet。"""
    from ai_employee.cli.database_maintenance import (
        DatabaseEndpointError,
        _load_database_endpoint,
    )

    username = malformed_value if component == "username" else "synthetic_owner"
    database_path = malformed_value if component == "database" else "synthetic_test"

    with pytest.raises(DatabaseEndpointError, match=r"^database endpoint is invalid$"):
        _load_database_endpoint(
            {"DATABASE_URL": (f"postgresql+psycopg://{username}@localhost:5432/{database_path}")}
        )


@pytest.mark.parametrize(
    "database_path",
    [
        "%00",
        "%FF",
        "%C3%28",
        "%C3%A9" * 32,
    ],
)
def test_database_endpoint_rejects_invalid_decoded_url_database_name(
    database_path: str,
) -> None:
    """解码后的 NUL、非法 UTF-8 与超过 63 字节名称必须在连接前拒绝。"""
    from ai_employee.cli.database_maintenance import (
        DatabaseEndpointError,
        _load_database_endpoint,
    )

    with pytest.raises(DatabaseEndpointError, match=r"^database endpoint is invalid$"):
        _load_database_endpoint(
            {
                "DATABASE_URL": (
                    "postgresql+psycopg://synthetic_owner@localhost:5432/" + database_path
                )
            }
        )


@pytest.mark.parametrize(
    "database_name",
    [
        "",
        "has\0nul",
        "a" * 64,
        "\udcff",
    ],
)
def test_database_endpoint_rejects_invalid_utf8_database_name(
    database_name: str,
) -> None:
    """数据库名必须是非空、无 NUL 且不超过 63 字节的严格 UTF-8。"""
    from ai_employee.cli.database_maintenance import DatabaseEndpoint, DatabaseEndpointError

    with pytest.raises(DatabaseEndpointError, match=r"^database endpoint is invalid$"):
        DatabaseEndpoint(
            host="localhost",
            port=5432,
            owner_role="synthetic_owner",
            database_name=database_name,
        )


def test_typed_alembic_runner_binds_the_same_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner 只能绑定 lease-issued token 的同一 Connection，不得补缺省 authority。"""
    import ai_employee.infrastructure.db.alembic as alembic_module
    from ai_employee.infrastructure.db.alembic import (
        load_published_alembic_authority,
        run_alembic_upgrade_on_connection,
    )
    from ai_employee.infrastructure.db.database_access import BootstrapCaller
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseAdmissionState,
        SqlAlchemyDatabaseMaintenanceContext,
        _DatabaseTargetCatalog,
        _SqlAlchemyTargetMaintenanceLease,
        derive_database_target_identity,
    )

    backend_root = Path(__file__).resolve().parents[3]
    engine = create_engine("sqlite://", hide_parameters=True)
    observed: list[tuple[object, str]] = []

    def fake_upgrade(config: object, revision: str) -> None:
        observed.append((config, revision))

    monkeypatch.setattr(alembic_module.command, "upgrade", fake_upgrade)
    try:
        with engine.connect() as connection:
            published = load_published_alembic_authority(Config(backend_root / "alembic.ini"))
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
                    system_identifier_ascii=b"1",
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
                revision="base",
            )
            lease._schema_lock_held = True
            lease._migration_admitted_revision = "base"
            monkeypatch.setattr(
                lease,
                "_read_stable_admission",
                lambda *, require_revision: (
                    (
                        "base",
                        DatabaseAdmissionState.PRISTINE_IDLE,
                    )
                    if require_revision == "base"
                    else pytest.fail("synthetic lease received a foreign source revision")
                ),
            )
            token = lease._issue_online_migration_authority(
                expected_target_revision=published.head_revision,
            )
            run_alembic_upgrade_on_connection(
                token,
                config_path=backend_root / "alembic.ini",
            )

            config, revision = observed[0]
            assert revision == "head"
            assert config.attributes["connection"] is connection
            assert config.attributes["online_migration_authority"] is token
            assert config.attributes["migration_grant_lifecycle"] is token.grant_lifecycle
            assert (
                config.attributes["expected_target_identity_digest"] == target.identity.digest_hex
            )
            assert config.attributes["expected_current_revision"] == "base"
            assert config.attributes["expected_target_revision"] == published.head_revision
            assert config.attributes["published_authority"] is published
            assert "repair" not in config.attributes
            assert "stamp" not in config.attributes
    finally:
        engine.dispose()


def test_alembic_environment_requires_injected_connection_without_second_engine() -> None:
    """env.py online 必须强制完整 token，且在同一 transaction 执行 grant lifecycle。"""
    backend_root = Path(__file__).resolve().parents[3]
    source = (backend_root / "migrations" / "env.py").read_text(encoding="utf-8")

    assert "require_online_migration_authority(config)" in source
    assert "async_engine_from_config" not in source
    assert "run_async_migrations" not in source
    assert "asyncio.run" not in source

    transaction = source.index("with context.begin_transaction():")
    precheck = source.index("lifecycle.verify_before_migrations()", transaction)
    migrations = source.index("context.run_migrations()", precheck)
    final = source.index("lifecycle.verify_after_migrations()", migrations)
    assert transaction < precheck < migrations < final


def test_engine_lifecycle_uses_psycopg_nullpool_and_disposes_both_engines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 构造显式 owner URL，不渲染密码，并在 success/failure 都释放两个 Engine。"""
    import ai_employee.cli.database_maintenance as cli_module

    owner = _write_secret(tmp_path / "owner", "owner-secret-marker")
    arguments = cli_module._parse_arguments(["migrate", "--owner-password-file", str(owner)])
    endpoint = cli_module.DatabaseEndpoint(
        host="localhost",
        port=5432,
        owner_role="synthetic_owner",
        database_name="synthetic_test",
    )

    class FakeEngine:
        def __init__(self, database: str) -> None:
            self.database = database
            self.disposed = False
            self.hide_parameters = True

        def dispose(self) -> None:
            self.disposed = True

    engines: list[FakeEngine] = []

    def fake_create_engine(url: object, **options: object) -> FakeEngine:
        assert options["poolclass"] is cli_module.NullPool
        assert options["hide_parameters"] is True
        assert url.drivername == "postgresql+psycopg"
        assert "owner-secret-marker" not in repr(url)
        engine = FakeEngine(cast(str, url.database))
        engines.append(engine)
        return engine

    monkeypatch.setattr(cli_module, "create_engine", fake_create_engine)
    with cli_module._open_maintenance_context(arguments, endpoint):
        pass

    assert [engine.database for engine in engines] == ["postgres", "synthetic_test"]
    assert all(engine.disposed for engine in engines)


def test_maintenance_engine_failure_hides_bound_secret_from_errors_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 Engine 配置必须同时脱敏 SQLAlchemy 异常、repr 与 engine 参数日志。"""
    import logging
    from uuid import uuid4

    from sqlalchemy import create_engine as create_sqlalchemy_engine
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    import ai_employee.cli.database_maintenance as cli_module

    owner = _write_secret(tmp_path / "owner", "synthetic-owner-secret")
    arguments = cli_module._parse_arguments(["migrate", "--owner-password-file", str(owner)])
    endpoint = cli_module.DatabaseEndpoint(
        host="localhost",
        port=5432,
        owner_role="synthetic_owner",
        database_name="synthetic_test",
    )

    def sqlite_create_engine(_url: object, **options: object) -> object:
        """复用生产 options 创建本地 Engine，不连接真实 PostgreSQL。"""
        options.pop("poolclass")
        return create_sqlalchemy_engine("sqlite://", **options)

    monkeypatch.setattr(cli_module, "create_engine", sqlite_create_engine)
    marker = f"synthetic-bound-{uuid4().hex}"

    class _MessageHandler(logging.Handler):
        """只在内存保存 message，并阻止 pytest root handler 回显 synthetic 参数。"""

        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    exception_leaked = False
    exception_hidden = False
    engine_logger = logging.getLogger("sqlalchemy.engine.Engine")
    previous_level = engine_logger.level
    previous_propagate = engine_logger.propagate
    handler = _MessageHandler()
    engine_logger.addHandler(handler)
    engine_logger.setLevel(logging.INFO)
    engine_logger.propagate = False
    try:
        with (
            cli_module._open_maintenance_context(arguments, endpoint) as context,
            context._target_engine.connect() as connection,
        ):
            try:
                connection.execute(
                    text("SELECT missing_sql_function(:secret_value)"),
                    {"secret_value": marker},
                )
            except SQLAlchemyError as error:
                exception_text = str(error)
                exception_repr = repr(error)
                exception_leaked = marker in exception_text or marker in exception_repr
                exception_hidden = "SQL parameters hidden" in exception_text
            else:  # pragma: no cover - SQLite 必须拒绝不存在的函数。
                raise AssertionError("synthetic SQL failure was not raised")
    finally:
        engine_logger.removeHandler(handler)
        engine_logger.setLevel(previous_level)
        engine_logger.propagate = previous_propagate
    logged_messages = tuple(handler.messages)
    log_leaked = any(marker in message for message in logged_messages)
    log_hidden = any("SQL parameters hidden" in message for message in logged_messages)
    # 先清空捕获内容再断言，确保即使 RED 也不会把 marker 交给 pytest failure report。
    handler.messages.clear()

    assert not exception_leaked, "bound Secret leaked through SQLAlchemy exception"
    assert exception_hidden, "SQLAlchemy exception did not report hidden parameters"
    assert not log_leaked, "bound Secret leaked through SQLAlchemy engine log"
    assert log_hidden, "SQLAlchemy engine log did not report hidden parameters"
