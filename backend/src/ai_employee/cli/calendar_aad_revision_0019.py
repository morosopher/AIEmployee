"""为正式 0019 one-off 提供固定、只读且无 artifact admission 的版本筛查。

本入口只复用 published revision reader 和 owner Secret/endpoint authority；不创建
maintenance context，不取得 rollout 授权，不运行迁移、grant repair、任务或供应商调用。
结果仅是当前版本筛查，正式命令仍必须独立验证 revision、lease、guard 与冻结 lifecycle。
"""

import sys
from collections.abc import Sequence
from pathlib import Path

from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadRevision,
    CalendarAadRolloutError,
)
from ai_employee.cli.calendar_aad_preflight_0019 import require_no_rollout_arguments
from ai_employee.cli.database_maintenance import _load_database_endpoint, _read_secret_file
from ai_employee.domain.errors import DomainError
from ai_employee.infrastructure.db.alembic import (
    PublishedAlembicAuthority,
    load_published_alembic_authority,
    read_current_alembic_revision,
)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "alembic.ini"


def read_rollout_revision(
    engine: Engine, *, authority: PublishedAlembicAuthority
) -> CalendarAadRevision:
    """在一个只读快照内读取发布链上的当前版本，只接受固定 0018/0019 闭集。

    Args:
        engine: 同一 owner Secret 派生的目标 NullPool engine，由调用者释放。
        authority: 当前固定镜像发布脚本的真实线性链，不能由 operator 手填。

    Returns:
        当前版本的固定闭集标签，不含 endpoint、credential 或业务内容。

    Raises:
        CalendarAadRolloutError: 镜像 head 或当前版本不在此维护窗口的精确范围。
        SQLAlchemyError: 连接、超时或只读查询失败；CLI 只输出稳定错误码。
    """
    if authority.head_revision != "20260809_0019":
        raise CalendarAadRolloutError("calendar_aad_revision_mismatch")
    with engine.connect() as connection:
        connection.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        revision = read_current_alembic_revision(connection, authority=authority)
        # 不调用 commit。Connection 退出时回滚只读事务，筛查结果不能成为持久维护授权。
    if revision == "20260809_0018":
        return "20260809_0018"
    if revision == "20260809_0019":
        return "20260809_0019"
    raise CalendarAadRolloutError("calendar_aad_revision_mismatch")


def main(arguments: Sequence[str] | None = None) -> int:
    """执行无参数筛查；Secret 只从固定文件读，PGPASSWORD 不是连接 authority。"""
    try:
        require_no_rollout_arguments(arguments)
        endpoint = _load_database_endpoint()
        password = _read_secret_file(Path("/run/secrets/postgres_bootstrap_password"))
        authority = load_published_alembic_authority(Config(_CONFIG_PATH))
        engine = create_engine(
            endpoint.owner_url(password, database_name=endpoint.database_name),
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"connect_timeout": 10, "options": "-c statement_timeout=10000"},
        )
        try:
            revision = read_rollout_revision(engine, authority=authority)
        finally:
            engine.dispose()
    except (DomainError, SQLAlchemyError, OSError, ValueError, RuntimeError) as error:
        print(
            error.error_code
            if isinstance(error, DomainError)
            else "calendar_aad_revision_screen_failed",
            file=sys.stderr,
        )
        return 1
    print(revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
