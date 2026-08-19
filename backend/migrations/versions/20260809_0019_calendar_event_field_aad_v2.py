"""为 CalendarEvent 敏感字段增加版本化、日历绑定的 AAD。

Revision ID: 20260809_0019
Revises: 20260809_0018

该前向 revision 只标记仍使用历史 AAD 的完整 AEAD 三元组，不在迁移事务内解密或重加密。
所有受影响连接/日历必须先具备精确的本地重同步事实；随后双阶段 typed guard、Schema、
游标失效、版本行与授权 lifecycle 保持在同一 Alembic 外层事务中。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Connection

from ai_employee.application.ports.calendar_aad_migration_guard import (
    calendar_aad_migration_invariant_error,
    invoke_calendar_aad_migration_guard,
    resolve_calendar_aad_migration_guard,
)

revision: str = "20260809_0019"
down_revision: str | Sequence[str] | None = "20260809_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DESCRIPTION_CONSTRAINT = "ck_calendar_events_description_aead_with_aad_all_or_none"
_LOCATION_CONSTRAINT = "ck_calendar_events_location_aead_with_aad_all_or_none"

_PARTIAL_TRIPLE_EXISTS_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM public.calendar_events AS event
    WHERE num_nonnulls(
        event.description_ciphertext,
        event.description_nonce,
        event.description_key_version
    ) NOT IN (0, 3)
    OR num_nonnulls(
        event.location_ciphertext,
        event.location_nonce,
        event.location_key_version
    ) NOT IN (0, 3)
) AS partial_triple_exists
"""

_RECOVERABILITY_GAP_EXISTS_SQL = """
WITH affected_events AS (
    SELECT event.user_id, event.connection_id, event.calendar_id
    FROM public.calendar_events AS event
    WHERE (
        event.description_ciphertext IS NOT NULL
        AND event.description_nonce IS NOT NULL
        AND event.description_key_version IS NOT NULL
    ) OR (
        event.location_ciphertext IS NOT NULL
        AND event.location_nonce IS NOT NULL
        AND event.location_key_version IS NOT NULL
    )
),
affected_pairs AS (
    SELECT DISTINCT affected.connection_id, affected.calendar_id
    FROM affected_events AS affected
)
SELECT EXISTS (
    SELECT 1
    FROM affected_pairs AS pair
    WHERE pair.calendar_id = 'directory'
    OR NOT EXISTS (
        SELECT 1
        FROM public.oauth_connections AS connection
        WHERE connection.id = pair.connection_id
        AND connection.status = 'connected'
        AND NOT EXISTS (
            SELECT 1
            FROM affected_events AS affected
            WHERE affected.connection_id = pair.connection_id
            AND affected.calendar_id = pair.calendar_id
            AND affected.user_id <> connection.user_id
        )
    )
    OR NOT EXISTS (
        SELECT 1
        FROM public.sync_cursors AS cursor
        WHERE cursor.connection_id = pair.connection_id
        AND cursor.resource_kind = 'calendar'
        AND cursor.scope_key = pair.calendar_id
        AND cursor.scope_key <> 'directory'
    )
    OR NOT EXISTS (
        SELECT 1
        FROM public.oauth_connections AS connection
        JOIN public.connection_capabilities AS capability
          ON capability.connection_id = connection.id
         AND capability.user_id = connection.user_id
        WHERE connection.id = pair.connection_id
        AND connection.status = 'connected'
        AND capability.capability = 'calendar.read'
        AND capability.status = 'enabled'
    )
    OR NOT EXISTS (
        SELECT 1
        FROM public.oauth_connections AS connection
        JOIN public.encrypted_credentials AS credential
          ON credential.connection_id = connection.id
         AND credential.user_id = connection.user_id
        WHERE connection.id = pair.connection_id
        AND credential.credential_kind = 'access_token'
    )
    OR NOT EXISTS (
        SELECT 1
        FROM public.oauth_connections AS connection
        JOIN public.encrypted_credentials AS credential
          ON credential.connection_id = connection.id
         AND credential.user_id = connection.user_id
        WHERE connection.id = pair.connection_id
        AND credential.credential_kind = 'refresh_token'
    )
    OR NOT EXISTS (
        SELECT 1
        FROM public.oauth_connections AS connection
        JOIN public.provider_calendars AS calendar
          ON calendar.connection_id = connection.id
         AND calendar.user_id = connection.user_id
        WHERE connection.id = pair.connection_id
        AND calendar.provider_calendar_id = pair.calendar_id
    )
) AS recoverability_gap_exists
"""

_DESCRIPTION_COMPLETE = """
description_ciphertext IS NOT NULL
AND description_nonce IS NOT NULL
AND description_key_version IS NOT NULL
"""
_LOCATION_COMPLETE = """
location_ciphertext IS NOT NULL
AND location_nonce IS NOT NULL
AND location_key_version IS NOT NULL
"""


def _read_preflight_facts(connection: Connection) -> tuple[bool, bool]:
    """在任何 0019 DDL/DML 前读取 partial 与本地恢复缺口事实。

    Args:
        connection: 当前 Alembic 外层事务绑定的同步连接。

    Returns:
        ``(存在 partial 三元组, 存在本地恢复缺口)`` 两个严格布尔值。

    Raises:
        CalendarAadMigrationInvariantError: 连接或查询结果不满足精确边界。
    """
    if not isinstance(connection, Connection):
        calendar_aad_migration_invariant_error()
    partial = connection.execute(sa.text(_PARTIAL_TRIPLE_EXISTS_SQL)).scalar_one()
    recoverability_gap = connection.execute(sa.text(_RECOVERABILITY_GAP_EXISTS_SQL)).scalar_one()
    if type(partial) is not bool or type(recoverability_gap) is not bool:
        calendar_aad_migration_invariant_error()
    return partial, recoverability_gap


def _field_constraint(field: str) -> str:
    """构造一个字段四元组全空或完整且版本受限的固定检查表达式。"""
    return f"""
    (
        {field}_ciphertext IS NULL
        AND {field}_nonce IS NULL
        AND {field}_key_version IS NULL
        AND {field}_aad_version IS NULL
    ) OR (
        {field}_ciphertext IS NOT NULL
        AND {field}_nonce IS NOT NULL
        AND {field}_key_version IS NOT NULL
        AND {field}_aad_version IS NOT NULL
        AND {field}_aad_version IN (1, 2)
    )
    """


def upgrade() -> None:
    """预检本地恢复事实，冻结 guard 后原子增加 AAD 版本并失效精确游标。"""
    connection = op.get_bind()
    partial, recoverability_gap = _read_preflight_facts(connection)
    config = op.get_context().config
    if config is None:
        # revision 必须复用 env.py 已冻结 guard 的同一 Config；脱离 Alembic 环境时拒绝迁移。
        calendar_aad_migration_invariant_error()
    guard = resolve_calendar_aad_migration_guard(config)
    invoke_calendar_aad_migration_guard(
        lambda: guard.verify(connection=connection, phase="before_mutation")
    )
    if partial or recoverability_gap:
        calendar_aad_migration_invariant_error()

    op.add_column(
        "calendar_events",
        sa.Column("description_aad_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "calendar_events",
        sa.Column("location_aad_version", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE public.calendar_events SET description_aad_version = 1 "
            f"WHERE {_DESCRIPTION_COMPLETE}"
        )
    )
    op.execute(
        sa.text(
            f"UPDATE public.calendar_events SET location_aad_version = 1 WHERE {_LOCATION_COMPLETE}"
        )
    )
    op.create_check_constraint(
        _DESCRIPTION_CONSTRAINT,
        "calendar_events",
        _field_constraint("description"),
    )
    op.create_check_constraint(
        _LOCATION_CONSTRAINT,
        "calendar_events",
        _field_constraint("location"),
    )
    op.execute(
        sa.text(
            "UPDATE public.sync_cursors AS cursor SET cursor = NULL, "
            "last_success_at = NULL, last_error_code = 'calendar_event_resync_required' "
            "WHERE cursor.resource_kind = 'calendar' "
            "AND cursor.scope_key <> 'directory' AND EXISTS ("
            "SELECT 1 FROM public.calendar_events AS event "
            "WHERE event.connection_id = cursor.connection_id "
            "AND event.calendar_id = cursor.scope_key AND ("
            f"({_DESCRIPTION_COMPLETE}) OR ({_LOCATION_COMPLETE})"
            ")"
            ")"
        )
    )


def downgrade() -> None:
    """拒绝丢弃 AAD 版本与 v2 行，保持 revision 完全前向。"""
    raise RuntimeError("calendar event field AAD migration cannot be downgraded safely")
