"""在真实临时 Redis 上验证 Taskiq 模块注册与 Stream 投递边界。"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from taskiq import AckableMessage

from ai_employee.infrastructure.queue.redis_url import RedisTestUrl

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_TASK_NAMES = [
    "ai_employee.workers.execute_task:execute_task",
    "ai_employee.workers.schedules:dispatch_due_briefs",
    "ai_employee.workers.schedules:dispatch_google_incremental_syncs",
    "ai_employee.workers.schedules:dispatch_overdue_brief_diagnostics",
    "ai_employee.workers.schedules:expire_approvals",
    "ai_employee.workers.schedules:expire_sessions",
    "ai_employee.workers.schedules:recover_approval_checkpoints",
    "ai_employee.workers.schedules:recover_task_retries",
    "ai_employee.workers.schedules:relay_outbox",
    "ai_employee.workers.schedules:run_retention_cleanup",
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
    """干净进程按真实启动模块注册五个任务，并把 task_id 写入临时 Redis Stream。"""
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
async def test_resume_envelope_carries_only_task_identifier_and_decision(
    empty_redis: RedisTestUrl,
) -> None:
    """审批恢复消息进入真实 Stream 时不得泄露冻结载荷或预览正文。"""
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
import sys
from uuid import UUID

from taskiq.cli.utils import import_object, import_tasks
from ai_employee.infrastructure.queue.enqueue import TaskiqTaskEnqueuer

broker = import_object(sys.argv[1])
broker.is_worker_process = True
import_tasks(sys.argv[2:-1], ["**/tasks.py"], False)

async def main():
    sender = broker.get_all_tasks()["ai_employee.workers.execute_task:execute_task"].kiq
    await TaskiqTaskEnqueuer(sender).enqueue(UUID(sys.argv[-1]), resume="rejected")

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
    assert completed.stderr == ""
    client: Redis = Redis.from_url(str(empty_redis), decode_responses=False)
    try:
        entries = await client.xrange("ai_employee_tasks")
    finally:
        await client.aclose()
    assert len(entries) == 1
    envelope = entries[0][1][b"data"]
    assert str(task_id).encode() in envelope
    assert b"rejected" in envelope
    assert b"proposal_payload" not in envelope
    assert b"preview_markdown" not in envelope


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


def test_taskiq_broker_exposes_no_redis_delay_schedule_source() -> None:
    """Redis Stream 仅接收 Outbox relay 的立即任务，不保存延迟重试事实。"""
    from ai_employee.infrastructure.queue.broker import broker

    assert broker.middlewares == []


@pytest.mark.parametrize("orphan_coordination_lock", [False, True])
async def test_expired_pending_survives_orphan_reclaim_lock_and_concurrent_listeners(
    empty_redis: RedisTestUrl, orphan_coordination_lock: bool,
) -> None:
    """原 pending 自然过期后可由真实 Broker 重领，孤儿辅助锁不能永久阻塞。

    两个消费端竞争同一 Redis 原 entry；XAUTOCLAIM 必须保持唯一当前 owner，ACK 必须
    确认原 entry。旧版 broker 在无 TTL 的孤儿协调锁下不再投递，此例直接捕获该缺陷。
    """
    from ai_employee.infrastructure.queue.broker import broker as configured_broker

    queue = "synthetic_recovery_tasks"
    group = "synthetic_recovery_workers"
    # 使用生产组合根实际选用的类型，避免测到另一个只存在于测试里的恢复实现。
    brokers = [type(configured_broker)(
        url=str(empty_redis), queue_name=queue, consumer_group_name=group,
        consumer_name=f"synthetic-consumer-{index}", consumer_id="0-0",
        idle_timeout=200, xread_block=5,
    ) for index in range(3)]
    client = Redis.from_url(str(empty_redis), decode_responses=False)
    listeners: list[AsyncGenerator[AckableMessage, None]] = []
    reads: list[asyncio.Task[AckableMessage]] = []
    try:
        for current in brokers:
            await current.startup()
        original_id = await client.xadd(queue, {"data": b"synthetic-pending-message"})
        listeners.append(brokers[0].listen())
        first = await asyncio.wait_for(anext(listeners[0]), timeout=1)
        assert first.data == b"synthetic-pending-message"
        await listeners[0].aclose()
        # fixture 在消费前声明旧版本进程崩溃留下的协调状态；恢复阶段不删锁、不 claim，
        # 只用 Redis 自己报告的 idle 等待真实过期。
        lock_key = f"autoclaim:{group}:{queue}"
        if orphan_coordination_lock:
            await client.set(lock_key, b"synthetic-orphan-coordination")
            assert await client.pttl(lock_key) == -1
        async with asyncio.timeout(2):
            while True:
                pending = await client.xpending_range(queue, group, original_id, original_id, 1)
                if pending[0]["time_since_delivered"] >= 200:
                    break
                await asyncio.sleep(0.01)
        for current in brokers[1:]:
            listener = current.listen()
            listeners.append(listener)
            reads.append(asyncio.create_task(anext(listener)))
        done, waiting = await asyncio.wait(reads, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert len(done) == 1, "the original pending entry was not recovered exactly once"
        winning_read = next(iter(done))
        message = winning_read.result()
        expected_owner = brokers[reads.index(winning_read) + 1].consumer_name.encode()
        for read in waiting:
            read.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        pending = await client.xpending_range(queue, group, original_id, original_id, 1)
        assert len(pending) == 1 and pending[0]["message_id"] == original_id
        assert pending[0]["times_delivered"] == 2
        assert pending[0]["consumer"] == expected_owner
        assert message.data == b"synthetic-pending-message"
        await message.ack()
        assert (await client.xpending(queue, group))["pending"] == 0
        if orphan_coordination_lock:
            assert await client.get(lock_key) == b"synthetic-orphan-coordination"
            assert await client.pttl(lock_key) == -1
    finally:
        for read in reads:
            read.cancel()
        await asyncio.gather(*reads, return_exceptions=True)
        for listener in listeners:
            await listener.aclose()
        for current in brokers:
            await current.shutdown()
        await client.aclose()


@pytest.mark.parametrize("additional_stream", [False, True])
async def test_deep_pending_scan_recovers_tail_then_wraps_after_natural_idle(
    redis_url: RedisTestUrl, additional_stream: bool,
) -> None:
    """深 PEL 的活跃前缀不能遮住到期尾部，扫描回绕后仍须恢复后来到期的首项。

    COUNT=1 时 Redis 每页最多检查十项；主 stream 的十二项和附加 stream 的二十二项
    前缀会产生真实空页。不相交的合成 entry ID 仅用于固定排序，防止跨 stream 共享游标
    偶然通过。所有 idle 均来自真实交付和 Redis 自然时间，不设置 IDLE/TIME、不改时钟。
    原消费者重读自己的前缀代表仍在处理这些消息；测试不领取、删除或 ACK 无关消息。
    """
    from ai_employee.infrastructure.queue.broker import broker as configured_broker

    namespace = f"synthetic-pel-cursor-{uuid4().hex}"
    group = f"{namespace}-workers"
    original_consumer = "synthetic-original-consumer"
    queue = f"{namespace}-main"
    extra = f"{namespace}-additional"
    # 附加 stream 的整个 ID 区间早于主 stream，错误复用主游标会跳过它的 PEL。
    cases = [(queue, 1000, 12, b"synthetic-tail-main")]
    if additional_stream:
        cases.append((extra, 1, 22, b"synthetic-tail-additional"))
    broker = type(configured_broker)(
        url=str(redis_url), queue_name=queue, consumer_group_name=group,
        consumer_name="synthetic-recovery-consumer", consumer_id="0-0",
        idle_timeout=2000, xread_block=5, unacknowledged_batch_size=1,
        additional_streams={extra: ">"} if additional_stream else {},
    )
    client = Redis.from_url(str(redis_url), decode_responses=False)
    listener = broker.listen()
    prefix_ids: dict[str, list[bytes]] = {}
    prefix_entries: dict[bytes, tuple[str, bytes]] = {}
    tail_ids: dict[bytes, tuple[str, bytes]] = {}
    prefix_state: dict[str, list[tuple[bytes, bytes, int]]] = {}
    try:
        await broker.startup()
        for position, (stream, first_id, prefix_count, tail_data) in enumerate(cases):
            prefix_ids[stream] = []
            for index in range(prefix_count):
                prefix_data = f"synthetic-prefix-{position}-{index}".encode()
                entry_id = await client.xadd(
                    stream, {"data": prefix_data},
                    id=f"{first_id + index}-0",
                )
                prefix_ids[stream].append(entry_id)
                prefix_entries[prefix_data] = (stream, entry_id)
            tail_id = await client.xadd(
                stream, {"data": tail_data}, id=f"{first_id + prefix_count}-0",
            )
            tail_ids[tail_data] = (stream, tail_id)
            delivered = await client.xreadgroup(
                group, original_consumer, {stream: ">"}, count=prefix_count + 1,
            )
            assert [entry[0] for entry in delivered[0][1]] == [*prefix_ids[stream], tail_id]

        # 先等全部原尾部自然到期；随后只用普通历史交付刷新前缀，尾部保持原 owner/idle。
        async with asyncio.timeout(4):
            while True:
                tails = [
                    (await client.xpending_range(stream, group, entry_id, entry_id, 1))[0]
                    for stream, entry_id in tail_ids.values()
                ]
                if all(item["time_since_delivered"] >= 2000 for item in tails):
                    break
                await asyncio.sleep(0.01)
        assert all(item["times_delivered"] == 1 for item in tails)
        for stream, _first_id, prefix_count, _tail_data in cases:
            refreshed = await client.xreadgroup(
                group, original_consumer, {stream: "0-0"}, count=prefix_count,
            )
            assert [entry[0] for entry in refreshed[0][1]] == prefix_ids[stream]
            prefix = await client.xpending_range(stream, group, "-", "+", prefix_count)
            prefix_state[stream] = [
                (item["message_id"], item["consumer"], item["times_delivered"])
                for item in prefix
            ]
            assert all(item["consumer"] == original_consumer.encode() for item in prefix)
            assert all(item["times_delivered"] == 2 for item in prefix)

        received: set[bytes] = set()
        acknowledgements: list[dict[str, object]] = []
        try:
            # 整个恢复窗口短于前缀 idle 门槛；超时后也先核验前缀仍活跃，避免错误归因。
            async with asyncio.timeout(1):
                while len(received) < len(cases):
                    message = await anext(listener)
                    assert message.data in tail_ids and message.data not in received
                    stream, entry_id = tail_ids[message.data]
                    pending = await client.xpending_range(stream, group, entry_id, entry_id, 1)
                    assert len(pending) == 1 and pending[0]["message_id"] == entry_id
                    assert pending[0]["consumer"] == broker.consumer_name.encode()
                    assert pending[0]["times_delivered"] == 2
                    before_ack = (await client.xpending(stream, group))["pending"]
                    await message.ack()
                    assert await client.xpending_range(stream, group, entry_id, entry_id, 1) == []
                    after_ack = (await client.xpending(stream, group))["pending"]
                    assert after_ack == before_ack - 1
                    acknowledgements.append({
                        "original_entry_id": entry_id.decode(), "deliveries": 2,
                        "pending_before_ack": before_ack, "pending_after_ack": after_ack,
                    })
                    received.add(message.data)
        except TimeoutError:
            # 预期 RED 仍保留真实 pending 状态；由下方业务断言给出明确失败原因。
            pass
        prefix_idle_maxima = []
        for stream, _first_id, prefix_count, _tail_data in cases:
            prefix = await client.xpending_range(stream, group, "-", "+", prefix_count)
            assert [
                (item["message_id"], item["consumer"], item["times_delivered"])
                for item in prefix
            ] == prefix_state[stream]
            prefix_idle_maxima.append(max(item["time_since_delivered"] for item in prefix))
        assert all(idle < 2000 for idle in prefix_idle_maxima), "active-prefix window elapsed"
        print(json.dumps({
            "phase": "deep_pel_tail", "streams": len(cases), "batch_size": 1,
            "prefix_counts": [case[2] for case in cases], "prefix_idle_max_ms": prefix_idle_maxima,
            "prefix_owner_and_delivery_unchanged": True, "tail_count": len(received),
            "original_entry_acknowledgements": acknowledgements,
        }))
        assert received == set(tail_ids), "expired original tail was starved behind active PEL prefix"

        # 保持同一 listener，等待首个前缀自然到期；尾部页返回 0-0 后必须从头恢复它。
        first_prefix_id = prefix_ids[queue][0]
        async with asyncio.timeout(4):
            while True:
                first = await client.xpending_range(queue, group, first_prefix_id, first_prefix_id, 1)
                if first[0]["time_since_delivered"] >= 2000:
                    break
                await asyncio.sleep(0.01)
        wrap_acknowledgements = []
        async with asyncio.timeout(1):
            while True:
                # 多 stream 交替扫描时可先继续当前页；允许合法顺序，但必须回绕到原首项。
                message = await anext(listener)
                assert message.data in prefix_entries
                stream, entry_id = prefix_entries[message.data]
                pending = await client.xpending_range(stream, group, entry_id, entry_id, 1)
                assert pending[0]["consumer"] == broker.consumer_name.encode()
                assert pending[0]["times_delivered"] == 3
                before_ack = (await client.xpending(stream, group))["pending"]
                await message.ack()
                assert await client.xpending_range(stream, group, entry_id, entry_id, 1) == []
                after_ack = (await client.xpending(stream, group))["pending"]
                assert after_ack == before_ack - 1
                wrap_acknowledgements.append({
                    "original_entry_id": entry_id.decode(), "deliveries": 3,
                    "pending_before_ack": before_ack, "pending_after_ack": after_ack,
                })
                if stream == queue and entry_id == first_prefix_id:
                    break
        print(json.dumps({
            "phase": "deep_pel_wrap", "streams": len(cases),
            "original_first_entry_id": first_prefix_id.decode(),
            "original_entry_acknowledgements": wrap_acknowledgements,
        }))
    finally:
        await listener.aclose()
        await broker.shutdown()
        # 仅清理本例创建的 UUID stream；不执行 FLUSHDB，也不接触其他消息或旧孤儿锁。
        await client.delete(*(case[0] for case in cases))
        await client.aclose()
