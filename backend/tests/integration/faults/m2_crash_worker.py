"""真实 Taskiq 崩溃演练的窄测试模块：暂停已发生的边界，绝不伪造执行结果。

由测试给真实 Worker 追加这个 positional module；执行、审批、数据库、Checkpoint 和
Redis ACK 都仍使用正式实现。只在合成供应商返回后或 ACK 前等待，由父进程发 SIGKILL。
"""

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from redis.asyncio import Redis

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.trusted_actions import ProviderWriteOutcome
from ai_employee.config import get_settings
from ai_employee.infrastructure.queue.broker import broker
from ai_employee.infrastructure.queue.redis_url import validate_test_redis_url
from ai_employee.infrastructure.testing.scenarios import M2FakeActionAdapter

_settings = get_settings()
if (
    _settings.app_env != "test" or not _settings.app_test_mode
    or _settings.external_writes_enabled or _settings.google_writes_enabled
    or _settings.microsoft_writes_enabled or _settings.write_test_account_allowlist
):
    raise RuntimeError("process drill requires isolated synthetic settings")
validate_test_redis_url(_settings.redis_url)
_point = os.environ["M2_PROCESS_CRASH_POINT"]
if _point not in {"after_provider_return", "before_queue_ack", "observe_only"}:
    raise RuntimeError("process drill point is invalid")
_directory = Path(os.environ["M2_PROCESS_CRASH_DIRECTORY"])
if not _directory.is_dir() or _directory.stat().st_mode & 0o777 != 0o700:
    raise RuntimeError("process drill evidence directory is not private")
# 仅在测试模块导入、Broker startup/首次认领之前缩短已有的等待参数。之后完全由
# Redis 自然累积 pending idle，并由正式 XAUTOCLAIM 恢复，不修改任何消息事实。
broker.idle_timeout = 1000
broker.block = 100


def _write_evidence(name: str, facts: Mapping[str, str | int | bool]) -> None:
    """只保存无内容的进程和队列事实；每个证据文件只创建一次并 fsync。"""
    path = _directory / name
    with path.open("x") as stream:
        os.chmod(path, 0o600)
        json.dump(dict(facts), stream)
        stream.flush()
        os.fsync(stream.fileno())


async def _pause_at_boundary(point: str) -> None:
    """先 fsync 精确进程/边界和启动时参数，再等待父进程杀死整个自有组。"""
    await asyncio.to_thread(_write_evidence, "boundary.json", {
        "point": point, "pid": os.getpid(), "task_lease_seconds": _settings.task_lease_seconds,
        "broker_idle_timeout_ms": broker.idle_timeout,
    })
    await asyncio.Event().wait()


_original_execute = M2FakeActionAdapter.execute


async def _execute(self: M2FakeActionAdapter, command: TrustedCommand) -> ProviderWriteOutcome:
    """正式 Fake 已在独立 Redis ledger 记下实际一次写，之后才暂停 Worker。"""
    result = await _original_execute(self, command)
    if _point == "after_provider_return":
        await _pause_at_boundary(_point)
    return result


M2FakeActionAdapter.execute = _execute
_original_ack_generator = broker._ack_generator


def _ack_generator(id: str | bytes, queue_name: str | bytes) -> Callable[[], Awaitable[None]]:
    """保留真实 Redis XACK；观察到 Taskiq when_executed 调用后才暂停，尚不确认。"""
    acknowledge = _original_ack_generator(id=id, queue_name=queue_name)

    async def ack() -> None:
        """只读观察交付次数，执行原 XACK 后记录同一 entry 的确认结果。"""
        if _point == "before_queue_ack":
            await _pause_at_boundary(_point)
        if _point == "observe_only":
            async with Redis(connection_pool=broker.connection_pool) as client:
                pending = await client.xpending_range(queue_name, broker.consumer_group_name, id, id, 1)
            assert len(pending) == 1
        await acknowledge()
        if _point == "observe_only":
            async with Redis(connection_pool=broker.connection_pool) as client:
                remaining = await client.xpending_range(queue_name, broker.consumer_group_name, id, id, 1)
            message_digest = hashlib.sha256(id.encode() if isinstance(id, str) else id).hexdigest()
            await asyncio.to_thread(_write_evidence, f"ack-{message_digest}.json", {
                "pid": os.getpid(), "message_sha256": message_digest,
                "delivery_count": pending[0]["times_delivered"], "pending_after_ack": len(remaining),
            })

    return ack


broker._ack_generator = _ack_generator
