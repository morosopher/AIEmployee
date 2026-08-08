"""将 CalendarEvent 供应商身份收缩到连接、日历和事件三元组。

Revision ID: 20260809_0018
Revises: 20260808_0017

该 contract revision 不能与旧 ``sync_calendar`` 写入实例滚动混跑。迁移先在旧二元唯一
约束仍存在时并发建立三元索引，再在短 metadata 事务中挂载新约束并移除旧约束；任何业务
行都不被删除、合并或重写。升级失败时保留旧约束，便于安全重试。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0018"
down_revision: str | Sequence[str] | None = "20260808_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "calendar_events"
_LEGACY_NAME = "uq_calendar_events_connection_provider_event"
_LEGACY_COLUMNS = ("connection_id", "provider_event_id")
_NEW_NAME = "uq_calendar_events_connection_calendar_provider_event"
_NEW_COLUMNS = ("connection_id", "calendar_id", "provider_event_id")


def _quote_identifier(identifier: str) -> str:
    """安全引用迁移内固定标识符，避免把列名拼接边界交给外部输入。"""
    return op.get_bind().dialect.identifier_preparer.quote(identifier)


def _constraint_row(table_name: str, constraint_name: str) -> tuple[object, ...] | None:
    """读取当前 schema 中固定名称约束的 catalog 事实。"""
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT c.oid, c.contype::text, c.convalidated, c.condeferrable, "
                "c.condeferred, c.confdeltype::text, c.conrelid::regclass::text "
                "FROM pg_catalog.pg_constraint AS c "
                "JOIN pg_catalog.pg_class AS table_info ON table_info.oid = c.conrelid "
                "JOIN pg_catalog.pg_namespace AS namespace_info "
                "ON namespace_info.oid = table_info.relnamespace "
                "WHERE namespace_info.nspname = current_schema() "
                "AND table_info.relname = :table_name "
                "AND c.conname = :constraint_name"
            ),
            {"table_name": table_name, "constraint_name": constraint_name},
        )
        .one_or_none()
    )
    return tuple(row) if row is not None else None


def _constraint_columns(table_name: str, constraint_name: str) -> tuple[str, ...] | None:
    """读取约束本地列的精确顺序，拒绝同名但列集合不同的对象。"""
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT ARRAY_AGG(attribute.attname ORDER BY key_info.position) "
                "FROM pg_catalog.pg_constraint AS constraint_info "
                "JOIN pg_catalog.pg_class AS table_info "
                "ON table_info.oid = constraint_info.conrelid "
                "JOIN pg_catalog.pg_namespace AS namespace_info "
                "ON namespace_info.oid = table_info.relnamespace "
                "JOIN unnest(constraint_info.conkey) WITH ORDINALITY "
                "AS key_info(attnum, position) ON TRUE "
                "JOIN pg_catalog.pg_attribute AS attribute "
                "ON attribute.attrelid = constraint_info.conrelid "
                "AND attribute.attnum = key_info.attnum "
                "WHERE namespace_info.nspname = current_schema() "
                "AND table_info.relname = :table_name "
                "AND constraint_info.conname = :constraint_name"
            ),
            {"table_name": table_name, "constraint_name": constraint_name},
        )
        .scalar_one_or_none()
    )
    return tuple(str(column) for column in row) if row is not None else None


def _index_row(index_name: str) -> tuple[object, ...] | None:
    """读取索引完整键形状，保留表达式键和 INCLUDE 项的 catalog 信息。"""
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT table_info.relname, index_info.indisvalid, "
                "index_info.indisunique, index_info.indpred IS NULL, "
                "index_info.indexprs IS NULL, index_info.indnkeyatts, "
                "index_info.indnatts, ARRAY("
                "SELECT CASE WHEN index_key.attnum = 0 THEN NULL "
                "ELSE attribute.attname::text END "
                "FROM unnest(index_info.indkey) WITH ORDINALITY "
                "AS index_key(attnum, position) "
                "LEFT JOIN pg_catalog.pg_attribute AS attribute "
                "ON attribute.attrelid = index_info.indrelid "
                "AND attribute.attnum = index_key.attnum "
                "ORDER BY index_key.position) "
                "FROM pg_catalog.pg_index AS index_info "
                "JOIN pg_catalog.pg_class AS index_class "
                "ON index_class.oid = index_info.indexrelid "
                "JOIN pg_catalog.pg_class AS table_info "
                "ON table_info.oid = index_info.indrelid "
                "JOIN pg_catalog.pg_namespace AS namespace_info "
                "ON namespace_info.oid = table_info.relnamespace "
                "WHERE namespace_info.nspname = current_schema() "
                "AND index_class.relname = :index_name"
            ),
            {"index_name": index_name},
        )
        .one_or_none()
    )
    return tuple(row) if row is not None else None


def _verify_index_shape(
    *, index_name: str, expected_table: str, expected_columns: tuple[str, ...]
) -> bool | None:
    """验证索引是普通、唯一、非部分且列顺序完全匹配的目标。"""
    row = _index_row(index_name)
    if row is None:
        return None
    (
        table_name,
        is_valid,
        is_unique,
        is_unpartial,
        has_no_expressions,
        key_attribute_count,
        attribute_count,
        index_columns,
    ) = row
    if (
        table_name != expected_table
        or not bool(is_unique)
        or not bool(is_unpartial)
        or not bool(has_no_expressions)
        or key_attribute_count != len(expected_columns)
        or attribute_count != len(expected_columns)
        or tuple(index_columns) != expected_columns
    ):
        raise RuntimeError(f"{index_name} has an unexpected index definition")
    return bool(is_valid)


def _verify_named_identity(*, name: str, columns: tuple[str, ...]) -> tuple[str, bool] | None:
    """验证约束或独立索引的精确形状，并返回对象类型与有效性。"""
    constraint = _constraint_row(_TABLE, name)
    index_state = _verify_index_shape(
        index_name=name,
        expected_table=_TABLE,
        expected_columns=columns,
    )
    if constraint is not None:
        if (
            constraint[1] != "u"
            or not bool(constraint[2])
            or _constraint_columns(_TABLE, name) != columns
        ):
            raise RuntimeError(f"{name} has an unexpected definition")
        # 唯一约束必须拥有同名、有效的 backing index；否则不能安全地复用/删除它。
        if index_state is not True:
            raise RuntimeError(f"{name} has an unexpected index definition")
        return "constraint", True
    if index_state is None:
        return None
    return "index", index_state


def _assert_no_legacy_duplicates() -> None:
    """在 contract 切换前后证明旧二元身份可安全表达。"""
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM calendar_events "
                "GROUP BY connection_id, provider_event_id "
                "HAVING count(*) > 1 LIMIT 1"
            )
        )
        .scalar_one_or_none()
    )
    if duplicate is not None:
        raise RuntimeError("calendar event legacy identity requires manual repair")


def _prepare_new_index() -> None:
    """在事务外并发构建三元索引，失败后只清理精确且无歧义的 invalid 对象。"""
    state = _verify_named_identity(name=_NEW_NAME, columns=_NEW_COLUMNS)
    if state in (("constraint", True), ("index", True)):
        return
    if state == ("index", False):
        quoted_name = _quote_identifier(_NEW_NAME)
        with op.get_context().autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {quoted_name}"))
    quoted_name = _quote_identifier(_NEW_NAME)
    quoted_table = _quote_identifier(_TABLE)
    quoted_columns = ", ".join(_quote_identifier(column) for column in _NEW_COLUMNS)
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                f"CREATE UNIQUE INDEX CONCURRENTLY {quoted_name} "
                f"ON {quoted_table} ({quoted_columns})"
            )
        )


def _drop_legacy_identity_after_attach() -> None:
    """新约束已挂载后移除旧二元约束或同名独立索引。"""
    state = _verify_named_identity(name=_LEGACY_NAME, columns=_LEGACY_COLUMNS)
    if state is None:
        return
    if state[0] == "constraint":
        op.drop_constraint(_LEGACY_NAME, _TABLE, type_="unique")
    else:
        # 独立索引不再承担业务身份；此时新约束已经存在，短事务删除不会丢失保护。
        op.execute(sa.text(f"DROP INDEX {_quote_identifier(_LEGACY_NAME)}"))


def _attach_new_identity() -> None:
    """把已验证的三元索引挂载为稳定命名约束。"""
    state = _verify_named_identity(name=_NEW_NAME, columns=_NEW_COLUMNS)
    if state is None or (state[0] == "index" and not state[1]):
        raise RuntimeError(f"{_NEW_NAME} is not a valid unique index")
    if state[0] == "index":
        quoted_table = _quote_identifier(_TABLE)
        quoted_name = _quote_identifier(_NEW_NAME)
        op.execute(
            sa.text(
                f"ALTER TABLE {quoted_table} ADD CONSTRAINT {quoted_name} "
                f"UNIQUE USING INDEX {quoted_name}"
            )
        )
    if _verify_named_identity(name=_NEW_NAME, columns=_NEW_COLUMNS) != ("constraint", True):
        raise RuntimeError(f"{_NEW_NAME} was not attached with the expected definition")


def upgrade() -> None:
    """以 expand/contract 顺序切换 CalendarEvent 的不可变三元身份。"""
    # 先检查所有同名对象，避免错误 DDL 在并发索引构建前被静默接受。
    _verify_named_identity(name=_LEGACY_NAME, columns=_LEGACY_COLUMNS)
    _verify_named_identity(name=_NEW_NAME, columns=_NEW_COLUMNS)
    _assert_no_legacy_duplicates()
    _prepare_new_index()
    _attach_new_identity()
    _drop_legacy_identity_after_attach()


def _assert_no_cross_calendar_duplicates() -> None:
    """downgrade 前证明旧二元身份不会合并不同日历的业务行。"""
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM calendar_events "
                "GROUP BY connection_id, provider_event_id "
                "HAVING count(DISTINCT calendar_id) > 1 LIMIT 1"
            )
        )
        .scalar_one_or_none()
    )
    if duplicate is not None:
        raise RuntimeError("cannot restore legacy calendar event identity")


def _recreate_legacy_identity() -> None:
    """在无跨日历重复时恢复旧约束，兼容已存在的精确 standalone index。"""
    state = _verify_named_identity(name=_LEGACY_NAME, columns=_LEGACY_COLUMNS)
    if state == ("constraint", True):
        return
    if state == ("index", False):
        raise RuntimeError(f"{_LEGACY_NAME} has an invalid index")
    if state == ("index", True):
        quoted_table = _quote_identifier(_TABLE)
        quoted_name = _quote_identifier(_LEGACY_NAME)
        op.execute(
            sa.text(
                f"ALTER TABLE {quoted_table} ADD CONSTRAINT {quoted_name} "
                f"UNIQUE USING INDEX {quoted_name}"
            )
        )
        return
    op.create_unique_constraint(_LEGACY_NAME, _TABLE, _LEGACY_COLUMNS)


def downgrade() -> None:
    """仅在可证明无跨日历重复时恢复旧身份，绝不删除或合并事件行。"""
    _assert_no_cross_calendar_duplicates()
    new_state = _verify_named_identity(name=_NEW_NAME, columns=_NEW_COLUMNS)
    if new_state is None:
        _recreate_legacy_identity()
        return
    if new_state[0] == "index" and not new_state[1]:
        raise RuntimeError(f"{_NEW_NAME} has an invalid index")
    if new_state[0] == "constraint":
        op.drop_constraint(_NEW_NAME, _TABLE, type_="unique")
    else:
        op.execute(sa.text(f"DROP INDEX {_quote_identifier(_NEW_NAME)}"))
    _recreate_legacy_identity()
