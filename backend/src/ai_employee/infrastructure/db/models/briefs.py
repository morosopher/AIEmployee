"""定义每日简报、会话与模型调用的 PostgreSQL 映射。"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_employee.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ConversationModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存用户拥有的轻量对话容器，消息通过外键级联清理。"""
    __tablename__ = "conversations"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)


class MessageModel(UUIDPrimaryKeyMixin, Base):
    """保存单条 Markdown 消息；内容仅供该用户本人读取。"""
    __tablename__ = "messages"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text(), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailyBriefModel(UUIDPrimaryKeyMixin, Base):
    """保存一个本地日期的某个简报版本及渲染前结构化结果。"""
    __tablename__ = "daily_briefs"
    __table_args__ = (UniqueConstraint("user_id", "local_date", "version", name="uq_daily_briefs_user_date_version"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    local_date: Mapped[date] = mapped_column(Date(), nullable=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("task_runs.id", ondelete="RESTRICT"), nullable=False)
    completeness: Mapped[str] = mapped_column(String(16), nullable=False)
    source_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    headline: Mapped[str] = mapped_column(Text(), nullable=False)
    structured_content: Mapped[dict[str, object]] = mapped_column(JSONB(), nullable=False)
    markdown: Mapped[str] = mapped_column(Text(), nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSONB(), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailyBriefItemModel(UUIDPrimaryKeyMixin, Base):
    """保存简报中的稳定顺序条目及其最小来源引用。"""
    __tablename__ = "daily_brief_items"
    __table_args__ = (UniqueConstraint("brief_id", "position", name="uq_daily_brief_items_brief_position"),)
    brief_id: Mapped[UUID] = mapped_column(ForeignKey("daily_briefs.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer(), nullable=False)
    section: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(Text(), nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text(), nullable=False)
    source_refs: Mapped[list[dict[str, object]]] = mapped_column(JSONB(), nullable=False)
    suggested_action_kind: Mapped[str | None] = mapped_column(String(100), nullable=True)


class LLMInvocationModel(UUIDPrimaryKeyMixin, Base):
    """保存不含 Prompt/输出正文的模型调用审计元数据。"""
    __tablename__ = "llm_invocations"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)
    step_id: Mapped[UUID | None] = mapped_column(ForeignKey("task_steps.id", ondelete="SET NULL"), nullable=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_schema: Mapped[str] = mapped_column(String(255), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    estimated_cost_microusd: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
