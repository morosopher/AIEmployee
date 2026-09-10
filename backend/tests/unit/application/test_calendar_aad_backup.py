"""通过真实 Bash producer 与合成可执行程序验证备份 hook、失败和取消资源边界。"""

import asyncio
import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import URL

from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
from ai_employee.cli import calendar_aad_backup_0019 as backup
from ai_employee.cli.calendar_aad_backup_0019 import _SCRIPT, run_backup_shell
from ai_employee.infrastructure import calendar_aad_publication as publication_files
from ai_employee.infrastructure.db.repositories import calendar_aad_preflight as resources
from tests.unit.application.test_calendar_aad_resources import (
    ThreadBoundary,
    assert_retained_custody,
    cancel_twice_while_pending,
    replace_after_identity_snapshot,
)
from tests.unit.application.test_calendar_aad_rollout import BINDING, zero_bytes


@pytest.fixture
def synthetic_backup_commands(tmp_path, monkeypatch):
    """只替换 pg_dump/openssl 可执行程序；真实生产 shell 和异步进程管理保持不变。"""
    directory = tmp_path / "backup"
    directory.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pg_dump = fake_bin / "pg_dump"
    pg_dump.write_text("""#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path
Path(os.environ["SYNTHETIC_BACKUP_PID"]).write_text(str(os.getpid()))
if os.environ.get("SYNTHETIC_BACKUP_BLOCK") == "yes":
    time.sleep(120)
if os.environ.get("SYNTHETIC_BACKUP_FAIL") == "yes":
    raise SystemExit(1)
Path(next(item.split("=", 1)[1] for item in sys.argv if item.startswith("--file="))).write_bytes(b"synthetic-dump")
""")
    openssl = fake_bin / "openssl"
    openssl.write_text("""#!/usr/bin/env python3
import sys
from pathlib import Path
args = sys.argv
Path(args[args.index("-out") + 1]).write_bytes(b"synthetic-encrypted:" + Path(args[args.index("-in") + 1]).read_bytes())
""")
    for executable in (pg_dump, openssl):
        executable.chmod(0o700)
    passphrase = tmp_path / "synthetic-passphrase"
    passphrase.write_text("synthetic-test-only")
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("BACKUP_PASSPHRASE_FILE", str(passphrase))
    monkeypatch.setenv("BACKUP_DIR", str(directory))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SYNTHETIC_BACKUP_PID", str(tmp_path / "producer.pid"))
    monkeypatch.delenv("BACKUP_RCLONE_REMOTE", raising=False)
    monkeypatch.delenv("BACKUP_ARTIFACT_BASENAME", raising=False)
    return directory, tmp_path / "producer.pid"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_calendar_aad_backup_uses_shared_shell_producer(
    synthetic_backup_commands, monkeypatch, failed
):
    """成功产出合成加密数据，dump 失败时清理明文；没有第二套 Python dump/encryption 实现。"""
    directory, _ = synthetic_backup_commands
    target = directory / "synthetic.dump.enc"
    if failed:
        monkeypatch.setenv("SYNTHETIC_BACKUP_FAIL", "yes")
        with pytest.raises(CalendarAadRolloutError) as failure:
            await run_backup_shell("produce", target)
        assert failure.value.error_code == "calendar_aad_backup_failed"
        assert list(directory.iterdir()) == []
    else:
        await run_backup_shell("produce", target)
        assert target.read_bytes() == b"synthetic-encrypted:synthetic-dump"
        assert list(directory.iterdir()) == [target]


@pytest.mark.asyncio
async def test_calendar_aad_backup_cancellation_stops_actual_child_group(
    synthetic_backup_commands, monkeypatch
):
    """等待实际 pg_dump 替身启动后取消，返回时该子进程不得仍在执行。"""
    directory, pid_file = synthetic_backup_commands
    monkeypatch.setenv("SYNTHETIC_BACKUP_BLOCK", "yes")
    task = asyncio.create_task(run_backup_shell("produce", directory / "synthetic.dump.enc"))
    try:
        async with asyncio.timeout(3):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        producer_pid = int(pid_file.read_text())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    process_state = Path(f"/proc/{producer_pid}/stat")
    assert not process_state.exists() or process_state.read_text().split()[2] == "Z"
    assert not list(directory.glob("*.dump.enc"))


def test_calendar_aad_backup_keeps_ordinary_script_entry(synthetic_backup_commands, tmp_path):
    """实际bash公开入口只委托镜像内父CLI；宿主替换exec边界，不建立生产路径fallback。"""
    directory, _ = synthetic_backup_commands
    recorded = tmp_path / "recorded-exec"
    boundary = tmp_path / "synthetic-bash-exec"
    boundary.write_text('exec() { printf "%s\\n" "$@" > "$SYNTHETIC_EXEC_RECORD"; }\n')
    environment = {
        **os.environ,
        "BASH_ENV": str(boundary),
        "SYNTHETIC_EXEC_RECORD": str(recorded),
    }
    result = subprocess.run(
        ["bash", str(_SCRIPT)], env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert recorded.read_text().splitlines() == [
        "/app/backend/.venv/bin/python",
        "-m",
        "ai_employee.cli.postgres_backup",
    ]
    assert list(directory.iterdir()) == [], "thin shell must not publish an unguarded legacy pair"


@pytest.fixture
def guarded_backup_resources(monkeypatch):
    """仅替代数据库事实/连接；保留真实 lease adapter、artifact、checksum 与发布路径。"""

    def prepare(directory, on_close):
        """为每个测试绑定独立目录，并记录生产 async lease 退出时的资源状态。"""
        directory.mkdir(mode=0o700, exist_ok=True)
        artifact_file = resources.CalendarAadArtifactFile(directory, BINDING)
        artifact_file.path.write_bytes(zero_bytes())
        artifact_file.path.chmod(0o600)
        closed = []

        @contextmanager
        def context(url):
            """模拟数据库专用 session；退出动作仍由真实异步 wrapper 拥有。"""
            try:
                yield object()
            finally:
                closed.append(on_close())

        async def verify(self):
            """数据库 guard 已有集成覆盖，本例只在其成功后注入本地资源取消。"""

        monkeypatch.setattr(resources, "calendar_aad_rollout_lease", context)
        monkeypatch.setattr(backup.CalendarAadCurrentGuard, "verify", verify)
        return SimpleNamespace(
            arguments={
                "sessions": SimpleNamespace(engine=SimpleNamespace(url=URL.create("postgresql"))),
                "backup_directory": directory,
                "basename": BINDING.basename,
                "immutable_image_id": BINDING.immutable_image_id,
                "clock": lambda: datetime(2030, 1, 1, tzinfo=UTC),
            },
            closed=closed,
            artifact_file=artifact_file,
        )

    return prepare


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", [False, True])
async def test_calendar_aad_group_publisher_keeps_receipt_through_final_lease(
    tmp_path, guarded_backup_resources, monkeypatch, late_failure
):
    """新三件套publisher独占暂存和checksum；最终lease失败仍必须撤回该receipt。"""
    from ai_employee.infrastructure.db.postgres_backup_manifest import BackupGroupPublication

    events = []

    def close():
        events.append("lease_exit")
        if late_failure:
            raise CalendarAadRolloutError("calendar_aad_rollout_lock_lost")

    fixture = guarded_backup_resources(tmp_path / "backups", close)
    staging = tmp_path / "backups" / ".partial.00000000-0000-0000-0000-000000000701"
    staging.mkdir(mode=0o700)
    receipt = BackupGroupPublication(())

    class Publisher:
        """仅替代完整27D发布边界，旧pair流程的任何访问都判错。"""

        staging_root = staging

        async def publish(self, staged, target, guard):
            assert staged.parent == staging and staged.read_bytes() == b"synthetic"
            assert target.parent == staging.parent
            assert not Path(f"{staged}.sha256").exists()
            await guard.verify()
            events.append("published")
            return receipt

        async def discard(self, owned):
            assert owned is receipt
            events.append("discard")

    async def producer(path):
        path.write_bytes(b"synthetic")

    def forbidden(*args):
        raise AssertionError("pair-only path was used by group publisher")

    monkeypatch.setattr(backup, "_prepare_checksum", forbidden)
    monkeypatch.setattr(backup, "_publish_backup", forbidden)
    if late_failure:
        with pytest.raises(CalendarAadRolloutError):
            await backup.run_guarded_backup(
                **fixture.arguments, producer=producer, publisher=Publisher()
            )
        assert events == ["published", "lease_exit", "discard"]
    else:
        await backup.run_guarded_backup(
            **fixture.arguments, producer=producer, publisher=Publisher()
        )
        assert events == ["published", "lease_exit"]
    assert staging.is_dir(), "only the outer locked owner may clean its staging root"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("discard_fails", [False, True])
async def test_group_body_failure_discards_receipt_before_revision_lease_exit(
    tmp_path: Path, guarded_backup_resources, monkeypatch, cancelled: bool, discard_fails: bool
) -> None:
    """末次主体guard失败/取消须先补偿再退出revision lease，且保留首异常对象。

    真实group hook和异步lease adapter执行；只替代数据库guard与完整publisher边界。
    若把补偿重新移到外层except，事件顺序会先出现lease_exit，从而直接捕获SPEC-04。
    """
    from ai_employee.infrastructure.db.postgres_backup_manifest import BackupGroupPublication

    events: list[str] = []
    primary = (
        asyncio.CancelledError("synthetic body cancellation")
        if cancelled
        else CalendarAadRolloutError("calendar_aad_rollout_expired")
    )
    cleanup_failure = RuntimeError("synthetic group discard failure")
    fixture = guarded_backup_resources(tmp_path / "backups", lambda: events.append("lease_exit"))
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    receipt = BackupGroupPublication(())
    published = False

    async def verify(_guard) -> None:
        if published:
            events.append("body_failure")
            raise primary

    class Publisher:
        """模拟外部组发布和补偿；receipt身份与调用顺序属于被测hook的行为。"""

        staging_root = staging

        async def publish(self, staged, target, guard) -> BackupGroupPublication:
            nonlocal published
            assert staged.read_bytes() == b"synthetic"
            published = True
            events.append("published")
            return receipt

        async def discard(self, owned: BackupGroupPublication) -> None:
            assert owned is receipt
            events.append("discard")
            if discard_fails:
                raise cleanup_failure

    async def producer(path: Path) -> None:
        path.write_bytes(b"synthetic")

    monkeypatch.setattr(backup.CalendarAadCurrentGuard, "verify", verify)
    with pytest.raises(type(primary)) as failure:
        await backup.run_guarded_backup(
            **fixture.arguments, producer=producer, publisher=Publisher()
        )
    assert failure.value is primary
    if discard_fails:
        assert failure.value.__cause__ is cleanup_failure
    assert events == ["published", "body_failure", "discard", "lease_exit"]


@pytest.mark.asyncio
async def test_group_body_failure_keeps_revision_lease_during_repeated_cleanup_cancellation(
    tmp_path: Path, guarded_backup_resources, monkeypatch
) -> None:
    """已知主体首错后的补偿遇重复取消仍须收完receipt，再退出原revision lease。"""
    from ai_employee.infrastructure.db.postgres_backup_manifest import BackupGroupPublication

    events: list[str] = []
    entered, release = asyncio.Event(), asyncio.Event()
    fixture = guarded_backup_resources(tmp_path / "backups", lambda: events.append("lease_exit"))
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    receipt = BackupGroupPublication(())
    primary = CalendarAadRolloutError("calendar_aad_rollout_expired")
    published = False

    async def verify(_guard) -> None:
        if published:
            raise primary

    class Publisher:
        """把真实hook的补偿停在可控await，观察lease是否被提前释放。"""

        staging_root = staging

        async def publish(self, staged, target, guard) -> BackupGroupPublication:
            nonlocal published
            published = True
            return receipt

        async def discard(self, owned: BackupGroupPublication) -> None:
            assert owned is receipt
            entered.set()
            await release.wait()
            events.append("discard_finished")

    async def producer(path: Path) -> None:
        path.write_bytes(b"synthetic")

    monkeypatch.setattr(backup.CalendarAadCurrentGuard, "verify", verify)
    task = asyncio.create_task(
        backup.run_guarded_backup(**fixture.arguments, producer=producer, publisher=Publisher())
    )
    await entered.wait()
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        returned_early = task.done()
        lease_exited_early = bool(fixture.closed)
    finally:
        release.set()
        with pytest.raises(CalendarAadRolloutError) as failure:
            await task
    assert failure.value is primary
    assert not returned_early and not lease_exited_early
    assert events == ["discard_finished", "lease_exit"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "collision"),
    [
        ("directory_creation", False),
        ("checksum", False),
        ("publication", False),
        ("publication", True),
        ("directory_cleanup", False),
    ],
)
async def test_calendar_aad_backup_repeated_cancellation_settles_owned_files_before_lease_exit(
    tmp_path, monkeypatch, guarded_backup_resources, boundary, collision
):
    """双取消覆盖创建、校验、发布和清理；本次输出全部撤回，外来碰撞成员始终保留。"""
    gate = ThreadBoundary()
    directory = tmp_path / "guarded"
    fixture = guarded_backup_resources(directory, gate.finished.is_set)
    target = directory / f"{BINDING.basename}.dump.enc"
    existing_checksum = target.with_suffix(".enc.sha256")
    original_directory = backup.tempfile.TemporaryDirectory
    original_cleanup = original_directory.cleanup
    original_checksum, original_publish = backup._prepare_checksum, backup._publish_backup
    directories, produced = [], []

    def create_directory(*args, **kwargs):
        """保留真实 0700 暂存目录并强引用，防止 GC 掩盖 creation 结果遗失。"""
        owned = original_directory(*args, **kwargs)
        directories.append(owned)
        if boundary == "directory_creation":
            gate.pause()
        return owned

    def cleanup(owned):
        """真实清理后暂停，证明取消仍会撤回刚发布的自身输出。"""
        original_cleanup(owned)
        if boundary == "directory_cleanup":
            gate.pause()

    def checksum(path):
        """保持真实同 fd 校验/checksum/fsync，只延迟收回线程结果。"""
        original_checksum(path)
        if boundary == "checksum":
            gate.pause()

    def publish(staged, destination):
        """保留真实 no-clobber 发布及失败回滚，在线程交付结果前暂停。"""
        try:
            return original_publish(staged, destination)
        finally:
            if boundary == "publication":
                gate.pause()

    async def producer(path):
        """生成本例合成 dump；碰撞只在入口检查之后产生，检验发布结果所有权。"""
        produced.append(path)
        path.write_bytes(b"synthetic-encrypted-dump")
        if collision:
            existing_checksum.write_bytes(b"synthetic-existing-checksum")

    monkeypatch.setattr(backup.tempfile, "TemporaryDirectory", create_directory)
    monkeypatch.setattr(original_directory, "cleanup", cleanup)
    monkeypatch.setattr(backup, "_prepare_checksum", checksum)
    monkeypatch.setattr(backup, "_publish_backup", publish)
    task = asyncio.create_task(backup.run_guarded_backup(**fixture.arguments, producer=producer))
    try:
        await gate.reached()
        returned_early = await cancel_twice_while_pending(task)
    finally:
        gate.release.set()
        try:
            with pytest.raises(asyncio.CancelledError) as cancellation:
                await task
        finally:
            await gate.settle()
            remaining = set(directory.iterdir())
            observed_close = list(fixture.closed)
            for owned in directories:
                original_cleanup(owned)
    expected = {fixture.artifact_file.path}
    if collision:
        expected.add(existing_checksum)
        assert existing_checksum.read_bytes() == b"synthetic-existing-checksum"
    assert remaining == expected
    assert fixture.artifact_file.path.read_bytes() == zero_bytes()
    assert not returned_early
    assert cancellation.value.args == ("first resource cancellation",)
    assert observed_close == [True]
    assert len(produced) == (0 if boundary == "directory_creation" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["creation", "stop"])
async def test_calendar_aad_backup_repeated_cancellation_stops_owned_process_before_lease_exit(
    synthetic_backup_commands, guarded_backup_resources, monkeypatch, boundary
):
    """真实 Bash/合成 pg_dump 在 spawn 或 stop 遇第二次取消，返回和 lease exit 必须等进程组。"""
    directory, pid_file = synthetic_backup_commands
    monkeypatch.setenv("SYNTHETIC_BACKUP_BLOCK", "yes")
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    processes = []

    def process_stopped():
        """把退出时的实际 pg_dump 状态作为顺序证据，不把 Fake 状态当作进程结果。"""
        if not pid_file.exists():
            return False
        state = Path(f"/proc/{int(pid_file.read_text())}/stat")
        return not state.exists() or state.read_text().split()[2] == "Z"

    fixture = guarded_backup_resources(directory, process_stopped)
    original_creation = asyncio.create_subprocess_exec
    original_stop = backup._stop_process

    async def create(*args, **kwargs):
        """创建真实独立进程组，只延迟将 Process 所有权交给调用方。"""
        process = await original_creation(*args, **kwargs)
        processes.append(process)
        if boundary == "creation":
            entered.set()
            await release.wait()
        return process

    async def stop(process):
        """暂停已选定进程组的收尾，再调用真实 TERM/wait/KILL 路径。"""
        if boundary == "stop":
            entered.set()
            await release.wait()
        await original_stop(process)
        stopped.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(backup, "_stop_process", stop)
    task = asyncio.create_task(
        backup.run_guarded_backup(
            **fixture.arguments, producer=lambda path: run_backup_shell("produce", path)
        )
    )
    try:
        async with asyncio.timeout(3):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
            if boundary == "stop":
                task.cancel("first process cancellation")
            await entered.wait()
        if boundary == "creation":
            returned_early = await cancel_twice_while_pending(task)
        else:
            task.cancel("second process cancellation")
            done, _ = await asyncio.wait({task}, timeout=0.05)
            returned_early = bool(done)
    finally:
        release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            state_at_return = process_stopped()
            observed_close = list(fixture.closed)
            for process in processes:
                await original_stop(process)
            if boundary == "stop":
                async with asyncio.timeout(3):
                    await stopped.wait()
    assert not returned_early
    assert state_at_return
    assert observed_close == [True]
    assert set(directory.iterdir()) == {fixture.artifact_file.path}


def test_calendar_aad_backup_checksum_rejects_fifo_without_writer(tmp_path):
    """同型 checksum reader 必须在 open 前保持非阻塞，否则拥有它的 lease 无法收尾。"""
    artifact = tmp_path / "synthetic.dump.enc"
    os.mkfifo(artifact, mode=0o600)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(backup._prepare_checksum, artifact)
        try:
            with pytest.raises(CalendarAadRolloutError) as failure:
                pending.result(timeout=0.2)
            assert failure.value.error_code == "calendar_aad_backup_failed"
        finally:
            if not pending.done():
                descriptor = os.open(artifact, os.O_WRONLY | os.O_NONBLOCK)
                os.close(descriptor)
            with pytest.raises(CalendarAadRolloutError):
                pending.result(timeout=3)
    assert not artifact.with_suffix(".enc.sha256").exists()


@pytest.mark.parametrize("member", ["dump", "checksum"])
@pytest.mark.parametrize(
    ("kind", "private_snapshot"),
    [("regular", False), ("fifo", False), ("symlink", False), ("regular", True)],
)
def test_calendar_aad_backup_preserves_replacement_after_identity_snapshot(
    tmp_path, monkeypatch, member, kind, private_snapshot
):
    """一个成员在真实身份快照后被替换时保留其 inode/内容，另一个本次成员仍须撤回。"""
    directory = tmp_path / "staged"
    directory.mkdir(mode=0o700)
    target = tmp_path / f"{BINDING.basename}.dump.enc"
    checksum = target.with_suffix(".enc.sha256")
    staged = directory / target.name
    staged.write_bytes(b"synthetic-encrypted-dump")
    backup._prepare_checksum(staged)
    identities = backup._publish_backup(staged, target)
    staged.unlink()
    staged.with_suffix(".enc.sha256").unlink()
    directory.rmdir()
    replaced = target if member == "dump" else checksum
    other = checksum if member == "dump" else target
    replacement = tmp_path / "synthetic-replacement"
    if kind == "fifo":
        os.mkfifo(replacement, mode=0o600)
    elif kind == "symlink":
        replacement.symlink_to("synthetic-foreign-target")
    else:
        replacement.write_bytes(b"synthetic-foreign-backup-member")
        replacement.chmod(0o600)

    with replace_after_identity_snapshot(
        monkeypatch, replaced, replacement, private_snapshot=private_snapshot
    ) as identity:
        backup._remove_published(target, identities)

    assert not other.exists(), "compensation skipped the other owned backup member"
    assert os.path.lexists(replaced), "compensation deleted a concurrent replacement"
    assert os.path.samestat(replaced.lstat(), identity)
    if kind == "fifo":
        assert stat.S_ISFIFO(replaced.lstat().st_mode)
    elif kind == "symlink":
        assert replaced.readlink() == Path("synthetic-foreign-target")
    else:
        assert replaced.read_bytes() == b"synthetic-foreign-backup-member"
    assert list(tmp_path.iterdir()) == [replaced]


def test_calendar_aad_partial_backup_publish_keeps_concurrent_replacement(tmp_path, monkeypatch):
    """第二成员发布失败时，第一成员的内部补偿也必须保留另一个写入者的替换文件。"""
    directory = tmp_path / "staged"
    directory.mkdir(mode=0o700)
    target = tmp_path / f"{BINDING.basename}.dump.enc"
    staged = directory / target.name
    staged.write_bytes(b"synthetic-encrypted-dump")
    backup._prepare_checksum(staged)
    replacement = tmp_path / "synthetic-replacement"
    replacement.write_bytes(b"synthetic-foreign-backup-member")
    identity = replacement.lstat()
    original_link = os.link
    failure = OSError("synthetic second publication failure")

    def link(source, destination, *args, **kwargs):
        """第一成员保持真实发布；仅在第二成员提交前替换公开 dump 并报告失败。"""
        if destination == target.with_suffix(".enc.sha256"):
            os.replace(replacement, target)
            raise failure
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", link)
    with pytest.raises(OSError) as raised:
        backup._publish_backup(staged, target)
    assert raised.value is failure
    assert not target.with_suffix(".enc.sha256").exists()
    assert target.exists(), "partial-publication cleanup deleted the replacement"
    assert os.path.samestat(target.lstat(), identity)
    assert target.read_bytes() == b"synthetic-foreign-backup-member"


@pytest.mark.parametrize("member", ["dump", "checksum"])
def test_calendar_aad_backup_compensation_conflict_still_removes_other_owned_member(
    tmp_path, monkeypatch, member
):
    """真实无覆盖返还冲突须保留两个外来对象，同时继续处理另一个本次 backup 成员。"""
    directory = tmp_path / "staged"
    directory.mkdir(mode=0o700)
    target = tmp_path / f"{BINDING.basename}.dump.enc"
    checksum = target.with_suffix(".enc.sha256")
    staged = directory / target.name
    staged.write_bytes(b"synthetic-encrypted-dump")
    backup._prepare_checksum(staged)
    identities = backup._publish_backup(staged, target)
    staged.unlink()
    staged.with_suffix(".enc.sha256").unlink()
    directory.rmdir()
    replaced = target if member == "dump" else checksum
    other = checksum if member == "dump" else target
    replacement, occupant = tmp_path / "synthetic-replacement", tmp_path / "synthetic-occupant"
    replacement.write_bytes(b"synthetic-foreign-capture")
    occupant.write_bytes(b"synthetic-current-occupant")
    occupant_identity = occupant.lstat()
    original_rename = os.rename

    def rename(source, destination, *args, **kwargs):
        """只在被替换成员真实捕获后安置新占用者，另一成员使用未改变的真实文件路径。"""
        result = original_rename(source, destination, *args, **kwargs)
        if kwargs.get("dst_dir_fd") is not None and source == replaced.name:
            os.replace(occupant, replaced)
        return result

    monkeypatch.setattr(os, "rename", rename)
    with (
        replace_after_identity_snapshot(monkeypatch, replaced, replacement) as identity,
        pytest.raises(CalendarAadRolloutError) as raised,
    ):
        backup._remove_published(target, identities)
    assert raised.value.error_code == "calendar_aad_publication_compensation_failed"
    assert not other.exists(), "one compensation failure skipped the other owned member"
    assert os.path.samestat(replaced.lstat(), occupant_identity)
    assert replaced.read_bytes() == b"synthetic-current-occupant"
    captured = assert_retained_custody(tmp_path, replaced, identity)
    assert captured.read_bytes() == b"synthetic-foreign-capture"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["capture", "return", "cleanup"])
async def test_calendar_aad_backup_later_cancellations_drain_new_custody_operations(
    tmp_path, monkeypatch, guarded_backup_resources, boundary
):
    """最终退出先失败，新的捕获/返还/空目录清理又遇双取消时仍保持首错并收完另一成员。"""
    gate = ThreadBoundary()
    exit_error = OSError("synthetic final lease exit failure")
    exited, produced = [], []

    def on_close():
        """只在主体发布成功后的最终物理退出失败；后来取消全部发生在本地补偿内部。"""
        exited.append(True)
        raise exit_error

    fixture = guarded_backup_resources(tmp_path, on_close)
    target = tmp_path / f"{BINDING.basename}.dump.enc"
    checksum = target.with_suffix(".enc.sha256")
    replacement = tmp_path / "synthetic-replacement"
    if boundary == "return":
        replacement.write_bytes(b"synthetic-foreign-return")
        replacement_identity = replacement.lstat()
    original_rename, original_rmdir = os.rename, Path.rmdir
    original_return = publication_files._return_without_replacement

    def rename(source, destination, *args, **kwargs):
        """捕获使用真实目录 fd；return 场景在真实预检查之后才替换公开文件。"""
        selected = kwargs.get("dst_dir_fd") is not None and source == target.name
        if selected and boundary == "return":
            os.replace(replacement, target)
        result = original_rename(source, destination, *args, **kwargs)
        if selected and boundary == "capture":
            gate.pause()
        return result

    def return_capture(name, custody_fd, parent_fd):
        """保留实际 RENAME_NOREPLACE，只在外来对象安全返还后延迟线程继续。"""
        original_return(name, custody_fd, parent_fd)
        if name == target.name and boundary == "return":
            gate.pause()

    def rmdir(path):
        """第一个保管空目录真实删除后暂停；补偿线程仍持有下一成员的清理责任。"""
        original_rmdir(path)
        if (
            boundary == "cleanup"
            and path.name.startswith(".calendar-aad-custody-")
            and not gate.entered.is_set()
        ):
            gate.pause()

    async def producer(path):
        """生成合成 dump，其他发布与补偿都保留真实父组合。"""
        produced.append(True)
        path.write_bytes(b"synthetic-encrypted-dump")

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(publication_files, "_return_without_replacement", return_capture)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    task = asyncio.create_task(backup.run_guarded_backup(**fixture.arguments, producer=producer))
    try:
        await gate.reached()
        assert exited == [True]
        assert checksum.exists(), "second member was already removed before the first settled"
        returned_early = await cancel_twice_while_pending(task)
    finally:
        gate.release.set()
        try:
            with pytest.raises(OSError) as raised:
                await task
        finally:
            await gate.settle()
    assert not returned_early
    assert raised.value is exit_error
    assert produced == [True]
    expected = {fixture.artifact_file.path}
    if boundary == "return":
        expected.add(target)
        assert os.path.samestat(target.lstat(), replacement_identity)
        assert target.read_bytes() == b"synthetic-foreign-return"
    assert set(tmp_path.iterdir()) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_error", [False, True])
async def test_calendar_aad_backup_later_cancel_waits_for_publication_rollback(
    tmp_path, monkeypatch, guarded_backup_resources, rollback_error
):
    """发布取消的补偿遇后来取消或错误，须收完两文件/目录并保留首错后再释放 lease。"""
    publication_gate, rollback_gate = ThreadBoundary(), ThreadBoundary()
    fixture = guarded_backup_resources(tmp_path, rollback_gate.finished.is_set)
    original_publish, original_remove = backup._publish_backup, backup._remove_published

    def publish(staged, target):
        """保持真实 no-clobber/fsync 及归属结果，在发布完成后注入首次取消。"""
        result = original_publish(staged, target)
        publication_gate.pause()
        return result

    def remove(target, identities):
        """真实撤回两个本次成员后暂停，使第二次取消落在补偿自身的等待边界。"""
        original_remove(target, identities)
        rollback_gate.pause()
        if rollback_error:
            raise CalendarAadRolloutError("synthetic_compensation_failed")

    async def producer(path):
        """只提供合成加密内容；checksum、发布与清理都由真实父流程执行。"""
        path.write_bytes(b"synthetic-encrypted-dump")

    monkeypatch.setattr(backup, "_publish_backup", publish)
    monkeypatch.setattr(backup, "_remove_published", remove)
    task = asyncio.create_task(backup.run_guarded_backup(**fixture.arguments, producer=producer))
    try:
        await publication_gate.reached()
        task.cancel("first backup publication cancellation")
        publication_gate.release.set()
        await rollback_gate.reached()
        if not rollback_error:
            task.cancel("second backup rollback cancellation")
        done, _ = await asyncio.wait({task}, timeout=0.05)
        returned_early = bool(done)
    finally:
        publication_gate.release.set()
        rollback_gate.release.set()
        try:
            with pytest.raises(asyncio.CancelledError) as cancellation:
                await task
        finally:
            await publication_gate.settle()
            await rollback_gate.settle()
    assert not returned_early
    assert cancellation.value.args == ("first backup publication cancellation",)
    assert fixture.closed == [True]
    assert set(tmp_path.iterdir()) == {fixture.artifact_file.path}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancellation", "exit_error"])
@pytest.mark.parametrize(
    ("initial", "cancel_rollback"),
    [
        ("new", False),
        ("dump_collision", False),
        ("checksum_collision", False),
        ("replacement", False),
        ("checksum_replacement", False),
        ("new", True),
    ],
)
async def test_calendar_aad_backup_final_exit_failure_keeps_publication_ownership(
    tmp_path, monkeypatch, guarded_backup_resources, failure, initial, cancel_rollback
):
    """真实备份父入口发布及私有目录清理完成后，最终 exit 失败只补偿本次仍拥有的成员。"""
    gate, rollback_gate = ThreadBoundary(), ThreadBoundary()
    exited, produced = [], []
    exit_error = OSError("synthetic final backup lease exit failure")

    def on_close():
        """仅在最终同步 context exit 暂停，取消不会提前落入内部 publication/cleanup。"""
        gate.pause()
        exited.append(True)
        if failure == "exit_error":
            raise exit_error
        return True

    fixture = guarded_backup_resources(tmp_path, on_close)
    target = tmp_path / f"{BINDING.basename}.dump.enc"
    checksum = target.with_suffix(".enc.sha256")
    original_discard = backup.discard_calendar_aad_publication
    previous = set()
    if initial in {"dump_collision", "checksum_collision"}:
        collision = target if initial == "dump_collision" else checksum
        collision.write_bytes(b"synthetic-existing-collision")
        previous.add(collision)

    async def producer(path):
        """只写合成加密字节，真实 checksum、no-clobber 发布与目录清理由父调用完成。"""
        produced.append(path)
        path.write_bytes(b"synthetic-encrypted-dump")

    def discard(path, identity):
        """第一个真实成员撤回后暂停；后续取消仍必须完成另一个成员的条件补偿。"""
        original_discard(path, identity)
        if cancel_rollback and path == target:
            rollback_gate.pause()

    monkeypatch.setattr(backup, "discard_calendar_aad_publication", discard)
    task = asyncio.create_task(backup.run_guarded_backup(**fixture.arguments, producer=producer))
    returned_early = False
    try:
        await gate.reached()
        published = {target, checksum} if not previous else previous
        assert set(tmp_path.iterdir()) == {fixture.artifact_file.path, *published}
        assert task.cancelling() == 0
        assert not task.done()
        if "replacement" in initial:
            replaced = target if initial == "replacement" else checksum
            previous_inode = replaced.stat().st_ino
            replacement = tmp_path / "synthetic-replacement"
            replacement.write_bytes(b"synthetic-replacement-member")
            os.replace(replacement, replaced)
            assert replaced.stat().st_ino != previous_inode
            previous.add(replaced)
        if failure == "cancellation":
            returned_early = await cancel_twice_while_pending(task)
        if cancel_rollback:
            gate.release.set()
            assert await asyncio.to_thread(rollback_gate.entered.wait, 0.2), (
                "final-exit failure did not enter owned backup rollback"
            )
            task.cancel("later backup rollback cancellation")
            done, _ = await asyncio.wait({task}, timeout=0.05)
            returned_early = returned_early or bool(done)
    finally:
        gate.release.set()
        rollback_gate.release.set()
        try:
            with pytest.raises(
                asyncio.CancelledError if failure == "cancellation" else OSError
            ) as raised:
                await task
        finally:
            await gate.settle()
            if rollback_gate.entered.is_set():
                await rollback_gate.settle()
    assert not returned_early, "backup returned before the final lease exit thread settled"
    assert exited == [True]
    if failure == "cancellation":
        assert raised.value.args == ("first resource cancellation",)
    else:
        assert raised.value is exit_error
    assert len(produced) == (0 if "collision" in initial else 1)
    expected = {fixture.artifact_file.path, *previous}
    assert set(tmp_path.iterdir()) == expected, "failed parent left its new dump/checksum"
    assert fixture.artifact_file.path.read_bytes() == zero_bytes()
    for path in previous:
        assert path.read_bytes() == (
            b"synthetic-replacement-member"
            if "replacement" in initial
            else b"synthetic-existing-collision"
        )
