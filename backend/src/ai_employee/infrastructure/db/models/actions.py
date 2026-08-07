"""定义 M2 本地加密邮件草稿与日历变更提案的 ORM 映射。

父实体保存可变的当前版本、状态与保留截止时间；版本/快照子实体保存不可变内容事实。
所有实体都直接记录非空 ``user_id``，并通过组合外键把连接或父实体绑定到同一用户。
邮件正文与日历快照只能写入 AEAD 三元组，地址等非正文元数据才允许使用 JSONB。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_employee.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MailDraftModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存用户本地邮件草稿的可变聚合头。

    ``creation_idempotency_key`` 在用户范围内唯一，使重复创建请求复用同一草稿；对应的
    ``creation_payload_hash`` 绑定规范创建输入，调用方可拒绝同键异载荷。源线程和源邮件
    是由精确连接解释的供应商标识，只在回复模式出现。正文不保存在本表，而由不可变版本
    以 AEAD 密文保存；``retain_until`` 为后续保留清理提供显式 UTC 截止时间。
    """

    __tablename__ = "mail_drafts"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_mail_drafts_id_user_id"),
        UniqueConstraint(
            "user_id",
            "creation_idempotency_key",
            name="uq_mail_drafts_user_creation_idempotency_key",
        ),
        # 同时携带 connection_id 与 user_id，避免跨用户连接被拼接到草稿。
        ForeignKeyConstraint(
            ["connection_id", "user_id"],
            ["oauth_connections.id", "oauth_connections.user_id"],
            name="fk_mail_drafts_connection_user",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    connection_id: Mapped[UUID] = mapped_column(nullable=False)
    creation_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    creation_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    retain_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MailDraftVersionModel(UUIDPrimaryKeyMixin, Base):
    """保存草稿的一个不可变版本及字段级加密正文。

    地址 JSONB 只承载规范化地址元数据，绝不包含正文。正文 AEAD 三元组正常时全部存在，
    达到保留期限后可由清理任务原子置空；数据库检查禁止只残留密文、nonce 或 key version。
    ``(draft_id, version)`` 阻止重试创建第二份同版本事实，组合外键保证版本与草稿归属同一
    用户。Prompt 与模型字段允许为空，以支持纯人工编辑且不伪造模型来源。
    """

    __tablename__ = "mail_draft_versions"
    __table_args__ = (
        UniqueConstraint(
            "draft_id",
            "version",
            name="uq_mail_draft_versions_draft_version",
        ),
        ForeignKeyConstraint(
            ["draft_id", "user_id"],
            ["mail_drafts.id", "mail_drafts.user_id"],
            name="fk_mail_draft_versions_draft_user",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "(body_ciphertext IS NULL AND body_nonce IS NULL AND body_key_version IS NULL) "
            "OR (body_ciphertext IS NOT NULL AND body_nonce IS NOT NULL "
            "AND body_key_version IS NOT NULL)",
            name="ck_mail_draft_versions_body_aead_all_or_none",
        ),
        # BYTEA 的声明长度不会由 PostgreSQL 自动执行，必须显式检查 AEAD nonce 字节数。
        CheckConstraint(
            "body_nonce IS NULL OR octet_length(body_nonce) = 12",
            name="ck_mail_draft_versions_body_nonce_length_12",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    draft_id: Mapped[UUID] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    to_recipients: Mapped[list[dict[str, str]]] = mapped_column(JSONB(), nullable=False)
    cc_recipients: Mapped[list[dict[str, str]]] = mapped_column(JSONB(), nullable=False)
    bcc_recipients: Mapped[list[dict[str, str]]] = mapped_column(JSONB(), nullable=False)
    subject: Mapped[str] = mapped_column(Text(), nullable=False)
    body_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    body_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12), nullable=True)
    body_key_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CalendarChangeProposalModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存非重复日程创建、修改或恢复提案的可变聚合头。

    创建幂等键按用户隔离并绑定规范载荷哈希；精确连接和日历 ID 决定供应商写目标。
    修改/恢复提案可记录供应商目标事件与基础 ETag，创建提案则保持为空。敏感的完整期望
    状态和修改前快照不在本表出现，而由不可变加密 snapshot 保存。
    """

    __tablename__ = "calendar_change_proposals"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "user_id",
            name="uq_calendar_change_proposals_id_user_id",
        ),
        UniqueConstraint(
            "user_id",
            "creation_idempotency_key",
            name="uq_calendar_change_proposals_user_creation_idempotency_key",
        ),
        # 提案与连接必须由同一用户拥有，不能只依赖应用层过滤。
        ForeignKeyConstraint(
            ["connection_id", "user_id"],
            ["oauth_connections.id", "oauth_connections.user_id"],
            name="fk_calendar_change_proposals_connection_user",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    connection_id: Mapped[UUID] = mapped_column(nullable=False)
    creation_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    creation_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(512), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    retain_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalendarChangeSnapshotModel(UUIDPrimaryKeyMixin, Base):
    """保存日历提案某版本的不可变加密内容或修改前快照。

    ``snapshot_kind`` 区分期望状态与补偿所需的修改前事实；同一提案版本的同类快照只能
    存在一份。内容只允许进入 AEAD bytes，``canonical_hash`` 绑定规范明文，保留清理可将
    三元组整体置空但仍保留不含内容的审计骨架。组合外键阻止跨用户复用提案 ID。
    """

    __tablename__ = "calendar_change_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "version",
            "snapshot_kind",
            name="uq_calendar_change_snapshots_proposal_version_kind",
        ),
        ForeignKeyConstraint(
            ["proposal_id", "user_id"],
            ["calendar_change_proposals.id", "calendar_change_proposals.user_id"],
            name="fk_calendar_change_snapshots_proposal_user",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "(content_ciphertext IS NULL AND content_nonce IS NULL "
            "AND content_key_version IS NULL) OR (content_ciphertext IS NOT NULL "
            "AND content_nonce IS NOT NULL AND content_key_version IS NOT NULL)",
            name="ck_calendar_change_snapshots_content_aead_all_or_none",
        ),
        CheckConstraint(
            "content_nonce IS NULL OR octet_length(content_nonce) = 12",
            name="ck_calendar_change_snapshots_content_nonce_length_12",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    proposal_id: Mapped[UUID] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    snapshot_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    content_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12), nullable=True)
    content_key_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    retain_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
