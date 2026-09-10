"""以真实目录、registry 和系统 fsync 验证 legacy 资源创建前的持久化边界。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[3]
ATTEMPT = UUID("00000000-0000-0000-0000-000000000901")
REGISTRY = ".legacy-conversion-registry"
type FileEvent = tuple[str, Path | tuple[int, int]]


@pytest.fixture
def legacy(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """只导入 image-owned 脚本的真实函数，不启动 CLI、Docker 或数据库。"""
    name = "legacy_backup_durability_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts/legacy-backup-operations.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _record(legacy: ModuleType, root: Path) -> object:
    """规范合成 record 只用于直接 publisher 测试，没有任何实际资源或凭据。"""
    project = "aiemployee-legacy-" + ATTEMPT.hex
    return legacy.LegacyRegistry(
        "ai_employee.legacy_backup_conversion.v1",
        str(ATTEMPT),
        "2030-01-01T00:00:00.000000Z",
        "legacy_conversion",
        str(root),
        "synthetic.dump.enc",
        "a" * 64,
        19,
        "converted",
        "sha256:" + "b" * 64,
        project,
        project + "-postgres",
        project + "-converter",
        project + "-internal",
        project + "-postgres-data",
        str(root / REGISTRY / (str(ATTEMPT) + ".secret")),
    )


def _observe_filesystem(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[FileEvent]:
    """在真实系统调用之后记录创建/同步顺序；fd 身份来自 fstat，不靠函数名猜同步对象。"""
    events: list[FileEvent] = []
    directories = (root / REGISTRY, root, *root.parents)
    real_mkdir, real_fsync, real_link = os.mkdir, os.fsync, os.link

    def mkdir(path: str | Path, mode: int = 0o777) -> None:
        real_mkdir(path, mode)
        events.append(("mkdir", Path(path)))

    def synchronize(descriptor: int) -> None:
        real_fsync(descriptor)
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            path = next(
                directory
                for directory in directories
                if directory.exists() and os.path.samestat(directory.stat(), info)
            )
            events.append(("directory_fsync", path))
        else:
            events.append(("file_fsync", (info.st_dev, info.st_ino)))

    def publish(source: str | Path, target: Path, *, follow_symlinks: bool = True) -> None:
        real_link(source, target, follow_symlinks=follow_symlinks)
        events.append(("publish", target))

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(os, "fsync", synchronize)
    monkeypatch.setattr(os, "link", publish)
    return events


@pytest.mark.parametrize("layout", ("first_registry", "existing_registry", "new_ancestors"))
def test_registry_persists_every_created_directory_entry_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: ModuleType, layout: str
) -> None:
    """首次 registry 与新建祖先都需同步自身及父目录项；已有 registry 保持文件发布保证。"""
    root = tmp_path / "fresh" / "backups" if layout == "new_ancestors" else tmp_path / "backups"
    if layout != "new_ancestors":
        root.mkdir(mode=0o700)
    if layout == "existing_registry":
        (root / REGISTRY).mkdir(mode=0o700)
    events = _observe_filesystem(monkeypatch, root)
    record = _record(legacy, root)

    target = legacy._publish_registry(record)

    info = target.stat()
    published = events.index(("publish", target))
    assert events.index(("file_fsync", (info.st_dev, info.st_ino))) < published
    assert ("directory_fsync", root / REGISTRY) in events[published + 1 :]
    for index, (kind, path) in enumerate(events):
        if kind == "mkdir":
            assert isinstance(path, Path)
            assert ("directory_fsync", path) in events[index + 1 :], f"unsynced directory: {path}"
            assert ("directory_fsync", path.parent) in events[index + 1 :], (
                f"unsynced parent entry: {path}"
            )
            assert path.stat().st_mode & 0o777 == 0o700
    assert info.st_mode & 0o777 == 0o600 and legacy.read_registry(target, root) == record
    assert sorted(path.name for path in (root / REGISTRY).iterdir()) == [str(ATTEMPT) + ".json"]


class _ResourceBoundaryReached(RuntimeError):
    """测试在首个 Secret 创建前停止；不能把此标记解释为真实容器转换成功。"""


def _read_only_engine(*arguments: str, **_options: object) -> str:
    """只替换外部 engine；真实 convert/registry/lock/cleanup 仍执行且资源查询为空。"""
    if arguments[:4] == ("compose", "--profile", "operations", "config"):
        return json.dumps({"services": {"backup": {"image": "synthetic-backend:fixture"}}})
    if arguments[:2] == ("image", "inspect"):
        return "sha256:" + "b" * 64
    if arguments[:4] == ("compose", "--profile", "legacy-conversion", "config"):
        return json.dumps(
            {
                "services": {
                    "legacy-conversion-postgres": {
                        "image": "docker.io/library/postgres:17.5-alpine"
                    },
                    "legacy-backup-converter": {},
                }
            }
        )
    if arguments[:2] in (("ps", "--all"), ("network", "ls"), ("volume", "ls")):
        return ""
    raise AssertionError("legacy attempted an external resource operation")


@pytest.mark.parametrize("failed_entry", ("registry_parent", "backup_parent", "ancestor_parent"))
def test_directory_fsync_failure_blocks_all_resources_and_retry_resyncs_existing_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: ModuleType, failed_entry: str
) -> None:
    """父目录 fsync 失败须零 Secret/Compose/容器资源；原路径重试也不能跳过失败的屏障。

    原目录项可在失败后仍存在，故存在性不能当作之前已持久化的证据。第二次只走到
    首个 Secret 创建边界即停止，未生成 Secret 字节或实际运行任何 Docker 命令。
    """
    source = tmp_path / "synthetic.dump.enc"
    source.write_bytes(b"synthetic-legacy-encrypted-backup")
    source.chmod(0o600)
    checksum = Path(f"{source}.sha256")
    checksum.write_text(f"{hashlib.sha256(source.read_bytes()).hexdigest()}  {source.name}\n")
    checksum.chmod(0o600)
    root = tmp_path / "fresh" / "backups"
    if failed_entry == "registry_parent":
        root.mkdir(mode=0o700, parents=True)
        created_child, failed_parent = root / REGISTRY, root
    elif failed_entry == "backup_parent":
        created_child, failed_parent = root, root.parent
    else:
        created_child, failed_parent = root.parent, tmp_path
    events = _observe_filesystem(monkeypatch, root)
    observed_fsync, real_open = os.fsync, os.open
    first_failure = OSError("synthetic directory fsync failure")
    failing = True
    secret_attempts = 0

    def synchronize(descriptor: int) -> None:
        if (
            failing
            and created_child.exists()
            and os.path.samestat(os.fstat(descriptor), failed_parent.stat())
        ):
            raise first_failure
        observed_fsync(descriptor)

    def open_file(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal secret_attempts
        if Path(path).suffix == ".secret":
            secret_attempts += 1
            if not failing:
                assert ("directory_fsync", failed_parent) in events
            raise _ResourceBoundaryReached
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("BACKUP_DIR", str(root))
    monkeypatch.setattr(legacy, "uuid4", lambda: ATTEMPT)
    monkeypatch.setattr(legacy, "_docker", _read_only_engine)
    monkeypatch.setattr(os, "fsync", synchronize)
    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(OSError) as refused:
        legacy.convert(source, "converted")
    assert refused.value is first_failure and secret_attempts == 0
    assert not list(root.rglob("*.secret")) and not list(root.rglob("*.lock"))
    assert created_child.is_dir()

    failing = False
    events.clear()
    with pytest.raises(_ResourceBoundaryReached):
        legacy.convert(source, "converted")
    assert secret_attempts == 1 and not list(root.rglob("*.secret"))


@pytest.mark.parametrize("unsafe", ("symlink_ancestor", "wrong_mode"))
def test_directory_creation_keeps_no_symlink_and_private_mode_checks(
    tmp_path: Path, legacy: ModuleType, unsafe: str
) -> None:
    """补齐 mkdir/fsync 不得把 symlink 路径或非 0700 的可写根变成受控目录。"""
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    if unsafe == "symlink_ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(private, target_is_directory=True)
        target = alias / "backups"
    else:
        private.chmod(0o750)
        target = private
    with pytest.raises(legacy.LegacyConversionError):
        legacy._directory(target, create=True)
    assert not (private / "backups").exists()
