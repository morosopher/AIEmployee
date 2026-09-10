"""把任务创建端口映射到 SQLAlchemy 与 PostgreSQL 事务。"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.tasks import (
    CreateTaskBatchItem,
    CreateTaskResult,
    TaskRepository,
)
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import JsonValue, TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
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

    async def get_existing(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> CreateTaskResult | None:
        """按用户与键读取任务，并验证它精确绑定当前 kind 和完整 JSON 输入。

        Args:
            user_id: 当前任务所属用户。
            kind: 调用方期望的稳定任务种类。
            input_payload: 调用方期望的完整 JSON 输入。
            idempotency_key: 当前用户范围内的创建键。

        Returns:
            键不存在时返回 ``None``；精确命中时返回稳定任务 ID。

        Raises:
            StateConflictError: 键已绑定到不同 kind 或输入。
            ValueError: 输入含 PostgreSQL JSONB 无法表达的非有限数字。
        """
        existing = await self._session.scalar(
            select(TaskRunModel).where(
                TaskRunModel.user_id == user_id,
                TaskRunModel.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            return None
        _require_matching_task_intent(
            existing=existing,
            kind=kind,
            input_payload=input_payload,
        )
        return CreateTaskResult(task_id=existing.id)

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
        existing = await self.get_existing(
            user_id=user_id,
            kind=kind,
            input_payload=input_payload,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing

        # 新任务尚无 TaskRun 可锁；在任何 INSERT 之前锁拥有者，和 privacy 屏障串行。
        # 既有幂等结果上方只读复用；不能以新的客户端键在匿名根行下重建业务图。
        active = await self._session.scalar(
            select(UserModel.is_active)
            .where(
                UserModel.id == user_id,
            )
            .with_for_update()
        )
        if active is not True:
            raise StateConflictError(error_code="user_inactive", message="User is inactive")
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
            # 输家必须重新比较赢家的 kind/input；仅按键返回 ID 会把并发异载荷伪装成重放。
            existing = await self.get_existing(
                user_id=user_id,
                kind=kind,
                input_payload=input_payload,
                idempotency_key=idempotency_key,
            )
            if existing is None:
                raise RuntimeError("task idempotency winner is not visible after conflict")
            return existing

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

    async def create_many_with_outbox(
        self, *, user_id: UUID, items: tuple[CreateTaskBatchItem, ...]
    ) -> tuple[CreateTaskResult, ...]:
        """在调用方已打开的单一事务中创建整批任务、审计与 Outbox。

        本方法复用单项幂等创建的数据库约束；不自行提交，因此第二项任何持久化异常都会
        由外围 ``sessionmaker.begin`` 回滚之前所有 TaskRun、AuditEvent 和 OutboxEvent。
        """
        results: list[CreateTaskResult] = []
        for item in items:
            results.append(
                await self.create_with_outbox(
                    user_id=user_id,
                    kind=item.kind,
                    input_payload=item.input_payload,
                    idempotency_key=item.idempotency_key,
                )
            )
        return tuple(results)


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


def _require_matching_task_intent(
    *,
    existing: TaskRunModel,
    kind: str,
    input_payload: dict[str, JsonValue],
) -> None:
    """以类型敏感的规范 JSON 比较任务输入，拒绝同键跨 kind 或异载荷复用。

    不能直接依赖 Python 容器相等，因为 ``True == 1`` 会把 JSON boolean 与 number 错误
    视为相同。规范序列化同时消除对象键顺序差异，并与 JSONB 的结构语义保持一致。

    Args:
        existing: 已由用户和幂等键定位的持久任务。
        kind: 当前调用方期望的稳定任务种类。
        input_payload: 当前调用方期望的完整 JSON 输入。

    Raises:
        StateConflictError: 已有任务 kind 或输入与当前意图不完全一致。
        ValueError: 任一输入包含 JSON 无法表达的非有限数字。
    """
    existing_payload = json.dumps(
        existing.input_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    requested_payload = json.dumps(
        input_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if existing.kind != kind or existing_payload != requested_payload:
        raise StateConflictError(
            error_code="idempotency_key_payload_mismatch",
            message="idempotency key is already bound to a different task intent",
        )
