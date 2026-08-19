"""定义任务创建用例、稳定返回值与事务型 Repository 端口。"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ai_employee.domain.tasks import JsonValue, TaskStatus


@dataclass(frozen=True, slots=True)
class CreateTaskResult:
    """返回已提交或按幂等键复用的稳定任务标识与当前持久状态。"""

    task_id: UUID
    status: TaskStatus = TaskStatus.CREATED


@dataclass(frozen=True, slots=True)
class CreateTaskBatchItem:
    """描述一个必须与同批任务共同提交或回滚的创建意图。"""

    kind: str
    input_payload: dict[str, JsonValue]
    idempotency_key: str


class TaskDispatcher(Protocol):
    """定义创建事务提交后立即 claim 并投递任务的应用端口。"""

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """尝试投递指定任务，并返回投递尝试后的 PostgreSQL 状态。

        队列不可用属于适配器内部可恢复故障；实现必须保留未发布 Outbox 并返回
        ``QUEUED``，不能把 Redis 可用性变成任务创建事务是否成功的条件。
        """


class TaskRepository(Protocol):
    """定义一次任务创建事务内所需的最小持久化能力。"""

    async def get_existing(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> CreateTaskResult | None:
        """读取同键任务，并把键绑定到精确 kind 与完整 JSON 输入。

        Args:
            user_id: 当前认证用户，查询必须显式隔离该值。
            kind: 调用方期望的稳定任务种类。
            input_payload: 调用方期望的完整规范任务输入。
            idempotency_key: 当前用户范围内的任务创建键。

        Returns:
            键不存在时返回 ``None``；精确命中时返回稳定任务标识。

        Raises:
            StateConflictError: 同键已经绑定其他 kind 或输入。
        """

    async def create_with_outbox(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> CreateTaskResult:
        """创建任务、审计和初始 Outbox，或返回同用户已有任务。"""

    async def create_many_with_outbox(
        self, *, user_id: UUID, items: tuple[CreateTaskBatchItem, ...]
    ) -> tuple[CreateTaskResult, ...]:
        """在一个事务中创建整批任务，任一项失败必须回滚全部事实。"""


class TaskRepositoryFactory(Protocol):
    """为一次任务用例提供自动提交或回滚的 Repository 事务上下文。"""

    def __call__(self) -> AbstractAsyncContextManager[TaskRepository]:
        """创建一个只覆盖当前用例调用的事务上下文。"""


class CreateTaskUseCase:
    """以一个明确事务原子创建任务、审计事件和初始 Outbox 事件。"""

    def __init__(
        self,
        repositories: TaskRepositoryFactory,
        dispatcher: TaskDispatcher,
    ) -> None:
        """注入不暴露 SQLAlchemy 的任务 Repository 事务工厂。

        Args:
            repositories: 每次调用创建独立提交/回滚边界的工厂。
            dispatcher: 必需的提交后立即投递端口。纯持久化测试也必须注入明确 Fake，
                以证明所有构造点都遵守同一 claim/transition/enqueue 路径。
        """
        self._repositories = repositories
        self._dispatcher = dispatcher

    async def execute(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> CreateTaskResult:
        """幂等创建一项待执行任务，并在事务提交后返回稳定 UUID。

        应用层通过抽象上下文明确拥有事务边界；具体适配器使用
        ``async_sessionmaker.begin()`` 实现提交与异常回滚。Repository 不自行提交，因而
        TaskRun、AuditEvent 与 OutboxEvent 不会出现部分可见状态。

        Args:
            user_id: 任务所属用户，参与所有幂等查询的隔离条件。
            kind: 稳定英文任务种类。
            input_payload: 已规范化且只含内部 JSON 值的任务输入。
            idempotency_key: 当前用户范围内稳定的创建幂等键。

        Returns:
            新建任务或同键已有任务的 UUID，不暴露 ORM 对象。
        """
        async with self._repositories() as repository:
            result = await repository.create_with_outbox(
                user_id=user_id,
                kind=kind,
                input_payload=input_payload,
                idempotency_key=idempotency_key,
            )

        # 外部队列 I/O 必须发生在创建事务提交之后。dispatcher 自身先用新事务 claim，
        # 即使 Redis 失败也会返回已经持久化的 QUEUED 状态并把未发布事实留给 minute relay。
        status = await self._dispatcher.dispatch(result.task_id)
        return CreateTaskResult(task_id=result.task_id, status=status)

    async def replay(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> CreateTaskResult | None:
        """在业务前置条件变化前识别一个已持久化的精确任务重放。

        该读取只认当前用户、任务种类和完整 JSON 输入都一致的已有事实；键已绑定到
        其他任务时 Repository 抛 ``idempotency_key_payload_mismatch``，不存在时返回
        ``None``，调用方才继续执行当前资源版本或能力校验。精确命中仍复用统一 dispatcher，
        保持与 :meth:`execute` 顺序重放相同的 PostgreSQL 恢复语义。

        Args:
            user_id: 当前认证用户。
            kind: 期望任务种类。
            input_payload: 期望的完整规范任务输入。
            idempotency_key: 用户范围内的任务创建键。

        Returns:
            精确已有任务的稳定结果；当前键尚无事实时返回 ``None``。

        Raises:
            StateConflictError: 同键已绑定到不同 kind 或输入。
        """
        async with self._repositories() as repository:
            existing = await repository.get_existing(
                user_id=user_id,
                kind=kind,
                input_payload=input_payload,
                idempotency_key=idempotency_key,
            )
        if existing is None:
            return None
        status = await self._dispatcher.dispatch(existing.task_id)
        return CreateTaskResult(task_id=existing.task_id, status=status)

    async def execute_many(
        self, *, user_id: UUID, items: tuple[CreateTaskBatchItem, ...]
    ) -> tuple[CreateTaskResult, ...]:
        """原子创建一批任务，提交后才分别投递每个已持久化任务。

        Args:
            user_id: 该批全部任务的拥有者，参与每项幂等隔离。
            items: 至少一项、每项包含种类、内部输入和稳定幂等键的创建意图。

        Returns:
            与输入相同顺序的稳定任务结果；每项均已完成提交后的首次投递尝试。

        Raises:
            ValueError: 调用方传入空批次时抛出，避免模糊的无副作用成功。
        """
        if not items:
            raise ValueError("items must not be empty")
        async with self._repositories() as repository:
            created = await repository.create_many_with_outbox(user_id=user_id, items=items)
        dispatched: list[CreateTaskResult] = []
        for result in created:
            dispatched.append(
                CreateTaskResult(
                    task_id=result.task_id,
                    status=await self._dispatcher.dispatch(result.task_id),
                )
            )
        return tuple(dispatched)
