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


@pytest.mark.asyncio
async def test_first_worker_start_consumes_message_enqueued_before_group_creation(
    empty_redis: RedisTestUrl,
) -> None:
    """首次 group 创建必须从 Stream 起点读取已经入队的 task，而不是从尾部跳过。"""
    broker_path, modules = await _worker_command_parts()
    task_id = uuid4()
    environment = os.environ.copy()
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

from redis.asyncio import Redis
from taskiq.cli.utils import import_object, import_tasks

from ai_employee.infrastructure.queue.enqueue import TaskiqTaskEnqueuer

broker = import_object(sys.argv[1])
broker.is_worker_process = True
modules = sys.argv[2:-2]
redis_url = sys.argv[-2]
task_id = UUID(sys.argv[-1])
import_tasks(modules, ["**/tasks.py"], False)
tasks = broker.get_all_tasks()

async def main():
    client = Redis.from_url(redis_url, decode_responses=False)
    listener = broker.listen()
    received = False
    pending_after_ack = -1
    try:
        sender = tasks["ai_employee.workers.execute_task:execute_task"].kiq
        await TaskiqTaskEnqueuer(sender).enqueue(task_id)
        groups_before_startup = await client.xinfo_groups("ai_employee_tasks")
        if groups_before_startup:
            raise RuntimeError("consumer group unexpectedly existed before worker startup")

        await broker.startup()
        try:
            message = await asyncio.wait_for(anext(listener), timeout=0.4)
        except TimeoutError:
            pass
        else:
            received = str(task_id).encode() in message.data
            await message.ack()
            pending = await client.xpending("ai_employee_tasks", "ai_employee_workers")
            pending_after_ack = int(pending["pending"])
    finally:
        await listener.aclose()
        await broker.shutdown()
        await client.aclose()

    print(
        json.dumps(
            {
                "received": received,
                "pending_after_ack": pending_after_ack,
                "tasks": sorted(tasks),
            }
        )
    )

asyncio.run(main())
""",
            broker_path,
            *modules,
            str(empty_redis),
            str(task_id),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    result = json.loads(completed.stdout)
    assert result == {
        "received": True,
        "pending_after_ack": 0,
        "tasks": EXPECTED_TASK_NAMES,
    }


@pytest.mark.asyncio
async def test_smart_retry_waits_in_redis_schedule_source_before_scheduler_enqueues_it(
    empty_redis: RedisTestUrl,
) -> None:
    """真实 SmartRetry 必须先保存五秒延迟任务，只有调度器到期处理后才写入 Stream。"""
    environment = os.environ.copy()
    environment["REDIS_URL"] = str(empty_redis)
    completed = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            """
import asyncio
import json

from redis.asyncio import Redis
from taskiq.message import TaskiqMessage
from taskiq.result import TaskiqResult
from taskiq.cli.scheduler.run import SchedulerLoop

import taskiq.middlewares.smart_retry as smart_retry_module
from ai_employee.domain.errors import TransientProviderError
from ai_employee.infrastructure.queue.broker import broker, retry_schedule_source
from ai_employee.infrastructure.queue.scheduler import scheduler

async def main():
    smart_retry_module.random.random = lambda: 0.0
    retry = broker.middlewares[0]
    message = TaskiqMessage(
        task_id="retry-delay-test",
        task_name="ai_employee.workers.execute_task:execute_task",
        labels={"retry_on_error": True},
        args=["synthetic-task-id"],
        kwargs={},
    )
    result = TaskiqResult(
        is_err=True,
        return_value=None,
        execution_time=0,
    )
    client = Redis.from_url(sys.argv[1], decode_responses=False)
    try:
        await retry.on_error(
            message,
            result,
            TransientProviderError(
                error_code="provider_temporarily_unavailable",
                message="synthetic transient failure",
            ),
        )
        before_due = await client.xlen("ai_employee_tasks")
        await asyncio.sleep(5.2)
        schedules = await retry_schedule_source.get_schedules()
        loop = SchedulerLoop(scheduler)
        due = [
            task
            for task in schedules
            if loop._is_schedule_ready_to_send(task, task.time)
        ]
        for task in due:
            await scheduler.on_ready(retry_schedule_source, task)
        after_due = await client.xlen("ai_employee_tasks")
        print(json.dumps({"before_due": before_due, "schedules": len(schedules), "after_due": after_due}))
    finally:
        await client.aclose()

asyncio.run(main())
""",
            str(empty_redis),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert json.loads(completed.stdout) == {
        "before_due": 0,
        "schedules": 1,
        "after_due": 1,
    }
