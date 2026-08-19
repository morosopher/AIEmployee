"""配置 Alembic 元数据，并只在 typed lifecycle 注入的同步 Connection 上迁移。"""

import os
from logging.config import fileConfig
from typing import cast

from alembic import context
from alembic.runtime.environment import OnVersionApplyFn

from ai_employee.application.ports.calendar_aad_migration_guard import (
    CALENDAR_AAD_0019_GUARD_ATTRIBUTE,
    resolve_calendar_aad_migration_guard,
)
from ai_employee.infrastructure.db import models as db_models
from ai_employee.infrastructure.db.alembic import (
    MigrationGrantLifecycle,
    OnlineMigrationAuthority,
    require_online_migration_authority,
    set_alembic_database_url,
)
from ai_employee.infrastructure.db.base import Base

config = context.config

database_url = os.environ.get("DATABASE_URL")
if database_url is not None and not config.get_main_option("sqlalchemy.url"):
    # 命令行迁移从环境接收部署配置，但不覆盖测试通过 Alembic Config 注入的隔离 URL。
    set_alembic_database_url(config, database_url)

if config.config_file_name is not None:
    # Alembic 既可由一次性 CLI 启动，也会被测试/维护编排嵌入已有进程。保留宿主已创建
    # logger 的 enabled 状态，避免迁移配置把 HTTP 安全边界及其他未列入 ini 的 logger
    # 永久置为 disabled；已声明的 root/SQLAlchemy/Alembic 配置仍按 ini 正常应用。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 导入模型模块是注册元数据所必需的显式副作用；保留引用可避免静态检查误判未使用导入。
_ = db_models
target_metadata = Base.metadata

_EXTERNAL_MANAGED_TABLES = frozenset(
    {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
)


def _alembic_on_version_apply(
    lifecycle: MigrationGrantLifecycle,
) -> OnVersionApplyFn:
    """把 keyword-only lifecycle callback 收窄为 Alembic 声明的 callback 类型。

    Alembic 的公开文档和运行时都以 ``ctx``、``step``、``heads``、``run_args`` 四个
    关键字调用 callback，但类型别名仍将它描述为位置参数 ``Callable``。这里只转换同一
    callable 的静态视图，不增加包装层、参数改写或异常处理，因此 lifecycle 的严格形状
    校验、调用顺序和同一实例状态保持不变。

    Args:
        lifecycle: 当前 online authority 绑定的 grant lifecycle 实例。

    Returns:
        原 callable 的 Alembic 静态类型视图。
    """
    return cast(OnVersionApplyFn, lifecycle.on_version_apply)


def include_object(
    object_: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """从 ORM 自动比对中排除由 LangGraph 固定协议管理的 checkpoint 表。

    表仍由本仓库 Alembic 迁移创建，但没有对应 ORM 模型；否则 ``alembic check`` 会把
    正确的第三方协议表误报为待删除。Task 7 的条件恢复索引同样尚未建模，保留迁移事实。
    """
    del object_, compare_to
    if type_ == "table" and reflected and name in _EXTERNAL_MANAGED_TABLES:
        return False
    return not (type_ == "index" and reflected and name == "ix_task_runs_retry_recovery_due")


def run_migrations_offline() -> None:
    """在无数据库连接时生成 SQL 迁移脚本。

    URL 仅由 Alembic Config 提供；迁移环境不会读取或记录额外凭据。
    """

    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(authority: OnlineMigrationAuthority) -> None:
    """在 token 绑定的同一同步 Connection/transaction 上执行 grant-aware 迁移。

    Args:
        authority: typed lifecycle lease 在持锁 admission 后发行的不可拆分 capability。
    """
    connection = authority.connection
    lifecycle = authority.grant_lifecycle
    if authority.expected_target_revision == "20260809_0019":
        calendar_guard = resolve_calendar_aad_migration_guard(config)
        # revision 与 final callback 都只读取这一已解析实例；即使 migration operation 修改
        # Config attribute，lifecycle 内仍保留同一 identity。
        config.attributes[CALENDAR_AAD_0019_GUARD_ATTRIBUTE] = calendar_guard
        lifecycle.bind_calendar_aad_guard(calendar_guard)

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        on_version_apply=_alembic_on_version_apply(lifecycle),
    )

    with context.begin_transaction():
        lifecycle.verify_before_migrations()
        context.run_migrations()
        lifecycle.verify_after_migrations()


def run_migrations_online() -> None:
    """只消费 typed lifecycle 注入的完整 token，任一字段缺失均 fail closed。

    ``database_maintenance`` CLI 已在该 target session 上持有 maintenance/schema advisory
    locks；env.py 不能从 URL/Connection/默认 head 自行补 authority，否则 DDL 会脱离
    identity-bound admission。
    """
    do_run_migrations(require_online_migration_authority(config))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
