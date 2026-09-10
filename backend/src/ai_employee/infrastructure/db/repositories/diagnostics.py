"""实现逾期简报诊断所需的 PostgreSQL 派生读取与无内容结果写入。"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select, update

from ai_employee.application.use_cases.diagnostics import ActiveUserOverdueBriefSchedule
from ai_employee.infrastructure.db.models.briefs import DailyBriefModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel, SyncCursorModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyOverdueBriefReader:
    """从 PostgreSQL 读取活动用户计划与同一用户的简报完整度。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存共享只读会话工厂，不把 ORM 实体泄露至应用层。"""
        self._session_factory = session_factory

    async def list_active(self) -> tuple[ActiveUserOverdueBriefSchedule, ...]:
        """按 UUID 稳定顺序返回当前活动用户的逾期判断所需偏好。"""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(UserModel.id, UserModel.timezone, UserModel.brief_time)
                    .where(UserModel.is_active.is_(True))
                    .order_by(UserModel.id)
                )
            ).all()
        return tuple(
            ActiveUserOverdueBriefSchedule(
                user_id=row.id, timezone=row.timezone, brief_time=row.brief_time
            )
            for row in rows
        )

    async def list_brief_completeness(
        self, *, user_id: UUID, local_date: date
    ) -> tuple[str, ...]:
        """只读取目标用户本地日期的完整度，避免跨用户简报影响告警。"""
        async with self._session_factory() as session:
            values = (
                await session.scalars(
                    select(DailyBriefModel.completeness).where(
                        DailyBriefModel.user_id == user_id,
                        DailyBriefModel.local_date == local_date,
                    )
                )
            ).all()
        return tuple(values)

    async def get_active_schedule(
        self, *, user_id: UUID
    ) -> ActiveUserOverdueBriefSchedule | None:
        """读取已认证用户自己的活动计划，避免系统告警查询暴露其他用户偏好。

        Args:
            user_id: 认证边界提供的拥有者标识，SQL 条件必须始终携带该值。

        Returns:
            活动用户的最小计划快照；用户不存在或已停用时返回 ``None``。
        """
        async with self._session_factory() as session:
            row = await session.execute(
                select(UserModel.id, UserModel.timezone, UserModel.brief_time).where(
                    UserModel.id == user_id,
                    UserModel.is_active.is_(True),
                )
            )
            schedule = row.one_or_none()
        if schedule is None:
            return None
        return ActiveUserOverdueBriefSchedule(
            user_id=schedule.id,
            timezone=schedule.timezone,
            brief_time=schedule.brief_time,
        )

    async def get_diagnostic_task_id(
        self, *, user_id: UUID, local_date: date
    ) -> UUID | None:
        """读取同一用户当日本地诊断任务，任务存在与否不改变逾期判断。

        Args:
            user_id: 已认证用户，必须与任务拥有者匹配。
            local_date: 用户 IANA 时区计算出的业务日期。

        Returns:
            按稳定幂等键创建的诊断任务 UUID；Scheduler 尚未执行时返回 ``None``。
        """
        idempotency_key = f"diagnostic:daily-brief:{user_id}:{local_date.isoformat()}"
        async with self._session_factory() as session:
            return await session.scalar(
                select(TaskRunModel.id).where(
                    TaskRunModel.user_id == user_id,
                    TaskRunModel.kind == "brief.overdue_diagnostic",
                    TaskRunModel.idempotency_key == idempotency_key,
                )
            )


class SqlAlchemyDiagnosticSnapshotStore:
    """采集诊断任务允许保存的来源新鲜度与失败任务标识，绝不读取来源内容。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存由 Worker 生命周期管理的数据库会话工厂。"""
        self._session_factory = session_factory

    async def save_snapshot(self, *, task_id: UUID, user_id: UUID, now: datetime) -> None:
        """原子写入不含邮件或日程内容的诊断摘要。

        Args:
            task_id: 当前持久诊断任务，更新同时携带用户条件防止错误归属。
            user_id: 任务所属用户，也是所有来源/失败任务查询的强制过滤条件。
            now: 诊断运行时刻，仅用于按最近失败排序的稳定边界。
        """
        del now
        async with self._session_factory.begin() as session:
            cursor_rows = (
                await session.execute(
                    select(
                        SyncCursorModel.resource_kind,
                        SyncCursorModel.last_success_at,
                        SyncCursorModel.last_error_code,
                    )
                    .join(
                        OAuthConnectionModel,
                        OAuthConnectionModel.id == SyncCursorModel.connection_id,
                    )
                    .where(
                        OAuthConnectionModel.user_id == user_id,
                        OAuthConnectionModel.provider.in_(("google", "microsoft")),
                    )
                    .order_by(SyncCursorModel.resource_kind, SyncCursorModel.id)
                )
            ).all()
            failed_rows = (
                await session.execute(
                    select(TaskRunModel.id, TaskRunModel.error_code)
                    .where(
                        TaskRunModel.user_id == user_id,
                        TaskRunModel.kind == "daily_brief",
                        TaskRunModel.status == "failed",
                    )
                    .order_by(TaskRunModel.finished_at.desc(), TaskRunModel.id.desc())
                    .limit(10)
                )
            ).all()
            # 只持久化可操作的时间戳、任务 UUID 和稳定错误码；禁止把来源标题、正文或
            # provider cursor 写入诊断结果，以免诊断任务成为第二份敏感数据副本。
            payload = {
                "freshness": [
                    {
                        "resource": row.resource_kind,
                        "last_success_at": row.last_success_at.isoformat()
                        if row.last_success_at is not None
                        else None,
                        "error_code": row.last_error_code,
                    }
                    for row in cursor_rows
                ],
                "failed_generation_tasks": [
                    {"task_id": str(row.id), "error_code": row.error_code}
                    for row in failed_rows
                ],
            }
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
                .values(result_payload=payload)
            )
