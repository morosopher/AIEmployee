"""验证线程边界取消时不泄漏 revision lease 或尚未发布的工件临时文件。"""

import asyncio
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import URL

from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadRolloutError,
    parse_rollout_artifact,
)
from ai_employee.cli import calendar_aad_preflight_0019 as preflight
from ai_employee.config import Settings
from ai_employee.infrastructure import calendar_aad_publication as publication_files
from ai_employee.infrastructure.db.repositories import calendar_aad_preflight as module
from tests.unit.application.test_calendar_aad_rollout import BINDING, zero_bytes


@pytest.mark.asyncio
async def test_calendar_aad_cancelled_lease_acquisition_closes_entered_context(monkeypatch):
    """__enter__ 在线程完成前收到取消时，也必须显式退出，不依赖 generator GC 释放锁。"""
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    contexts, closed = [], []

    @contextmanager
    def waiting_context():
        """代替物理 session，仅暂停 acquire 边界；保持真实 context 协议。"""
        entered.set()
        assert release.wait(3)
        finished.set()
        try:
            yield object()
        finally:
            closed.append(True)

    def factory(url):
        """保持强引用，证明清理是显式行为而不是 CPython 引用计数巧合。"""
        context = waiting_context()
        contexts.append(context)
        return context

    monkeypatch.setattr(module, "calendar_aad_rollout_lease", factory)

    async def acquire():
        """取消之后永不进入已授权执行块。"""
        async with module.async_calendar_aad_rollout_lease(URL.create("postgresql")):
            pytest.fail("cancelled operation entered lease body")

    task = asyncio.create_task(acquire())
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 3)
    assert closed == [True]


@pytest.mark.asyncio
async def test_calendar_aad_cancelled_stage_removes_tempfile(tmp_path, monkeypatch):
    """fsync 完成尚未返回 Path 时取消，也不能遗留未发布成功工件或临时文件。"""
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    original = artifact_file._stage

    def stage(artifact):
        """写真实临时文件，仅延迟线程返回。"""
        path = original(artifact)
        entered.set()
        assert release.wait(3)
        finished.set()
        return path

    class Guard:
        """在 stage 尚未完成的取消场景中 guard 不应被调用。"""

        async def verify(self):
            pytest.fail("cancelled stage reached publication guard")

    monkeypatch.setattr(artifact_file, "_stage", stage)
    task = asyncio.create_task(
        artifact_file.publish(parse_rollout_artifact(zero_bytes(), BINDING), Guard())
    )
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 3)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_calendar_aad_cancelled_publish_waits_and_removes_only_new_artifact(
    tmp_path, monkeypatch, existing
):
    """rename 线程尚未返回时取消，不能在 guard lease 已退出后留下本次成功文件。"""
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    if existing:
        artifact_file.path.write_bytes(zero_bytes())
        artifact_file.path.chmod(0o600)
    original = artifact_file._publish

    def publish(path, artifact):
        """保留真实 rename/fsync，模拟 syscall 返回前收到外层 timeout。"""
        result = original(path, artifact)
        entered.set()
        assert release.wait(3)
        finished.set()
        return result

    class Guard:
        """本例只观察文件取消；数据库 guard 已由集成测试验证。"""

        async def verify(self):
            return None

    monkeypatch.setattr(artifact_file, "_publish", publish)
    task = asyncio.create_task(
        artifact_file.publish(parse_rollout_artifact(zero_bytes(), BINDING), Guard())
    )
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 3)
    assert list(tmp_path.iterdir()) == ([artifact_file.path] if existing else [])


class ThreadBoundary:
    """只暂停测试拥有的同步调用；任何断言失败后仍能释放并等待真实线程。"""

    def __init__(self) -> None:
        """每个场景拥有独立事件，避免取消测试依赖共享线程池时序。"""
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def pause(self) -> None:
        """在线程完成真实副作用后暂停返回，暴露 coroutine 与资源所有权的间隙。"""
        self.entered.set()
        try:
            assert self.release.wait(3), "owned test thread was not released"
        finally:
            self.finished.set()

    async def reached(self) -> None:
        """只在确认工作已经进入受控边界后发送取消。"""
        assert await asyncio.to_thread(self.entered.wait, 3)

    async def settle(self) -> None:
        """释放并等完合成线程；即使 RED 也不把阻塞工作留给测试退出处理。"""
        self.release.set()
        assert await asyncio.to_thread(self.finished.wait, 3)


@contextmanager
def replace_after_identity_snapshot(
    monkeypatch, target: Path, replacement: Path, *, private_snapshot=False
):
    """真实 no-follow 快照返回前，由独立线程替换公开路径，不伪造 inode 或删除结果。

    以真实文件身份识别补偿读取，允许安全实现先把同一文件移入自身目录；公开路径的
    并发写入仍在该快照之后。所有门控、线程和替换文件均由调用测试拥有并显式收敛。
    """
    original_lstat = Path.lstat
    original_stat = os.stat
    owned_identity = original_lstat(target)
    replacement_identity = original_lstat(replacement)
    assert not os.path.samestat(owned_identity, replacement_identity)
    gate = ThreadBoundary()
    snapshots = []

    def lstat(path, *args, **kwargs):
        """先取得未经修改的真实快照，再允许另一个写入者改变公开目录项。"""
        current = original_lstat(path, *args, **kwargs)
        if not snapshots and os.path.samestat(current, owned_identity):
            snapshots.append(current)
            gate.pause()
        return current

    def descriptor_stat(path, *args, **kwargs):
        """新增对照在捕获后的真实 fd 身份读取处暂停，公开路径后续写入仍由独立线程执行。"""
        current = original_stat(path, *args, **kwargs)
        if (
            kwargs.get("dir_fd") is not None
            and not snapshots
            and os.path.samestat(current, owned_identity)
        ):
            snapshots.append(current)
            gate.pause()
        return current

    def replace():
        """仅在产品已读到自有 inode 之后，实际替换为另一 inode 并释放原读取。"""
        try:
            assert gate.entered.wait(3), "compensation never read the owned identity"
            if stat.S_ISDIR(replacement_identity.st_mode):
                # POSIX 不允许用目录覆盖 regular；测试写入者先自行移走旧目录项。
                target.unlink(missing_ok=True)
            os.replace(replacement, target)
        finally:
            gate.release.set()

    with ThreadPoolExecutor(max_workers=1) as executor, monkeypatch.context() as patch:
        if private_snapshot:
            patch.setattr(os, "stat", descriptor_stat)
        else:
            patch.setattr(Path, "lstat", lstat)
        writer = executor.submit(replace)
        try:
            yield replacement_identity
        finally:
            gate.release.set()
            writer.result(timeout=3)
            assert gate.finished.is_set()
            assert len(snapshots) == 1
            if private_snapshot:
                assert os.path.samestat(snapshots[0], owned_identity)
            else:
                assert snapshots[0] == owned_identity


@pytest.mark.parametrize(
    ("kind", "private_snapshot"),
    [("regular", False), ("fifo", False), ("symlink", False), ("regular", True)],
)
def test_calendar_aad_artifact_preserves_replacement_after_identity_snapshot(
    tmp_path, monkeypatch, kind, private_snapshot
):
    """补偿检查读到本次文件之后发生的不同 inode 替换，不得被后续按路径删除。"""
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    artifact = parse_rollout_artifact(zero_bytes(), BINDING)
    temporary = artifact_file._stage(artifact)
    assert artifact_file._publish(temporary, artifact)
    replacement = tmp_path / "synthetic-replacement"
    if kind == "fifo":
        os.mkfifo(replacement, mode=0o600)
    elif kind == "symlink":
        replacement.symlink_to("synthetic-foreign-target")
    else:
        replacement.write_bytes(b"synthetic-foreign-artifact")
        replacement.chmod(0o600)

    with replace_after_identity_snapshot(
        monkeypatch, artifact_file.path, replacement, private_snapshot=private_snapshot
    ) as identity:
        artifact_file.discard_new_publication()

    assert os.path.lexists(artifact_file.path), "compensation deleted a concurrent replacement"
    assert os.path.samestat(artifact_file.path.lstat(), identity)
    if kind == "fifo":
        assert stat.S_ISFIFO(artifact_file.path.lstat().st_mode)
    elif kind == "symlink":
        assert artifact_file.path.readlink() == Path("synthetic-foreign-target")
    else:
        assert artifact_file.path.read_bytes() == b"synthetic-foreign-artifact"
    assert list(tmp_path.iterdir()) == [artifact_file.path]


def test_calendar_aad_failed_artifact_publish_keeps_concurrent_replacement(tmp_path, monkeypatch):
    """发布后目录 fsync 失败的内部补偿也不得删除刚被另一写入者替换的公开文件。"""
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    artifact = parse_rollout_artifact(zero_bytes(), BINDING)
    temporary = artifact_file._stage(artifact)
    replacement = tmp_path / "synthetic-replacement"
    replacement.write_bytes(b"synthetic-foreign-artifact")
    replacement.chmod(0o600)
    identity = replacement.lstat()
    original_fsync = os.fsync
    failure = OSError("synthetic publication fsync failure")
    failed = False

    def fsync(descriptor):
        """真实发布已发生后替换 basename，只拒绝第一个目录 fsync，不替代补偿操作。"""
        nonlocal failed
        if not failed:
            failed = True
            os.replace(replacement, artifact_file.path)
            raise failure
        return original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError) as raised:
        artifact_file._publish(temporary, artifact)
    assert raised.value is failure
    assert artifact_file.path.exists(), "partial-publication cleanup deleted the replacement"
    assert os.path.samestat(artifact_file.path.lstat(), identity)
    assert artifact_file.path.read_bytes() == b"synthetic-foreign-artifact"
    assert list(tmp_path.iterdir()) == [artifact_file.path]


@pytest.fixture
def published_artifact(tmp_path):
    """创建本测试拥有的真实发布文件；后续探针不替代 stage、rename、fsync 或 identity。"""
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    artifact = parse_rollout_artifact(zero_bytes(), BINDING)
    assert artifact_file._publish(artifact_file._stage(artifact), artifact)
    return artifact_file


def assert_retained_custody(directory: Path, target: Path, identity: os.stat_result) -> Path:
    """检查人工可定位的单一保管对象，验证目录权限、原 basename 与真实 inode。"""
    directories = list(directory.glob(".calendar-aad-custody-*"))
    assert len(directories) == 1
    custody = directories[0]
    assert stat.S_IMODE(custody.lstat().st_mode) == 0o700
    captured = custody / target.name
    assert list(custody.iterdir()) == [captured]
    assert os.path.samestat(captured.lstat(), identity)
    return captured


@pytest.mark.parametrize("after_capture", [False, True])
def test_calendar_aad_capture_error_never_erases_an_unknown_result(
    tmp_path, monkeypatch, published_artifact, after_capture
):
    """rename 在生效前/后报错都不能把外来捕获物误认为占位文件删除。"""
    target = published_artifact.path
    replacement = tmp_path / "synthetic-replacement"
    replacement.write_bytes(b"synthetic-foreign-capture")
    replacement.chmod(0o600)
    original_rename = os.rename

    def rename(source, destination, *args, **kwargs):
        """只在私有捕获边界注入错误；after 场景先执行真实 rename 再报告未知结果。"""
        if kwargs.get("dst_dir_fd") is not None:
            if after_capture:
                original_rename(source, destination, *args, **kwargs)
            raise OSError("synthetic capture outcome failure")
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    with (
        replace_after_identity_snapshot(monkeypatch, target, replacement) as identity,
        pytest.raises(CalendarAadRolloutError) as raised,
    ):
        published_artifact.discard_new_publication()
    assert raised.value.error_code == "calendar_aad_publication_compensation_failed"
    if after_capture:
        assert not target.exists()
        captured = assert_retained_custody(tmp_path, target, identity)
        assert captured.read_bytes() == b"synthetic-foreign-capture"
    else:
        assert list(tmp_path.iterdir()) == [target]
        assert os.path.samestat(target.lstat(), identity)
        assert target.read_bytes() == b"synthetic-foreign-capture"


@pytest.mark.parametrize("kind", ["regular", "fifo", "symlink"])
@pytest.mark.parametrize("return_failure", ["collision", "unavailable"])
def test_calendar_aad_failed_return_retains_foreign_capture_without_clobber(
    tmp_path, monkeypatch, published_artifact, kind, return_failure
):
    """原子返还遇新占用者或运行环境不支持时，外来内容与新占用者都保留且明确失败。"""
    target = published_artifact.path
    replacement = tmp_path / "synthetic-replacement"
    if kind == "fifo":
        os.mkfifo(replacement, mode=0o600)
    elif kind == "symlink":
        replacement.symlink_to("synthetic-foreign-target")
    else:
        replacement.write_bytes(b"synthetic-foreign-return")
        replacement.chmod(0o600)
    occupant = tmp_path / "synthetic-next-occupant"
    if return_failure == "collision":
        # 悬空链接也是已占用的真实目录项，不能用 exists() 把它当空位置覆盖。
        occupant.symlink_to("synthetic-next-target")
        occupant_identity = occupant.lstat()
    else:
        # 只模拟 libc 缺少该入口；捕获、metadata 和保管内容仍为真实文件操作。
        monkeypatch.setattr(
            publication_files.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace()
        )
    original_rename = os.rename

    def rename(source, destination, *args, **kwargs):
        """真实捕获后才引入另一个公开占用者，让实际 RENAME_NOREPLACE 拒绝覆盖。"""
        result = original_rename(source, destination, *args, **kwargs)
        if kwargs.get("dst_dir_fd") is not None and return_failure == "collision":
            os.replace(occupant, target)
        return result

    monkeypatch.setattr(os, "rename", rename)
    with (
        replace_after_identity_snapshot(monkeypatch, target, replacement) as identity,
        pytest.raises(CalendarAadRolloutError) as raised,
    ):
        published_artifact.discard_new_publication()
    assert raised.value.error_code == "calendar_aad_publication_compensation_failed"
    captured = assert_retained_custody(tmp_path, target, identity)
    if kind == "regular":
        assert captured.read_bytes() == b"synthetic-foreign-return"
    elif kind == "symlink":
        assert captured.readlink() == Path("synthetic-foreign-target")
    else:
        assert stat.S_ISFIFO(captured.lstat().st_mode)
    if return_failure == "collision":
        assert os.path.samestat(target.lstat(), occupant_identity)
        assert target.readlink() == Path("synthetic-next-target")
    else:
        assert not os.path.lexists(target)


def test_calendar_aad_custody_placeholder_rejects_foreign_directory(
    tmp_path, monkeypatch, published_artifact
):
    """快照后被换成非空目录时，regular 占位必须使 rename 拒绝捕获或删除其内容。"""
    target = published_artifact.path
    replacement = tmp_path / "synthetic-foreign-directory"
    replacement.mkdir(mode=0o700)
    (replacement / "synthetic-member").write_bytes(b"synthetic-foreign-directory-content")
    with (
        replace_after_identity_snapshot(monkeypatch, target, replacement) as identity,
        pytest.raises(CalendarAadRolloutError) as raised,
    ):
        published_artifact.discard_new_publication()
    assert raised.value.error_code == "calendar_aad_publication_compensation_failed"
    assert os.path.samestat(target.lstat(), identity)
    assert (target / "synthetic-member").read_bytes() == b"synthetic-foreign-directory-content"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("phase", ["directory_creation", "capture_stat", "capture_fsync"])
def test_calendar_aad_custody_failure_preserves_files_and_closes_descriptors(
    tmp_path, monkeypatch, published_artifact, phase
):
    """创建/捕获后校验/持久化失败均保守保留对象，已打开的父目录/保管目录/fd 全部关闭。"""
    target = published_artifact.path
    identity = target.lstat()
    original_open, original_close, original_stat = os.open, os.close, os.stat
    opened = set()

    def open_descriptor(*args, **kwargs):
        """记录真实 open 返回的 fd，只观察本同步补偿作用域的资源所有权。"""
        descriptor = original_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def close_descriptor(descriptor):
        """真实关闭之后移出活跃集，防止用 Fake fd 状态掩盖泄漏。"""
        original_close(descriptor)
        opened.remove(descriptor)

    def descriptor_stat(path, *args, **kwargs):
        """真实捕获后的同 fd 身份读取失败，不允许退回公开路径清理。"""
        current = original_stat(path, *args, **kwargs)
        if kwargs.get("dir_fd") is not None:
            raise OSError("synthetic captured metadata failure")
        return current

    def fail(*args, **kwargs):
        """只在指定真实文件边界拒绝操作，其他边界保持原生产行为。"""
        raise OSError("synthetic custody boundary failure")

    monkeypatch.setattr(os, "open", open_descriptor)
    monkeypatch.setattr(os, "close", close_descriptor)
    if phase == "directory_creation":
        monkeypatch.setattr(publication_files.tempfile, "mkdtemp", fail)
    elif phase == "capture_stat":
        monkeypatch.setattr(os, "stat", descriptor_stat)
    else:
        monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(CalendarAadRolloutError) as raised:
        published_artifact.discard_new_publication()
    assert not opened, "compensation leaked its opened file descriptors"
    assert raised.value.error_code == "calendar_aad_publication_compensation_failed"
    if phase == "directory_creation":
        assert list(tmp_path.iterdir()) == [target]
        assert os.path.samestat(target.lstat(), identity)
    else:
        assert not target.exists()
        captured = assert_retained_custody(tmp_path, target, identity)
        assert captured.read_bytes() == zero_bytes()


@pytest.mark.parametrize("phase", ["unlink_before", "unlink_after", "directory_cleanup"])
def test_calendar_aad_custody_cleanup_failure_preserves_new_public_file(
    tmp_path, monkeypatch, published_artifact, phase
):
    """私有删除/空目录清理失败时，新公开文件仍独立存在，未知私有结果不会被递归删除。"""
    target = published_artifact.path
    owned_identity = target.lstat()
    replacement = tmp_path / "synthetic-replacement"
    replacement.write_bytes(b"synthetic-after-capture")
    original_unlink, original_rmdir = os.unlink, Path.rmdir

    def unlink(path, *args, **kwargs):
        """只在私有目录 fd 的真实删除前/后注入失败；不替换公开目录项操作。"""
        if kwargs.get("dir_fd") is not None:
            if phase == "unlink_after":
                original_unlink(path, *args, **kwargs)
            raise OSError("synthetic private unlink failure")
        return original_unlink(path, *args, **kwargs)

    def rmdir(path):
        """只拒绝清理本例空保管目录，保留可由运维辨认的失败证据。"""
        if path.name.startswith(".calendar-aad-custody-"):
            raise OSError("synthetic empty custody cleanup failure")
        return original_rmdir(path)

    if phase == "directory_cleanup":
        monkeypatch.setattr(Path, "rmdir", rmdir)
    else:
        monkeypatch.setattr(os, "unlink", unlink)
    with (
        replace_after_identity_snapshot(
            monkeypatch, target, replacement, private_snapshot=True
        ) as identity,
        pytest.raises(CalendarAadRolloutError) as raised,
    ):
        published_artifact.discard_new_publication()
    assert raised.value.error_code == "calendar_aad_publication_compensation_failed"
    assert os.path.samestat(target.lstat(), identity)
    assert target.read_bytes() == b"synthetic-after-capture"
    if phase == "unlink_before":
        captured = assert_retained_custody(tmp_path, target, owned_identity)
        assert captured.read_bytes() == zero_bytes()
    elif phase == "directory_cleanup":
        directories = list(tmp_path.glob(".calendar-aad-custody-*"))
        assert len(directories) == 1
        assert list(directories[0].iterdir()) == []
    else:
        assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("change", ["content", "mode", "missing"])
def test_calendar_aad_custody_skips_changes_visible_before_capture(
    tmp_path, published_artifact, change
):
    """保留身份核对时已经可见的原地改写、权限变更和消失，不把预检查当删除授权。"""
    target = published_artifact.path
    if change == "missing":
        target.unlink()
    elif change == "mode":
        target.chmod(0o400)
    else:
        target.write_bytes(b"synthetic-modified-content")
    published_artifact.discard_new_publication()
    if change == "missing":
        assert list(tmp_path.iterdir()) == []
    else:
        assert list(tmp_path.iterdir()) == [target]
        if change == "content":
            assert target.read_bytes() == b"synthetic-modified-content"
        else:
            assert stat.S_IMODE(target.lstat().st_mode) == 0o400


async def cancel_twice_while_pending(task: asyncio.Task) -> bool:
    """分两轮事件循环注入取消，观察父调用是否在资源仍被暂停时提前返回。"""
    task.cancel("first resource cancellation")
    await asyncio.sleep(0)
    task.cancel("second resource cancellation")
    done, _ = await asyncio.wait({task}, timeout=0.05)
    return bool(done)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["acquisition", "verification", "cleanup"])
async def test_calendar_aad_repeated_cancellation_waits_for_owned_lease_thread(
    monkeypatch, boundary
):
    """双取消不得丢失 acquire 结果、并行关闭校验连接，或先于 context exit 返回。"""
    gate = ThreadBoundary()
    lease = module.PostgreSQLCalendarAadRolloutLease(object())
    contexts, closed, entered_body = [], [], []

    def verification():
        """保留真实异步 adapter，仅在物理数据库校验的位置暂停线程。"""
        gate.pause()

    @contextmanager
    def context():
        """在指定资源边界等待，并记录真实异步 wrapper 调用 exit 时的顺序。"""
        if boundary == "acquisition":
            gate.pause()
        try:
            yield lease
        finally:
            if boundary == "cleanup":
                gate.pause()
            closed.append(gate.finished.is_set())

    def factory(url):
        """强引用 generator，避免 GC 代替生产代码取回 acquisition 的所有权。"""
        owner = context()
        contexts.append(owner)
        return owner

    monkeypatch.setattr(lease, "verify_owned", verification)
    monkeypatch.setattr(module, "calendar_aad_rollout_lease", factory)

    async def operation():
        """所有可执行步骤均在真实异步 lease 生命周期之内。"""
        async with module.async_calendar_aad_rollout_lease(URL.create("postgresql")):
            entered_body.append(True)
            if boundary == "verification":
                await lease.assert_owned()

    task = asyncio.create_task(operation())
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
            observed_close = list(closed)
            for owner in contexts:
                owner.gen.close()
    assert not returned_early, "lease caller returned while its thread still owned a resource"
    assert cancellation.value.args == ("first resource cancellation",)
    assert observed_close == [True]
    assert entered_body == ([] if boundary == "acquisition" else [True])


@pytest.mark.asyncio
async def test_calendar_aad_overlapping_timeouts_wait_for_verification_before_exit(monkeypatch):
    """真实嵌套 timeout 先后取消同一任务，仍须等校验线程结束后才能释放 lease。"""
    gate = ThreadBoundary()
    lease = module.PostgreSQLCalendarAadRolloutLease(object())
    closed = []
    outer, inner = asyncio.timeout(None), asyncio.timeout(None)

    @contextmanager
    def context(url):
        """只替代物理数据库连接，记录线程完成之前是否发生了 context exit。"""
        try:
            yield lease
        finally:
            closed.append(gate.finished.is_set())

    monkeypatch.setattr(module, "calendar_aad_rollout_lease", context)
    monkeypatch.setattr(lease, "verify_owned", gate.pause)

    async def operation():
        """两个真实 timeout 覆盖同一 lease 校验，与 preflight 的嵌套取消来源一致。"""
        async with outer, inner, module.async_calendar_aad_rollout_lease(URL.create("postgresql")):
            await lease.assert_owned()

    task = asyncio.create_task(operation())
    try:
        await gate.reached()
        # 显式触发已进入的 timeout，避免用墙钟先后差来决定哪一次取消先到。
        for budget in (outer, inner):
            budget.reschedule(asyncio.get_running_loop().time() - 1)
            while not budget.expired():
                await asyncio.sleep(0)
            await asyncio.sleep(0)
        done, _ = await asyncio.wait({task}, timeout=0.05)
        returned_early = bool(done)
    finally:
        gate.release.set()
        try:
            with pytest.raises(TimeoutError):
                await task
        finally:
            await gate.settle()
    assert not returned_early
    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("boundary", ["stage", "publication", "temporary_cleanup"])
async def test_calendar_aad_repeated_cancellation_preserves_artifact_ownership(
    tmp_path, monkeypatch, existing, boundary
):
    """真实 stage/rename/fsync 与清理遇双取消，必须取回结果并只撤回本次新 artifact。"""
    gate = ThreadBoundary()
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    if existing:
        artifact_file.path.write_bytes(zero_bytes())
        artifact_file.path.chmod(0o600)
    original_stage, original_publish = artifact_file._stage, artifact_file._publish
    original_unlink = Path.unlink

    def stage(artifact):
        """真实写入后暂停；取消不能丢失刚创建但尚未返回的 Path。"""
        path = original_stage(artifact)
        if boundary == "stage":
            gate.pause()
        return path

    def publication(path, artifact):
        """真实发布后暂停，分别保留新文件与已存在文件的 ownership 结果。"""
        created = original_publish(path, artifact)
        if boundary == "publication":
            gate.pause()
        return created

    def unlink(path, *args, **kwargs):
        """仅暂停本例临时文件的真实清理，检验取消是否会留下已发布的成功文件。"""
        result = original_unlink(path, *args, **kwargs)
        if boundary == "temporary_cleanup" and path.name.startswith(".calendar-aad-"):
            gate.pause()
        return result

    class Guard:
        """本例隔离文件资源路径；stage 取消不得继续进入发布 guard。"""

        async def verify(self):
            assert boundary != "stage"

    monkeypatch.setattr(artifact_file, "_stage", stage)
    monkeypatch.setattr(artifact_file, "_publish", publication)
    monkeypatch.setattr(Path, "unlink", unlink)
    task = asyncio.create_task(
        artifact_file.publish(parse_rollout_artifact(zero_bytes(), BINDING), Guard())
    )
    try:
        await gate.reached()
        returned_early = await cancel_twice_while_pending(task)
    finally:
        gate.release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await gate.settle()
    assert list(tmp_path.iterdir()) == ([artifact_file.path] if existing else [])
    assert not returned_early, "artifact caller returned before its owned I/O finished"
    if existing:
        assert artifact_file.path.read_bytes() == zero_bytes()


def test_calendar_aad_artifact_fifo_without_writer_is_rejected_before_open_can_block(tmp_path):
    """无 writer 的真实 0600 FIFO 应直接拒绝；RED 时也主动唤醒并收完阻塞线程。"""
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    os.mkfifo(artifact_file.path, mode=0o600)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(artifact_file.read_optional)
        try:
            with pytest.raises(CalendarAadRolloutError) as failure:
                pending.result(timeout=0.2)
            assert failure.value.error_code == "calendar_aad_artifact_invalid"
        finally:
            if not pending.done():
                # 只打开本测试 FIFO 的 writer、不写入数据，使旧实现到达既有 fstat 拒绝分支。
                descriptor = os.open(artifact_file.path, os.O_WRONLY | os.O_NONBLOCK)
                os.close(descriptor)
            with pytest.raises(CalendarAadRolloutError):
                pending.result(timeout=3)


@pytest.mark.parametrize("kind", ["regular", "symlink", "directory", "oversize", "mode"])
def test_calendar_aad_artifact_reader_keeps_regular_file_and_rejects_invalid_objects(
    tmp_path, kind
):
    """非阻塞打开不能放松同一 fd 的 regular、0600、大小或 no-follow 约束。"""
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    if kind == "directory":
        artifact_file.path.mkdir(mode=0o600)
    elif kind == "symlink":
        target = tmp_path / "synthetic-target"
        target.write_bytes(zero_bytes())
        target.chmod(0o600)
        artifact_file.path.symlink_to(target)
    else:
        artifact_file.path.write_bytes(b"x" * 1_048_577 if kind == "oversize" else zero_bytes())
        artifact_file.path.chmod(0o644 if kind == "mode" else 0o600)
    if kind == "regular":
        assert artifact_file.read_optional() == parse_rollout_artifact(zero_bytes(), BINDING)
    else:
        with pytest.raises(CalendarAadRolloutError) as failure:
            artifact_file.read_optional()
        assert failure.value.error_code == "calendar_aad_artifact_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["preflight", "recovery", "backup", "current_guard"])
async def test_calendar_aad_cancelled_artifact_read_finishes_before_lease_exit(
    tmp_path, monkeypatch, entry
):
    """同一 lease 下的实际 artifact 读取也须收回线程，取消后不得进入 provider/业务读取。"""
    from ai_employee.cli.calendar_aad_0019 import run_recovery
    from ai_employee.cli.calendar_aad_backup_0019 import run_guarded_backup
    from ai_employee.cli.calendar_aad_preflight_0019 import run_preflight

    gate = ThreadBoundary()
    lease = module.PostgreSQLCalendarAadRolloutLease(object())
    closed = []
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    artifact_file.path.write_bytes(zero_bytes())
    artifact_file.path.chmod(0o600)
    artifact = artifact_file.read()
    original_read = module.CalendarAadArtifactFile.read_optional

    @contextmanager
    def context(url):
        """物理连接使用合成替身；仍通过生产 wrapper 记录 lease 的释放顺序。"""
        try:
            yield lease
        finally:
            closed.append(gate.finished.is_set())

    def read(owned):
        """保留真实 open/fstat/parse，仅在读取资源的终态交付前暂停。"""
        result = original_read(owned)
        gate.pause()
        return result

    class Repository:
        """取消不得从本地 artifact 读取继续推进到数据库事实查询。"""

        async def read_facts(self, **kwargs):
            pytest.fail("cancelled artifact read continued into business facts")

    monkeypatch.setattr(module, "calendar_aad_rollout_lease", context)
    monkeypatch.setattr(lease, "verify_owned", lambda: None)
    monkeypatch.setattr(module.CalendarAadArtifactFile, "read_optional", read)
    arguments = {
        "sessions": SimpleNamespace(engine=SimpleNamespace(url=URL.create("postgresql"))),
        "backup_directory": tmp_path,
        "basename": BINDING.basename,
        "immutable_image_id": BINDING.immutable_image_id,
        "clock": lambda: datetime(2030, 1, 1, tzinfo=UTC),
    }

    async def operation():
        """各入口使用真实编排，只有外部数据库/供应商端口是不可调用的替身。"""
        if entry == "preflight":
            await run_preflight(**arguments, coordinator=object(), adapters=object())
        elif entry == "recovery":
            await run_recovery(
                **arguments,
                cipher=object(),
                coordinator=object(),
                adapters=object(),
                settings=Settings(app_env="test", app_test_mode=True),
            )
        elif entry == "backup":
            await run_guarded_backup(**arguments, producer=object())
        else:
            async with module.async_calendar_aad_rollout_lease(URL.create("postgresql")):
                await module.CalendarAadCurrentGuard(
                    repository=Repository(),
                    artifact_file=artifact_file,
                    artifact=artifact,
                    lease=lease,
                    clock=arguments["clock"],
                    expected_revision="20260809_0018",
                ).verify()

    task = asyncio.create_task(operation())
    try:
        await gate.reached()
        returned_early = await cancel_twice_while_pending(task)
    finally:
        gate.release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await gate.settle()
    assert not returned_early
    assert closed == [True]
    assert artifact_file.path.read_bytes() == zero_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_error", [False, True])
async def test_calendar_aad_later_cancellation_cannot_interrupt_publication_rollback(
    tmp_path, monkeypatch, rollback_error
):
    """取消触发撤回后遇后来取消或补偿错误，都必须收完真实清理并保留首次取消。"""
    publication_gate, rollback_gate = ThreadBoundary(), ThreadBoundary()
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    original_publish = artifact_file._publish
    original_discard = artifact_file.discard_new_publication

    def publish(path, artifact):
        """完成真实新文件发布，延迟交回 True 所有权结果。"""
        result = original_publish(path, artifact)
        publication_gate.pause()
        return result

    def discard():
        """真实撤回及保管目录清理后暂停，第二次取消不能先于线程的结果交付返回。"""
        original_discard()
        rollback_gate.pause()
        if rollback_error:
            raise CalendarAadRolloutError("synthetic_compensation_failed")

    class Guard:
        """本例只覆盖资源补偿，既有真实数据库 guard 由相邻集成验证。"""

        async def verify(self):
            return None

    monkeypatch.setattr(artifact_file, "_publish", publish)
    monkeypatch.setattr(artifact_file, "discard_new_publication", discard)
    task = asyncio.create_task(
        artifact_file.publish(parse_rollout_artifact(zero_bytes(), BINDING), Guard())
    )
    try:
        await publication_gate.reached()
        task.cancel("first publication cancellation")
        publication_gate.release.set()
        await rollback_gate.reached()
        if not rollback_error:
            task.cancel("second rollback cancellation")
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
    assert cancellation.value.args == ("first publication cancellation",)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "exit_error"])
@pytest.mark.parametrize(
    ("initial", "cancel_rollback"),
    [("new", False), ("existing", False), ("replacement", False), ("new", True)],
)
async def test_calendar_aad_preflight_final_exit_failure_keeps_publication_ownership(
    tmp_path, monkeypatch, failure, initial, cancel_rollback
):
    """真实父入口发布及临时清理完成后，最终 exit 超时/失败只能撤回本次仍拥有的文件。"""
    gate, rollback_gate = ThreadBoundary(), ThreadBoundary()
    artifact_file = module.CalendarAadArtifactFile(tmp_path, BINDING)
    artifact = parse_rollout_artifact(zero_bytes(), BINDING)
    if initial == "existing":
        artifact_file.path.write_bytes(zero_bytes())
        artifact_file.path.chmod(0o600)
    closed, budgets, executions = [], [], []
    exit_error = OSError("synthetic final lease exit failure")
    original_timeout = asyncio.timeout
    original_discard = module.CalendarAadArtifactFile.discard_new_publication

    def timeout(delay):
        """保留父入口自己创建的真实总 timeout，仅在进入最终 exit 后显式触发它。"""
        budget = original_timeout(delay)
        budgets.append(budget)
        return budget

    @contextmanager
    def context(url):
        """仅替代物理数据库 context；发布、异步 exit 等待和最终异常都走真实父入口。"""
        try:
            yield object()
        finally:
            gate.pause()
            closed.append(True)
            if failure == "exit_error":
                raise exit_error

    async def execute(self, *, binding, existing):
        """替代已成功的外部用例，只交付完整合成 artifact，不执行数据库或供应商访问。"""
        assert binding == BINDING
        assert existing == (artifact if initial == "existing" else None)
        executions.append(True)
        return artifact

    async def verify(self):
        """本例固定当前 guard 成功，把故障严格放在发布完成之后的最终 lease exit。"""

    def discard(owned):
        """撤回已真实完成后暂停，后续取消不能穿透收尾或替换原 timeout/退出异常。"""
        original_discard(owned)
        if cancel_rollback:
            rollback_gate.pause()

    monkeypatch.setattr(asyncio, "timeout", timeout)
    monkeypatch.setattr(module, "calendar_aad_rollout_lease", context)
    monkeypatch.setattr(preflight.CalendarAadPreflightUseCase, "execute", execute)
    monkeypatch.setattr(module.CalendarAadCurrentGuard, "verify", verify)
    monkeypatch.setattr(module.CalendarAadArtifactFile, "discard_new_publication", discard)
    task = asyncio.create_task(
        preflight.run_preflight(
            sessions=SimpleNamespace(engine=SimpleNamespace(url=URL.create("postgresql"))),
            coordinator=object(),
            adapters=object(),
            backup_directory=tmp_path,
            basename=BINDING.basename,
            immutable_image_id=BINDING.immutable_image_id,
            clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
            task_timeout_seconds=30,
        )
    )
    returned_early = False
    try:
        await gate.reached()
        # 门控位于真正的 context.__exit__：临时文件已清除，publication 已交回父调用。
        assert list(tmp_path.iterdir()) == [artifact_file.path]
        assert artifact_file.path.read_bytes() == zero_bytes()
        assert task.cancelling() == 0
        assert not task.done()
        if initial == "replacement":
            previous_inode = artifact_file.path.stat().st_ino
            replacement = tmp_path / "synthetic-replacement"
            replacement.write_bytes(zero_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, artifact_file.path)
            assert artifact_file.path.stat().st_ino != previous_inode
        if failure == "timeout":
            assert len(budgets) == 1
            budgets[0].reschedule(asyncio.get_running_loop().time() - 1)
            while not budgets[0].expired():
                await asyncio.sleep(0)
            done, _ = await asyncio.wait({task}, timeout=0.05)
            returned_early = bool(done)
        if cancel_rollback:
            gate.release.set()
            assert await asyncio.to_thread(rollback_gate.entered.wait, 0.2), (
                "final-exit failure did not enter owned artifact rollback"
            )
            task.cancel("later artifact rollback cancellation")
            done, _ = await asyncio.wait({task}, timeout=0.05)
            returned_early = returned_early or bool(done)
    finally:
        gate.release.set()
        rollback_gate.release.set()
        try:
            with pytest.raises(TimeoutError if failure == "timeout" else OSError) as raised:
                await task
        finally:
            await gate.settle()
            if rollback_gate.entered.is_set():
                await rollback_gate.settle()
    assert not returned_early, "preflight returned before the final lease exit thread settled"
    assert closed == [True]
    assert executions == [True]
    if failure == "exit_error":
        assert raised.value is exit_error
    expected = [] if initial == "new" else [artifact_file.path]
    assert list(tmp_path.iterdir()) == expected, "failed parent left its new success artifact"
    if expected:
        assert artifact_file.path.read_bytes() == zero_bytes()
