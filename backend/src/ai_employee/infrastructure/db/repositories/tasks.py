"""把任务创建端口映射到 SQLAlchemy 与 PostgreSQL 事务。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.tasks import (
    CreateTaskResult,
    TaskRepository,
)
from ai_employee.domain.tasks import JsonValue, TaskStatus
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

TASK_IDEMPOTENCY_CONSTRAINT = "uq_task_runs_user_id_idempotency_key"


class SqlAlchemyTaskRepository:
    """在调用方拥有的单一事务中实现幂等任务创建。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由 Repository factory 管理生命周期的异步 Session。"""
        self._session = session

    async def create_with_outbox(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> CreateTaskResult:
        """创建任务、审计和 Outbox，或复用同一用户的已有任务。

        查询必须同时携带 ``user_id`` 与幂等键；否则另一个用户复用同一客户端键时会错误
        取得不属于自己的任务。初始查询是顺序重放的快速路径；两个事务都读到空值时，
        PostgreSQL 通过命名唯一约束原子决定哪个事务认领 TaskRun。只有认领者写审计与
        Outbox，并对这两个 ORM 事实进行一次显式 flush；三类事实仍由调用方外层事务一起
        提交或回滚。``ON CONFLICT`` 只指定任务幂等约束，因此其他唯一、FK 或数据错误不会
        被解释为已有任务。

        Args:
            user_id: 任务所属用户，也是幂等查询的强制隔离条件。
            kind: 稳定英文任务种类。
            input_payload: 已规范化的内部 JSON 对象。
            idempotency_key: 当前用户范围内唯一的创建键。

        Returns:
            新建或已有任务的基础设施无关 UUID 结果。
        """
        existing_task_query = select(TaskRunModel.id).where(
            TaskRunModel.user_id == user_id,
            TaskRunModel.idempotency_key == idempotency_key,
        )
        existing_task_id = await self._session.scalar(existing_task_query)
        if existing_task_id is not None:
            return CreateTaskResult(task_id=existing_task_id)

        task_id = uuid4()
        status = TaskStatus.CREATED.value
        claimed_task_id: UUID | None = await self._session.scalar(
            insert(TaskRunModel)
            .values(
                id=task_id,
                user_id=user_id,
                kind=kind,
                status=status,
                idempotency_key=idempotency_key,
                input_payload=input_payload,
            )
            .on_conflict_do_nothing(constraint=TASK_IDEMPOTENCY_CONSTRAINT)
            .returning(TaskRunModel.id)
        )
        if claimed_task_id is None:
            # PostgreSQL 的 READ COMMITTED 会在冲突事务提交后让下一条 SELECT 看到赢家；
            # 若仍不可见，说明数据库隔离或约束与本适配器的不变量不一致，不能伪造结果。
            existing_task_id = await self._session.scalar(existing_task_query)
            if existing_task_id is None:
                raise RuntimeError("task idempotency winner is not visible after conflict")
            return CreateTaskResult(task_id=existing_task_id)

        audit = AuditEventModel(
            user_id=user_id,
            task_id=claimed_task_id,
            event_type="task.created",
            actor_type="system",
            actor_id=None,
            event_metadata={"kind": kind, "status": status},
        )
        outbox = OutboxEventModel(
            topic="task.execute",
            aggregate_id=claimed_task_id,
            deduplication_key=f"task.execute:{claimed_task_id}:initial",
            payload={"task_id": str(claimed_task_id)},
        )
        self._session.add_all((audit, outbox))
        await self._session.flush()
        return CreateTaskResult(task_id=claimed_task_id)


class SqlAlchemyTaskRepositoryFactory:
    """为每次任务创建提供独立且自动提交/回滚的 SQLAlchemy 事务。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级 Session factory，不在构造阶段占用连接。"""
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[TaskRepository]:
        """用 ``sessionmaker.begin`` 将单次用例限制在一个数据库事务中。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyTaskRepository(session)
