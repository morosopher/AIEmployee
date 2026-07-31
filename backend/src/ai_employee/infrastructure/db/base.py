"""定义 SQLAlchemy ORM 的声明式基类与通用字段混入。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    """异步 ORM 模型的统一声明式基类。

    ``AsyncAttrs`` 为后续显式等待关系加载提供能力；领域层与 API 层不得直接依赖本类，
    从而将 SQLAlchemy 类型限制在基础设施边界内。
    """


class UUIDPrimaryKeyMixin:
    """为持久实体提供由应用进程生成的 UUID 主键。

    UUID 在发出 INSERT 前生成，使调用方可以在同一事务中安全引用新实体，而不依赖数据库
    往返或宿主机时区。数据库列使用原生 UUID 语义，不接受字符串主键。
    """

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)


class TimestampMixin:
    """为需要审计创建与更新时间的实体提供带时区时间戳。

    PostgreSQL 负责首次写入的时间事实，更新表达式也由数据库执行。应用传入的时间必须是
    带时区 UTC 值；展示层再按用户的 IANA 时区转换。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
