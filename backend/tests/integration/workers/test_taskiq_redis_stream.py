"""在真实临时 Redis 上验证 Taskiq 模块注册与 Stream 投递边界。"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from ai_employee.infrastructure.queue.redis_url import RedisTestUrl

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_TASK_NAMES = [
    "ai_employee.workers.execute_task:execute_task",
    "ai_employee.workers.schedules:dispatch_due_briefs",
    "ai_employee.workers.schedules:expire_sessions",
    "ai_employee.workers.schedules:relay_outbox",
]


async def _worker_command_parts() -> tuple[str, list[str]]:
    """从真实 ``just worker`` dry-run 命令提取 broker 与 positional modules。"""
    completed = await asyncio.to_thread(
        subprocess.run,
        ["just", "--dry-run", "worker"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = completed.stdout.strip() or completed.stderr.strip()
    tokens = shlex.split(rendered)
    broker_path = "ai_employee.infrastructure.queue.broker:broker"
    broker_index = tokens.index(broker_path)
    return broker_path, tokens[broker_index + 1 :]


@pytest.mark.asyncio
async def test_clean_worker_registry_and_enqueuer_write_real_redis_stream(
    empty_redis: RedisTestUrl,
) -> None:
    """干净进程按真实启动模块注册四个任务，并把 task_id 写入临时 Redis Stream。"""
    broker_path, modules = await _worker_command_parts()
    task_id = uuid4()
    environment = os.environ.copy()
    # Settings 只读取应用 ``REDIS_URL``；测试目标已经由 fixture fail-closed 校验。
    environment["REDIS_URL"] = str(empty_redis)
    completed = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            """
import asyncio
import json
import sys
from uuid import UUID

from taskiq.cli.utils import import_object, import_tasks

from ai_employee.infrastructure.queue.enqueue import TaskiqTaskEnqueuer

broker = import_object(sys.argv[1])
broker.is_worker_process = True
modules = sys.argv[2:-1]
import_tasks(modules, ["**/tasks.py"], False)
tasks = broker.get_all_tasks()

async def main():
    sender = tasks["ai_employee.workers.execute_task:execute_task"].kiq
    await TaskiqTaskEnqueuer(sender).enqueue(UUID(sys.argv[-1]))
    print(json.dumps(sorted(tasks)))

asyncio.run(main())
""",
            broker_path,
            *modules,
            str(task_id),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == EXPECTED_TASK_NAMES
    client: Redis = Redis.from_url(str(empty_redis), decode_responses=False)
    try:
        assert await client.xlen("ai_employee_tasks") == 1
        entries = await client.xrange("ai_employee_tasks")
    finally:
        await client.aclose()

    assert len(entries) == 1
    message = entries[0][1][b"data"]
    assert str(task_id).encode() in message
