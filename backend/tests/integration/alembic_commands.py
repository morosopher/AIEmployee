"""为历史 migration 契约测试提供同一外部 Connection 的 Alembic 命令封装。"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Connection, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from ai_employee.infrastructure.db.alembic import (
    AlembicMigrationInvariantError,
    MigrationGrantLifecycle,
    OnlineMigrationAuthority,
    bind_alembic_connection,
    load_published_alembic_authority,
)
from ai_employee.infrastructure.db.database_access import BootstrapCaller
from ai_employee.infrastructure.db.database_grants import (
    CatalogObjectSnapshot,
    apply_migration_grant_delta,
    verify_object_grants,
)
from ai_employee.infrastructure.db.database_maintenance import (
    SqlAlchemyDatabaseMaintenanceContext,
    _SqlAlchemyTargetMaintenanceLease,
)

_TEST_REVERSE_ERROR = "tests-only reverse migration invariant violation"


def _sync_database_url(config: Config) -> URL:
    """把测试已注入的 PostgreSQL URL 收窄为 psycopg 同步 URL，不记录凭据。"""
    database_url = config.get_main_option("sqlalchemy.url")
    if type(database_url) is not str or database_url == "":
        raise AssertionError("integration Alembic config must contain a database URL")
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql" or parsed.database is None:
        raise AssertionError("integration Alembic config must target PostgreSQL")
    return parsed.set(drivername="postgresql+psycopg")


def _reject_context_migration_runner(_authority: OnlineMigrationAuthority) -> None:
    """拒绝 helper 误走 context 的产品 head-only runner。

    历史 migration 契约测试需要官方 upgrade/downgrade/stamp/check 四种精确命令，
    因此它们只能在同一 concrete target lease 内发行 token 后调用
    ``runner(config)``，不得退化为 context 的生产 ``upgrade head`` callback。
    """
    raise AssertionError("historical migration helper used the product migration runner")


def _tests_only_single_revision(revisions: object) -> str:
    """把 Alembic reverse step 的单 head tuple 收窄为精确 revision。"""
    if type(revisions) is not tuple or len(revisions) != 1:
        raise AlembicMigrationInvariantError(_TEST_REVERSE_ERROR)
    revision = revisions[0]
    if type(revision) is not str or revision == "" or "\0" in revision:
        raise AlembicMigrationInvariantError(_TEST_REVERSE_ERROR)
    return revision


def _tests_only_reverse_on_version_apply(
    self: MigrationGrantLifecycle,
    *,
    ctx: object,
    step: object,
    heads: set[str],
    run_args: dict[str, object],
) -> None:
    """仅为历史 migration 契约测试执行严格相邻 reverse grant lifecycle。

    生产 ``MigrationGrantLifecycle.on_version_apply`` 永远拒绝 reverse；本 callback 只在
    tests-only ``run_alembic_downgrade`` 的 patch 窗口内存在。它仍要求同一三锁
    Connection、精确前一 revision、空 ``run_args`` 与完整 source snapshot，并复用生产
    canonical delta/destination verifier 更新同一 lifecycle state。任何 metadata 偏差均在
    GRANT/REVOKE 或 state mutation 前失败。
    """
    current_revision = self._current_revision
    snapshot = self._snapshot
    connection = getattr(ctx, "connection", None)
    is_upgrade = getattr(step, "is_upgrade", None)
    is_stamp = getattr(step, "is_stamp", None)
    source_revision_ids = getattr(step, "source_revision_ids", None)
    destination_revision_ids = getattr(step, "destination_revision_ids", None)
    if (
        type(self) is not MigrationGrantLifecycle
        or current_revision is None
        or type(snapshot) is not CatalogObjectSnapshot
        or self._finished
        or connection is not self.connection
        or not isinstance(connection, Connection)
        or is_upgrade is not False
        or is_stamp is not False
        or type(heads) is not set
        or type(run_args) is not dict
        or run_args
    ):
        raise AlembicMigrationInvariantError(_TEST_REVERSE_ERROR)

    source_revision = _tests_only_single_revision(source_revision_ids)
    destination_revision = _tests_only_single_revision(destination_revision_ids)
    if source_revision != current_revision or not self.authority.contains(
        destination_revision
    ):
        raise AlembicMigrationInvariantError(_TEST_REVERSE_ERROR)
    expected_heads = {destination_revision}
    if heads != expected_heads:
        raise AlembicMigrationInvariantError(_TEST_REVERSE_ERROR)
    source_index = self.authority.revisions.index(source_revision)
    if source_index == 0 or self.authority.revisions[source_index - 1] != destination_revision:
        raise AlembicMigrationInvariantError(_TEST_REVERSE_ERROR)

    apply_migration_grant_delta(
        self.connection,
        source_revision=source_revision,
        destination_revision=destination_revision,
        phase=self.phase,
        before=snapshot,
    )
    destination_snapshot = verify_object_grants(
        self.connection,
        revision=destination_revision,
        phase=self.phase,
    )
    if (
        destination_snapshot.revision != destination_revision
        or destination_snapshot.phase is not self.phase
    ):
        raise AlembicMigrationInvariantError(_TEST_REVERSE_ERROR)
    self._current_revision = destination_revision
    self._snapshot = destination_snapshot


def _run_tests_only_downgrade(config: Config, revision: str) -> None:
    """在局部 patch 内运行官方 downgrade，不向生产 env 暴露 reverse waiver。"""
    with patch.object(
        MigrationGrantLifecycle,
        "on_version_apply",
        _tests_only_reverse_on_version_apply,
    ):
        command.downgrade(config, revision)


def _run_bound_command(
    config: Config,
    *,
    expected_target_revision: str | None,
    runner: Callable[[Config], None],
    after_bind_before_command: Callable[[Config], None] | None = None,
) -> None:
    """在真实三锁 concrete lease 内绑定、执行并核验官方 Alembic 命令。

    helper 与产品路径共用 ``SqlAlchemyDatabaseMaintenanceContext`` 和冻结锁序，
    只在 target revision 选择上保留历史契约测试的 upgrade/downgrade/stamp/check
    能力。成功结果必须在 management→target→schema 锁释放前由同一
    concrete lease 重读 revision、stable admission 与 grant lifecycle 消费状态。

    Args:
        config: 含合成 PostgreSQL target URL 与当前 migration script location 的配置。
        expected_target_revision: 精确已发布 target；``None`` 表示 check 必须停在当前 revision。
        runner: 只接收已绑定 config 的官方 Alembic 命令。
        after_bind_before_command: 仅测试可用 hook；在 target admission、authority 发行与
            Config bind 均完成后、官方命令前调用，用于制造竞争窗口事实。它不改变生产
            maintenance/env，也不能替代 runner 或绕过三锁。
    """
    if after_bind_before_command is not None and not callable(after_bind_before_command):
        raise AssertionError("integration Alembic hook must be callable")
    target_url = _sync_database_url(config)
    target_database_name = target_url.database
    if type(target_database_name) is not str:
        raise AssertionError("integration Alembic target database name must be text")
    management_engine = create_engine(
        target_url.set(database="postgres"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    target_engine = create_engine(target_url, poolclass=NullPool, hide_parameters=True)
    published = load_published_alembic_authority(config)
    context = SqlAlchemyDatabaseMaintenanceContext(
        management_engine=management_engine,
        target_engine=target_engine,
        target_database_name=target_database_name,
        bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
        app_password=None,
        retention_password=None,
        migration_runner=_reject_context_migration_runner,
        published_authority=published,
    )
    try:
        with context.acquire_management_lifecycle_lock() as management:  # noqa: SIM117
            with management.acquire_target_lock() as target:
                if type(target) is not _SqlAlchemyTargetMaintenanceLease:
                    raise AssertionError(
                        "integration migration helper requires concrete target lease"
                    )
                concrete_target = cast(_SqlAlchemyTargetMaintenanceLease, target)
                concrete_target.assert_migration_allowed()
                with concrete_target.acquire_schema_lifecycle_lock():
                    exact_target = (
                        concrete_target.revision
                        if expected_target_revision is None
                        else expected_target_revision
                    )
                    token = concrete_target._issue_online_migration_authority(
                        expected_target_revision=exact_target,
                    )
                    bind_alembic_connection(config, token)
                    if after_bind_before_command is not None:
                        # hook 位于 bind 与官方 command 之间，精确复现 admission 后 catalog
                        # 漂移；三把锁仍由本 helper 持有，异常也会沿原 context 正常释放。
                        after_bind_before_command(config)
                    runner(config)
                    concrete_target._verify_online_migration_result(token)
    finally:
        target_engine.dispose()
        management_engine.dispose()


def run_alembic_upgrade(
    config: Config,
    revision: str,
    *,
    after_bind_before_command: Callable[[Config], None] | None = None,
) -> None:
    """升级到已发布 revision，并可在 tests-only post-bind 窗口注入事实。"""
    authority = load_published_alembic_authority(config)
    expected = authority.head_revision if revision == "head" else revision
    _run_bound_command(
        config,
        expected_target_revision=expected,
        runner=lambda bound_config: command.upgrade(bound_config, revision),
        after_bind_before_command=after_bind_before_command,
    )


def run_alembic_downgrade(config: Config, revision: str) -> None:
    """只在 tests-only 严格 reverse callback 下验证历史 downgrade 行保真。"""
    _run_bound_command(
        config,
        expected_target_revision=revision,
        runner=lambda bound_config: _run_tests_only_downgrade(bound_config, revision),
    )


def run_alembic_stamp(config: Config, revision: str) -> None:
    """在真实三锁与外部 Connection 上尝试官方 stamp 到精确已发布 revision。

    本入口仅供迁移不变量测试触发 Alembic 原生 ``StampStep``，不会向产品 CLI 暴露
    stamp 能力，也不会绕过 target admission、发布脚本权威、对象授权 lifecycle 或
    同一事务结果核验。若 callback 拒绝 ``is_stamp=True``，Alembic 对 version row 的
    事务内尝试必须随整个命令一并回滚。

    Args:
        config: 含合成 PostgreSQL target URL 与当前 migration script location 的配置。
        revision: 官方 ``command.stamp`` 尝试写入的精确已发布目标 revision。
    """
    _run_bound_command(
        config,
        expected_target_revision=revision,
        runner=lambda bound_config: command.stamp(bound_config, revision),
    )


def run_alembic_check(config: Config) -> None:
    """在当前已发布 revision 上执行 metadata check，不假定或修改目标 revision。"""
    _run_bound_command(
        config,
        expected_target_revision=None,
        runner=command.check,
    )
