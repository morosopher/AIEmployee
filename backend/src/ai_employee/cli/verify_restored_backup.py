"""在既有维护holder上计算版本选择的完整恢复指纹及只读健康证据。

所有业务行先在PostgreSQL内转换为canonical JSON并逐行hash，再按hash排序聚合；
只有计数/摘要返回进程。PG catalog OID、ACL、维护GUC、序列当前计数器不进入指纹，
分别由共享权限/authority及序列健康校验保护。只有严格合格的restore completion审计
按共同谓词归一化；malformed reserved事件硬失败。此模块没有独立连接或恢复后入口。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Connection, text

from ai_employee.infrastructure.db.database_grants import restore_inventory_objects
from ai_employee.infrastructure.db.database_maintenance import (
    DatabaseMaintenanceInvariantError,
    RestoreFingerprint,
    restore_completion_audit_predicate,
)

SUPPORTED_RESTORE_REVISIONS = frozenset({"20260809_0018", "20260809_0019"})
_FINGERPRINT_DOMAIN = b"ai_employee.restore_fingerprint.v1\0"
_FORMAT_TIME = 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
# PG17把0011/0012中两个varchar数组检查经dump/deparse/reparse改写为等价text数组。
# 只对这两个revision/表/约束的完整表达式SHA256闭集合归一；不去掉任意cast或忽略
# constraint内容。全文摘要在PG计算，表达式/字面量不返回Python，也没有动态模板授权。
_CONSTRAINT_EXPRESSION_DIGEST_SQL = """CASE
 WHEN :fingerprint_revision IN ('20260809_0018','20260809_0019')
 AND c.relname='oauth_connections' AND co.conname='ck_oauth_connections_microsoft_identity'
 AND d.expression_digest IN (
 'ef16888e1c46515ccbaaf05d19cc631f744e8273604c25ead47a45e3d913e4f1',
 'a00757999e186c05f49066ea519b8a46a843d0ddf32a5963a759c7254ec69a82')
 THEN 'ef16888e1c46515ccbaaf05d19cc631f744e8273604c25ead47a45e3d913e4f1'
 WHEN :fingerprint_revision IN ('20260809_0018','20260809_0019')
 AND c.relname='tool_executions' AND co.conname='ck_tool_executions_manual_resolution_value'
 AND d.expression_digest IN (
 'a9e7fdd014c6f1660b98c890afc5eaec0f2362c92515dcd0b22c18bc83bd5d3e',
 '76c94a664e7beae11c67215345d4f1add87ea7b65e7c43acd0997fb6086dd247')
 THEN 'a9e7fdd014c6f1660b98c890afc5eaec0f2362c92515dcd0b22c18bc83bd5d3e'
 ELSE d.expression_digest END"""
_SCHEMA_QUERIES = (
    """SELECT jsonb_build_array(c.relname,c.relkind,c.relpersistence,c.relrowsecurity,c.relforcerowsecurity,c.relreplident,c.reloptions) AS item
       FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname='public' ORDER BY c.relname""",
    """SELECT jsonb_build_array(c.relname,a.attname,a.attnum,format_type(a.atttypid,a.atttypmod),
       a.attnotnull,a.attidentity,a.attgenerated,cn.nspname,coll.collname,
       pg_get_expr(d.adbin,d.adrelid),a.attstorage,a.attcompression) AS item
       FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
       JOIN pg_namespace n ON n.oid=c.relnamespace
       LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
       LEFT JOIN pg_collation coll ON coll.oid=a.attcollation
       LEFT JOIN pg_namespace cn ON cn.oid=coll.collnamespace
       WHERE n.nspname='public' AND c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped
       ORDER BY c.relname,a.attnum""",
    f"""SELECT jsonb_build_array(c.relname,co.conname,co.contype,co.convalidated,
       co.condeferrable,co.condeferred,{_CONSTRAINT_EXPRESSION_DIGEST_SQL}) AS item
       FROM pg_constraint co JOIN pg_class c ON c.oid=co.conrelid
       JOIN pg_namespace n ON n.oid=c.relnamespace
       CROSS JOIN LATERAL (SELECT encode(sha256(convert_to(
         pg_get_constraintdef(co.oid,true),'UTF8')),'hex') AS expression_digest) d
       WHERE n.nspname='public'
       ORDER BY c.relname,co.conname""",
    """SELECT jsonb_build_array(c.relname,ic.relname,i.indisvalid,i.indisready,i.indislive,
       pg_get_indexdef(i.indexrelid)) AS item FROM pg_index i
       JOIN pg_class c ON c.oid=i.indrelid JOIN pg_class ic ON ic.oid=i.indexrelid
       JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
       ORDER BY c.relname,ic.relname""",
    """SELECT jsonb_build_array(c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid,true)) AS item
       FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
       JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname""",
    """SELECT jsonb_build_array(c.relname,format_type(s.seqtypid,NULL),s.seqstart,s.seqincrement,
       s.seqmax,s.seqmin,s.seqcache,s.seqcycle,tn.nspname,tc.relname,a.attname,d.deptype) AS item
       FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
       JOIN pg_namespace n ON n.oid=c.relnamespace
       LEFT JOIN pg_depend d ON d.objid=c.oid AND d.classid='pg_class'::regclass AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
       LEFT JOIN pg_class tc ON tc.oid=d.refobjid LEFT JOIN pg_namespace tn ON tn.oid=tc.relnamespace
       LEFT JOIN pg_attribute a ON a.attrelid=tc.oid AND a.attnum=d.refobjsubid
       WHERE n.nspname='public' ORDER BY c.relname,tn.nspname,tc.relname,a.attname""",
    """SELECT jsonb_build_array(n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),
       l.lanname,p.prosrc,p.provolatile,p.prosecdef,p.proconfig) AS item
       FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
       WHERE n.nspname='public' ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)""",
    """SELECT jsonb_build_array(c.relname,p.polname,p.polcmd,p.polpermissive,
       pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid)) AS item
       FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
       JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY c.relname,p.polname""",
)


@dataclass(frozen=True)
class ExportedBackupSnapshot:
    """与pg_dump共用的只读快照；事务退出后ID不再有效，不能用于新鲜post-CAS。"""

    snapshot_id: str
    fingerprint: RestoreFingerprint
    postgres_server_version: str
    created_at: str
    schema_digest: str


def _fail() -> None:
    raise DatabaseMaintenanceInvariantError("restore fingerprint verification failed")


def _canonical_settings(connection: Connection) -> None:
    # 所有JSON/text输出与排序都由固定服务器设置决定，不依赖宿主locale/时区。
    for statement in (
        "SET LOCAL timezone = 'UTC'",
        "SET LOCAL datestyle = 'ISO, YMD'",
        "SET LOCAL intervalstyle = 'iso_8601'",
        "SET LOCAL extra_float_digits = 3",
        "SET LOCAL bytea_output = 'hex'",
        "SET LOCAL search_path = pg_catalog, public",
    ):
        connection.execute(text(statement))


def _schema_digest(connection: Connection, *, revision: str) -> str:
    """将稳定schema内容在PG聚合，不返回SQL定义、OID或可能承载字面值的default。"""
    hashes: list[str] = []
    for query in _SCHEMA_QUERIES:
        digest = connection.scalar(
            text(
                "SELECT encode(sha256(convert_to(COALESCE(jsonb_agg(item)::text,'[]'),'UTF8')),'hex') FROM ("
                + query
                + ") AS structural_rows"
            ),
            {"fingerprint_revision": revision},
        )
        if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            _fail()
        hashes.append(str(digest))
    return hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()


def verify_restore_sequence_health(connection: Connection, *, revision: str) -> None:
    """只读验证精确owned序列的结构、可读性及安全高水位；永远不调用nextval/setval。

    唯一非空NULL例外是固定checkpoint版本集合0..9的从未调用序列，它不分配业务ID。
    实际恢复会先排空旧backend；本查询不能承诺并发旧session的cached nextval值。
    """
    _, sequences = restore_inventory_objects(revision)
    for name in sequences:
        rows = (
            connection.execute(
                text("""SELECT c.relname,format_type(s.seqtypid,NULL) AS seqtype,
           s.seqstart,s.seqincrement,s.seqmin,s.seqmax,s.seqcache,s.seqcycle,
           tn.nspname AS table_schema,tc.relname AS table_name,a.attname AS column_name,
           format_type(a.atttypid,a.atttypmod) AS column_type,a.attidentity,a.attnotnull,
           pg_get_expr(ad.adbin,ad.adrelid) AS column_default,d.deptype,
           pg_sequence_last_value(c.oid::regclass) AS high_water,
           has_sequence_privilege(current_user,c.oid,'USAGE') AS can_use
           FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
           JOIN pg_namespace n ON n.oid=c.relnamespace
           LEFT JOIN pg_depend d ON d.objid=c.oid AND d.classid='pg_class'::regclass
             AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
           LEFT JOIN pg_class tc ON tc.oid=d.refobjid LEFT JOIN pg_namespace tn ON tn.oid=tc.relnamespace
           LEFT JOIN pg_attribute a ON a.attrelid=tc.oid AND a.attnum=d.refobjsubid
           LEFT JOIN pg_attrdef ad ON ad.adrelid=tc.oid AND ad.adnum=a.attnum
           WHERE n.nspname='public' AND c.relname=:name"""),
                {"name": name},
            )
            .mappings()
            .all()
        )
        if len(rows) != 1:
            _fail()
        row = rows[0]
        checkpoint = name == "checkpoint_migrations_v_seq"
        table_name, column = (
            ("checkpoint_migrations", "v") if checkpoint else ("audit_events", "id")
        )
        bits = 32 if checkpoint else 64
        expected_type = "integer" if checkpoint else "bigint"
        if (
            row["table_schema"] != "public"
            or row["table_name"] != table_name
            or row["column_name"] != column
            or row["seqtype"] != expected_type
            or row["column_type"] != expected_type
            or row["seqstart"] != 1
            or row["seqincrement"] != 1
            or row["seqmin"] != 1
            or row["seqmax"] != 2 ** (bits - 1) - 1
            or row["seqcache"] != 1
            or row["seqcycle"] is not False
            or row["attnotnull"] is not True
            or row["can_use"] is not True
            or (
                checkpoint
                and (
                    row["deptype"] != "a"
                    or row["attidentity"] != ""
                    or row["column_default"] != "nextval('checkpoint_migrations_v_seq'::regclass)"
                )
            )
            or (
                not checkpoint
                and (
                    row["deptype"] != "i"
                    or row["attidentity"] != "d"
                    or row["column_default"] is not None
                )
            )
        ):
            _fail()
        count, maximum = connection.execute(
            text(f'SELECT count(*), max("{column}") FROM public."{table_name}"')
        ).one()
        high_water = row["high_water"]
        if checkpoint:
            # 版本集合是pinned LangGraph迁移契约，不从观察到的max猜测或动态扩展。
            versions = connection.scalar(
                text("SELECT array_agg(v ORDER BY v) FROM public.checkpoint_migrations")
            )
            if versions != list(range(10)):
                _fail()
        if high_water is None:
            if count != 0 and not checkpoint:
                _fail()
            continue
        if (
            type(high_water) is not int
            or not 1 <= high_water < 2 ** (bits - 1) - 1
            or (count and (type(maximum) is not int or high_water < maximum))
        ):
            _fail()


def read_restore_fingerprint(connection: Connection) -> RestoreFingerprint:
    """只读取当前事务中的revision、全部schema/业务行hash及sequence健康。

    任意额外业务schema/relation、丢失表、损坏reserved审计或未知revision都拒绝。
    完成审计的合法插入/保留期删除与安全sequence-ahead不会改变归一化摘要。
    backup/export和verifier调用方必须提供RR/RO事务；starting调用方则在同一owner
    事务内把当前pre观察与CAS一起冻结。本函数只SELECT与设置确定输出的LOCAL选项。
    """
    _canonical_settings(connection)
    versions = (
        connection.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all()
    )
    if len(versions) != 1 or versions[0] not in SUPPORTED_RESTORE_REVISIONS:
        _fail()
    revision = str(versions[0])
    tables, sequences = restore_inventory_objects(revision)
    actual_tables = (
        connection.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname"
            )
        )
        .scalars()
        .all()
    )
    actual_sequences = (
        connection.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='S' ORDER BY c.relname"
            )
        )
        .scalars()
        .all()
    )
    if tuple(actual_tables) != tables or tuple(actual_sequences) != sequences:
        _fail()
    if (
        connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('public','information_schema') AND nspname NOT LIKE 'pg_%') OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind NOT IN ('r','p','S','i'))"
            )
        )
        is not False
    ):
        _fail()
    predicate = restore_completion_audit_predicate()
    if (
        connection.scalar(
            text(
                f"SELECT EXISTS (SELECT 1 FROM public.audit_events a WHERE a.event_type='database.restore.completed' AND NOT ({predicate}))"
            )
        )
        is not False
    ):
        _fail()
    verify_restore_sequence_health(connection, revision=revision)
    aggregates: list[tuple[str, int, str]] = []
    for table_name in tables:
        # 每行256bit固定长度hash先排序再聚合，保留重复行计数，列顺序由jsonb规范化。
        where = f"WHERE NOT ({predicate})" if table_name == "audit_events" else ""
        row = connection.execute(
            text(f"""SELECT count(*),encode(sha256(convert_to(COALESCE(string_agg(h,'' ORDER BY h COLLATE \"C\"),''),'UTF8')),'hex')
            FROM (SELECT encode(sha256(convert_to(to_jsonb(a)::text,'UTF8')),'hex') AS h
            FROM public.\"{table_name}\" a {where}) hashed_rows""")
        ).one()
        if type(row[0]) is not int or type(row[1]) is not str:
            _fail()
        aggregates.append((table_name, row[0], row[1]))
    payload = json.dumps(
        {
            "revision": revision,
            "schema": _schema_digest(connection, revision=revision),
            "tables": aggregates,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return RestoreFingerprint(revision, hashlib.sha256(_FINGERPRINT_DOMAIN + payload).hexdigest())


@contextmanager
def export_backup_snapshot(connection: Connection) -> Iterator[ExportedBackupSnapshot]:
    """在已持锁owner上导出唯一RR/RO快照，dump及所有MVCC摘要读取必须共享它。

    此scope不持有或释放session advisory locks；结束事务后，父流程必须重新读取
    revision、schema与live lease证明，禁止使用旧快照充当post-CAS。
    """
    if connection.in_transaction():
        _fail()
    with connection.begin():
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        snapshot = connection.scalar(text("SELECT pg_export_snapshot()"))
        if (
            type(snapshot) is not str
            or re.fullmatch(r"[0-9A-F]{8}-[0-9A-F]{8}-[0-9]+", snapshot) is None
        ):
            _fail()
        fingerprint = read_restore_fingerprint(connection)
        version = connection.scalar(text("SELECT current_setting('server_version_num')::integer"))
        if type(version) is not int or version < 170000:
            _fail()
        created = connection.scalar(
            text(f"SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC','{_FORMAT_TIME}')")
        )
        yield ExportedBackupSnapshot(
            str(snapshot),
            fingerprint,
            f"{version // 10000}.{version % 10000}",
            str(created),
            _schema_digest(connection, revision=fingerprint.revision),
        )


def verify_restored_backup(connection: Connection, expected: RestoreFingerprint) -> None:
    """在门禁内既有SET ROLE app的READ ONLY事务检查完整post，失败不得重开CONNECT。"""
    if (
        connection.scalar(text("SELECT current_user")) != "ai_employee_app"
        or connection.scalar(text("SHOW transaction_read_only")) != "on"
    ):
        _fail()
    if read_restore_fingerprint(connection) != expected:
        _fail()
