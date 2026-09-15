"""显式构造新 CI service 的真实 0018 测试基线，保留全部失败资源。

本模块只由 ``just prepare-ci-database`` 调用。原集成编排不隐式创建/修补 anchor；
目标必须同时通过 job service、engine 与 PostgreSQL 的身份验证。独立 donor 使用
既有 typed bootstrap/历史迁移，导入只接受本次只读 snapshot 生成的精确私有归档。
这里没有产品 restore authority、角色清理、资源删除或失败重试分支。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from time import monotonic, sleep
from typing import cast
from uuid import uuid4

from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

from ai_employee.cli.database_maintenance import (
    DatabaseEndpoint,
    MigrateArguments,
    RoleBootstrapArguments,
    _open_maintenance_context,
)
from ai_employee.cli.verify_restored_backup import (
    _SCHEMA_QUERIES,
    export_backup_snapshot,
    read_restore_fingerprint,
)
from ai_employee.infrastructure.db.alembic import set_alembic_database_url
from ai_employee.infrastructure.db.database_maintenance import role_bootstrap_database
from ai_employee.infrastructure.db.database_url import (
    ValidatedTestDatabaseUrl,
    validate_test_database_url,
)
from tests.integration.alembic_commands import run_alembic_upgrade
from tests.integration.integration_suite import _ANCHOR_RUNTIME_ROLES_SQL

REVISION = "20260809_0018"
POSTGRES_IMAGE = "postgres:17.5-alpine"
_RUNTIME_ROLES = ("ai_employee_app", "ai_employee_retention")
# 固定 PG17.5 initdb 的三条内建 membership；不能把“无业务角色”误当“无额外继承关系”。
_DEFAULT_MEMBERSHIPS = (
    ("pg_read_all_settings", "pg_monitor", "postgres", False, True, True),
    ("pg_read_all_stats", "pg_monitor", "postgres", False, True, True),
    ("pg_stat_scan_tables", "pg_monitor", "postgres", False, True, True),
)
_DEFAULT_OWNER_ATTRIBUTES = (True, True, True, True, True, True, True, -1, None, None)
_DEFAULT_PUBLIC_ACL = "{pg_database_owner=UC/pg_database_owner,=U/pg_database_owner}"


class CiDatabasePreparationError(RuntimeError):
    """只携带稳定阶段错误，不包含 DSN、engine 原始响应或 Secret。"""


@dataclass(frozen=True)
class CiDatabaseRequest:
    """绑定 workflow 的精确 service 与新私有目录；URL 永不进入 repr 或证据。"""

    repository_root: Path
    directory: Path
    container_id: str
    run_id: str
    run_attempt: str
    job_name: str
    target: ValidatedTestDatabaseUrl = field(repr=False)
    role: str = "target"
    expected_volume: str | None = None
    expected_container_name: str | None = None
    fixture_id: str | None = None

    @property
    def labels(self) -> dict[str, str]:
        """返回身份核验必需的标签；其余 engine 标签也绑定摘要以检测运行中漂移。"""
        labels = {
            "ai_employee.ci_run": self.run_id, "ai_employee.ci_attempt": self.run_attempt,
            "ai_employee.ci_job": self.job_name, "ai_employee.ci_role": self.role,
        }
        if self.fixture_id is not None:
            labels["ai_employee.ci_fixture"] = self.fixture_id
        return labels


@dataclass(frozen=True)
class ServiceBinding:
    """规范化 engine 身份；仅保留无凭据的资源定位事实与完整标签摘要。"""

    container_id: str
    container_name: str
    image_id: str
    database_name: str
    owner_name: str
    host_port: int
    volume_name: str
    volume_mountpoint: str
    labels_digest: str
    volume_labels_digest: str


@dataclass(frozen=True)
class CiDatabaseTarget:
    """同一 endpoint 的 engine 绑定与已校验网络 URL，不以名字代替不可变 ID。"""

    binding: ServiceBinding
    url: ValidatedTestDatabaseUrl = field(repr=False)


@dataclass(frozen=True)
class DatabaseIdentity:
    """网络和容器内只读查询必须完全一致的服务器/数据库身份。"""

    system_identifier: str
    database_oid: int
    database_name: str
    owner_oid: int
    owner_name: str
    server_version: int


@dataclass(frozen=True)
class SeedContents:
    """全部 public schema、表数据和序列的无内容证明，排除不可比较的物理 OID。"""

    revision: str
    fingerprint: str
    schema_categories: tuple[tuple[int, str], ...]
    table_summaries: tuple[tuple[str, int, str], ...]
    namespace_digest: str
    sequence_values: tuple[tuple[str, int, bool], ...]


@dataclass(frozen=True)
class DatabaseSnapshot:
    """完整准入与内容快照；设置和角色全行只记录摘要，避免诊断泄露配置值。"""

    identity: DatabaseIdentity
    owner_role_digest: str
    role_memberships: tuple[tuple[str, str, str, bool, bool, bool], ...]
    database_acl: str | None
    public_schema_owner: str
    public_schema_acl: str | None
    settings_count: int
    settings_digest: str
    runtime_roles: tuple[str, ...]
    runtime_roles_digest: str
    maintenance_fact_count: int
    other_client_count: int
    advisory_lock_count: int
    foreign_schema_count: int
    unexpected_role_count: int
    public_object_count: int
    object_acl_count: int
    contents: SeedContents | None


@dataclass(frozen=True)
class ArchiveRecord:
    """只允许本次导出的私有归档，文件摘要、源 schema/data 与工具版本不可拆分。"""

    path: Path
    size: int
    sha256: str
    contents: SeedContents
    pg_dump_version: str


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise CiDatabasePreparationError(code)


def _mapping(value: object) -> Mapping[str, object]:
    _require(isinstance(value, dict) and all(isinstance(key, str) for key in value), "engine_shape_invalid")
    return cast(Mapping[str, object], value)


def _items(value: object) -> list[object]:
    _require(isinstance(value, list), "engine_shape_invalid")
    return cast(list[object], value)


def _string(value: object) -> str:
    _require(isinstance(value, str) and bool(value), "engine_shape_invalid")
    return cast(str, value)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _image_id(value: object) -> str:
    identifier = _string(value).removeprefix("sha256:")
    _require(re.fullmatch(r"[0-9a-f]{64}", identifier) is not None, "image_identity_invalid")
    return "sha256:" + identifier


def _save(path: Path, value: object) -> None:
    """排他写入 0600 证据并 fsync；既有资源记录和失败产物永不覆盖。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, default=str).encode())
        output.flush()
        os.fsync(output.fileno())


def bind_service(
    request: CiDatabaseRequest, inspected: object, volume: object, image_id: str,
) -> ServiceBinding:
    """同时校验完整 ID、job 标签、不可变镜像、loopback、数据库及精确数据卷。

    输入仅来自 engine 的 JSON 边界；归一化后才允许数据库连接。任何字段缺失、重复
    endpoint、公共端口或不同卷路径都拒绝，不能根据可连接或容器名称推断归属。
    """
    info, volume_info = _mapping(inspected), _mapping(volume)
    config = _mapping(info.get("Config"))
    labels = _mapping(config.get("Labels"))
    _require(info.get("Id") == request.container_id, "service_id_mismatch")
    _require(request.expected_container_name is None
             or _string(info.get("Name")).lstrip("/") == request.expected_container_name, "service_name_mismatch")
    _require(_mapping(info.get("State")).get("Running") is True, "service_not_running")
    _require(all(labels.get(key) == value for key, value in request.labels.items()), "service_job_mismatch")
    _require(_image_id(info.get("Image")) == _image_id(image_id), "service_image_mismatch")
    environment: dict[str, str] = {}
    for raw in _items(config.get("Env")):
        key, separator, value = _string(raw).partition("=")
        _require(bool(separator) and key not in environment, "service_environment_invalid")
        environment[key] = value
    _require(environment.get("POSTGRES_DB") == request.target.database_name, "service_database_mismatch")
    _require(environment.get("POSTGRES_USER") == request.target.parsed.username == "postgres", "service_owner_mismatch")
    _require(request.target.parsed.host == "127.0.0.1", "service_endpoint_invalid")
    ports = _mapping(_mapping(info.get("NetworkSettings")).get("Ports"))
    _require(ports.get("5432/tcp") == [{"HostIp": "127.0.0.1", "HostPort": str(request.target.parsed.port)}], "service_endpoint_mismatch")
    mounts = [_mapping(item) for item in _items(info.get("Mounts"))]
    data_mounts = [item for item in mounts if item.get("Destination") == "/var/lib/postgresql/data"]
    _require(len(data_mounts) == 1, "service_volume_invalid")
    mount = data_mounts[0]
    name = _string(mount.get("Name"))
    _require(mount.get("Type") == "volume" and mount.get("RW") is True, "service_volume_invalid")
    _require(request.expected_volume is None or request.expected_volume == name, "service_volume_mismatch")
    _require(volume_info.get("Name") == name and volume_info.get("Driver") == "local", "service_volume_mismatch")
    _require(volume_info.get("Mountpoint") == mount.get("Source"), "service_volume_path_mismatch")
    if request.fixture_id is not None:
        # donor 的 UUID 在创建前登记，容器与卷必须共同回指同一登记，不能只满足 run/job 标签。
        volume_labels = _mapping(volume_info.get("Labels"))
        _require(all(volume_labels.get(key) == value for key, value in request.labels.items()), "service_volume_labels_mismatch")
    return ServiceBinding(
        request.container_id, _string(info.get("Name")).lstrip("/"), _image_id(image_id),
        request.target.database_name, "postgres", cast(int, request.target.parsed.port), name,
        _string(mount.get("Source")), _digest(labels), _digest(volume_info.get("Labels") or {}),
    )


def _assert_quiet(snapshot: DatabaseSnapshot) -> None:
    _require(snapshot.identity.server_version == 170005, "postgres_version_mismatch")
    _require(snapshot.owner_role_digest == _digest(_DEFAULT_OWNER_ATTRIBUTES)
             and snapshot.role_memberships == _DEFAULT_MEMBERSHIPS, "cluster_role_posture_changed")
    _require(not any((snapshot.settings_count, snapshot.maintenance_fact_count, snapshot.other_client_count,
                      snapshot.advisory_lock_count, snapshot.foreign_schema_count, snapshot.unexpected_role_count)), "database_not_quiet")


def _assert_pristine(snapshot: DatabaseSnapshot) -> None:
    """仅接受整个 public 空、原 ACL 和角色缺失的新服务，任何已有状态均保留并拒绝。"""
    _assert_quiet(snapshot)
    _require(snapshot.database_acl is None and not snapshot.runtime_roles and snapshot.public_object_count == 0
             and snapshot.object_acl_count == 0 and snapshot.contents is None, "target_not_pristine")
    _require(snapshot.public_schema_owner == "pg_database_owner"
             and snapshot.public_schema_acl == _DEFAULT_PUBLIC_ACL, "public_schema_not_pristine")


def _assert_empty_contents(contents: SeedContents | None) -> None:
    _require(contents is not None and contents.revision == REVISION, "seed_revision_invalid")
    assert contents is not None
    _require(bool(contents.schema_categories) and bool(contents.table_summaries), "seed_evidence_empty")
    _require(all(count == 0 for name, count, _ in contents.table_summaries if name not in {"alembic_version", "checkpoint_migrations"}), "seed_business_data_present")
    _require(any(name == "alembic_version" and count == 1 for name, count, _ in contents.table_summaries), "seed_version_row_invalid")


def _archive_bytes(record: ArchiveRecord, directory: Path) -> bytes:
    """导入前重新检查唯一私有路径、owner/mode、固定格式与精确哈希，拒绝替换归档。"""
    _require(record.path == directory / "empty-0018.dump", "archive_path_mismatch")
    descriptor = os.open(record.path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        _require(info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o600, "archive_permissions_invalid")
        data = source.read()
    _require(data.startswith(b"PGDMP") and len(data) == record.size and hashlib.sha256(data).hexdigest() == record.sha256, "archive_changed")
    return data


def prepare_ci_database(request: CiDatabaseRequest) -> None:
    """逐阶段冻结身份/完整快照与归档；失败零修补，只有完整后验后发布 ready。

    Args:
        request: 已校验的本 job service 身份和不存在的私有目录。
    Raises:
        CiDatabasePreparationError: 身份、空态、归档或后验不匹配，保留失败现场。
        Exception: 外部工具或数据库失败；同样不重试、不清理，记录静态阶段及异常类别。
    """
    _require(request.directory.is_absolute() and not request.directory.exists()
             and request.directory.parent.resolve() == request.directory.parent, "evidence_directory_invalid")
    request.directory.mkdir(mode=0o700)
    phase = "target_identity"
    try:
        operations = NativeCiDatabaseOperations(request)
        target = operations.inspect_target()
        before = operations.observe(target)
        _assert_pristine(before)
        _save(request.directory / "target-pristine.json", {"binding": asdict(target.binding), "snapshot": asdict(before)})
        phase = "donor_create"
        donor = operations.create_donor(target.binding.image_id)
        _assert_pristine(operations.observe(donor))
        phase = "donor_migrate"
        operations.initialize_donor(donor)
        donor_before = operations.observe(donor)
        _assert_quiet(donor_before)
        _assert_empty_contents(donor_before.contents)
        _save(request.directory / "donor-baseline.json", {"binding": asdict(donor.binding), "snapshot": asdict(donor_before)})
        phase = "donor_dump"
        archive = operations.export_donor(donor)
        _require(archive.contents == donor_before.contents, "donor_snapshot_changed")
        _archive_bytes(archive, request.directory)
        _save(request.directory / "archive.json", asdict(archive))
        phase = "target_pre_import"
        _require(operations.observe(target) == before, "target_snapshot_changed")
        phase = "target_import"
        operations.import_archive(target, archive)
        phase = "target_verify"
        after = operations.observe(target)
        _assert_quiet(after)
        _require(after.contents == archive.contents and after.public_object_count == donor_before.public_object_count, "seed_contents_mismatch")
        # 只有 public 对象及其内容允许变化。ACL、owner、设置、角色和全部空闲事实
        # 必须逐字段仍等于原快照；物理身份从不拿 donor 的 OID 替换 target 的 OID。
        _require(replace(after, contents=None, public_object_count=0) == before, "target_posture_changed")
        _save(request.directory / "target-seeded.json", asdict(after))
        phase = "donor_stop"
        _require(operations.observe(donor) == donor_before, "donor_changed_before_stop")
        operations.stop_donor(donor)
        _save(request.directory / "ready.json", {
            "result_code": "ci_database_anchor_ready", "revision": REVISION,
            "target": asdict(target.binding), "donor": asdict(donor.binding),
            "archive_sha256": archive.sha256, "contents": asdict(archive.contents),
            "donor_stopped": True, "resources_preserved": True,
        })
    except BaseException as error:
        _save(request.directory / "failure.json", {"phase": phase, "error_type": type(error).__name__, "resources_preserved": True})
        raise


_IDENTITY_SQL = """SELECT control.system_identifier::text AS system_identifier,
    db.oid::bigint AS database_oid, db.datname AS database_name, db.datdba::bigint AS owner_oid,
    pg_get_userbyid(db.datdba) AS owner_name, current_setting('server_version_num')::integer AS server_version
FROM pg_database db CROSS JOIN pg_control_system() control WHERE db.datname=current_database()"""

_OWNER_ROLE_SQL = """SELECT rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,
rolreplication,rolbypassrls,rolconnlimit,rolvaliduntil,rolconfig FROM pg_roles WHERE oid=:owner_oid"""

_MEMBERSHIPS_SQL = """SELECT parent.rolname,member_role.rolname,grantor.rolname,
membership.admin_option,membership.inherit_option,membership.set_option
FROM pg_auth_members membership
JOIN pg_roles parent ON parent.oid=membership.roleid
JOIN pg_roles member_role ON member_role.oid=membership.member
JOIN pg_roles grantor ON grantor.oid=membership.grantor
ORDER BY parent.rolname COLLATE "C",member_role.rolname COLLATE "C",grantor.rolname COLLATE "C",
membership.admin_option,membership.inherit_option,membership.set_option"""

_NAMESPACE_SQL = """SELECT identified.type, identified.schema, identified.name, identified.identity
FROM pg_depend dependency
CROSS JOIN LATERAL pg_identify_object(dependency.classid,dependency.objid,dependency.objsubid) identified
WHERE dependency.refclassid='pg_namespace'::regclass
  AND dependency.refobjid=(SELECT oid FROM pg_namespace WHERE nspname='public')
ORDER BY identified.type,identified.schema,identified.name,identified.identity"""

_OBJECT_ACL_COUNT_SQL = """SELECT
 (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relacl IS NOT NULL)
+(SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND a.attacl IS NOT NULL)
+(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proacl IS NOT NULL)
+(SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public' AND t.typacl IS NOT NULL)
+(SELECT count(*) FROM pg_default_acl)"""


def _identity(value: Mapping[str, object]) -> DatabaseIdentity:
    """同时收窄网络/native 两种查询的固定结果，拒绝未知服务器或缺失 owner。"""
    identifier = value.get("system_identifier")
    _require(isinstance(identifier, str) and identifier.isdecimal() and int(identifier) > 0, "server_identity_invalid")
    for key in ("database_oid", "owner_oid", "server_version"):
        _require(type(value.get(key)) is int and cast(int, value[key]) > 0, "server_identity_invalid")
    return DatabaseIdentity(
        cast(str, identifier), cast(int, value["database_oid"]), _string(value.get("database_name")),
        cast(int, value["owner_oid"]), _string(value.get("owner_name")), cast(int, value["server_version"]),
    )


def _count(connection: Connection, sql: str) -> int:
    value = connection.scalar(text(sql))
    _require(type(value) is int and value >= 0, "catalog_count_invalid")
    return cast(int, value)


def _contents(connection: Connection) -> SeedContents:
    """在调用方同一只读快照内，计算每类完整 schema、全部表和精确序列的证明。

    复用产品冻结的 schema/fingerprint 查询；namespace 的全对象身份摘要额外覆盖
    关系之外的类型/函数等对象。所有行内容与 SQL 定义在 PostgreSQL 内聚合为摘要；
    序列值只属于该无并发业务写入的合成 donor，不能推广为生产序列回滚保证。
    """
    fingerprint = read_restore_fingerprint(connection)
    categories: list[tuple[int, str]] = []
    for sql in _SCHEMA_QUERIES:
        count, digest = connection.execute(text(
            "SELECT count(*),encode(sha256(convert_to(COALESCE(jsonb_agg(item)::text,'[]'),'UTF8')),'hex') FROM ("
            + sql + ") complete_schema"
        ), {"fingerprint_revision": fingerprint.revision}).one()
        categories.append((int(count), str(digest)))
    tables = connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).scalars()
    summaries: list[tuple[str, int, str]] = []
    for table in tables:
        quoted = connection.dialect.identifier_preparer.quote_identifier(table)
        count, digest = connection.execute(text(
            "SELECT count(*),encode(sha256(convert_to(COALESCE(string_agg(h,'' ORDER BY h COLLATE \"C\"),''),'UTF8')),'hex') "
            "FROM (SELECT encode(sha256(convert_to(to_jsonb(row_value)::text,'UTF8')),'hex') h FROM public."
            + quoted + " row_value) complete_rows"
        )).one()
        summaries.append((str(table), int(count), str(digest)))
    sequences: list[tuple[str, int, bool]] = []
    names = connection.execute(text("SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename")).scalars()
    for name in names:
        quoted = connection.dialect.identifier_preparer.quote_identifier(name)
        value, called = connection.execute(text("SELECT last_value,is_called FROM public." + quoted)).one()
        sequences.append((str(name), int(value), bool(called)))
    return SeedContents(fingerprint.revision, fingerprint.digest, tuple(categories), tuple(summaries),
                        _digest([tuple(row) for row in connection.execute(text(_NAMESPACE_SQL))]), tuple(sequences))


def _snapshot(connection: Connection) -> DatabaseSnapshot:
    """采集一份完整只读事实；设置/角色原始行只在内存参与摘要，不进入诊断文件。"""
    identity = _identity(connection.execute(text(_IDENTITY_SQL)).mappings().one())
    owner_attributes = tuple(connection.execute(text(_OWNER_ROLE_SQL), {"owner_oid": identity.owner_oid}).one())
    memberships: list[tuple[str, str, str, bool, bool, bool]] = []
    for row in connection.execute(text(_MEMBERSHIPS_SQL)):
        _require(all(type(value) is bool for value in row[3:]), "membership_catalog_invalid")
        memberships.append((_string(row[0]), _string(row[1]), _string(row[2]), row[3], row[4], row[5]))
    acl = connection.scalar(text("SELECT datacl::text FROM pg_database WHERE datname=current_database()"))
    schema_owner, schema_acl = connection.execute(text("SELECT pg_get_userbyid(nspowner),nspacl::text FROM pg_namespace WHERE nspname='public'")).one()
    settings = [tuple(row) for row in connection.execute(text("SELECT setdatabase,setrole,setconfig FROM pg_db_role_setting ORDER BY setdatabase,setrole"))]
    roles = [tuple(row) for row in connection.execute(text(_ANCHOR_RUNTIME_ROLES_SQL), {
        "app_role_name": _RUNTIME_ROLES[0], "retention_role_name": _RUNTIME_ROLES[1],
    })]
    public_objects = _count(connection, "SELECT count(*) FROM (" + _NAMESPACE_SQL + ") complete_objects")
    content = _contents(connection) if connection.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")) else None
    return DatabaseSnapshot(
        identity, _digest(owner_attributes), tuple(memberships), acl, str(schema_owner), schema_acl, len(settings), _digest(settings),
        tuple(str(row[0]) for row in roles), _digest(roles),
        _count(connection, "SELECT count(*) FROM pg_db_role_setting CROSS JOIN LATERAL unnest(setconfig) value WHERE split_part(value,'=',1) LIKE 'ai_employee.%'"),
        _count(connection, "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid()"),
        _count(connection, "SELECT count(*) FROM pg_locks WHERE locktype='advisory'"),
        _count(connection, "SELECT count(*) FROM pg_namespace WHERE left(nspname,3)<>'pg_' AND nspname NOT IN ('public','information_schema')"),
        _count(connection, "SELECT count(*) FROM pg_roles WHERE left(rolname,3)<>'pg_' AND rolname NOT IN ('postgres','ai_employee_app','ai_employee_retention')"),
        public_objects, _count(connection, _OBJECT_ACL_COUNT_SQL), content,
    )


class NativeCiDatabaseOperations:
    """仅对已绑定 service 和本实例新建 donor 执行原生 I/O，错误不回显原始响应。

    外部工具只走 Docker-compatible API；本地验证沿用显式 engine wrapper。网络查询
    使用完整已验证 URL，native 查询独立对照 cluster/OID/owner，防止端口误指另一服务。
    """

    def __init__(self, request: CiDatabaseRequest) -> None:
        self.request = request
        self._requests = {request.container_id: request}
        self._donor_secrets: dict[str, Path] = {}
        self._command_count = 0
        (request.directory / "commands").mkdir(mode=0o700)
        source_files = sorted({
            *request.repository_root.joinpath("backend/src").rglob("*.py"),
            *request.repository_root.joinpath("backend/migrations").rglob("*.py"),
            Path(__file__), request.repository_root / "backend/tests/integration/alembic_commands.py",
            request.repository_root / "backend/uv.lock", request.repository_root / "scripts/prepare_ci_database.py",
        })
        sources = [(str(path.relative_to(request.repository_root)), hashlib.sha256(path.read_bytes()).hexdigest()) for path in source_files]
        _save(request.directory / "sources.json", {"sha256": _digest(sources), "files": sources})

    def _execute(self, arguments: Sequence[str], *, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        """保留无内容工具回执；inspect 或失败消息可能携带凭据，原始输出不落日志。"""
        result = subprocess.run(["docker", *arguments], input=data, capture_output=True, check=False)
        self._command_count += 1
        _save(self.request.directory / "commands" / f"{self._command_count:04d}.json", {
            "arguments": list(arguments), "exit_code": result.returncode,
            "stdout_bytes": len(result.stdout), "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "stderr_bytes": len(result.stderr), "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
        })
        return result

    def _docker(self, *arguments: str, data: bytes | None = None) -> bytes:
        result = self._execute(arguments, data=data)
        _require(result.returncode == 0, "engine_command_failed")
        return result.stdout

    def _json(self, *arguments: str) -> Mapping[str, object]:
        rows = _items(json.loads(self._docker(*arguments)))
        _require(len(rows) == 1, "engine_identity_ambiguous")
        return _mapping(rows[0])

    def _binding(self, request: CiDatabaseRequest, image_id: str) -> ServiceBinding:
        info = self._json("inspect", request.container_id)
        mounts = [_mapping(item) for item in _items(info.get("Mounts"))]
        names = [_string(item.get("Name")) for item in mounts if item.get("Destination") == "/var/lib/postgresql/data"]
        _require(len(names) == 1, "service_volume_invalid")
        volume = self._json("volume", "inspect", names[0])
        return bind_service(request, info, volume, image_id)

    def _revalidate(self, target: CiDatabaseTarget) -> None:
        _require(self._binding(self._requests[target.binding.container_id], target.binding.image_id) == target.binding, "service_identity_changed")

    def inspect_target(self) -> CiDatabaseTarget:
        """以固定版本的本地 image ID 比较 job service，不拉取镜像或根据标签猜测 ID。"""
        image_id = _image_id(self._docker("image", "inspect", "--format", "{{.Id}}", POSTGRES_IMAGE).decode().strip())
        return CiDatabaseTarget(self._binding(self.request, image_id), self.request.target)

    def observe(self, target: CiDatabaseTarget) -> DatabaseSnapshot:
        """重验 engine 绑定并对照网络/native 身份；所有连接在返回前退出。"""
        self._revalidate(target)
        engine = create_engine(target.url.parsed.set(drivername="postgresql+psycopg"), poolclass=NullPool,
                               hide_parameters=True, connect_args={"connect_timeout": 5})
        try:
            with engine.connect() as connection, connection.begin():
                connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                snapshot = _snapshot(connection)
        finally:
            engine.dispose()
        native = json.loads(self._docker(
            "exec", "--env", "PGOPTIONS=-c default_transaction_read_only=on", target.binding.container_id,
            "psql", "--no-psqlrc", "--quiet", "--tuples-only", "--no-align", "--set=ON_ERROR_STOP=1",
            "--username=postgres", "--dbname=" + target.binding.database_name,
            "--command=SELECT row_to_json(identity_row) FROM (" + _IDENTITY_SQL + ") identity_row",
        ))
        _require(snapshot.identity == _identity(_mapping(native)), "network_native_identity_mismatch")
        _require(snapshot.identity.database_name == target.binding.database_name
                 and snapshot.identity.owner_name == target.binding.owner_name, "database_owner_mismatch")
        self._revalidate(target)
        return snapshot

    def create_donor(self, image_id: str) -> CiDatabaseTarget:
        """先登记 UUID 及不存在证据，再创建独立 donor；没有复用、删除或失败清理分支。"""
        identifier = uuid4().hex
        name, volume = "ai-employee-ci-donor-" + identifier, "ai-employee-ci-donor-data-" + identifier
        database = "ai_employee_ci_" + identifier + "_test"
        labels = {**replace(self.request, role="donor").labels, "ai_employee.ci_fixture": identifier}
        _save(self.request.directory / "donor-registered.json", {"container_name": name, "volume": volume, "database": database, "labels": labels, "image_id": image_id})
        existing_containers = self._docker("container", "ls", "--all", "--filter", "name=" + name, "--format", "{{.Names}}")
        existing_volumes = self._docker("volume", "ls", "--filter", "name=" + volume, "--format", "{{.Name}}")
        _require(not existing_containers.strip() and not existing_volumes.strip(), "donor_resource_exists")
        _save(self.request.directory / "donor-absence.json", {"container_absent": True, "volume_absent": True})
        secret_directory = self.request.directory / "donor-secrets"
        secret_directory.mkdir(mode=0o700)
        for secret_name in ("owner", "app", "retention"):
            _save(secret_directory / secret_name, secrets.token_urlsafe(40).encode())
        label_args = [piece for key, value in labels.items() for piece in ("--label", key + "=" + value)]
        _require(self._docker("volume", "create", *label_args, volume).decode().strip() == volume, "donor_volume_creation_invalid")
        container_id = self._docker(
            "run", "--detach", "--pull", "never", "--name", name, "--user", "0:0", *label_args,
            "--publish", "127.0.0.1::5432", "--volume", volume + ":/var/lib/postgresql/data",
            "--volume", str(secret_directory / "owner") + ":/run/secrets/owner:ro",
            "--env", "POSTGRES_USER=postgres", "--env", "POSTGRES_DB=" + database,
            "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/owner", image_id,
        ).decode().strip()
        _require(re.fullmatch(r"[0-9a-f]{64}", container_id) is not None, "donor_container_creation_invalid")
        _save(self.request.directory / "donor-created.json", {"container_id": container_id, "container_name": name, "volume": volume})
        info = self._json("inspect", container_id)
        ports = _items(_mapping(_mapping(info.get("NetworkSettings")).get("Ports")).get("5432/tcp"))
        _require(len(ports) == 1, "donor_endpoint_invalid")
        port = int(_string(_mapping(ports[0]).get("HostPort")))
        donor_url = self.request.target.parsed.set(database=database, port=port, password=(secret_directory / "owner").read_text())
        request = replace(self.request, container_id=container_id, role="donor", expected_volume=volume,
                          expected_container_name=name, fixture_id=identifier,
                          target=validate_test_database_url(donor_url.render_as_string(hide_password=False)))
        self._requests[container_id] = request
        self._donor_secrets[container_id] = secret_directory
        donor = CiDatabaseTarget(self._binding(request, image_id), request.target)
        deadline = monotonic() + 60
        while True:
            result = self._execute(("exec", container_id, "pg_isready", "--host=127.0.0.1", "--username=postgres", "--dbname=" + database))
            if result.returncode == 0:
                break
            _require(monotonic() < deadline, "donor_readiness_timeout")
            sleep(0.1)
        _save(self.request.directory / "donor-identity.json", asdict(donor.binding))
        return donor

    def _donor_endpoint(self, donor: CiDatabaseTarget) -> tuple[DatabaseEndpoint, Path]:
        _require(donor.binding.container_id in self._donor_secrets, "target_is_not_owned_donor")
        self._revalidate(donor)
        return DatabaseEndpoint("127.0.0.1", donor.binding.host_port, "postgres", donor.binding.database_name), self._donor_secrets[donor.binding.container_id]

    def initialize_donor(self, donor: CiDatabaseTarget) -> None:
        """复用唯一 typed bootstrap 与原历史升级 helper，不向产品增加版本选择旁路。"""
        endpoint, secret_directory = self._donor_endpoint(donor)
        arguments = RoleBootstrapArguments(secret_directory / "owner", secret_directory / "app", secret_directory / "retention")
        with _open_maintenance_context(arguments, endpoint) as context:
            role_bootstrap_database(context)
        config = Config(self.request.repository_root / "backend/alembic.ini")
        set_alembic_database_url(config, donor.url.value)
        run_alembic_upgrade(config, REVISION)

    def export_donor(self, donor: CiDatabaseTarget) -> ArchiveRecord:
        """同一 typed holder 和只读导出 snapshot 覆盖完整证明与 native pg_dump。"""
        endpoint, secret_directory = self._donor_endpoint(donor)
        with (_open_maintenance_context(MigrateArguments(secret_directory / "owner"), endpoint) as context,
              context.acquire_management_lifecycle_lock() as management,
              management.acquire_target_lock() as target, target.acquire_schema_lifecycle_lock(),
              target.read_only_holder() as holder):
            holder.assert_idle()
            with export_backup_snapshot(holder.connection) as snapshot:
                contents = _contents(holder.connection)
                _assert_empty_contents(contents)
                data = self._docker(
                    "exec", "--env", "PGOPTIONS=-c default_transaction_read_only=on", donor.binding.container_id,
                    "pg_dump", "--username=postgres", "--dbname=" + donor.binding.database_name,
                    "--format=custom", "--no-owner", "--no-privileges", "--snapshot=" + snapshot.snapshot_id,
                )
                _require(data.startswith(b"PGDMP"), "archive_format_invalid")
                path = self.request.directory / "empty-0018.dump"
                _save(path, data)
                version = self._docker("exec", donor.binding.container_id, "pg_dump", "--version").decode().strip()
                _require(re.fullmatch(r"pg_dump \(PostgreSQL\) 17\.5(?: .*)?", version) is not None, "dump_version_invalid")
                _save(self.request.directory / "donor-snapshot.json", {
                    "snapshot_id": snapshot.snapshot_id, "image_id": donor.binding.image_id,
                    "fingerprint": snapshot.fingerprint.digest, "schema_digest": snapshot.schema_digest,
                    "postgres_server_version": snapshot.postgres_server_version, "read_only": True,
                })
            holder.assert_idle()
        return ArchiveRecord(path, len(data), hashlib.sha256(data).hexdigest(), contents, version)

    def import_archive(self, target: CiDatabaseTarget, archive: ArchiveRecord) -> None:
        """只向原 CI target 导入本次精确归档；固定无 clean/create/drop 且一次执行。"""
        _require(target.binding.container_id == self.request.container_id, "seed_target_mismatch")
        self._revalidate(target)
        data = _archive_bytes(archive, self.request.directory)
        arguments = (
            "exec", "-i", target.binding.container_id, "pg_restore", "--username=postgres",
            "--dbname=" + target.binding.database_name, "--single-transaction", "--exit-on-error", "--no-owner", "--no-privileges",
        )
        _save(self.request.directory / "import-attempt.json", {"target": asdict(target.binding), "archive_sha256": archive.sha256, "arguments": arguments})
        self._docker(*arguments, data=data)

    def stop_donor(self, donor: CiDatabaseTarget) -> None:
        """完整后验后停止精确自有 donor，保留容器、卷和归档；不停止 target。"""
        self._donor_endpoint(donor)
        self._docker("stop", "--time", "20", donor.binding.container_id)
        info = self._json("inspect", donor.binding.container_id)
        _require(info.get("Id") == donor.binding.container_id and _mapping(info.get("State")).get("Running") is False, "donor_stop_unproved")


def read_request(environment: Mapping[str, str], repository_root: Path) -> CiDatabaseRequest:
    """从显式 workflow 参数创建请求；拒绝缺失身份、隐式 libpq 覆盖和非本地服务。"""
    container_id = environment.get("CI_DATABASE_CONTAINER_ID", "")
    run_id, attempt = environment.get("CI_DATABASE_RUN_ID", ""), environment.get("CI_DATABASE_RUN_ATTEMPT", "")
    _require(re.fullmatch(r"[0-9a-f]{64}", container_id) is not None, "service_id_required")
    _require(re.fullmatch(r"[1-9][0-9]*", run_id) is not None and re.fullmatch(r"[1-9][0-9]*", attempt) is not None
             and environment.get("CI_DATABASE_JOB") == "verify", "job_identity_required")
    _require(not any(key in environment for key in (
        "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGPASSFILE",
    )), "libpq_override_rejected")
    target = validate_test_database_url(environment.get("TEST_DATABASE_URL", ""))
    _require(target.parsed.host == "127.0.0.1" and target.parsed.username == "postgres"
             and target.database_name == "ai_employee_test", "ci_target_invalid")
    temporary = Path(environment.get("RUNNER_TEMP", ""))
    _require(temporary.is_absolute() and temporary.is_dir() and temporary.resolve() == temporary
             and temporary.stat().st_uid == os.getuid(), "runner_directory_invalid")
    return CiDatabaseRequest(repository_root, temporary / f"ai-employee-ci-0018-{run_id}-{attempt}-verify",
                             container_id, run_id, attempt, "verify", target)


def main() -> int:
    """只输出稳定结果与私有目录位置；未知数据库/engine 错误不把原始文本写到控制台。"""
    try:
        root = Path(__file__).resolve().parents[3]
        _require(Path.cwd() == root, "repository_directory_required")
        request = read_request(os.environ, root)
        prepare_ci_database(request)
    except Exception as error:  # noqa: BLE001 - DBAPI/engine 异常可能含凭据，只输出类型。
        print("CI database preparation failed; resources preserved; error_type=" + type(error).__name__)
        return 1
    print("CI synthetic database anchor ready; revision=" + REVISION + "; evidence=" + str(request.directory))
    return 0
