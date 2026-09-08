"""验证线程边界取消时不泄漏 revision lease 或尚未发布的工件临时文件。"""

import asyncio
import threading
from contextlib import contextmanager

import pytest
from sqlalchemy import URL

from ai_employee.application.use_cases.calendar_aad_rollout import parse_rollout_artifact
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
