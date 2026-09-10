"""以真实临时三件套验证密文持久化先于任何最终名；不连接数据库或远端。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from ai_employee.infrastructure.db import postgres_backup_manifest as backup


def _staged_group(root: Path) -> tuple[Path, Path]:
    """只生成本测试的合成密文，走真实 metadata writer 后交给实际 publisher。"""
    stage = root / "stage"
    stage.mkdir(mode=0o700)
    destination = root / "published"
    destination.mkdir(mode=0o700)
    dump = stage / "synthetic.dump.enc"
    content = b"synthetic-encrypted-backup"
    dump.write_bytes(content)
    dump.chmod(0o600)
    backup.write_backup_group(
        dump,
        backup.BackupManifest(
            dump_basename=dump.name,
            created_at="2030-01-01T00:00:00.000000Z",
            postgres_server_version="17.5",
            alembic_revision="20260809_0019",
            dump_sha256=hashlib.sha256(content).hexdigest(),
            dump_size=len(content),
            checksum_filename=dump.name + ".sha256",
            immutable_image_id="sha256:" + "a" * 64,
            build_metadata={"release_version": "0.1.0"},
            restore_fingerprint_v1="b" * 64,
            executable_digests={name: "c" * 64 for name in backup.OPERATIONS_EXECUTABLES},
        ),
    )
    return dump, destination


def test_dump_fsync_precedes_every_final_group_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删掉密文 fsync 或把它移至最终名之后，真实 publisher 的事件顺序立即失败。"""
    dump, destination = _staged_group(tmp_path)
    dump_identity = dump.stat()
    events: list[str] = []
    real_fsync, real_link = os.fsync, os.link

    def synchronize(descriptor: int) -> None:
        """记录实际 fd 对应的 inode，仍执行系统 fsync 而非模拟持久化成功。"""
        real_fsync(descriptor)
        if os.path.samestat(os.fstat(descriptor), dump_identity):
            events.append("dump_fsynced")

    def publish(source: Path, target: Path, *, follow_symlinks: bool = True) -> None:
        """记录实际最终目录项出现时刻；writer 的 staging 元数据不算发布。"""
        real_link(source, target, follow_symlinks=follow_symlinks)
        events.append(target.name)

    monkeypatch.setattr(backup.os, "fsync", synchronize)
    monkeypatch.setattr(backup.os, "link", publish)
    receipt = backup.publish_backup_group(dump, destination)

    assert events == [
        "dump_fsynced",
        "synthetic.dump.enc",
        "synthetic.dump.enc.sha256",
        "synthetic.dump.enc.manifest.json",
    ]
    assert tuple(member.path.name for member in receipt.members) == tuple(events[1:])
    assert (destination / dump.name).read_bytes() == b"synthetic-encrypted-backup"
    assert all(member.path.stat().st_mode & 0o777 == 0o600 for member in receipt.members)


@pytest.mark.parametrize("fault", ("fsync_error", "replaced_inode", "changed_mode"))
def test_dump_sync_failure_or_identity_change_publishes_no_final_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """同步失败或同步期间的同字节替换/权限漂移必须在零最终名边界拒绝。

    仅注入精确密文 fd 的系统调用边界；完整三件套读取、验证、发布和补偿仍为生产代码。
    其他 basename 的既存文件、staging 及替换 inode 都保留，不凭文件名获得清理权限。
    """
    dump, destination = _staged_group(tmp_path)
    original_identity = dump.stat()
    foreign = destination / "other.dump.enc"
    foreign.write_bytes(b"unrelated-encrypted-backup")
    foreign.chmod(0o600)
    foreign_identity = foreign.stat()
    first_failure = OSError("synthetic encrypted dump fsync failure")
    sync_attempted = False
    real_fsync = os.fsync

    def synchronize(descriptor: int) -> None:
        """故障只针对验证过的原 inode；不影响真实目录/metadata 的同步。"""
        nonlocal sync_attempted
        if os.path.samestat(os.fstat(descriptor), original_identity):
            sync_attempted = True
            if fault == "fsync_error":
                raise first_failure
            if fault == "replaced_inode":
                replacement = dump.parent / "replacement"
                replacement.write_bytes(dump.read_bytes())
                replacement.chmod(0o600)
                replacement.replace(dump)
            else:
                dump.chmod(0o640)
        real_fsync(descriptor)

    monkeypatch.setattr(backup.os, "fsync", synchronize)
    with pytest.raises(backup.BackupManifestError) as refused:
        backup.publish_backup_group(dump, destination)

    assert sync_attempted
    assert sorted(path.name for path in destination.iterdir()) == ["other.dump.enc"]
    assert os.path.samestat(foreign.stat(), foreign_identity)
    assert foreign.read_bytes() == b"unrelated-encrypted-backup"
    assert dump.read_bytes() == b"synthetic-encrypted-backup"
    assert Path(f"{dump}.sha256").is_file() and Path(f"{dump}.manifest.json").is_file()
    if fault == "fsync_error":
        assert refused.value.__cause__ is first_failure
    elif fault == "replaced_inode":
        assert not os.path.samestat(dump.stat(), original_identity)
    else:
        assert dump.stat().st_mode & 0o777 == 0o640
