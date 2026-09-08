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
from pathlib import Path

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
    "backup": 'export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec bash /app/scripts/backup-postgres.sh',
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


def _require_stopped() -> None:
    """检查当前 Compose 项目全部容器，paused/restarting 也不是可接受的已停止状态。"""
    raw = _docker("compose", "ps", "--all", "--format", "json").strip()
    try:
        parsed: object = json.loads(raw or "[]")
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in raw.splitlines()]
    rows = parsed if isinstance(parsed, list) else [parsed]
    for value in rows:
        row = _mapping(value)
        if row.get("Service") in {
            "caddy",
            "api",
            "worker",
            "scheduler",
            "migration",
            "backup",
        } and row.get("State") not in {"exited", "created", "dead"}:
            raise RolloutHostError("calendar_aad_services_running")


def _oneoff(
    config: dict[str, object],
    service: str,
    command: str,
    *,
    timeout: float | None = None,
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
        result = subprocess.run(
            [
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
                "--entrypoint",
                "/bin/sh",
                service,
                "-ec",
                command,
            ],
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
    config = _mapping(json.loads(_docker("compose", "config", "--format", "json")))
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
    environment = _mapping(selected.get("environment", {}))
    environment.update({flag: "false" for flag in _FLAGS})
    environment.update(
        {
            "BACKUP_DIR": "/backups",
            "BACKUP_ARTIFACT_BASENAME": basename,
            "CALENDAR_AAD_ROLLOUT_STATE": f"/backups/{basename}.calendar-aad-preflight.json",
            "CALENDAR_AAD_IMMUTABLE_IMAGE_ID": image_id,
        }
    )
    if operation == "backup":
        environment["DATABASE_URL"] = _mapping(worker.get("environment")).get(
            "DATABASE_URL"
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
