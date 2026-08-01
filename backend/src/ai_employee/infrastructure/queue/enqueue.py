"""把应用层 task_id 映射为 Taskiq Redis Stream 消息。"""

from uuid import UUID

from ai_employee.workers.execute_task import execute_task


class TaskiqTaskEnqueuer:
    """只投递任务 UUID 的 Taskiq 队列适配器。

    PostgreSQL 保存输入、状态与结果，Redis Stream 只携带可重建标识。适配器不配置或
    查询 result backend；至少一次重复消息由数据库租约和终态条件保护。
    """

    async def enqueue(self, task_id: UUID) -> None:
        """调用 Taskiq decorated task 的 ``kiq`` 发送规范 UUID 字符串。

        Args:
            task_id: 已经由 Outbox claim 的持久任务标识。

        Raises:
            Exception: Taskiq/Redis enqueue 失败原样交给 Outbox relay，以便在新事务中记录
                固定安全错误码和下一次退避时间。
        """
        await execute_task.kiq(str(task_id))
