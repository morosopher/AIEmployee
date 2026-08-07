"""创建 M2 加密草稿、日历提案并扩展可信审批与工具执行。

Revision ID: 20260806_0012
Revises: 20260806_0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260806_0012"
down_revision: str | Sequence[str] | None = "20260806_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_mail_draft_tables() -> None:
    """创建用户作用域幂等草稿及不可变加密版本。"""
    op.create_table(
        "mail_drafts",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("creation_idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("creation_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("source_thread_id", sa.String(length=255), nullable=True),
        sa.Column("source_message_id", sa.String(length=255), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        # 连接和草稿直接用户归属必须同时匹配，应用层过滤不是唯一防线。
        sa.ForeignKeyConstraint(
            ["connection_id", "user_id"],
            ["oauth_connections.id", "oauth_connections.user_id"],
            name="fk_mail_drafts_connection_user",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="uq_mail_drafts_id_user_id"),
        sa.UniqueConstraint(
            "user_id",
            "creation_idempotency_key",
            name="uq_mail_drafts_user_creation_idempotency_key",
        ),
    )
    op.create_table(
        "mail_draft_versions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("to_recipients", postgresql.JSONB(), nullable=False),
        sa.Column("cc_recipients", postgresql.JSONB(), nullable=False),
        sa.Column("bcc_recipients", postgresql.JSONB(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("body_nonce", sa.LargeBinary(length=12), nullable=True),
        sa.Column("body_key_version", sa.Integer(), nullable=True),
        sa.Column("prompt_version", sa.String(length=100), nullable=True),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(body_ciphertext IS NULL AND body_nonce IS NULL "
            "AND body_key_version IS NULL) OR (body_ciphertext IS NOT NULL "
            "AND body_nonce IS NOT NULL AND body_key_version IS NOT NULL)",
            name="ck_mail_draft_versions_body_aead_all_or_none",
        ),
        # PostgreSQL BYTEA 忽略声明长度，因此必须以 octet_length 强制 12-byte nonce。
        sa.CheckConstraint(
            "body_nonce IS NULL OR octet_length(body_nonce) = 12",
            name="ck_mail_draft_versions_body_nonce_length_12",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["draft_id", "user_id"],
            ["mail_drafts.id", "mail_drafts.user_id"],
            name="fk_mail_draft_versions_draft_user",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "draft_id",
            "version",
            name="uq_mail_draft_versions_draft_version",
        ),
    )


def _create_calendar_proposal_tables() -> None:
    """创建日历提案聚合头及不可变加密 snapshot。"""
    op.create_table(
        "calendar_change_proposals",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("creation_idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("creation_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("calendar_id", sa.String(length=512), nullable=False),
        sa.Column("operation_kind", sa.String(length=32), nullable=False),
        sa.Column("target_event_id", sa.String(length=255), nullable=True),
        sa.Column("base_etag", sa.String(length=255), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["connection_id", "user_id"],
            ["oauth_connections.id", "oauth_connections.user_id"],
            name="fk_calendar_change_proposals_connection_user",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "user_id",
            name="uq_calendar_change_proposals_id_user_id",
        ),
        sa.UniqueConstraint(
            "user_id",
            "creation_idempotency_key",
            name="uq_calendar_change_proposals_user_creation_idempotency_key",
        ),
    )
    op.create_table(
        "calendar_change_snapshots",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot_kind", sa.String(length=32), nullable=False),
        sa.Column("content_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("content_nonce", sa.LargeBinary(length=12), nullable=True),
        sa.Column("content_key_version", sa.Integer(), nullable=True),
        sa.Column("canonical_hash", sa.String(length=64), nullable=False),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(content_ciphertext IS NULL AND content_nonce IS NULL "
            "AND content_key_version IS NULL) OR (content_ciphertext IS NOT NULL "
            "AND content_nonce IS NOT NULL AND content_key_version IS NOT NULL)",
            name="ck_calendar_change_snapshots_content_aead_all_or_none",
        ),
        sa.CheckConstraint(
            "content_nonce IS NULL OR octet_length(content_nonce) = 12",
            name="ck_calendar_change_snapshots_content_nonce_length_12",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["proposal_id", "user_id"],
            ["calendar_change_proposals.id", "calendar_change_proposals.user_id"],
            name="fk_calendar_change_snapshots_proposal_user",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "proposal_id",
            "version",
            "snapshot_kind",
            name="uq_calendar_change_snapshots_proposal_version_kind",
        ),
    )


def _extend_approval_requests() -> None:
    """为 M2 冻结命令增加兼容 legacy 的可空 AEAD 与提案绑定列。"""
    op.add_column(
        "approval_requests",
        sa.Column("schema_version", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("risk_level", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("payload_nonce", sa.LargeBinary(length=12), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("payload_key_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("proposal_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("proposal_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column(
            "approved_execution_deadline_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # 兼容 M1 行的全空三元组；M2 行和保留清理都必须原子改变三列。
    op.create_check_constraint(
        "ck_approval_requests_payload_aead_all_or_none",
        "approval_requests",
        "(payload_ciphertext IS NULL AND payload_nonce IS NULL "
        "AND payload_key_version IS NULL) OR (payload_ciphertext IS NOT NULL "
        "AND payload_nonce IS NOT NULL AND payload_key_version IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_approval_requests_payload_nonce_length_12",
        "approval_requests",
        "payload_nonce IS NULL OR octet_length(payload_nonce) = 12",
    )


def _extend_tool_executions() -> None:
    """增加稳定 operation 认领、供应商关联、核对与人工收敛事实。"""
    op.add_column("tool_executions", sa.Column("operation_id", sa.Uuid(), nullable=True))
    op.add_column(
        "tool_executions",
        sa.Column("provider", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "tool_executions",
        sa.Column("provider_resource_id", sa.String(length=512), nullable=True),
    )
    # ``provider_request_id`` 已由 M1 创建；0012 保留原列并补齐其余关联事实。
    op.add_column(
        "tool_executions",
        sa.Column("correlation_id", sa.String(length=255), nullable=True),
    )
    for column_name in ("claimed_at", "request_started_at", "completed_at"):
        op.add_column(
            "tool_executions",
            sa.Column(column_name, sa.DateTime(timezone=True), nullable=True),
        )
    op.add_column(
        "tool_executions",
        sa.Column(
            "write_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "tool_executions",
        sa.Column(
            "reconciliation_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "tool_executions",
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tool_executions",
        sa.Column("manual_resolution", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "tool_executions",
        sa.Column("manual_resolved_by_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "tool_executions",
        sa.Column("manual_resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tool_executions_manual_resolved_by_user_id",
        "tool_executions",
        "users",
        ["manual_resolved_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_tool_executions_operation",
        "tool_executions",
        ["task_id", "operation_id"],
    )
    op.create_check_constraint(
        "ck_tool_executions_write_attempt_count_non_negative",
        "tool_executions",
        "write_attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_tool_executions_reconciliation_attempt_count_non_negative",
        "tool_executions",
        "reconciliation_attempt_count >= 0",
    )
    # 人工结论缺少 actor 或时间就不可审计，三列只能全空或全有。
    op.create_check_constraint(
        "ck_tool_executions_manual_resolution_all_or_none",
        "tool_executions",
        "(manual_resolution IS NULL AND manual_resolved_by_user_id IS NULL "
        "AND manual_resolved_at IS NULL) OR (manual_resolution IS NOT NULL "
        "AND manual_resolved_by_user_id IS NOT NULL AND manual_resolved_at IS NOT NULL)",
    )
    # 只记录用户确认的结果枚举，数据库不接受正文、地址或其他自由文本证据。
    op.create_check_constraint(
        "ck_tool_executions_manual_resolution_value",
        "tool_executions",
        "manual_resolution IS NULL OR manual_resolution IN "
        "('confirmed_executed', 'confirmed_not_executed')",
    )


def upgrade() -> None:
    """按父表、不可变子表和兼容扩展顺序前向建立可信操作 Schema。"""
    _create_mail_draft_tables()
    _create_calendar_proposal_tables()
    _extend_approval_requests()
    _extend_tool_executions()


def downgrade() -> None:
    """拒绝会丢失草稿、提案、审批密文或真实写结果的破坏性回滚。"""
    raise RuntimeError("M2 trusted action schema cannot be downgraded safely")
