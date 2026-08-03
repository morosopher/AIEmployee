"""集中构造异步数据库引擎、会话工厂及提交后任务事件通知。"""

import asyncio
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ai_employee.infrastructure.db.models.tasks import AuditEventModel


class TaskEventNotificationPublisher(Protocol):
    """定义事务提交后可调用的瞬时任务事件通知端口。"""

    async def publish_after_commit(self, *, task_id: UUID, event_id: int) -> None:
        """通知在线订阅者重新读取已经提交的审计事实。"""


class TaskEventNotifyingAsyncSession(AsyncSession):
    """收集当前事务新增 AuditEvent，并且仅在提交成功后发出 Redis 唤醒提示。

    Repository 只维护 PostgreSQL 领域事实，完全不知道 Redis 的存在。本会话适配器在
    ``after_flush`` 取得数据库已分配的审计 ID，在 SQLAlchemy ``after_commit`` 钩子中才
    创建尽力通知的异步任务；因此回滚事件永不通知，Redis 不可用也不能改变已提交的数据库
    结果。此钩子覆盖 ``async with session_factory.begin()`` 的实际提交路径。
    """

    def __init__(
        self,
        bind: AsyncEngine | None = None,
        task_event_publisher: TaskEventNotificationPublisher | None = None,
        **kwargs: Any,
    ) -> None:
        """初始化会话级通知收集器，并注册到其同步 SQLAlchemy 会话。

        Args:
            task_event_publisher: 可选的 Redis 瞬时通知适配器；未提供时保持纯 PostgreSQL
                会话行为，供迁移和不需要 SSE 低延迟的测试使用。
        """
        # ``kwargs`` 是 SQLAlchemy 会话构造器的第三方开放参数边界，无法在本层进一步收窄。
        super().__init__(bind=bind, **kwargs)
        self._task_event_publisher = task_event_publisher
        self._pending_task_events: set[tuple[UUID, int]] = set()
        event.listen(self.sync_session, "after_flush", self._collect_task_events)
        event.listen(self.sync_session, "after_commit", self._publish_task_events_after_commit)
        event.listen(self.sync_session, "after_rollback", self._discard_task_events_after_rollback)

    def _collect_task_events(self, *_: object) -> None:
        """记录本事务已获得主键的新任务审计行，并合并重复 flush 的同一行。"""
        if self._task_event_publisher is None:
            return
        for instance in self.sync_session.new:
            if isinstance(instance, AuditEventModel) and instance.task_id is not None and instance.id is not None:
                self._pending_task_events.add((instance.task_id, instance.id))

    def _publish_task_events_after_commit(self, *_: object) -> None:
        """在底层事务确认提交后异步尽力通知，不等待或改变提交的成功语义。

        ``async_sessionmaker.begin()`` 由同步事务的 ``__exit__`` 执行 commit，不能依赖
        ``AsyncSession.commit`` 覆写。Redis I/O 由独立任务执行，避免在 SQLAlchemy 提交
        临界区访问网络；即使进程在通知前退出，持久事件仍会由 SSE 轮询或重连回放。
        """
        pending_events = tuple(self._pending_task_events)
        self._pending_task_events.clear()
        publisher = self._task_event_publisher
        if publisher is not None:
            for task_id, event_id in pending_events:
                asyncio.create_task(publisher.publish_after_commit(task_id=task_id, event_id=event_id))

    def _discard_task_events_after_rollback(self, *_: object) -> None:
        """丢弃未提交审计行的通知候选，防止回滚事实唤醒 SSE。"""
        self._pending_task_events.clear()


class ManagedAsyncSessionMaker(async_sessionmaker[TaskEventNotifyingAsyncSession]):
    """拥有异步引擎生命周期的类型化会话工厂。

    SQLAlchemy 的 ``async_sessionmaker`` 只负责创建会话，不负责释放调用方传入的引擎。
    这里保留其 ``__call__`` 与 ``begin`` 行为，并把引擎所有权记录在受支持的公开子类中，
    让 API、Worker 和集成测试可以在生命周期结束时显式 ``await dispose()``，而无需读取
    SQLAlchemy 内部的 ``kw`` 字典或依赖实现细节。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        task_event_publisher: TaskEventNotificationPublisher | None = None,
    ) -> None:
        """创建绑定到引擎的工厂，并可选择在提交后通知在线任务事件订阅者。"""
        self._owned_engine = engine
        super().__init__(
            bind=engine,
            class_=TaskEventNotifyingAsyncSession,
            expire_on_commit=False,
            task_event_publisher=task_event_publisher,
        )

    async def dispose(self) -> None:
        """异步释放工厂拥有的连接池；重复调用保持安全。"""
        await self._owned_engine.dispose()


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


def build_session_factory(
    database_url: str,
    *,
    task_event_publisher: TaskEventNotificationPublisher | None = None,
) -> ManagedAsyncSessionMaker:
    """创建绑定到独立异步引擎的强类型会话工厂。

    提交后保留已加载属性，避免业务用例在事务完成后因属性过期触发不可见的异步 I/O。
    事务的提交或回滚仍由调用方通过显式上下文控制。

    Args:
        database_url: SQLAlchemy 异步数据库 URL。
        task_event_publisher: 生产进程注入的提交后 Redis 提示适配器；其失败绝不影响
            PostgreSQL 事务。省略时不安装低延迟提示，仍可使用持久事件重放。

    Returns:
        ``expire_on_commit=False`` 且可显式释放引擎的异步会话工厂。
    """

    engine = build_engine(database_url)
    return ManagedAsyncSessionMaker(engine, task_event_publisher=task_event_publisher)
