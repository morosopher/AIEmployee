"""集中构造异步数据库引擎与会话工厂。"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_engine(database_url: str) -> AsyncEngine:
    """按给定异步 DSN 创建数据库引擎。

    连接池在借出连接前执行存活检查，以便开发进程和 Worker 在 PostgreSQL 短暂重启后
    丢弃失效连接。调用方拥有返回引擎，并应在进程或测试结束时调用 ``dispose``。

    Args:
        database_url: SQLAlchemy 异步数据库 URL，生产与测试均由配置边界注入。

    Returns:
        启用连接预检查的 SQLAlchemy 异步引擎。
    """

    return create_async_engine(database_url, pool_pre_ping=True)


def build_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    """创建绑定到独立异步引擎的强类型会话工厂。

    提交后保留已加载属性，避免业务用例在事务完成后因属性过期触发不可见的异步 I/O。
    事务的提交或回滚仍由调用方通过显式上下文控制。

    Args:
        database_url: SQLAlchemy 异步数据库 URL。

    Returns:
        ``expire_on_commit=False`` 的异步会话工厂。
    """

    engine = build_engine(database_url)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
