"""在线收缩邮件连接身份扩展，并建立组合归属约束。

Revision ID: 20260808_0017
Revises: 20260808_0016

0016 只增加可空列并从可信的 thread 回填连接。这里把长时间运行的唯一性索引改为
``CONCURRENTLY`` 创建，再把索引挂载为约束；所有组合外键先以 ``NOT VALID`` 加入、再
显式验证，最后才设置非空并移除旧 thread 级唯一约束。这样失败的索引构建可以留下可识别
的中间状态，下一次迁移只会清理本迁移明确命名的 invalid index，不会误删业务索引。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0017"
down_revision: str | Sequence[str] | None = "20260808_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MESSAGE_UNIQUE = "uq_email_messages_connection_provider_message"
_MESSAGE_UNIQUE_COLUMNS = ("connection_id", "provider_message_id")
_MESSAGE_UNIQUE_TABLE = "email_messages"
_THREAD_UNIQUE = "uq_email_threads_id_connection_user"
_THREAD_UNIQUE_COLUMNS = ("id", "connection_id", "user_id")
_THREAD_UNIQUE_TABLE = "email_threads"
_LEGACY_MESSAGE_UNIQUE = "uq_email_messages_thread_provider_message"
_NULL_CHECK = "ck_email_messages_connection_id_present"
_BACKFILL_BATCH_SIZE = 1_000


def _column_exists(table_name: str, column_name: str) -> bool:
    """返回当前 public schema 是否已有指定列。"""
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table_name AND column_name = :column_name"
                ")"
            ),
            {"table_name": table_name, "column_name": column_name},
        )
        .scalar()
    )


def _constraint_row(table_name: str, constraint_name: str) -> tuple[object, ...] | None:
    """读取固定表上的约束元数据；名称不来自用户输入。"""
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT c.oid, c.contype::text, c.convalidated, c.condeferrable, c.condeferred, "
                "c.confdeltype::text, c.conrelid::regclass::text "
                "FROM pg_catalog.pg_constraint AS c "
                "JOIN pg_catalog.pg_class AS t ON t.oid = c.conrelid "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = t.relnamespace "
                "WHERE n.nspname = current_schema() AND t.relname = :table_name "
                "AND c.conname = :constraint_name"
            ),
            {"table_name": table_name, "constraint_name": constraint_name},
        )
        .one_or_none()
    )
    return tuple(row) if row is not None else None


def _constraint_columns(table_name: str, constraint_name: str) -> tuple[str, ...] | None:
    """读取唯一约束或外键本地列的稳定顺序。"""
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT ARRAY_AGG(attribute.attname ORDER BY key_info.position) "
                "FROM pg_catalog.pg_constraint AS c "
                "JOIN pg_catalog.pg_class AS t ON t.oid = c.conrelid "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid = t.relnamespace "
                "JOIN unnest(c.conkey) WITH ORDINALITY AS key_info(attnum, position) ON TRUE "
                "JOIN pg_catalog.pg_attribute AS attribute "
                "ON attribute.attrelid = c.conrelid AND attribute.attnum = key_info.attnum "
                "WHERE n.nspname = current_schema() AND t.relname = :table_name "
                "AND c.conname = :constraint_name"
            ),
            {"table_name": table_name, "constraint_name": constraint_name},
        )
        .scalar_one_or_none()
    )
    return tuple(str(value) for value in row) if row is not None else None


def _foreign_key_shape(
    table_name: str,
    constraint_name: str,
) -> tuple[tuple[str, ...], str, tuple[str, ...]] | None:
    """读取外键本地列、目标表和目标列，防止同名错误约束被静默接受。"""
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT ARRAY_AGG(local_attribute.attname ORDER BY local_key.position), "
                "target_table.relname, "
                "ARRAY_AGG(target_attribute.attname ORDER BY target_key.position) "
                "FROM pg_catalog.pg_constraint AS c "
                "JOIN pg_catalog.pg_class AS local_table ON local_table.oid = c.conrelid "
                "JOIN pg_catalog.pg_namespace AS local_namespace "
                "ON local_namespace.oid = local_table.relnamespace "
                "JOIN pg_catalog.pg_class AS target_table ON target_table.oid = c.confrelid "
                "JOIN unnest(c.conkey) WITH ORDINALITY "
                "AS local_key(attnum, position) ON TRUE "
                "JOIN pg_catalog.pg_attribute AS local_attribute "
                "ON local_attribute.attrelid = c.conrelid "
                "AND local_attribute.attnum = local_key.attnum "
                "JOIN unnest(c.confkey) WITH ORDINALITY "
                "AS target_key(attnum, position) ON target_key.position = local_key.position "
                "JOIN pg_catalog.pg_attribute AS target_attribute "
                "ON target_attribute.attrelid = c.confrelid "
                "AND target_attribute.attnum = target_key.attnum "
                "WHERE local_namespace.nspname = current_schema() "
                "AND local_table.relname = :table_name AND c.conname = :constraint_name "
                "GROUP BY target_table.relname"
            ),
            {"table_name": table_name, "constraint_name": constraint_name},
        )
        .one_or_none()
    )
    if row is None:
        return None
    return (
        tuple(str(value) for value in row[0]),
        str(row[1]),
        tuple(str(value) for value in row[2]),
    )


def _check_expression(table_name: str, constraint_name: str) -> str | None:
    """读取固定表上 CHECK 的规范 PostgreSQL 表达式，用于拒绝同名错误约束。"""
    value = op.get_bind().execute(
        sa.text(
            "SELECT pg_catalog.pg_get_expr(c.conbin, c.conrelid, true) "
            "FROM pg_catalog.pg_constraint AS c "
            "JOIN pg_catalog.pg_class AS t ON t.oid = c.conrelid "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = :table_name "
            "AND c.conname = :constraint_name AND c.contype = 'c'"
        ),
        {"table_name": table_name, "constraint_name": constraint_name},
    ).scalar_one_or_none()
    return str(value) if value is not None else None


def _index_row(index_name: str) -> tuple[object, ...] | None:
    """读取指定索引的完整键形状，保留表达式与 INCLUDE 项的 catalog 事实。

    PostgreSQL 用 ``indkey`` 中的 ``attnum=0`` 表示表达式键；因此这里必须使用
    ``LEFT JOIN`` 并保留 ``NULL`` 位置，同时读取 ``indexprs``、``indnkeyatts`` 与
    ``indnatts``。否则额外表达式会被 INNER JOIN 静默吞掉，错误的 invalid 同名索引
    可能被误判成迁移目标并遭到删除。
    """
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT table_class.relname, index_info.indisvalid, "
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
                "JOIN pg_catalog.pg_class AS table_class "
                "ON table_class.oid = index_info.indrelid "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = table_class.relnamespace "
                "WHERE namespace.nspname = current_schema() "
                "AND index_class.relname = :index_name"
            ),
            {"index_name": index_name},
        )
        .one_or_none()
    )
    return tuple(row) if row is not None else None


def _quote_identifier(identifier: str) -> str:
    """用当前 PostgreSQL 方言安全引用固定迁移标识符。"""
    return op.get_bind().dialect.identifier_preparer.quote(identifier)


def _catch_up_connection_identity() -> None:
    """有界追赶 0016 部署窗口内旧应用实例留下的 nullable direct identity。

    0016 初始回填完成后仍必须先部署双写应用并排空旧实例，期间旧列集合可以继续插入
    ``connection_id IS NULL``。这里重复同一可信 thread 投影和固定批次 AUTOCOMMIT 更新，
    每批释放行锁且可安全重跑；任何无法从 thread 证明归属的行会留给后续 preflight
    fail closed，绝不猜测或删除业务事实。
    """
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        while True:
            result = bind.execute(
                sa.text(
                    "WITH batch AS ("
                    "SELECT message.id, thread.connection_id "
                    "FROM email_messages AS message "
                    "JOIN email_threads AS thread ON thread.id = message.thread_id "
                    "WHERE message.connection_id IS NULL "
                    "ORDER BY message.id "
                    "LIMIT :batch_size "
                    "FOR UPDATE OF message SKIP LOCKED"
                    ") UPDATE email_messages AS message "
                    "SET connection_id = batch.connection_id "
                    "FROM batch WHERE message.id = batch.id "
                    "RETURNING message.id"
                ),
                {"batch_size": _BACKFILL_BATCH_SIZE},
            )
            if not result.fetchall():
                break


def _assert_preflight() -> None:
    """在任何 contract DDL 前拒绝无法确定归属或唯一性的历史数据。"""
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM email_messages WHERE connection_id IS NULL LIMIT 1")
    ).scalar_one_or_none() is not None:
        raise RuntimeError("mail message connection backfill is incomplete")
    if bind.execute(
        sa.text(
            "SELECT 1 FROM email_messages "
            "GROUP BY connection_id, provider_message_id "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).scalar_one_or_none() is not None:
        raise RuntimeError("mail message connection-level duplicate requires manual repair")
    if bind.execute(
        sa.text(
            "SELECT 1 FROM email_messages AS message "
            "JOIN email_threads AS thread ON thread.id = message.thread_id "
            "LEFT JOIN oauth_connections AS connection ON connection.id = thread.connection_id "
            "WHERE connection.id IS NULL OR message.user_id <> thread.user_id "
            "OR message.connection_id <> thread.connection_id "
            "OR thread.user_id <> connection.user_id LIMIT 1"
        )
    ).scalar_one_or_none() is not None:
        raise RuntimeError("mail message ownership mismatch requires manual repair")


def _assert_unique_shape(table_name: str, constraint_name: str, expected: tuple[str, ...]) -> None:
    """验证已存在同名唯一约束仍指向预期列，避免错误元数据被复用。"""
    actual = _constraint_columns(table_name, constraint_name)
    if actual != expected:
        raise RuntimeError(f"{constraint_name} has an unexpected definition")


def _prepare_unique_indexes() -> None:
    """在事务外准备两个唯一索引，且只触碰本迁移声明的固定名称。"""
    targets = (
        (_MESSAGE_UNIQUE_TABLE, _MESSAGE_UNIQUE, _MESSAGE_UNIQUE_COLUMNS),
        (_THREAD_UNIQUE_TABLE, _THREAD_UNIQUE, _THREAD_UNIQUE_COLUMNS),
    )
    operations: list[tuple[str, str, tuple[str, ...]]] = []
    for table_name, constraint_name, columns in targets:
        constraint = _constraint_row(table_name, constraint_name)
        if constraint is not None:
            if constraint[1] != "u":
                raise RuntimeError(f"{constraint_name} is not a unique constraint")
            _assert_unique_shape(table_name, constraint_name, columns)
            continue

        index = _index_row(constraint_name)
        if index is not None:
            (
                index_table,
                is_valid,
                is_unique,
                is_unpartial,
                has_no_expressions,
                key_attribute_count,
                attribute_count,
                index_columns,
            ) = index
            if index_table != table_name:
                raise RuntimeError(f"{constraint_name} belongs to an unexpected table")
            if (
                not bool(is_unique)
                or not bool(is_unpartial)
                or not bool(has_no_expressions)
                or key_attribute_count != len(columns)
                or attribute_count != len(columns)
                or tuple(index_columns) != columns
            ):
                # 同名对象即使 invalid 也可能由其他部署或人工 DDL 创建。必须先证明它正是
                # 本迁移声明的普通列、无 INCLUDE 精确目标，才能把 DROP CONCURRENTLY
                # 视为安全、精确的恢复动作；表达式位置绝不能靠 JOIN 丢弃后当作缺席。
                raise RuntimeError(f"{constraint_name} has an unexpected index definition")
            if not bool(is_valid):
                # failed CONCURRENTLY build leaves an invalid catalog row. Preserve the declared
                # target columns so the same pass can rebuild the exact two/three-column index.
                operations.append(("drop", constraint_name, columns))
            else:
                operations.append(("attach", constraint_name, columns))
        else:
            operations.append(("create", constraint_name, columns))

    if not operations:
        return
    with op.get_context().autocommit_block():
        for operation, name, columns in operations:
            quoted_name = _quote_identifier(name)
            if operation == "drop":
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {quoted_name}"))
                operation = "create"
            if operation == "create":
                quoted_table = _quote_identifier(
                    _MESSAGE_UNIQUE_TABLE
                    if name == _MESSAGE_UNIQUE
                    else _THREAD_UNIQUE_TABLE
                )
                quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
                op.execute(
                    sa.text(
                        f"CREATE UNIQUE INDEX CONCURRENTLY {quoted_name} "
                        f"ON {quoted_table} ({quoted_columns})"
                    )
                )


def _attach_unique_constraints() -> None:
    """把已完成的并发索引挂载为约束；有效索引可来自上次中断的迁移。"""
    for table_name, constraint_name, _columns in (
        (_MESSAGE_UNIQUE_TABLE, _MESSAGE_UNIQUE, _MESSAGE_UNIQUE_COLUMNS),
        (_THREAD_UNIQUE_TABLE, _THREAD_UNIQUE, _THREAD_UNIQUE_COLUMNS),
    ):
        if _constraint_row(table_name, constraint_name) is not None:
            continue
        quoted_table = _quote_identifier(table_name)
        quoted_constraint = _quote_identifier(constraint_name)
        op.execute(
            sa.text(
                f"ALTER TABLE {quoted_table} ADD CONSTRAINT {quoted_constraint} "
                f"UNIQUE USING INDEX {quoted_constraint}"
            )
        )


def _ensure_foreign_key(
    *,
    table_name: str,
    constraint_name: str,
    local_columns: tuple[str, ...],
    target_table: str,
    target_columns: tuple[str, ...],
) -> None:
    """幂等添加并验证一个组合归属外键。"""
    existing = _constraint_row(table_name, constraint_name)
    expected_shape = (local_columns, target_table, target_columns)
    if existing is None:
        quoted_table = _quote_identifier(table_name)
        quoted_constraint = _quote_identifier(constraint_name)
        quoted_local = ", ".join(_quote_identifier(column) for column in local_columns)
        quoted_target = ", ".join(_quote_identifier(column) for column in target_columns)
        op.execute(
            sa.text(
                f"ALTER TABLE {quoted_table} ADD CONSTRAINT {quoted_constraint} "
                f"FOREIGN KEY ({quoted_local}) REFERENCES {_quote_identifier(target_table)} "
                f"({quoted_target}) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED NOT VALID"
            )
        )
    elif (
        existing[1] != "f"
        or _foreign_key_shape(table_name, constraint_name) != expected_shape
        or not bool(existing[3])
        or not bool(existing[4])
        or existing[5] != "c"
    ):
        raise RuntimeError(f"{constraint_name} has an unexpected definition")

    current = _constraint_row(table_name, constraint_name)
    if current is None:
        raise RuntimeError(f"{constraint_name} was not created")
    if (
        current[1] != "f"
        or _foreign_key_shape(table_name, constraint_name) != expected_shape
        or not bool(current[3])
        or not bool(current[4])
        or current[5] != "c"
    ):
        raise RuntimeError(f"{constraint_name} has an unexpected definition")
    if not bool(current[2]):
        quoted_table = _quote_identifier(table_name)
        quoted_constraint = _quote_identifier(constraint_name)
        op.execute(
            sa.text(
                f"ALTER TABLE {quoted_table} VALIDATE CONSTRAINT {quoted_constraint}"
            )
        )


def _ensure_message_not_null() -> None:
    """先验证临时检查，再设置列非空，避免 SET NOT NULL 盲目扫描失败。"""
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM email_messages WHERE connection_id IS NULL LIMIT 1")
    ).scalar_one_or_none() is not None:
        raise RuntimeError("mail message connection backfill is incomplete")
    check = _constraint_row(_MESSAGE_UNIQUE_TABLE, _NULL_CHECK)
    if check is None:
        op.execute(
            sa.text(
                f"ALTER TABLE {_quote_identifier(_MESSAGE_UNIQUE_TABLE)} "
                f"ADD CONSTRAINT {_quote_identifier(_NULL_CHECK)} "
                "CHECK (connection_id IS NOT NULL) NOT VALID"
            )
        )
    elif check[1] != "c" or _check_expression(_MESSAGE_UNIQUE_TABLE, _NULL_CHECK) not in {
        "connection_id IS NOT NULL",
        "(connection_id IS NOT NULL)",
    }:
        raise RuntimeError(f"{_NULL_CHECK} has an unexpected definition")
    check = _constraint_row(_MESSAGE_UNIQUE_TABLE, _NULL_CHECK)
    if check is not None and not bool(check[2]):
        op.execute(
            sa.text(
                f"ALTER TABLE {_quote_identifier(_MESSAGE_UNIQUE_TABLE)} "
                f"VALIDATE CONSTRAINT {_quote_identifier(_NULL_CHECK)}"
            )
        )
    # Compatibility databases may already have applied the old 0016 contract.
    nullable = bind.execute(
        sa.text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'email_messages' "
            "AND column_name = 'connection_id'"
        )
    ).scalar_one_or_none()
    if nullable == "YES":
        op.alter_column(
            "email_messages",
            "connection_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
    if _constraint_row(_MESSAGE_UNIQUE_TABLE, _NULL_CHECK) is not None:
        op.drop_constraint(_NULL_CHECK, _MESSAGE_UNIQUE_TABLE, type_="check")


def upgrade() -> None:
    """完成邮件身份 contract，并兼容已执行旧版 0016 的数据库。"""
    if not _column_exists(_MESSAGE_UNIQUE_TABLE, "connection_id"):
        raise RuntimeError("mail message connection expand is required before contract")
    if not _column_exists(_MESSAGE_UNIQUE_TABLE, "provider_updated_at"):
        # 早期 0016 已经建立 contract 但没有供应商版本列；此处只补 nullable 扩展。
        op.add_column(
            _MESSAGE_UNIQUE_TABLE,
            sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        )
    _catch_up_connection_identity()
    _assert_preflight()
    _prepare_unique_indexes()
    _attach_unique_constraints()
    _ensure_foreign_key(
        table_name="email_threads",
        constraint_name="fk_email_threads_connection_user",
        local_columns=("connection_id", "user_id"),
        target_table="oauth_connections",
        target_columns=("id", "user_id"),
    )
    _ensure_foreign_key(
        table_name="email_messages",
        constraint_name="fk_email_messages_connection_user",
        local_columns=("connection_id", "user_id"),
        target_table="oauth_connections",
        target_columns=("id", "user_id"),
    )
    _ensure_foreign_key(
        table_name="email_messages",
        constraint_name="fk_email_messages_thread_connection_user",
        local_columns=("thread_id", "connection_id", "user_id"),
        target_table="email_threads",
        target_columns=("id", "connection_id", "user_id"),
    )
    _ensure_message_not_null()
    if _constraint_row(_MESSAGE_UNIQUE_TABLE, _LEGACY_MESSAGE_UNIQUE) is not None:
        op.drop_constraint(_LEGACY_MESSAGE_UNIQUE, _MESSAGE_UNIQUE_TABLE, type_="unique")


def downgrade() -> None:
    """仅撤销 0017 contract，保留 0016 新增列与所有邮件业务行。"""
    for table_name, constraint_name in (
        ("email_messages", "fk_email_messages_thread_connection_user"),
        ("email_messages", "fk_email_messages_connection_user"),
        ("email_threads", "fk_email_threads_connection_user"),
    ):
        if _constraint_row(table_name, constraint_name) is not None:
            op.drop_constraint(constraint_name, table_name, type_="foreignkey")
    for table_name, constraint_name in (
        (_MESSAGE_UNIQUE_TABLE, _MESSAGE_UNIQUE),
        (_THREAD_UNIQUE_TABLE, _THREAD_UNIQUE),
    ):
        if _constraint_row(table_name, constraint_name) is not None:
            op.drop_constraint(constraint_name, table_name, type_="unique")
    if _column_exists(_MESSAGE_UNIQUE_TABLE, "connection_id"):
        nullable = op.get_bind().execute(
            sa.text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'email_messages' "
                "AND column_name = 'connection_id'"
            )
        ).scalar_one_or_none()
        if nullable == "NO":
            op.alter_column(
                _MESSAGE_UNIQUE_TABLE,
                "connection_id",
                existing_type=sa.Uuid(),
                nullable=True,
            )
    if _constraint_row(_MESSAGE_UNIQUE_TABLE, _LEGACY_MESSAGE_UNIQUE) is None:
        op.create_unique_constraint(
            _LEGACY_MESSAGE_UNIQUE,
            _MESSAGE_UNIQUE_TABLE,
            ["thread_id", "provider_message_id"],
        )
