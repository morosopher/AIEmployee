"""固定合成历史数据：两个用户、并列微秒、已知及未知用途，不持久化正文。"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

BASE_TIME = datetime(2029, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class HistoryDataset:
    """隔离断言使用的合成用户和所属任务标识；任务按创建顺序升序排列。"""

    owner_id: UUID
    other_id: UUID
    owner_task_ids: tuple[UUID, ...]


async def seed_history_dataset(session_factory: ManagedAsyncSessionMaker) -> HistoryDataset:
    """在独立写事务创建25条所属任务及异用户失败任务，供只读快照测试。

    Args:
        session_factory: 受管临时数据库工厂，清理由外层生命周期负责。
    Returns:
        固定标识集合；每五条轮换用途，前三条时间相同以验证UUID排序。
    """
    dataset = HistoryDataset(
        UUID(int=1001), UUID(int=1002), tuple(UUID(int=2000 + i) for i in range(25))
    )
    async with session_factory.begin() as session:
        session.add_all(
            UserModel(
                id=user,
                email=f"history-{user.int}@example.test",
                display_name="合成用户",
                timezone="UTC",
                brief_time=time(9),
            )
            for user in (dataset.owner_id, dataset.other_id)
        )
        await session.flush()
        for index, task_id in enumerate(dataset.owner_task_ids):
            session.add(
                TaskRunModel(
                    id=task_id,
                    user_id=dataset.owner_id,
                    kind=(
                        "daily_brief",
                        "conversation.respond",
                        "sync_mail",
                        "sync_calendar",
                        "legacy.synthetic",
                    )[index % 5],
                    status="failed" if index % 2 == 0 else "succeeded",
                    idempotency_key=f"history-{index}",
                    input_payload={},
                    created_at=BASE_TIME + timedelta(microseconds=max(0, index - 2)),
                )
            )
        session.add(
            TaskRunModel(
                id=UUID(int=3000),
                user_id=dataset.other_id,
                kind="sync_mail",
                status="failed",
                idempotency_key="other-history",
                input_payload={},
                created_at=BASE_TIME + timedelta(days=1),
            )
        )
    return dataset
