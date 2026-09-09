#!/usr/bin/env python3
"""仅development/test的旧备份隔离转换与固定grace回收。

registry先于任何临时Secret/Compose/容器资源持久化。每个project只拥有独立internal
网络和空卷；容器内先真实恢复并只读验证，再调用普通锁定备份。这里不导入通用恢复
executor、不修改workspace catalog，不制造manifest、不修复或stamp revision。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadRolloutError,
)
from ai_employee.infrastructure.db.database_maintenance import RestoreFingerprint
from ai_employee.infrastructure.db.postgres_backup_manifest import (
    ORPHAN_GRACE_SECONDS,
    _regular,
    _unique_object,
    validate_backup_basename,
)
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

_SCHEMA = "ai_employee.legacy_backup_conversion.v1"
_REGISTRY = ".legacy-conversion-registry"
_KIND = "legacy_conversion"
_DATABASE = "ai_employee_legacy_conversion"
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")


class LegacyConversionError(RuntimeError):
    """只暴露固定错误码；daemon输出、文件路径和任何Secret内容均不回显。"""


@dataclass(frozen=True)
class LegacyRegistry:
    """可独立核查的资源归属；路径/名字均由UUID和已验证输出根确定，不接受任意删除对象。"""

    schema: str
    attempt_id: str
    created_at: str
    kind: str
    backup_directory: str
    source_basename: str
    source_sha256: str
    source_size: int
    output_basename: str
    image_id: str
    project: str
    postgres_container: str
    converter_container: str
    network: str
    volume: str
    secret_path: str

    @property
    def labels(self) -> dict[str, str]:
        """每个container/network/volume都必须逐项匹配的最小归属标签。"""
        return {
            "com.ai-employee.maintenance.kind": _KIND,
            "com.ai-employee.maintenance.attempt": self.attempt_id,
            "com.ai-employee.maintenance.created_at": self.created_at,
        }

    def canonical_bytes(self) -> bytes:
        """只保存内容无关字段；canonical字节使registry替换可被精确识别。"""
        return (
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise LegacyConversionError("legacy_metadata_invalid")
    return value


def _directory(path: Path, *, create: bool = False) -> Path:
    """可写根与registry都必须是本UID的0700真实目录；不把relative或symlink当删除依据。"""
    if (
        not path.is_absolute()
        or path == Path("/")
        or any(item.is_symlink() for item in (path, *path.parents))
    ):
        raise LegacyConversionError("legacy_directory_invalid")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise LegacyConversionError("legacy_directory_invalid")
    return path


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_registry(record: LegacyRegistry, directory: Path | None = None) -> None:
    """重算每个名字/路径/label，不允许编辑registry把scavenger指向其他project或Secret。"""
    try:
        if (
            str(UUID(record.attempt_id)) != record.attempt_id
            or _TIME.fullmatch(record.created_at) is None
        ):
            raise ValueError
        datetime.fromisoformat(record.created_at)
        validate_backup_basename(record.output_basename)
        if not record.source_basename.endswith(".dump.enc"):
            raise ValueError
        validate_backup_basename(record.source_basename.removesuffix(".dump.enc"))
        root = Path(record.backup_directory)
        project = f"aiemployee-legacy-{UUID(record.attempt_id).hex}"
        if (
            record.schema != _SCHEMA
            or record.kind != _KIND
            or not root.is_absolute()
            or root == Path("/")
            or ".." in root.parts
            or (directory is not None and root != directory)
            or _IMAGE.fullmatch(record.image_id) is None
            or _HEX.fullmatch(record.source_sha256) is None
            or type(record.source_size) is not int
            or record.source_size < 1
            or record.project != project
            or record.postgres_container != project + "-postgres"
            or record.converter_container != project + "-converter"
            or record.network != project + "-internal"
            or record.volume != project + "-postgres-data"
            or record.secret_path
            != str(root / _REGISTRY / (record.attempt_id + ".secret"))
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise LegacyConversionError("legacy_registry_invalid") from None


def read_registry(path: Path, directory: Path | None = None) -> LegacyRegistry:
    """只读0600固定schema并重算canonical文件名/全部派生事实，未知文件不产生删除资格。"""
    raw, _, _ = _regular(path, limit=16384)
    try:
        values = _mapping(json.loads(raw, object_pairs_hook=_unique_object))
        record = LegacyRegistry(**values)
        _validate_registry(record, directory)
        if record.canonical_bytes() != raw:
            raise ValueError
        if directory is not None and path != directory / _REGISTRY / (
            record.attempt_id + ".json"
        ):
            raise ValueError
        return record
    except (TypeError, ValueError):
        raise LegacyConversionError("legacy_registry_invalid") from None


def _publish_registry(record: LegacyRegistry) -> Path:
    """先完成文件与目录fsync的no-clobber发布；返回前不得创建任何本次临时资源。"""
    directory = _directory(Path(record.backup_directory) / _REGISTRY, create=True)
    target = directory / f"{record.attempt_id}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".registry-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(record.canonical_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target, follow_symlinks=False)
        _sync_directory(directory)
    finally:
        os.unlink(temporary)
    return target


@contextmanager
def _attempt_lock(record: LegacyRegistry) -> Iterator[bool]:
    """每次attempt有固定0600 nonblocking flock；活跃调用和scavenger永不同时接管。"""
    path = Path(record.backup_directory) / _REGISTRY / (record.attempt_id + ".lock")
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise LegacyConversionError("legacy_lock_invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def _docker(
    *arguments: str, environment: Mapping[str, str] | None = None, timeout: int = 60
) -> str:
    """固定engine操作吸收所有原始诊断；未知结果不能作为资源不存在或已清理的证据。"""
    result = subprocess.run(
        ["docker", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        raise LegacyConversionError("legacy_docker_failed")
    return result.stdout


def _template(record: LegacyRegistry) -> dict[str, object]:
    """只渲染专用profile模板；仅把UUID等非Secret元数据注入，不继承workspace连接或卷。"""
    environment = {
        **os.environ,
        "LEGACY_PROJECT_NAME": record.project,
        "LEGACY_ATTEMPT_ID": record.attempt_id,
        "LEGACY_CREATED_AT": record.created_at,
        "LEGACY_DATABASE_SECRET_PATH": record.secret_path,
    }
    raw = json.loads(
        _docker(
            "compose",
            "--profile",
            "legacy-conversion",
            "config",
            "--format",
            "json",
            environment=environment,
        )
    )
    services = _mapping(_mapping(raw).get("services"))
    postgres = _mapping(services.get("legacy-conversion-postgres"))
    converter = _mapping(services.get("legacy-backup-converter"))
    if (
        postgres.get("image") != "docker.io/library/postgres:17.5-alpine"
        or postgres.get("ports")
        or converter.get("ports")
        or postgres.get("privileged")
        or converter.get("privileged")
    ):
        raise LegacyConversionError("legacy_template_invalid")
    # 运行配置只保留明确定义的隔离服务；全部网络/卷/命令/环境由registry封闭生成。
    return _mapping(raw)


def _configuration(
    record: LegacyRegistry, template: dict[str, object], source: Path | None
) -> dict[str, object]:
    """生成仅包含两项专用服务的配置；down也使用相同project/names/labels，不执行通用服务。"""
    labels = record.labels
    common = {
        "profiles": ["legacy-conversion"],
        "restart": "no",
        "networks": ["legacy_conversion"],
        "labels": labels,
    }
    postgres = {
        **common,
        "image": "docker.io/library/postgres:17.5-alpine",
        # 官方入口需先初始化本次空卷再自行降权；固定默认root，避免rootless引擎继承宿主UID。
        "user": "0:0",
        "container_name": record.postgres_container,
        "environment": {
            "POSTGRES_USER": "ai_employee_owner",
            "POSTGRES_DB": _DATABASE,
            "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_bootstrap_password",
        },
        "secrets": [
            {
                "source": "legacy_database_password",
                "target": "postgres_bootstrap_password",
            }
        ],
        "volumes": ["legacy_conversion_data:/var/lib/postgresql/data"],
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U ai_employee_owner -d " + _DATABASE],
            "interval": "2s",
            "timeout": "3s",
            "retries": 30,
        },
    }
    secret_targets = [
        "postgres_bootstrap_password",
        "app_database_password",
        "retention_database_password",
    ]
    volumes = [
        {"type": "bind", "source": record.backup_directory, "target": "/backups"},
        {
            "type": "bind",
            "source": str(
                Path(record.backup_directory)
                / _REGISTRY
                / (record.attempt_id + ".json")
            ),
            "target": "/legacy/registry.json",
            "read_only": True,
        },
    ]
    if source is not None:
        volumes.append(
            {
                "type": "bind",
                "source": str(source.parent),
                "target": "/legacy/source",
                "read_only": True,
            }
        )
    converter = {
        **common,
        "image": record.image_id,
        "pull_policy": "never",
        "container_name": record.converter_container,
        "user": f"{os.geteuid()}:{os.getegid()}",
        "environment": {
            "APP_ENV": "test",
            "DATABASE_URL": "postgresql+psycopg://ai_employee_owner@legacy-conversion-postgres:5432/"
            + _DATABASE,
            "BACKUP_DIR": "/backups",
            "LEGACY_ATTEMPT_ID": record.attempt_id,
        },
        "secrets": [
            {"source": "legacy_database_password", "target": name}
            for name in secret_targets
        ]
        + ["backup_passphrase"],
        "volumes": volumes,
        "command": [
            "/app/backend/.venv/bin/python",
            "/app/scripts/legacy-backup-operations.py",
            "isolated",
        ],
        "depends_on": {"legacy-conversion-postgres": {"condition": "service_healthy"}},
    }
    definitions = _mapping(template.get("secrets", {}))
    backup_secret = definitions.get("backup_passphrase", {"external": True})
    return {
        "name": record.project,
        "services": {
            "legacy-conversion-postgres": postgres,
            "legacy-backup-converter": converter,
        },
        "networks": {
            "legacy_conversion": {
                "name": record.network,
                "internal": True,
                "labels": labels,
            }
        },
        "volumes": {
            "legacy_conversion_data": {"name": record.volume, "labels": labels}
        },
        "secrets": {
            "legacy_database_password": {"file": record.secret_path},
            "backup_passphrase": backup_secret,
        },
    }


def _compose(record: LegacyRegistry, config: dict[str, object], *arguments: str) -> str:
    """registry已落盘后才创建0600临时Compose；无pull/build、无workspace profile继承。"""
    with tempfile.TemporaryDirectory(prefix="legacy-compose-") as directory:
        path = Path(directory) / "compose.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream)
        return _docker(
            "compose",
            "-p",
            record.project,
            "-f",
            str(path),
            "--profile",
            "legacy-conversion",
            *arguments,
            timeout=900,
        )


@dataclass(frozen=True)
class LegacyResource:
    """已核查的单项资源身份；active仅用于scavenger停止条件，不等同本地进程锁。"""

    kind: str
    identity: str
    name: str
    active: bool


def _resources(record: LegacyRegistry) -> tuple[LegacyResource, ...]:
    """两组归属标签加精确名字并集查询，任何错label/意外成员/daemon不确定都拒绝删除。"""
    expected = {
        "container": {record.postgres_container, record.converter_container},
        "network": {record.network},
        "volume": {record.volume},
    }
    result = []
    for kind, names in expected.items():
        identities: set[str] = set()
        for criterion in (
            "label=com.docker.compose.project=" + record.project,
            "label=com.ai-employee.maintenance.attempt=" + record.attempt_id,
            *("name=" + name for name in sorted(names)),
        ):
            args = ("ps", "--all") if kind == "container" else (kind, "ls")
            field = "{{.Name}}" if kind == "volume" else "{{.ID}}"
            identities.update(
                _docker(*args, "--filter", criterion, "--format", field).splitlines()
            )
        for identity in sorted(identities):
            values = json.loads(_docker("inspect", "--type", kind, identity))
            if not isinstance(values, list) or len(values) != 1:
                raise LegacyConversionError("legacy_resource_invalid")
            value = _mapping(values[0])
            name = str(value.get("Name", "")).removeprefix("/")
            configuration = (
                _mapping(value.get("Config", {})) if kind == "container" else value
            )
            labels = _mapping(configuration.get("Labels", {}))
            if (
                name not in names
                or any(labels.get(key) != item for key, item in record.labels.items())
                or labels.get("com.docker.compose.project") != record.project
            ):
                raise LegacyConversionError("legacy_resource_mismatch")
            if kind == "network" and value.get("Internal") is not True:
                raise LegacyConversionError("legacy_resource_mismatch")
            state = _mapping(value.get("State", {})) if kind == "container" else {}
            if kind == "container" and (
                any(
                    type(state.get(flag)) is not bool
                    for flag in ("Running", "Paused", "Restarting")
                )
                or state.get("Status")
                not in {
                    "created",
                    "running",
                    "paused",
                    "restarting",
                    "removing",
                    "exited",
                    "dead",
                }
            ):
                raise LegacyConversionError("legacy_resource_state_unknown")
            active = any(
                state.get(flag) is True for flag in ("Running", "Paused", "Restarting")
            )
            if kind == "container" and state.get("Status") in {
                "running",
                "paused",
                "restarting",
                "removing",
            }:
                active = True
            result.append(LegacyResource(kind, identity, name, active))
    return tuple(result)


def _cleanup(
    record: LegacyRegistry,
    *,
    allow_active: bool,
    config: dict[str, object] | None = None,
) -> bool:
    """持attempt锁重查registry和全部资源后profile-scoped down；证据不完整时保留所有对象。"""
    directory = Path(record.backup_directory)
    path = directory / _REGISTRY / (record.attempt_id + ".json")
    if read_registry(path, directory) != record:
        raise LegacyConversionError("legacy_registry_changed")
    first = _resources(record)
    if not allow_active and any(item.active for item in first):
        return False
    # 删除前再次读取labels/active状态，不能用之前的daemon列表判定当前已经停止。
    if _resources(record) != first:
        raise LegacyConversionError("legacy_resources_changed")
    if first:
        _compose(
            record,
            config or _configuration(record, {}, None),
            "down",
            "--volumes",
            "--remove-orphans",
        )
    if _resources(record):
        raise LegacyConversionError("legacy_cleanup_incomplete")
    secret = Path(record.secret_path)
    if os.path.lexists(secret):
        info = secret.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise LegacyConversionError("legacy_secret_changed")
        secret.unlink()
    if read_registry(path, directory) != record:
        raise LegacyConversionError("legacy_registry_changed")
    path.unlink()
    _sync_directory(path.parent)
    return True


def _nonproduction() -> None:
    if os.environ.get("APP_ENV") not in {"development", "test"}:
        raise LegacyConversionError("legacy_production_forbidden")


def scavenge(directory: Path | None = None, *, now: datetime | None = None) -> int:
    """固定3600秒grace加nonblocking attempt锁；active/recent/未知全部保留，不做强制清理。"""
    _nonproduction()
    root = _directory(
        directory or Path(os.environ.get("BACKUP_DIR", "./var/backups")).absolute(),
        create=True,
    )
    registry = root / _REGISTRY
    if not registry.exists():
        return 0
    _directory(registry)
    current = now or datetime.now(UTC)
    if current.tzinfo != UTC:
        raise LegacyConversionError("legacy_time_invalid")
    count = 0
    for path in sorted(registry.glob("*.json")):
        record = read_registry(path, root)
        if (
            current - datetime.fromisoformat(record.created_at)
        ).total_seconds() < ORPHAN_GRACE_SECONDS:
            continue
        with _attempt_lock(record) as locked:
            if locked and _cleanup(record, allow_active=False):
                count += 1
    return count


def _legacy_source(source: Path) -> tuple[str, int]:
    """验证旧式唯一checksum只针对显式输入文件；绝不执行checksum内可能含路径的引用。"""
    if not source.name.endswith(".dump.enc") or os.path.lexists(
        Path(f"{source}.manifest.json")
    ):
        raise LegacyConversionError("legacy_source_invalid")
    validate_backup_basename(source.name.removesuffix(".dump.enc"))
    _, digest, info = _regular(source)
    checksum, _, _ = _regular(Path(f"{source}.sha256"), limit=1024)
    allowed = {f"{digest}  {source.name}\n".encode(), f"{digest}  {source}\n".encode()}
    if checksum not in allowed:
        raise LegacyConversionError("legacy_checksum_invalid")
    return digest, info.st_size


def convert(source: Path, output_basename: str) -> Path:
    """先纯校验/回收，再registry→Secret→隔离restore→正常backup；每个catchable出口清理。"""
    _nonproduction()
    source = source.absolute()
    digest, size = _legacy_source(source)
    validate_backup_basename(output_basename)
    directory = _directory(
        Path(os.environ.get("BACKUP_DIR", "./var/backups")).absolute(), create=True
    )
    target = directory / (output_basename + ".dump.enc")
    if any(
        os.path.lexists(Path(str(target) + suffix))
        for suffix in ("", ".sha256", ".manifest.json")
    ):
        raise LegacyConversionError("legacy_output_collision")
    scavenge(directory)
    # 读取完整 operations profile，避免真实 Compose 将尚未激活的 backup 服务省略。
    config = _mapping(
        json.loads(
            _docker("compose", "--profile", "operations", "config", "--format", "json")
        )
    )
    image_reference = _mapping(_mapping(config.get("services")).get("backup")).get(
        "image"
    )
    if (
        type(image_reference) is not str
        or ":" not in image_reference
        or image_reference.rsplit(":", 1)[-1] == "latest"
    ):
        raise LegacyConversionError("legacy_image_invalid")
    image_id = _docker(
        "image", "inspect", "--format", "{{.Id}}", image_reference
    ).strip()
    attempt = str(uuid4())
    project = "aiemployee-legacy-" + UUID(attempt).hex
    record = LegacyRegistry(
        _SCHEMA,
        attempt,
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        _KIND,
        str(directory),
        source.name,
        digest,
        size,
        output_basename,
        image_id,
        project,
        project + "-postgres",
        project + "-converter",
        project + "-internal",
        project + "-postgres-data",
        str(directory / _REGISTRY / (attempt + ".secret")),
    )
    _validate_registry(record, directory)
    _publish_registry(record)
    with _attempt_lock(record) as locked:
        if not locked:
            raise LegacyConversionError("legacy_attempt_active")
        previous_handler = None
        configuration = None
        first_failure: BaseException | None = None
        try:

            def interrupted(signum, frame):
                raise KeyboardInterrupt

            previous_handler = signal.signal(signal.SIGTERM, interrupted)
            template = _template(record)
            descriptor = os.open(
                record.secret_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(secrets.token_urlsafe(48) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(Path(record.secret_path).parent)
            configuration = _configuration(record, template, source)
            if _legacy_source(source) != (digest, size):
                raise LegacyConversionError("legacy_source_changed")
            _compose(
                record,
                configuration,
                "up",
                "--detach",
                "--wait",
                "--pull",
                "never",
                "legacy-conversion-postgres",
            )
            output = _compose(
                record,
                configuration,
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "--name",
                record.converter_container,
                "legacy-backup-converter",
            )
            if output.strip() != "legacy_conversion_completed":
                raise LegacyConversionError("legacy_output_invalid")
            # 只有真实container内新backup发布的完整组可成功；不能在host给源字节附加manifest。
            from ai_employee.infrastructure.db.postgres_backup_manifest import _group_at

            group = _group_at(target)
            if group.manifest.immutable_image_id != record.image_id:
                raise LegacyConversionError("legacy_image_changed")
        except BaseException as failure:
            first_failure = failure
            raise
        finally:
            if previous_handler is not None:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                _cleanup(record, allow_active=True, config=configuration)
            except BaseException as cleanup_failure:
                if first_failure is not None:
                    raise first_failure from cleanup_failure
                raise
            finally:
                if previous_handler is not None:
                    signal.signal(signal.SIGTERM, previous_handler)
    return target


def _verify_isolated_health(connection: Connection) -> RestoreFingerprint:
    """同一RR/RO快照先读owner revision/fingerprint，再实际以app角色验证业务可读与禁止写。

    此函数只供专用isolated流程在正常candidate→baseline完成后调用，不产生grant、
    gate或restore authority。版本由同一快照中的owner读取，app不获得baseline禁止的
    alembic_version权限；SET LOCAL ROLE随外层事务退出自动恢复，不创建独立app连接。
    """
    from ai_employee.cli.verify_restored_backup import (
        read_restore_fingerprint,
        verify_restore_sequence_health,
    )
    from ai_employee.infrastructure.db.database_grants import restore_inventory_objects

    posture = connection.execute(
        text(
            "SELECT current_user=session_user,current_setting('transaction_read_only'),current_setting('transaction_isolation')"
        )
    ).one()
    if tuple(posture) != (True, "on", "repeatable read"):
        raise LegacyConversionError("legacy_health_readonly_required")
    fingerprint = read_restore_fingerprint(connection)
    connection.execute(text("SET LOCAL ROLE ai_employee_app"))
    if connection.scalar(text("SELECT current_user")) != "ai_employee_app":
        raise LegacyConversionError("legacy_health_role_invalid")
    rejected = False
    try:
        with connection.begin_nested():
            connection.execute(text("UPDATE public.users SET id=id WHERE false"))
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "25006":
            raise
        rejected = True
    if not rejected:
        raise LegacyConversionError("legacy_health_readonly_required")
    tables, _ = restore_inventory_objects(fingerprint.revision)
    for table_name in tables:
        if table_name != "alembic_version":
            # 表名只来自已验证的revision registry；仅返回计数，不把任何业务内容带出数据库。
            connection.scalar(text(f'SELECT count(*) FROM public."{table_name}"'))
    verify_restore_sequence_health(connection, revision=fingerprint.revision)
    return fingerprint


def isolated() -> None:
    """专用容器只认固定隔离DNS/数据库；真实restore/RO health后正常backup，不进入workspace。"""
    from dataclasses import replace
    from importlib.metadata import version

    from ai_employee.cli.database_maintenance import (
        DatabaseEndpoint,
        RoleBootstrapArguments,
        _load_database_endpoint,
        _open_maintenance_context,
        _read_secret_file,
        role_bootstrap_database,
    )
    from ai_employee.cli.postgres_backup import BackupRequest, run_backup
    from ai_employee.infrastructure.db.postgres_backup_manifest import (
        image_executable_digests,
    )
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    _nonproduction()
    record = read_registry(Path("/legacy/registry.json"))
    if os.environ.get("LEGACY_ATTEMPT_ID") != record.attempt_id:
        raise LegacyConversionError("legacy_attempt_mismatch")
    endpoint = _load_database_endpoint()
    if endpoint != DatabaseEndpoint(
        "legacy-conversion-postgres", 5432, "ai_employee_owner", _DATABASE
    ):
        raise LegacyConversionError("legacy_target_invalid")
    source = Path("/legacy/source") / record.source_basename
    _, digest, info = _regular(source)
    if digest != record.source_sha256 or info.st_size != record.source_size:
        raise LegacyConversionError("legacy_source_changed")
    executable_digests = image_executable_digests()
    secret = Path("/run/secrets/postgres_bootstrap_password")
    password = _read_secret_file(secret)
    passphrase = Path("/run/secrets/backup_passphrase")
    _read_secret_file(passphrase)
    environment = {
        "PATH": os.environ["PATH"],
        "PGHOST": endpoint.host,
        "PGPORT": str(endpoint.port),
        "PGDATABASE": endpoint.database_name,
        "PGUSER": endpoint.owner_role,
        "PGPASSWORD": password.get_secret_value(),
    }
    with tempfile.TemporaryDirectory(prefix="legacy-restore-") as temporary:
        plain = Path(temporary) / "legacy.dump"
        descriptor = os.open(plain, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        for command in (
            [
                "openssl",
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-pass",
                f"file:{passphrase}",
                "-in",
                str(source),
                "-out",
                str(plain),
            ],
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--single-transaction",
                "--dbname",
                _DATABASE,
                str(plain),
            ],
        ):
            result = subprocess.run(
                command,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=900,
            )
            if result.returncode:
                raise LegacyConversionError("legacy_restore_failed")
    engine = create_engine(
        endpoint.owner_url(password, database_name=_DATABASE),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            revision = tuple(
                connection.scalars(
                    text("SELECT version_num FROM public.alembic_version")
                )
            )
            if revision not in {("20260809_0018",), ("20260809_0019",)}:
                raise LegacyConversionError("legacy_revision_unsupported")
        # 仅新建隔离target的正常candidate→baseline流程，不修改历史schema或revision。
        with _open_maintenance_context(
            RoleBootstrapArguments(
                secret,
                Path("/run/secrets/app_database_password"),
                Path("/run/secrets/retention_database_password"),
            ),
            endpoint,
        ) as context:
            role_bootstrap_database(context)
        with engine.connect() as connection, connection.begin():
            connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            fingerprint = _verify_isolated_health(connection)
            if fingerprint.revision != revision[0]:
                raise LegacyConversionError("legacy_revision_changed")
        request = BackupRequest(
            Path("/backups"),
            record.output_basename,
            record.image_id,
            executable_digests,
            version("ai-employee"),
            passphrase,
        )
        run_backup(request, endpoint=replace(endpoint), owner_password_file=secret)
    finally:
        engine.dispose()


def main(arguments: Sequence[str] | None = None) -> int:
    """公开shell仅固定convert/scavenge；isolated只在专用镜像命令中调用，错误输出无内容。"""
    try:
        args = list(arguments) if arguments is not None else sys.argv[1:]
        if len(args) == 3 and args[0] == "convert":
            convert(Path(args[1]), args[2])
        elif args == ["scavenge"]:
            scavenge()
            print("legacy_scavenge_completed")
            return 0
        elif args == ["isolated"]:
            isolated()
        else:
            raise LegacyConversionError("legacy_arguments_invalid")
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
        SQLAlchemyError,
        CalendarAadRolloutError,
    ):
        print("legacy_conversion_failed", file=sys.stderr)
        return 1
    print("legacy_conversion_completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
