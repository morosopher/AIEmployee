"""只允许 guarded 0018→0019 的无参数迁移组合根。

复用既有 management→target→schema 三锁与 typed online authority；本模块只构造
artifact-backed guard 并注入冻结属性，不复制 env/revision 的 resolver 或 grant lifecycle。
"""

import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from ai_employee.application.ports.calendar_aad_migration_guard import (
    CALENDAR_AAD_0019_GUARD_ATTRIBUTE,
)
from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
from ai_employee.cli.calendar_aad_preflight_0019 import (
    require_no_rollout_arguments,
    require_sealed_settings,
    rollout_environment,
)
from ai_employee.cli.database_maintenance import _load_database_endpoint, _read_secret_file
from ai_employee.config import Settings
from ai_employee.domain.errors import DomainError
from ai_employee.infrastructure.db.alembic import (
    OnlineMigrationAuthority,
    bind_alembic_connection,
    load_published_alembic_authority,
)
from ai_employee.infrastructure.db.database_access import BootstrapCaller
from ai_employee.infrastructure.db.database_maintenance import (
    SqlAlchemyDatabaseMaintenanceContext,
    migrate_database,
)
from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
    CalendarAadArtifactFile,
    CalendarAadMigrationArtifactGuard,
    calendar_aad_rollout_lease,
)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "alembic.ini"


def run_migration(
    *,
    management_engine: Engine,
    target_engine: Engine,
    artifact_file: CalendarAadArtifactFile,
    clock: Callable[[], datetime],
) -> None:
    """在同一 revision lease 下读取工件并交给既有三锁 lifecycle 完成唯一迁移。

    Args:
        management_engine: 同 owner Secret 的固定 postgres 管理连接工厂。
        target_engine: 同 owner Secret 的精确目标；调用方负责释放两个 NullPool engines。
        artifact_file: 当前 host image/basename 绑定；首次文件访问发生在 revision lease 后。
        clock: 每个 frozen guard phase 重新采样的 UTC 时钟。
    """
    with calendar_aad_rollout_lease(target_engine.url) as lease:
        artifact = artifact_file.read()
        guard = CalendarAadMigrationArtifactGuard(
            artifact_file=artifact_file, lease=lease, clock=clock, artifact=artifact
        )
        published = load_published_alembic_authority(Config(_CONFIG_PATH))
        if published.head_revision != "20260809_0019":
            raise CalendarAadRolloutError("calendar_aad_revision_mismatch")

        def migration_runner(authority: OnlineMigrationAuthority) -> None:
            """只绑定 lifecycle 发行的原 token 和 guard；两个 phase 均由 frozen 路径调用。"""
            if (
                authority.expected_current_revision != "20260809_0018"
                or authority.expected_target_revision != "20260809_0019"
                or authority.published_authority != published
            ):
                raise CalendarAadRolloutError("calendar_aad_revision_mismatch")
            lease.verify_owned()
            config = Config(_CONFIG_PATH)
            config.attributes[CALENDAR_AAD_0019_GUARD_ATTRIBUTE] = guard
            bind_alembic_connection(config, authority)
            command.upgrade(config, "20260809_0019")

        database_name = target_engine.url.database
        if database_name is None:
            raise CalendarAadRolloutError("calendar_aad_revision_mismatch")
        context = SqlAlchemyDatabaseMaintenanceContext(
            management_engine=management_engine,
            target_engine=target_engine,
            target_database_name=database_name,
            bootstrap_caller=BootstrapCaller.ROLE_BOOTSTRAP,
            app_password=None,
            retention_password=None,
            migration_runner=migration_runner,
            published_authority=published,
        )
        migrate_database(context)


def main(arguments: Sequence[str] | None = None) -> int:
    """使用 migration service 唯一 owner Secret；不接受 scope/revision/override 参数。"""
    try:
        require_no_rollout_arguments(arguments)
        directory, binding = rollout_environment()
        require_sealed_settings(Settings())
        endpoint = _load_database_endpoint()
        password = _read_secret_file(Path("/run/secrets/postgres_bootstrap_password"))
        management = create_engine(
            endpoint.owner_url(password, database_name="postgres"),
            poolclass=NullPool,
            hide_parameters=True,
        )
        target = create_engine(
            endpoint.owner_url(password, database_name=endpoint.database_name),
            poolclass=NullPool,
            hide_parameters=True,
        )
        try:
            run_migration(
                management_engine=management,
                target_engine=target,
                artifact_file=CalendarAadArtifactFile(directory, binding),
                clock=lambda: datetime.now(UTC),
            )
        finally:
            target.dispose()
            management.dispose()
    except (DomainError, CommandError, SQLAlchemyError, OSError, ValueError, RuntimeError) as error:
        print(
            error.error_code if isinstance(error, DomainError) else "calendar_aad_rollout_failed",
            file=sys.stderr,
        )
        return 1
    print("calendar_aad_migration_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
