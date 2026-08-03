"""定义可信任务、步骤、审批、工具执行、审计与 Outbox 的 ORM 映射。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_employee.domain.tasks import ApprovalStatus, JsonValue, StepStatus, TaskStatus
from ai_employee.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TaskRunModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存可恢复任务的当前快照与租约状态。

    ``user_id`` 与幂等键共同定义创建边界，避免不同用户恰好复用同一客户端键时互相
    看见任务。重试引用同时携带当前任务的 ``user_id``，组合外键阻止跨用户串联；原有
    单列 ``SET NULL`` 外键仍负责保留策略清理，组合 guard 延迟到事务结束校验，使 action
    trigger 先完成且跨用户错配仍无法提交。状态列保存领域枚举的稳定英文 ``value``，不使用
    PostgreSQL enum，以便后续通过向前兼容迁移扩展状态。
    """

    __tablename__ = "task_runs"
    __table_args__ = (
        Index(
            "ix_task_runs_approval_checkpoint_recovery",
            "approval_checkpoint_recovery_at",
            "id",
            postgresql_where=text("approval_checkpoint_recovery_at IS NOT NULL"),
        ),
        UniqueConstraint("id", "user_id", name="uq_task_runs_id_user_id"),
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_task_runs_user_id_idempotency_key",
        ),
        ForeignKeyConstraint(
            ["retry_of_task_id", "user_id"],
            ["task_runs.id", "task_runs.user_id"],
            name="fk_task_runs_retry_of_task_id_user_id",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    retry_of_task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TaskStatus.CREATED.value,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    input_payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB(), nullable=False)
    result_payload: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    graph_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    retry_recovery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    approval_checkpoint_recovery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskStepModel(UUIDPrimaryKeyMixin, Base):
    """保存 TaskRun 内按稳定序号排列的节点级执行时间线。

    步骤随父任务级联清理；``(id, task_id)`` 组合唯一键为审批与工具的归属 guard 提供
    可引用目标。``output_summary``、错误与起止时间允许为空，因为 PENDING 步骤尚未产生
    执行结果。唯一序号约束让至少一次节点重放不能制造同一位置的第二条事实。
    """

    __tablename__ = "task_steps"
    __table_args__ = (
        UniqueConstraint("id", "task_id", name="uq_task_steps_id_task_id"),
        UniqueConstraint("task_id", "sequence", name="uq_task_steps_task_id_sequence"),
    )

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer(), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=StepStatus.PENDING.value,
    )
    input_summary: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB(),
        nullable=False,
        default=dict,
    )
    output_summary: Mapped[dict[str, JsonValue] | None] = mapped_column(
        JSONB(),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalRequestModel(UUIDPrimaryKeyMixin, Base):
    """冻结某个任务步骤的精确工具载荷及人工决定生命周期。

    待审批记录尚无决定时间和决定人，因此二者允许为空；版本、哈希与过期时间必须始终
    存在，后续审批用例才能拒绝过期、篡改或陈旧决定。删除父任务或步骤会清理审批记录；
    组合外键额外保证 ``step_id`` 必须属于同一个 ``task_id``，并延迟到事务结束校验，避免
    阻断既有单列级联 trigger。删除决定人身份时只置空引用，以保留不含个人资料的审批事实。
    """

    __tablename__ = "approval_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["step_id", "task_id"],
            ["task_steps.id", "task_steps.task_id"],
            name="fk_approval_requests_step_id_task_id",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB(), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_markdown: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ApprovalStatus.PENDING.value,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class ToolExecutionModel(UUIDPrimaryKeyMixin, Base):
    """记录一次可幂等重放的工具调用及标准化结果摘要。

    全局幂等键约束在数据库层阻止队列重投或 Graph 恢复创建第二次工具事实；组合外键
    保证 ``step_id`` 属于记录声明的 ``task_id``，并延迟到事务结束校验，避免阻断既有单列
    级联 trigger。供应商请求标识、结果和错误在调用结束前可能未知，因此允许为空；完整
    供应商响应不得写入本表。
    """

    __tablename__ = "tool_executions"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_tool_executions_idempotency_key",
        ),
        ForeignKeyConstraint(
            ["step_id", "task_id"],
            ["task_steps.id", "task_steps.task_id"],
            name="fk_tool_executions_step_id_task_id",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_summary: Mapped[dict[str, JsonValue] | None] = mapped_column(
        JSONB(),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class AuditEventModel(Base):
    """保存只追加的用户与任务审计事件。

    ``task_id`` 允许为空以支持身份或隐私删除等不再关联具体任务的事件，也在任务按保留
    策略清理时通过 ``SET NULL`` 保留审计事实。组合外键在任务存在时强制其与 ``user_id``
    归属一致；它延迟到事务结束校验，让单列 ``SET NULL`` trigger 先完成，任务清理后仍
    保留审计用户。Python 属性使用 ``event_metadata``，因为 SQLAlchemy 声明式基类保留
    ``metadata``；数据库列仍严格命名为 ``metadata``。本模型不提供任何更新或删除方法。
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_task_id_id", "task_id", "id"),
        ForeignKeyConstraint(
            ["task_id", "user_id"],
            ["task_runs.id", "task_runs.user_id"],
            name="fk_audit_events_task_id_user_id",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger(), Identity(), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_metadata: Mapped[dict[str, JsonValue]] = mapped_column(
        "metadata",
        JSONB(),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class OutboxEventModel(UUIDPrimaryKeyMixin, Base):
    """保存提交后才能投递的持久队列事件。

    Outbox 不声明到聚合表的外键，使已发布事件可以在业务聚合清理后按独立保留策略保存。
    部分索引只覆盖未发布事实，并按 ``available_at, id`` 对齐 relay 的到期扫描与稳定锁定
    顺序。``available_at`` 由 PostgreSQL 记录首次可投递时刻；失败重试只修改尝试次数、
    下一可用时间与脱敏错误，不把 Redis 消息当作唯一业务事实。
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint(
            "deduplication_key",
            name="uq_outbox_events_deduplication_key",
        ),
        Index(
            "ix_outbox_events_unpublished_available_at_id",
            "available_at",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB(), nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
