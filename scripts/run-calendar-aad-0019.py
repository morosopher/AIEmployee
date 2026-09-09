#!/usr/bin/env python3
"""封闭 0019 窗口的宿主 Compose 入口，先只读筛查再执行固定 one-off。

公开 just recipe 无参数；本脚本的 operation 仅由 recipe 固定传入。宿主只检查目录边界、
停止状态、实际镜像和当前版本；artifact 文件 admission 归持数据库 lease 的正式 CLI。
"""

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_FLAGS = (
    "EXTERNAL_WRITES_ENABLED",
    "GOOGLE_WRITES_ENABLED",
    "MICROSOFT_WRITES_ENABLED",
)
_SERVICES = {
    "preflight": "worker",
    "migrate": "migration",
    "resync": "worker",
    "backup": "backup",
}
_COMMANDS = {
    "preflight": 'export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_preflight_0019',
    "migrate": 'export PGPASSWORD="$(cat /run/secrets/postgres_bootstrap_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_migrate_0019',
    "resync": 'export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_0019',
    "backup": "exec bash /app/scripts/backup-postgres.sh",
}
_SCREEN_COMMAND = 'export PGPASSWORD="$(cat /run/secrets/postgres_bootstrap_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_revision_0019'


class RolloutHostError(ValueError):
    """仅承载稳定代码，不包含 Compose 配置、Secret 路径、argv 或 daemon 原始输出。"""


def _docker(*arguments: str) -> str:
    """执行只读 Docker 查询并吸收原始诊断，禁止意外把渲染后的环境输出到终端。"""
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode:
        raise RolloutHostError("calendar_aad_docker_query_failed")
    return result.stdout


def _mapping(value: object) -> dict[str, object]:
    """在 JSON/Compose 边界显式收窄对象，格式异常即停止。"""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RolloutHostError("calendar_aad_compose_invalid")
    return value


def _image_id(service: dict[str, object]) -> str:
    """只 inspect Compose 实际选择的本地镜像；缺失/latest/无内容 ID 均不 build 或 pull。"""
    reference = service.get("image")
    if (
        not isinstance(reference, str)
        or not reference
        or reference.rsplit(":", 1)[-1].lower() == "latest"
    ):
        raise RolloutHostError("calendar_aad_image_invalid")
    if ":" not in reference and "@" not in reference:
        raise RolloutHostError("calendar_aad_image_invalid")
    image_id = _docker("image", "inspect", "--format", "{{.Id}}", reference).strip()
    if _IMAGE_ID.fullmatch(image_id) is None:
        raise RolloutHostError("calendar_aad_image_invalid")
    return image_id


def _require_stopped(
    *, own_name: str | None = None, own_image: str | None = None
) -> None:
    """检查当前 Compose 项目全部容器，paused/restarting 也不是可接受的已停止状态。"""
    raw = _docker("compose", "ps", "--all", "--format", "json").strip()
    try:
        parsed: object = json.loads(raw or "[]")
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in raw.splitlines()]
    rows = parsed if isinstance(parsed, list) else [parsed]
    for value in rows:
        row = _mapping(value)
        if row.get("State") in {"exited", "created", "dead"} or row.get("Service") in {
            "postgres",
            "redis",
            "prometheus",
            "grafana",
        }:
            continue
        if (
            own_name is not None
            and row.get("Name") == own_name
            and row.get("Service") == "backup"
        ):
            identifier = row.get("ID")
            if (
                type(identifier) is not str
                or re.fullmatch(r"[0-9a-f]{12,64}", identifier) is None
            ):
                raise RolloutHostError("calendar_aad_services_running")
            result = json.loads(_docker("inspect", "--type", "container", identifier))
            if not isinstance(result, list) or len(result) != 1:
                raise RolloutHostError("calendar_aad_services_running")
            container = _mapping(result[0])
            labels = _mapping(_mapping(container.get("Config")).get("Labels"))
            if (
                container.get("Name") != "/" + own_name
                or container.get("Image") != own_image
                or labels.get("com.docker.compose.service") != "backup"
            ):
                raise RolloutHostError("calendar_aad_services_running")
            continue
        raise RolloutHostError("calendar_aad_services_running")


def _oneoff(
    config: dict[str, object],
    service: str,
    command: str,
    *,
    timeout: float | None = None,
    guard: Callable[[], None] | None = None,
    own_name: str | None = None,
) -> str:
    """以 0600 临时 Compose 执行一个内部固定命令，只返回成功 stdout 或稳定错误码。

    临时文件与 stdout/stderr 都不写持久日志；screen 使用有界超时，正式命令继续由
    自己的任务/步骤预算管理。service/command 只能来自本模块的固定 composition。
    """
    with tempfile.TemporaryDirectory(prefix="calendar-aad-compose-") as temporary:
        path = Path(temporary) / "compose.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream)
        arguments = [
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
            *(["--name", own_name, "-T"] if own_name is not None else []),
            "--entrypoint",
            "/bin/sh",
            service,
            "-ec",
            command,
        ]
        if guard is not None:
            # 只有新的sealed backup父流程使用匿名握手；固定preflight/migrate/resync命令保持原契约。
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(
                    arguments,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    text=True,
                    bufsize=1,
                )
                try:
                    assert process.stdin is not None and process.stdout is not None
                    completed = False
                    while line := process.stdout.readline(128):
                        if line == "operations.guard.request\n":
                            guard()
                            process.stdin.write("operations.guard.confirmed\n")
                            process.stdin.flush()
                        elif line == "postgres_backup_completed\n" and not completed:
                            completed = True
                        else:
                            raise RolloutHostError("calendar_aad_backup_output_invalid")
                    process.stdin.close()
                    if process.wait() != 0 or not completed:
                        raise RolloutHostError("calendar_aad_backup_failed")
                    guard()
                    return "postgres_backup_completed\n"
                finally:
                    if not process.stdin.closed:
                        process.stdin.close()
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    if result.returncode:
        lines = result.stderr.strip().splitlines()
        code = lines[-1] if lines else ""
        if re.fullmatch(r"(?:calendar_aad|oauth)_[a-z0-9_]{1,90}", code) is None:
            code = "calendar_aad_oneoff_failed"
        raise RolloutHostError(code)
    return result.stdout


def _screen_revision(
    config: dict[str, object], image_id: str, expected_revision: str
) -> None:
    """正式容器前只读筛查，不挂载 backup/source，不读取或判断 artifact/guard。

    复制配置防止 image pin 改写随后再次 inspect 的原引用。固定短容器只保留 migration
    原有 owner Secret；筛查结果只用于当前顺序检查，不能替代正式命令里的事务授权。
    """
    screen_config = copy.deepcopy(config)
    migration = _mapping(_mapping(screen_config.get("services")).get("migration"))
    migration["image"] = image_id
    migration["pull_policy"] = "never"
    migration.pop("build", None)
    migration["volumes"] = []
    environment = _mapping(migration.get("environment", {}))
    for key in ("CALENDAR_AAD_ROLLOUT_STATE", "BACKUP_DIR", "BACKUP_ARTIFACT_BASENAME"):
        environment.pop(key, None)
    environment.update({flag: "false" for flag in _FLAGS})
    migration["environment"] = environment
    try:
        revision = _oneoff(
            screen_config, "migration", _SCREEN_COMMAND, timeout=30
        ).strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise RolloutHostError("calendar_aad_revision_screen_failed") from error
    if revision != expected_revision:
        raise RolloutHostError("calendar_aad_revision_mismatch")


def run(operation: str) -> None:
    """冻结目录/镜像，先只读筛查版本并重查停服/镜像，再执行固定正式命令。

    生成的临时 Compose 文件保持原项目网络/Secret 定义，只有 one-off 的 image、备份挂载
    与内部绑定环境被固定。它为 0600 且随操作删除；不在宿主输出渲染配置或 operator 输入。
    """
    if operation not in _SERVICES:
        raise RolloutHostError("calendar_aad_arguments_invalid")
    tag = os.environ.get("APP_IMAGE_TAG", "")
    basename = os.environ.get("BACKUP_ARTIFACT_BASENAME", "")
    directory = Path(os.environ.get("BACKUP_DIR", ""))
    if not tag or tag.lower() == "latest" or _BASENAME.fullmatch(tag) is None:
        raise RolloutHostError("calendar_aad_image_invalid")
    if "CALENDAR_AAD_IMMUTABLE_IMAGE_ID" in os.environ:
        raise RolloutHostError("calendar_aad_image_override_forbidden")
    if _BASENAME.fullmatch(basename) is None:
        raise RolloutHostError("calendar_aad_basename_invalid")
    if not directory.is_absolute():
        raise RolloutHostError("calendar_aad_backup_directory_invalid")
    try:
        directory = directory.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RolloutHostError("calendar_aad_backup_directory_invalid") from error
    if directory == Path("/") or not directory.is_dir():
        raise RolloutHostError("calendar_aad_backup_directory_invalid")
    # 后续 mount 必须复用此物理路径；若在 Docker 查询后重新解析 operator symlink，
    # 校验与挂载之间的替换会绕过非根目录约束。
    # preflight 与 backup 共用固定 profile，必须包含默认不激活的 operations 服务。
    config = _mapping(
        json.loads(
            _docker("compose", "--profile", "operations", "config", "--format", "json")
        )
    )
    services = _mapping(config.get("services"))
    worker = _mapping(services.get("worker"))
    migration = _mapping(services.get("migration"))
    _require_stopped()
    for name in ("api", "worker", "scheduler", _SERVICES[operation]):
        environment = _mapping(_mapping(services.get(name)).get("environment", {}))
        if any(
            str(environment.get(flag, "false")).lower() not in {"false", "0"}
            for flag in _FLAGS
        ):
            raise RolloutHostError("calendar_aad_write_switch_enabled")
    image_id = _image_id(worker)
    if _image_id(migration) != image_id:
        raise RolloutHostError("calendar_aad_image_mismatch")
    service_name = _SERVICES[operation]
    selected = _mapping(services.get(service_name))
    if operation == "backup" and _image_id(selected) != image_id:
        raise RolloutHostError("calendar_aad_image_mismatch")
    volumes = selected.get("volumes", [])
    if not isinstance(volumes, list):
        raise RolloutHostError("calendar_aad_compose_invalid")
    # sealed one-off 的可执行代码必须来自已 inspect 的镜像；源码 bind 或其它覆盖挂载会
    # 使内容 ID 失去实际运行身份。Secret 仍走 Compose 原有独立 secrets 定义。
    if any(_mapping(value).get("target") != "/backups" for value in volumes):
        raise RolloutHostError("calendar_aad_image_mount_invalid")
    if operation == "backup":
        # 无网络、无数据/Secret挂载的镜像内部摘要检查必须早于owner revision screen。
        inventory = _mapping(
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
                    "import json; from ai_employee.infrastructure.db.postgres_backup_manifest import image_executable_digests; print(json.dumps(dict(image_executable_digests()),sort_keys=True))",
                )
            )
        )
        if not inventory or any(
            type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in inventory.values()
        ):
            raise RolloutHostError("calendar_aad_image_invalid")
    _screen_revision(
        config, image_id, "20260809_0019" if operation == "resync" else "20260809_0018"
    )
    _require_stopped()
    if _image_id(worker) != image_id or _image_id(migration) != image_id:
        raise RolloutHostError("calendar_aad_image_mismatch")
    if operation == "backup" and _image_id(selected) != image_id:
        raise RolloutHostError("calendar_aad_image_mismatch")
    selected["image"] = image_id
    selected["pull_policy"] = "never"
    selected.pop("build", None)
    if operation in {"preflight", "resync"}:
        # 仅固定命令在screen/停服/image全部复查后取得只读facts Secret；普通worker永不持有。
        secrets = selected.get("secrets", [])
        if not isinstance(secrets, list):
            raise RolloutHostError("calendar_aad_compose_invalid")
        selected["secrets"] = [
            *secrets,
            {
                "source": "postgres_bootstrap_password",
                "target": "postgres_bootstrap_password",
            },
        ]
    environment = _mapping(selected.get("environment", {}))
    environment.update({flag: "false" for flag in _FLAGS})
    environment.update(
        {
            "BACKUP_DIR": "/backups",
            "BACKUP_ARTIFACT_BASENAME": basename,
            "CALENDAR_AAD_ROLLOUT_STATE": f"/backups/{basename}.calendar-aad-preflight.json",
            "CALENDAR_AAD_IMMUTABLE_IMAGE_ID": image_id,
            "BACKUP_IMMUTABLE_IMAGE_ID": image_id,
        }
    )
    selected["environment"] = environment
    selected["volumes"] = [
        {
            "type": "bind",
            "source": str(directory),
            "target": "/backups",
            "read_only": operation in {"migrate", "resync"},
        },
    ]
    if operation == "backup":
        selected["user"] = f"{os.geteuid()}:{os.getegid()}"
        name = "aiemployee-calendar-backup-" + uuid4().hex
        directory_identity = directory.stat()

        def backup_guard() -> None:
            """每次容器请求都重新inspect停服、开关、三个image和已固定目录，不接收外部批准。"""
            current = _mapping(
                json.loads(
                    _docker(
                        "compose",
                        "--profile",
                        "operations",
                        "config",
                        "--format",
                        "json",
                    )
                )
            )
            current_services = _mapping(current.get("services"))
            _require_stopped(own_name=name, own_image=image_id)
            for service in current_services.values():
                values = _mapping(_mapping(service).get("environment", {}))
                if any(
                    str(values.get(flag, "false")).lower() not in {"false", "0"}
                    for flag in _FLAGS
                ):
                    raise RolloutHostError("calendar_aad_write_switch_enabled")
            if any(
                _image_id(_mapping(current_services.get(service))) != image_id
                for service in ("worker", "migration", "backup")
            ):
                raise RolloutHostError("calendar_aad_image_mismatch")
            if not os.path.samestat(directory.lstat(), directory_identity):
                raise RolloutHostError("calendar_aad_backup_directory_invalid")

        backup_guard()
        print(
            _oneoff(
                config,
                service_name,
                _COMMANDS[operation],
                guard=backup_guard,
                own_name=name,
            ),
            end="",
        )
    else:
        print(_oneoff(config, service_name, _COMMANDS[operation]), end="")


def main() -> int:
    """宿主失败只输出稳定码；内部 operation 以外的参数不回显。"""
    try:
        if len(sys.argv) != 2:
            raise RolloutHostError("calendar_aad_arguments_invalid")
        run(sys.argv[1])
    except (RolloutHostError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(
            str(error)
            if isinstance(error, RolloutHostError)
            else "calendar_aad_host_failed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
