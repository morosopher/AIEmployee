#!/usr/bin/env python3
"""固定备份/普通恢复/sealed恢复/audit宿主入口；不读取或输出任何Secret值。

先纯文件检查，再从本地immutable image读取可执行摘要，最后才允许owner one-off。
恢复通过专属stdin/stdout握手重复检查停止状态，容器不挂载Docker socket或宿主代码。
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from uuid import uuid4

from ai_employee.cli.calendar_aad_audit_0019 import load_audit, load_preflight
from ai_employee.cli.database_maintenance import (
    DatabaseEndpoint,
    _load_database_endpoint,
)
from ai_employee.cli.postgres_restore import (
    RestoreStateStore,
    parse_restore_ordinal_options,
)
from ai_employee.infrastructure.db.postgres_backup_manifest import (
    OPERATIONS_EXECUTABLES,
    ValidatedBackupGroup,
    _regular,
    parse_backup_manifest,
    validate_backup_basename,
    validate_backup_group,
    validate_generic_backup_group,
)

_FLAGS = (
    "EXTERNAL_WRITES_ENABLED",
    "GOOGLE_WRITES_ENABLED",
    "MICROSOFT_WRITES_ENABLED",
)
_SERVICES = {
    "backup": "backup",
    "restore": "postgres-restore",
    "sealed": "calendar-aad-restore-0018",
    "audit": "backup",
}
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class OperationsHostError(RuntimeError):
    """宿主只输出稳定错误码，不暴露Docker配置/文件内容/凭据或个人路径。"""


def _mapping(value: object) -> dict[str, object]:
    """Compose/JSON边界先收窄结构，后续不把任意对象当受信配置。"""
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise OperationsHostError("operations_configuration_invalid")
    return value


def _docker(*arguments: str) -> str:
    """固定查询/无网络image检查共用有界process；stderr可能含敏感配置，全部吸收。"""
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode:
        raise OperationsHostError("operations_docker_failed")
    return result.stdout


def _config() -> dict[str, object]:
    # operations 服务默认不激活；显式选择 profile，才能校验实际 one-off 的完整配置。
    return _mapping(
        json.loads(
            _docker("compose", "--profile", "operations", "config", "--format", "json")
        )
    )


def _endpoint(service: dict[str, object]) -> DatabaseEndpoint:
    """复用唯一typed parser在owner容器启动前固定目标；拒绝URL密码和非固定owner角色。"""
    value = _mapping(service.get("environment", {})).get("DATABASE_URL")
    if type(value) is not str:
        raise OperationsHostError("operations_endpoint_invalid")
    endpoint = _load_database_endpoint({"DATABASE_URL": value})
    if endpoint.owner_role != "ai_employee_owner":
        raise OperationsHostError("operations_endpoint_invalid")
    return endpoint


def _image_id(service: dict[str, object]) -> str:
    """只解析本地tag/reference的实际content ID，never pull/build；latest和缺失硬失败。"""
    reference = service.get("image")
    if (
        not isinstance(reference, str)
        or ":" not in reference
        or reference.rsplit(":", 1)[-1] == "latest"
    ):
        raise OperationsHostError("operations_image_invalid")
    image_id = _docker("image", "inspect", "--format", "{{.Id}}", reference).strip()
    if _IMAGE.fullmatch(image_id) is None:
        raise OperationsHostError("operations_image_invalid")
    return image_id


def _image_inventory(image_id: str) -> dict[str, str]:
    """仅启动无网络、无mount、无Secret的精确image检查进程，读取镜像内固定脚本摘要。"""
    program = "import json; from ai_employee.infrastructure.db.postgres_backup_manifest import image_executable_digests; print(json.dumps(dict(image_executable_digests()),sort_keys=True))"
    raw = _mapping(
        json.loads(
            _docker(
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--entrypoint",
                "/app/backend/.venv/bin/python",
                image_id,
                "-c",
                program,
            )
        )
    )
    if set(raw) != set(OPERATIONS_EXECUTABLES) or any(
        type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in raw.values()
    ):
        raise OperationsHostError("operations_image_inventory_invalid")
    return {key: str(value) for key, value in raw.items()}


def _reject_executable_mounts(service: dict[str, object]) -> None:
    """代码、解释器、启动hook和Secret目标共同绑定实际执行；只允许固定数据/Secret路径。"""
    volumes = service.get("volumes", [])
    if not isinstance(volumes, list):
        raise OperationsHostError("operations_mount_invalid")
    for raw in volumes:
        volume = _mapping(raw)
        target = volume.get("target")
        if type(target) is not str or target not in {
            "/backups",
            "/var/lib/ai-employee/restore-state",
        }:
            raise OperationsHostError("operations_executable_mount_forbidden")
    if any(
        service.get(key)
        for key in (
            "entrypoint",
            "configs",
            "volumes_from",
            "devices",
            "device_cgroup_rules",
            "privileged",
            "cap_add",
            "post_start",
            "pre_stop",
            "develop",
            "network_mode",
            "pid",
            "ipc",
            "uts",
            "use_api_socket",
        )
    ):
        raise OperationsHostError("operations_service_invalid")
    secrets = service.get("secrets", [])
    if not isinstance(secrets, list):
        raise OperationsHostError("operations_secret_invalid")
    allowed = {
        "postgres_bootstrap_password",
        "app_database_password",
        "retention_database_password",
        "backup_passphrase",
    }
    seen = set()
    for item in secrets:
        definition = {"source": item} if type(item) is str else _mapping(item)
        source = definition.get("source")
        target = definition.get("target", source)
        if (
            type(source) is not str
            or source not in allowed
            or source in seen
            or type(target) is not str
            or target not in {source, "/run/secrets/" + source}
        ):
            raise OperationsHostError("operations_secret_invalid")
        seen.add(source)
    environment = _mapping(service.get("environment", {}))
    if any(
        key in {"PATH", "BASH_ENV", "ENV", "SHELLOPTS", "CDPATH"}
        or key.startswith(("PYTHON", "LD_", "UV_"))
        or (key.startswith("PG") and key not in {"PGHOST", "PGPORT", "PGDATABASE"})
        for key in environment
    ):
        raise OperationsHostError("operations_executable_environment_forbidden")


def _require_stopped(
    config: dict[str, object],
    *,
    own_name: str | None = None,
    own_image: str | None = None,
    own_service: str | None = None,
) -> None:
    """检查整个workspace的普通/owner/one-off容器；只放行基础设施和本次精确命名容器。"""
    services = _mapping(config.get("services"))
    for raw in services.values():
        environment = _mapping(_mapping(raw).get("environment", {}))
        if any(str(environment.get(flag, "false")) != "false" for flag in _FLAGS):
            raise OperationsHostError("operations_write_switch_enabled")
    raw = _docker("compose", "ps", "--all", "--format", "json").strip()
    try:
        parsed: object = json.loads(raw or "[]")
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in raw.splitlines()]
    rows = parsed if isinstance(parsed, list) else [parsed]
    for item in rows:
        row = _mapping(item)
        if row.get("State") in {"exited", "created", "dead"}:
            continue
        if row.get("Service") in {"postgres", "redis", "prometheus", "grafana"}:
            continue
        if (
            own_name is not None
            and row.get("Name") == own_name
            and row.get("Service") == own_service
        ):
            # name/service只定位候选；必须从daemon重新证明精确容器ID、image和项目标签。
            identifier = row.get("ID")
            if (
                type(identifier) is not str
                or re.fullmatch(r"[0-9a-f]{12,64}", identifier) is None
            ):
                raise OperationsHostError("operations_own_container_invalid")
            values = json.loads(_docker("inspect", "--type", "container", identifier))
            if not isinstance(values, list) or len(values) != 1:
                raise OperationsHostError("operations_own_container_invalid")
            actual = _mapping(values[0])
            labels = _mapping(_mapping(actual.get("Config")).get("Labels"))
            if (
                not str(actual.get("Id", "")).startswith(identifier)
                or actual.get("Name") != "/" + own_name
                or actual.get("Image") != own_image
                or labels.get("com.docker.compose.service") != own_service
                or labels.get("com.docker.compose.project") != config.get("name")
                or _mapping(actual.get("State")).get("Running") is not True
            ):
                raise OperationsHostError("operations_own_container_invalid")
            continue
        raise OperationsHostError("operations_services_running")


def _pure_group(path: Path) -> ValidatedBackupGroup:
    """先只验证文件自洽，随后必须与实际image再次比较；本函数不建立owner服务。"""
    raw, _, _ = _regular(Path(str(path) + ".manifest.json"), limit=65536)
    manifest = parse_backup_manifest(raw)
    return validate_backup_group(
        path,
        supported_revisions=frozenset({"20260809_0018", "20260809_0019"}),
        expected_image_id=manifest.immutable_image_id,
        expected_executable_digests=manifest.executable_digests,
    )


def _restore_state(app_env: str, source: Path) -> Path:
    """development/test使用ignored默认目录，production必须显式绝对目录；拒绝只读源重叠。"""
    raw = os.environ.get("RESTORE_STATE_DIR", "./var/restore-state")
    path = Path(raw)
    if app_env == "production" and (not raw or not path.is_absolute()):
        raise OperationsHostError("operations_restore_state_explicit_required")
    path = path.absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise OperationsHostError("operations_restore_state_invalid")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    RestoreStateStore(path, (source,))
    return path


def _oneoff(
    config: dict[str, object],
    service: str,
    *,
    own_name: str,
    guard: Callable[[], None] | None,
) -> None:
    """所有可执行来自固定image；恢复握手每次重查宿主，EOF/错误立即关闭stdin阻止下一步。"""
    with tempfile.TemporaryDirectory(
        prefix="postgres-operations-compose-"
    ) as temporary:
        path = Path(temporary) / "compose.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream)
        command = [
            "docker",
            "compose",
            "--project-directory",
            str(Path.cwd()),
            "-f",
            str(path),
            "--profile",
            "operations",
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "--name",
            own_name,
            "-T",
            service,
        ]
        # stderr进入私有临时fd以避免pipe死锁；不回显、不持久化，容器本身只输出稳定码。
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                bufsize=1,
            )
            try:
                assert process.stdout is not None and process.stdin is not None
                result_code = None
                while line := process.stdout.readline(128):
                    if len(line) >= 128 or result_code is not None:
                        raise OperationsHostError("operations_output_invalid")
                    if line == "operations.guard.request\n":
                        if guard is None:
                            raise OperationsHostError("operations_guard_unexpected")
                        guard()
                        process.stdin.write("operations.guard.confirmed\n")
                        process.stdin.flush()
                    elif re.fullmatch(
                        r"(?:restore|postgres_backup|calendar_aad_audit)_[a-z_]+\n",
                        line,
                    ):
                        result_code = line.strip()
                    elif line.strip():
                        raise OperationsHostError("operations_output_invalid")
                process.stdin.close()
                if process.wait() != 0 or result_code not in {
                    "restore_completed",
                    "restore_already_applied",
                    "postgres_backup_completed",
                    "calendar_aad_audit_passed",
                }:
                    raise OperationsHostError("operations_oneoff_failed")
                print(result_code)
            finally:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                if process.poll() is None:
                    # EOF先撤销后续授权；TERM走controller的owned-child cleanup，超时再强制收尾。
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if process.stdout is not None:
                    process.stdout.close()


def run(
    operation: str,
    path_value: str | None = None,
    phase: str | None = None,
    *,
    ordinal_options: Sequence[str] = (),
) -> None:
    """把一个公开请求变成可复核的固定service/command；任何错配在owner Secret之前拒绝。"""
    if operation not in _SERVICES:
        raise OperationsHostError("operations_arguments_invalid")
    parse_restore_ordinal_options(ordinal_options)
    if ordinal_options and operation not in {"restore", "sealed"}:
        raise OperationsHostError("operations_arguments_invalid")
    group = None
    if operation != "backup":
        if not path_value:
            raise OperationsHostError("operations_arguments_invalid")
        path = Path(path_value).absolute()
        group = _pure_group(
            Path(str(path) + ".dump.enc") if operation == "audit" else path
        )
        if operation in {"sealed", "audit"}:
            if group.manifest.alembic_revision != "20260809_0018":
                raise OperationsHostError("operations_sealed_revision_invalid")
            load_preflight(group)
            if operation == "sealed":
                load_audit(group, "pre-migration")
            elif phase not in {"pre-migration", "post-migration", "post-resync"}:
                raise OperationsHostError("operations_arguments_invalid")
            elif phase != "pre-migration":
                load_audit(
                    group,
                    "pre-migration" if phase == "post-migration" else "post-migration",
                )
    app_env = os.environ.get("APP_ENV")
    if app_env not in {"development", "test", "production"}:
        raise OperationsHostError("operations_environment_invalid")
    if (
        group is not None
        and app_env == "production"
        and group.dump.parent != Path(os.environ.get("BACKUP_DIR", "")).absolute()
    ):
        raise OperationsHostError("operations_backup_directory_mismatch")
    config = _config()
    services = _mapping(config.get("services"))
    selected = _mapping(services.get(_SERVICES[operation]))
    _reject_executable_mounts(selected)
    endpoint = _endpoint(selected)
    image_service = _mapping(services.get("backup"))
    _reject_executable_mounts(image_service)
    image_id = _image_id(image_service)
    inventory = _image_inventory(image_id)
    if group is not None:
        if operation == "restore":
            group = validate_generic_backup_group(
                group.dump, current_image_id=image_id, executable_digests=inventory
            )
        else:
            group = validate_backup_group(
                group.dump,
                supported_revisions=frozenset({"20260809_0018"}),
                expected_image_id=image_id,
                expected_executable_digests=inventory,
            )
    if operation != "backup":
        _require_stopped(config)
    if operation in {"restore", "sealed"} and app_env == "production":
        if os.environ.get("ALLOW_PRODUCTION_RESTORE") != "yes":
            raise OperationsHostError("operations_production_confirmation_required")
        print("请输入本次恢复文件的精确路径以确认：", file=sys.stderr)
        if sys.stdin.readline().rstrip("\n") != path_value:
            raise OperationsHostError("operations_production_confirmation_mismatch")
    frozen = copy.deepcopy(config)
    selected = _mapping(_mapping(frozen.get("services")).get(_SERVICES[operation]))
    selected["image"], selected["pull_policy"] = image_id, "never"
    selected.pop("build", None)
    selected["user"] = f"{os.geteuid()}:{os.getegid()}"
    environment = _mapping(selected.get("environment", {}))
    environment.update(
        {
            "APP_ENV": app_env,
            "OPERATIONS_IMMUTABLE_IMAGE_ID": image_id,
            "BACKUP_IMMUTABLE_IMAGE_ID": image_id,
        }
    )
    if operation in {"restore", "sealed"}:
        environment["ALLOW_PRODUCTION_RESTORE"] = os.environ.get(
            "ALLOW_PRODUCTION_RESTORE", ""
        )
    if operation != "backup":
        environment.update({flag: "false" for flag in _FLAGS})
    own_name = f"aiemployee-operation-{uuid4().hex}"
    if group is not None:
        source = group.dump.parent
        volumes: list[dict[str, object]] = [
            {
                "type": "bind",
                "source": str(source),
                "target": "/backups",
                "read_only": operation != "audit",
            }
        ]
        if operation in {"restore", "sealed"}:
            if "PGUSER" in environment or "PGPASSWORD" in environment:
                raise OperationsHostError("operations_owner_environment_forbidden")
            state = _restore_state(app_env, source)
            volumes.append(
                {
                    "type": "bind",
                    "source": str(state),
                    "target": "/var/lib/ai-employee/restore-state",
                    "read_only": False,
                }
            )
            selected["command"] = [
                "bash",
                "/app/scripts/restore-calendar-aad-0018.sh"
                if operation == "sealed"
                else "/app/scripts/restore-postgres.sh",
                f"/backups/{group.dump.name}",
                *ordinal_options,
            ]
            if operation == "sealed":
                environment["CALENDAR_AAD_RESTORE_IMAGE_ID"] = image_id
        else:
            selected["command"] = [
                "bash",
                "/app/scripts/audit-calendar-aad-0019.sh",
                phase,
                f"/backups/{group.dump.name.removesuffix('.dump.enc')}",
            ]
        selected["volumes"] = volumes
    else:
        directory = Path(os.environ.get("BACKUP_DIR", "./var/backups")).absolute()
        if directory == Path("/") or any(
            parent.is_symlink() for parent in (directory, *directory.parents)
        ):
            raise OperationsHostError("operations_backup_directory_invalid")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o700:
            raise OperationsHostError("operations_backup_directory_invalid")
        if "BACKUP_ARTIFACT_BASENAME" in os.environ:
            validate_backup_basename(os.environ["BACKUP_ARTIFACT_BASENAME"])
            raise OperationsHostError("operations_sealed_backup_entry_required")
        selected["volumes"] = [
            {
                "type": "bind",
                "source": str(directory),
                "target": "/backups",
                "read_only": False,
            }
        ]
        selected["command"] = ["bash", "/app/scripts/backup-postgres.sh"]
    selected["environment"] = environment

    def guard() -> None:
        """服务/开关/可变tag/文件在host-container边界再次校验，永不复用旧布尔授权。"""
        current = _config()
        _require_stopped(
            current,
            own_name=own_name,
            own_image=image_id,
            own_service=_SERVICES[operation],
        )
        service = _mapping(_mapping(current.get("services")).get("backup"))
        _reject_executable_mounts(service)
        if _image_id(service) != image_id:
            raise OperationsHostError("operations_image_changed")
        current_selected = _mapping(
            _mapping(current.get("services")).get(_SERVICES[operation])
        )
        if _endpoint(current_selected) != endpoint:
            raise OperationsHostError("operations_endpoint_changed")
        if group is not None:
            observed = (
                validate_generic_backup_group(
                    group.dump, current_image_id=image_id, executable_digests=inventory
                )
                if operation == "restore"
                else validate_backup_group(
                    group.dump,
                    supported_revisions=frozenset({"20260809_0018"}),
                    expected_image_id=image_id,
                    expected_executable_digests=inventory,
                )
            )
            if observed != group:
                raise OperationsHostError("operations_backup_changed")
            if operation == "sealed":
                load_audit(group, "pre-migration")
            if operation in {"restore", "sealed"}:
                RestoreStateStore(state, (source,))

    if operation != "backup":
        guard()
    _oneoff(
        frozen,
        _SERVICES[operation],
        own_name=own_name,
        guard=guard if operation != "backup" else None,
    )


def main() -> int:
    """固定argv只转交相应operation；失败仅打印稳定码。"""
    try:
        args = sys.argv[1:]
        if args == ["backup"]:
            run("backup")
        elif len(args) >= 2 and args[0] in {"restore", "sealed"}:
            run(args[0], args[1], ordinal_options=args[2:])
        elif len(args) == 3 and args[0] == "audit":
            run("audit", args[2], args[1])
        else:
            raise OperationsHostError("operations_arguments_invalid")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(
            str(error)
            if isinstance(error, OperationsHostError)
            else "operations_failed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
