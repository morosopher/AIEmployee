"""为真实 PostgreSQL 集成测试提供迁移与确定性数据隔离。"""

import os
from collections.abc import AsyncIterator, Iterator
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from ai_employee.infrastructure.db.alembic import (
    load_published_alembic_authority,
    run_alembic_upgrade_on_connection,
)
from ai_employee.infrastructure.db.database_access import (
    BootstrapCaller,
    DatabaseAclProfile,
    plan_baseline_connect_reassertion,
    read_database_access_snapshot_sync,
)
from ai_employee.infrastructure.db.database_grants import GrantPhase, verify_object_grants
from ai_employee.infrastructure.db.database_maintenance import (
    SqlAlchemyDatabaseMaintenanceContext,
    migrate_database,
    role_bootstrap_database,
)
from ai_employee.infrastructure.db.database_url import (
    InvalidTestDatabaseUrl,
    TestDatabaseUrl,
    validate_test_database_url,
)
from ai_employee.infrastructure.db.session import build_engine
from tests.integration.disposable_database import (
    DisposableDatabaseCleanupError,
    DisposableDatabaseCleanupLease,
    managed_disposable_database_cleanup,
)
from tests.integration.integration_suite import (
    SqlAlchemyIntegrationSuiteResources,
    managed_cycle5_regular_database,
    managed_session_factory_registry,
)

# 清理范围只允许使用经过代码审查的应用表，顺序先子后父，且绝不包含 alembic_version。
APPLICATION_TABLES: tuple[str, ...] = (
    "mail_draft_versions",
    "calendar_change_snapshots",
    "mail_drafts",
    "calendar_change_proposals",
    "llm_invocations",
    "messages",
    "daily_brief_items",
    "daily_briefs",
    "conversations",
    "email_analyses",
    "email_messages",
    "calendar_events",
    "email_threads",
    "sync_cursors",
    "encrypted_credentials",
    "connection_capabilities",
    "provider_calendars",
    "oauth_connections",
    "oauth_attempts",
    "tool_executions",
    "approval_requests",
    "task_steps",
    "audit_events",
    "outbox_events",
    "task_runs",
    "user_sessions",
    "users",
)


@pytest.fixture(scope="session")
def database_url() -> TestDatabaseUrl:
    """读取并校验隔离的 PostgreSQL 集成测试数据库 URL。

    只接受显式 ``TEST_DATABASE_URL``，避免意外回退到开发或生产配置。数据库名必须以
    ``_test`` 结尾，因为本测试套件会清空白名单内的应用表；凭据不会写入日志或仓库。

    Returns:
        供迁移和异步 SQLAlchemy 会话共用的测试数据库 URL。

    Raises:
        pytest.UsageError: URL 缺失或不符合本地测试数据库安全约束。
    """

    value = os.environ.get("TEST_DATABASE_URL")
    if value is None:
        raise pytest.UsageError("integration tests require TEST_DATABASE_URL")
    try:
        return validate_test_database_url(value).value
    except InvalidTestDatabaseUrl as error:
        # 只传递静态拒绝原因，绝不把环境变量原值交给 pytest 输出。
        raise pytest.UsageError(str(error)) from None


@pytest.fixture(scope="session")
def cycle5_regular_database_url() -> Iterator[TestDatabaseUrl]:
    """为 Cycle 5 混合矩阵提供稳定 head 数据库且不改变 lifecycle anchor。

    Step 26 在同一 pytest 命令中先执行 roles-absent 的 migration/retention 文件，再执行
    普通 Calendar 集成文件。若调用方已经由两阶段 integration orchestrator 提供精确
    head/baseline 数据库，本 fixture 只做只读 catalog 复核并直接复用；若调用方仍是
    revision 0018、fresh-default ACL 且两个 runtime role 均不存在的 Task 13 anchor，
    则通过现有 typed role-bootstrap→migrate 生命周期创建一个 UUID disposable 数据库。
    临时库退出后必须证明 anchor 的完整逻辑快照逐字不变，禁止为 focused 命令隐式
    bootstrap、迁移或广授权共享目标。

    Yields:
        可供普通 Cycle 5 Calendar/restore 集成测试独占使用的 head 数据库 URL。

    Raises:
        pytest.fail: 当前目标既不是精确 regular head，也不是允许的 roles-absent 0018
            anchor，或 disposable 生命周期未能保持 anchor 不变。
    """
    raw_database_url = os.environ.get("TEST_DATABASE_URL")
    if raw_database_url is None:
        raise pytest.UsageError("integration tests require TEST_DATABASE_URL")
    try:
        validated = validate_test_database_url(raw_database_url)
    except InvalidTestDatabaseUrl as error:
        raise pytest.UsageError(str(error)) from None
    database_url = validated.value
    repository_root = Path(__file__).resolve().parents[3]
    backend_root = repository_root / "backend"
    published_head = load_published_alembic_authority(
        Config(backend_root / "alembic.ini")
    ).head_revision
    target_engine = create_engine(
        validated.parsed.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    reuse_current_database = False
    try:
        with target_engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            target_database_oid = connection.execute(
                text(
                    "SELECT oid FROM pg_database "
                    "WHERE datname = current_database()"
                )
            ).scalar_one()
            access_snapshot = read_database_access_snapshot_sync(
                connection,
                target_database_oid=target_database_oid,
            )
            if revision == published_head:
                # 纯 planner 会逐字段验证 baseline ACL、两个 safe role 与零 membership；
                # grant verifier 再证明当前 head 的对象权限完整，整个复用分支零写。
                plan_baseline_connect_reassertion(access_snapshot)
                verify_object_grants(
                    connection,
                    revision=published_head,
                    phase=GrantPhase.BASELINE,
                )
                reuse_current_database = True
    finally:
        target_engine.dispose()

    if reuse_current_database:
        # catalog 复核连接已关闭，普通测试不会继承一个跨模块的只读事务。
        yield database_url
        return

    is_roles_absent_0018_anchor = (
        revision == "20260809_0018"
        and access_snapshot.acl_profile is DatabaseAclProfile.FRESH_DEFAULT
        and access_snapshot.app_role is None
        and access_snapshot.retention_role is None
        and access_snapshot.memberships == ()
    )
    if not is_roles_absent_0018_anchor:
        pytest.fail("BLOCKED: Cycle 5 regular database posture is not admissible")

    resources = SqlAlchemyIntegrationSuiteResources(repository_root)
    with managed_cycle5_regular_database(
        anchor=validated,
        resources=resources,
    ) as regular_url:
        yield regular_url


@pytest.fixture
async def cycle5_tracked_session_factories(
    isolated_database: None,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    """让 Cycle 5 regular 测试异常时仍先释放本测试创建的全部数据库 pool。

    此 fixture 只由显式 opt-in 的 Cycle 5 regular 模块请求。依赖 ``isolated_database``
    使 registry 的 teardown 先于后置 TRUNCATE；函数级 teardown 又必然先于 session 级
    disposable cleanup，因此不需要终止未知 PostgreSQL session 或放宽 provenance 检查。
    """
    del isolated_database
    module = request.module
    if module is None or not hasattr(module, "build_session_factory"):
        pytest.fail("BLOCKED: Cycle 5 module has no trackable session factory builder")

    async with managed_session_factory_registry() as registry:
        monkeypatch.setattr(module, "build_session_factory", registry.build)
        yield


@pytest.fixture(scope="session", autouse=True)
def migrated_database(database_url: TestDatabaseUrl) -> Iterator[None]:
    """在本测试会话开始时只经 typed lifecycle 升级已稳定的测试数据库。

    fixture 复用生产 migration 的 management→target→schema admission 与同连接 Alembic
    runner。若目标仍是 Candidate，本入口必须零写拒绝；首次安装应由显式
    role-bootstrap/reset 流程先收敛，不能为了让普通 integration test 启动而隐式授权。

    Args:
        database_url: 已通过测试专用数据库安全校验的异步 URL。

    Yields:
        迁移到最新版本且可供测试使用的会话级生命周期标记。
    """

    validated = validate_test_database_url(database_url)
    backend_root = Path(__file__).resolve().parents[2]
    management_engine = create_engine(
        validated.maintenance_url().set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    target_engine = create_engine(
        validated.parsed.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        migrate_database(
            SqlAlchemyDatabaseMaintenanceContext(
                management_engine=management_engine,
                target_engine=target_engine,
                target_database_name=validated.database_name,
                bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
                app_password=None,
                retention_password=None,
                migration_runner=partial(
                    run_alembic_upgrade_on_connection,
                    config_path=backend_root / "alembic.ini",
                ),
                published_authority=load_published_alembic_authority(
                    Config(backend_root / "alembic.ini")
                ),
            )
        )
        yield
    finally:
        target_engine.dispose()
        management_engine.dispose()


def _bootstrap_empty_migration_database(
    database_url: URL,
    *,
    cleanup: DisposableDatabaseCleanupLease,
    management_engine: Engine,
) -> None:
    """把新建 PostgreSQL Candidate 通过 typed lifecycle 收敛为精确 base baseline。

    新库此时仍无 ``alembic_version`` 或业务表；role-bootstrap 只冻结安全 runtime roles、
    baseline database ACL 与 base schema grants。随后历史 Alembic helper 才能在同一发布
    authority 下证明 ``base`` inventory，而不是让 env.py 暗中修复 Candidate。

    Args:
        database_url: fixture 刚创建、由当前测试 owner 持有的精确临时数据库 URL。
        cleanup: 仍持有同一 disposable target provenance lock 的 cleanup lease；bootstrap
            异常时从它读回已提交的 safe runtime roles，避免 cleanup 因缺少 role OID 而拒绝
            删除一个已经创建角色的临时库。
        management_engine: 外层 fixture 使用的同一 NullPool management engine，确保 typed
            lifecycle 与 cleanup lease 处于同一个受控测试目标边界。
    """
    backend_root = Path(__file__).resolve().parents[2]
    target_engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        context = SqlAlchemyDatabaseMaintenanceContext(
            management_engine=management_engine,
            target_engine=target_engine,
            target_database_name=str(database_url.database),
            bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
            # 与 retention role 集成 fixture 共享固定合成密码，避免临时库 bootstrap
            # 改变 cluster role 后让相邻测试使用另一套凭据事实。
            app_password="app-role-integration-password",
            retention_password="retention-role-integration-password",
            migration_runner=lambda connection: None,
            published_authority=load_published_alembic_authority(
                Config(backend_root / "alembic.ini")
            ),
        )
        try:
            role_bootstrap_database(context)
        except BaseException as original_error:
            # role-bootstrap 的数据库事务可能已经提交，随后 post-commit verifier 或锁
            # 释放阶段才失败；此时必须先从仍持有 cleanup lease 的 management session
            # 读取 exact safe role provenance，再把原始异常原样交给 fixture teardown。
            try:
                cleanup.record_created_runtime_roles_if_safe()
            except DisposableDatabaseCleanupError as cleanup_error:
                raise cleanup_error from original_error
            raise
        if cleanup.record_created_runtime_roles_if_safe() is not True:
            raise AssertionError(
                "empty migration bootstrap did not create safe runtime roles"
            )
    finally:
        target_engine.dispose()


@pytest.fixture
def empty_migration_database(database_url: TestDatabaseUrl) -> Iterator[URL]:
    """提供从零开始的唯一本地测试库，并在测试结束时删除同一精确目标。

    该 fixture 永远不操作共享 ``ai_employee_test``：先验证基础 URL，再生成并验证带 UUID
    的 ``_test`` 标识符，最后才创建维护引擎。若测试角色没有 CREATEDB 权限，明确报告
    BLOCKED，不能为了让套件“通过”而复用共享数据库或跳过隔离。
    """
    validated = validate_test_database_url(database_url)
    temporary_name = f"ai_employee_mig_{uuid4().hex}_test"
    temporary_url = validated.for_database(temporary_name)
    management_engine = create_engine(
        validated.maintenance_url().set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with managed_disposable_database_cleanup(
            management_engine,
            database_name=temporary_name,
        ) as cleanup:
            try:
                cleanup.assert_initial_absence()
            except DisposableDatabaseCleanupError:
                pytest.fail(
                    "BLOCKED: migration fixture target or fixed runtime roles already exist"
                )
            if not cleanup.create_database():
                pytest.fail("BLOCKED: configured test role lacks CREATEDB")
            _bootstrap_empty_migration_database(
                temporary_url,
                cleanup=cleanup,
                management_engine=management_engine,
            )
            yield temporary_url
    finally:
        management_engine.dispose()


async def _truncate_application_tables(database_url: TestDatabaseUrl) -> None:
    """仅清空固定白名单内的应用表，并保留 Alembic 迁移版本。

    表名不是来自环境变量、数据库反射或测试输入，因此 SQL 标识符插值的范围是封闭且可审计
    的。显式子表到父表顺序也让后续改为逐表删除时仍保持外键安全。

    Args:
        database_url: 已通过测试专用数据库安全校验的异步 URL。
    """

    validated = validate_test_database_url(database_url)
    engine = build_engine(validated.value)
    quoted_tables = ", ".join(
        engine.dialect.identifier_preparer.quote(name) for name in APPLICATION_TABLES
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {quoted_tables}"))
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def isolated_database(
    migrated_database: None,
    database_url: TestDatabaseUrl,
) -> AsyncIterator[None]:
    """在每个集成测试前后清理应用表，阻断跨测试状态泄漏。

    Args:
        migrated_database: 保证 Schema 已升级到当前 Alembic head 的依赖标记。
        database_url: 已通过测试专用数据库安全校验的异步 URL。

    Yields:
        当前测试独占的空应用数据集合；``alembic_version`` 始终保留。
    """

    await _truncate_application_tables(database_url)
    try:
        yield
    finally:
        await _truncate_application_tables(database_url)
