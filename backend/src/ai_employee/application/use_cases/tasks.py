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


class TaskDispatcher(Protocol):
    """定义创建事务提交后立即 claim 并投递任务的应用端口。"""

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """尝试投递指定任务，并返回投递尝试后的 PostgreSQL 状态。

        队列不可用属于适配器内部可恢复故障；实现必须保留未发布 Outbox 并返回
        ``QUEUED``，不能把 Redis 可用性变成任务创建事务是否成功的条件。
        """


class TaskRepository(Protocol):
    """定义一次任务创建事务内所需的最小持久化能力。"""

    async def create_with_outbox(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> CreateTaskResult:
        """创建任务、审计和初始 Outbox，或返回同用户已有任务。"""


class TaskRepositoryFactory(Protocol):
    """为一次任务用例提供自动提交或回滚的 Repository 事务上下文。"""

    def __call__(self) -> AbstractAsyncContextManager[TaskRepository]:
        """创建一个只覆盖当前用例调用的事务上下文。"""


class CreateTaskUseCase:
    """以一个明确事务原子创建任务、审计事件和初始 Outbox 事件。"""

    def __init__(
        self,
        repositories: TaskRepositoryFactory,
        dispatcher: TaskDispatcher | None = None,
    ) -> None:
        """注入不暴露 SQLAlchemy 的任务 Repository 事务工厂。

        Args:
            repositories: 每次调用创建独立提交/回滚边界的工厂。
            dispatcher: 可选的提交后立即投递端口。Task 6 的纯持久化测试可以省略；
                API 与 Scheduler 的运行时组合必须注入同一个 Outbox relay 路径。
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
        if self._dispatcher is None:
            return result
        status = await self._dispatcher.dispatch(result.task_id)
        return CreateTaskResult(task_id=result.task_id, status=status)
