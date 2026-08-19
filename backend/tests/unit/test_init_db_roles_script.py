"""约束数据库角色初始化脚本只能充当 Secret-safe typed lifecycle wrapper。"""

import stat
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_deployment_gate_checks_thin_wrapper_secret_forwarding_not_role_inventory() -> None:
    """部署门禁应验证 typed wrapper 的 Secret 转发，而不是要求脚本复制角色名。"""
    deployment_script = (REPOSITORY_ROOT / "scripts" / "test-deployment.sh").read_text(
        encoding="utf-8"
    )

    assert (
        "grep -Fq -- '--retention-password-file \"$RETENTION_DATABASE_PASSWORD_FILE\"' "
        "scripts/init-db-roles.sh"
    ) in deployment_script
    assert "grep -q 'ai_employee_retention' scripts/init-db-roles.sh" not in deployment_script


def test_role_bootstrap_script_is_thin_secret_file_safe_lifecycle_wrapper() -> None:
    """脚本不得拥有 SQL/inventory，只能把三个 Secret 文件路径交给 typed CLI。"""
    script = (REPOSITORY_ROOT / "scripts" / "init-db-roles.sh").read_text(encoding="utf-8")
    uppercase_script = script.upper()

    assert "python -m ai_employee.cli.database_maintenance" in script
    assert "role-bootstrap" in script
    assert '"$POSTGRES_BOOTSTRAP_PASSWORD_FILE"' in script
    assert '"$APP_DATABASE_PASSWORD_FILE"' in script
    assert '"$RETENTION_DATABASE_PASSWORD_FILE"' in script
    assert '"$DATABASE_URL"' not in script
    assert "read -r app_password" not in script
    assert "read -r retention_password" not in script
    assert "base64" not in script
    assert "psql" not in script
    assert "PGPASSWORD" not in script
    assert "cat " not in script
    for forbidden_sql in (
        "CREATE ROLE",
        "ALTER ROLE",
        "GRANT ",
        "REVOKE ",
        "ALL TABLES",
        "ALL SEQUENCES",
        "DO $$",
        "TO_REGCLASS",
    ):
        assert forbidden_sql not in uppercase_script

    mode = (REPOSITORY_ROOT / "scripts" / "init-db-roles.sh").stat().st_mode
    assert stat.S_IMODE(mode) & 0o111 == 0o111


def test_role_bootstrap_script_requires_only_secret_safe_inputs() -> None:
    """wrapper 只能要求目标名和 Secret 路径，不能要求明文密码或 waiver token。"""
    script = (REPOSITORY_ROOT / "scripts" / "init-db-roles.sh").read_text(encoding="utf-8")

    for required_name in (
        "DATABASE_URL",
        "POSTGRES_BOOTSTRAP_PASSWORD_FILE",
        "APP_DATABASE_PASSWORD_FILE",
        "RETENTION_DATABASE_PASSWORD_FILE",
    ):
        assert f"${{{required_name}:?" in script
    for forbidden_name in (
        "POSTGRES_PASSWORD_FILE",
        "POSTGRES_PASSWORD",
        "APP_DATABASE_PASSWORD",
        "RETENTION_DATABASE_PASSWORD",
    ):
        assert f"${{{forbidden_name}:?" not in script
        assert f'"${forbidden_name}"' not in script
    for forbidden_token in ("SKIP", "WAIVE", "BYPASS"):
        assert forbidden_token not in script


def test_retention_integration_uses_disposable_typed_database_lifecycle() -> None:
    """真实角色测试必须隔离到 UUID 数据库并经 typed bootstrap→migrate 后精确清理。"""
    source = (
        REPOSITORY_ROOT
        / "backend"
        / "tests"
        / "integration"
        / "retention"
        / "test_role_permissions.py"
    ).read_text(encoding="utf-8")
    assert '@pytest.fixture(scope="module", autouse=True)' in source
    assert "def migrated_database() -> Iterator[None]:" in source
    assert "async def isolated_database() -> AsyncIterator[None]:" in source
    fixture_start = source.index("def disposable_role_database(")
    fixture_end = source.index("@pytest.mark.asyncio", fixture_start)
    fixture_source = source[fixture_start:fixture_end]
    cleanup_source = (
        REPOSITORY_ROOT / "backend" / "tests" / "integration" / "disposable_database.py"
    ).read_text(encoding="utf-8")

    assert "validate_test_database_url(database_url)" in fixture_source
    assert 'f"ai_employee_retention_{uuid4().hex}_test"' in fixture_source
    assert "managed_disposable_database_cleanup(" in fixture_source
    assert "cleanup.assert_initial_absence()" in fixture_source
    assert "cleanup.create_database()" in fixture_source
    assert "cleanup.record_created_runtime_roles_if_safe()" in fixture_source
    assert 'drivername="postgresql+psycopg"' in fixture_source
    bootstrap_index = fixture_source.index("role_bootstrap_database(context)")
    record_roles_index = fixture_source.index("cleanup.record_created_runtime_roles_if_safe()")
    upgrade_0018_index = fixture_source.index(
        'run_alembic_upgrade(config, "20260809_0018")'
    )
    assert bootstrap_index < record_roles_index < upgrade_0018_index
    assert "except BaseException as original_error" in fixture_source
    assert "except DisposableDatabaseCleanupError as cleanup_error" in fixture_source
    assert "raise cleanup_error from original_error" in fixture_source
    # retention source-inventory 测试必须停在 0018；产品 ``migrate_database`` 固定升级到
    # 当前 head 0019，若在此复用就会提前获得本测试刻意要求不存在的 destination grants。
    assert "migrate_database(context)" not in fixture_source
    assert 'run_alembic_upgrade(config, "20260809_0018")' in fixture_source
    assert "run_alembic_upgrade_on_connection" in fixture_source
    assert "verify_object_grants(" in fixture_source
    assert 'revision != "20260809_0018"' in fixture_source
    assert "owner_url=owner_async_url.render_as_string" in fixture_source
    assert "app_url=app_url.render_as_string" in fixture_source
    assert "retention_url=retention_url.render_as_string" in fixture_source
    assert "target_engine.dispose()" in fixture_source

    assert "DISPOSABLE_DATABASE_CLEANUP_LOCK" in cleanup_source
    assert "system_identifier" in cleanup_source
    assert 'text(f"DROP DATABASE {self._quoted_database_name}")' in cleanup_source
    for dependency_name in (
        "_ROLE_SESSION_COUNT_SQL",
        "_ROLE_MEMBERSHIP_COUNT_SQL",
        "_ROLE_SETTING_COUNT_SQL",
        "_ROLE_DATABASE_OWNERSHIP_COUNT_SQL",
        "_ROLE_DATABASE_ACL_COUNT_SQL",
        "_ROLE_SHARED_DEPENDENCY_COUNT_SQL",
    ):
        assert dependency_name in cleanup_source
    assert "self._verify_roles()" in cleanup_source
    assert 'self._connection.execute(text(f"DROP ROLE {quoted_role}"))' in cleanup_source
    assert "DROP DATABASE {self._quoted_database_name} CASCADE" not in cleanup_source
    assert "DROP ROLE {quoted_role} CASCADE" not in cleanup_source
    assert "if _read_database_rows(" in cleanup_source
    assert "if _read_role_rows(self._connection):" in cleanup_source

    for forbidden_path in (
        "initialized_database_roles",
        "subprocess.run",
        "PGPASSWORD",
        "_LIBPQ_ENDPOINT_OVERRIDE_NAMES",
        'shutil.which("psql")',
        "command.upgrade",
    ):
        assert forbidden_path not in source


def test_disposable_bootstrap_records_roles_when_bootstrap_raises() -> None:
    """bootstrap 异常也可能已提交角色；两个 disposable fixture 必须先冻结安全 provenance。"""
    catalog_source = (
        REPOSITORY_ROOT
        / "backend"
        / "tests"
        / "integration"
        / "db"
        / "test_database_grants_catalog.py"
    ).read_text(encoding="utf-8")
    retention_source = (
        REPOSITORY_ROOT
        / "backend"
        / "tests"
        / "integration"
        / "retention"
        / "test_role_permissions.py"
    ).read_text(encoding="utf-8")

    for source in (catalog_source, retention_source):
        fixture_start = source.index("role_bootstrap_database(context)")
        fixture_end = source.index("run_alembic_upgrade(config", fixture_start)
        bootstrap_block = source[fixture_start:fixture_end]
        assert "try:" in bootstrap_block
        assert "except BaseException as original_error" in bootstrap_block
        assert "cleanup.record_created_runtime_roles_if_safe()" in bootstrap_block
        assert "except DisposableDatabaseCleanupError as cleanup_error" in bootstrap_block
        assert "raise cleanup_error from original_error" in bootstrap_block
        assert "raise original_error" not in bootstrap_block
