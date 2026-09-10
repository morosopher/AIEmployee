"""用 SQLAlchemy 实现 Redis 延迟调度丢失后的任务重试恢复。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import String, and_, cast, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql.elements import ColumnElement

from ai_employee.application.use_cases.privacy import (
    PRIVACY_DELETION_STARTED_EVENT_TYPE,
    PRIVACY_DELETION_STARTED_SCHEMA_VERSION,
)
from ai_employee.application.use_cases.task_execution import utc_instant
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.task_execution import (
    load_deletion_started_authority,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyTaskRetryRecoveryStore:
    """以短事务和行锁把到期重试安全恢复到既有 Outbox relay 路径。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级会话工厂，不在构造阶段占用数据库连接。"""
        self._session_factory = session_factory

    async def recover_due(self, *, now: datetime, limit: int) -> int:
        """锁定失去 Redis 投递的任务并原子创建新的未发布执行事件。

        ``FOR UPDATE SKIP LOCKED`` 允许多个 scheduler 实例分担积压而不重复处理同一行。
        limit 前按 active-M1 / inactive 精确赢家的互斥联合预筛，并排除当前桶已发布事实，
        防止 inactive 零变化前缀永久占用全部名额。预筛不能充当授权：逐行锁定 TaskRun
        后再锁 user，并用共享 parser 重读完整 per-user authority 集。
        选择条件还要求不存在未发布的 ``task.execute`` Outbox：正常 relay 即使长时间停在
        PostgreSQL 与 Redis 的交接中，已有事实仍会自行重试，扫描器不得抢先复制。状态、
        恢复期限清除、审计与补发 Outbox 在一个事务中提交；Redis 仅在后续 relay 成功后承载
        任务 UUID。每个恢复事件的去重键使用持久到期瞬间，故崩溃重扫仍指向同一事实。
        """
        now = utc_instant(now, field="now")
        # QUEUED 没有租约，因此用五分钟静默窗口区分 relay 正常交接和 Redis 已丢失的
        # 消息；RUNNING 则以租约到期作为明确的崩溃恢复信号。
        stale_queued_at = now - timedelta(minutes=5)
        async with self._session_factory.begin() as session:
            tasks = (
                await session.scalars(
                    select(TaskRunModel)
                    .join(UserModel, UserModel.id == TaskRunModel.user_id)
                    .where(
                        or_(
                            and_(
                                UserModel.is_active.is_(True),
                                or_(
                                    and_(
                                        TaskRunModel.status == TaskStatus.RETRY_SCHEDULED.value,
                                        TaskRunModel.retry_recovery_at.is_not(None),
                                        TaskRunModel.retry_recovery_at <= now,
                                    ),
                                    and_(
                                        TaskRunModel.status == TaskStatus.QUEUED.value,
                                        TaskRunModel.updated_at <= stale_queued_at,
                                    ),
                                    and_(
                                        TaskRunModel.status == TaskStatus.RUNNING.value,
                                        TaskRunModel.lease_expires_at.is_not(None),
                                        TaskRunModel.lease_expires_at <= now,
                                    ),
                                ),
                            ),
                            _inactive_deletion_candidate(now),
                        ),
                        ~exists(
                            select(OutboxEventModel.id).where(
                                OutboxEventModel.aggregate_id == TaskRunModel.id,
                                OutboxEventModel.topic == "task.execute",
                                OutboxEventModel.published_at.is_(None),
                            )
                        ),
                    )
                    .order_by(TaskRunModel.retry_recovery_at, TaskRunModel.id)
                    .limit(limit)
                    .with_for_update(of=TaskRunModel, skip_locked=True)
                )
            ).all()
            recovered = 0
            for task in tasks:
                # 显式指定 OF TaskRun 后再锁 user，不能让 JOIN 的隐式双表锁改变屏障锁序。
                user = await session.scalar(
                    select(UserModel).where(UserModel.id == task.user_id).with_for_update()
                )
                if user is None:
                    continue
                if not user.is_active:
                    if await load_deletion_started_authority(session, task=task) is None:
                        continue
                    # winner 保持 RUNNING 和原过期 owner/expiry；只用标识符请求重新接管。
                    # ON CONFLICT 不抛出事务失败，并仅把实际插入的一行计入处理数量。
                    inserted = await session.scalar(
                        insert(OutboxEventModel)
                        .values(
                            topic="task.execute",
                            aggregate_id=task.id,
                            deduplication_key=(
                                f"task.execute:{task.id}:inactive-deletion-recovery:"
                                f"{_five_minute_bucket(now)}"
                            ),
                            payload={"task_id": str(task.id)},
                            available_at=now,
                        )
                        .on_conflict_do_nothing(index_elements=[OutboxEventModel.deduplication_key])
                        .returning(OutboxEventModel.id)
                    )
                    recovered += int(inserted is not None)
                    continue
                recovery_at = task.retry_recovery_at
                is_retry_recovery = task.status == TaskStatus.RETRY_SCHEDULED.value
                if is_retry_recovery:
                    if recovery_at is None:
                        raise RuntimeError("locked retry task has no recovery deadline")
                    # 仅重试恢复路径在锁定行中要求截止时间；在此处收窄后，去重键可稳定
                    # 绑定原始到期瞬间，避免后续清空 ORM 字段造成重复投递事实。
                    retry_recovery_at: datetime = recovery_at
                    deduplication_key = (
                        f"task.execute:{task.id}:retry-recovery:{retry_recovery_at.isoformat()}"
                    )
                else:
                    deduplication_key = (
                        f"task.execute:{task.id}:recovery:{_five_minute_bucket(now)}"
                    )
                task.status = TaskStatus.QUEUED.value
                task.lease_owner = None
                task.lease_expires_at = None
                task.retry_recovery_at = None
                task.updated_at = now
                session.add_all(
                    (
                        AuditEventModel(
                            user_id=task.user_id,
                            task_id=task.id,
                            event_type="task.queued",
                            actor_type="system",
                            actor_id=None,
                            event_metadata={"reason": "task_recovery"},
                        ),
                        OutboxEventModel(
                            topic="task.execute",
                            aggregate_id=task.id,
                            deduplication_key=deduplication_key,
                            payload={"task_id": str(task.id)},
                        ),
                    )
                )
                recovered += 1
            await session.flush()
            return recovered


def _inactive_deletion_candidate(now: datetime) -> ColumnElement[bool]:
    """只为扫描公平性构造精确赢家 SQL 预筛；锁后共享 parser 仍是最终权威。

    关闭 JSON 对象通过整个 JSONB 相等验证，不能仅提取一个字符串而忽略多余键或
    非字符串请求值。authority 计数按完整 per-user 集计算，防止隐藏另一个竞争者。
    """
    request = TaskRunModel.input_payload["deletion_request_id"]
    authority_count = (
        select(func.count())
        .select_from(AuditEventModel)
        .where(
            AuditEventModel.user_id == TaskRunModel.user_id,
            AuditEventModel.event_type == PRIVACY_DELETION_STARTED_EVENT_TYPE,
        )
        .correlate(TaskRunModel)
        .scalar_subquery()
    )
    bucket_key = (
        literal("task.execute:")
        + cast(TaskRunModel.id, String)
        + literal(f":inactive-deletion-recovery:{_five_minute_bucket(now)}")
    )
    return and_(
        UserModel.is_active.is_(False),
        TaskRunModel.kind == "privacy.delete_all_data",
        TaskRunModel.status == TaskStatus.RUNNING.value,
        TaskRunModel.started_at.is_not(None),
        TaskRunModel.attempt_count > 0,
        TaskRunModel.lease_expires_at.is_not(None),
        TaskRunModel.lease_expires_at <= now,
        func.jsonb_typeof(request) == "string",
        request.astext != "",
        TaskRunModel.input_payload == func.jsonb_build_object("deletion_request_id", request),
        ~exists(select(ToolExecutionModel.id).where(ToolExecutionModel.task_id == TaskRunModel.id)),
        authority_count == 1,
        exists(
            select(AuditEventModel.id).where(
                AuditEventModel.user_id == TaskRunModel.user_id,
                AuditEventModel.task_id == TaskRunModel.id,
                AuditEventModel.event_type == PRIVACY_DELETION_STARTED_EVENT_TYPE,
                AuditEventModel.event_metadata
                == func.jsonb_build_object(
                    "schema_version",
                    PRIVACY_DELETION_STARTED_SCHEMA_VERSION,
                    "request_id",
                    request,
                ),
            )
        ),
        ~exists(
            select(OutboxEventModel.id).where(OutboxEventModel.deduplication_key == bucket_key)
        ),
    )


def _five_minute_bucket(now: datetime) -> str:
    """返回 UTC 五分钟桶，用于同一扫描窗口内的恢复 Outbox 幂等键。"""
    normalized = now.astimezone(UTC)
    return normalized.replace(
        minute=normalized.minute - normalized.minute % 5,
        second=0,
        microsecond=0,
    ).isoformat()
