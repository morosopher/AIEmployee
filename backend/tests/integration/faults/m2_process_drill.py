"""编排一个合成邮件的真实 Worker 进程崩溃、Redis 重投和持久结果核对演练。

复用正式 E2E argv、Taskiq/Outbox、受管测试数据库与 Fake ledger。子进程原始输出只
保存在0700/0600临时证据目录；测试断言和可见回执不包含命令明文或环境值。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import signal
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import BinaryIO
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from taskiq.kicker import AsyncKicker
from taskiq_redis import RedisStreamBroker

from ai_employee.application.use_cases.outbox import OutboxRelay
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl
from ai_employee.infrastructure.db.models.sources import (
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    OutboxEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.outbox import SqlAlchemyOutboxStore
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.events.publisher import TaskEventPublisher
from ai_employee.infrastructure.queue.enqueue import TaskiqTaskEnqueuer
from ai_employee.infrastructure.queue.redis_url import validate_test_redis_url
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.infrastructure.testing.scenarios import M2FakeActionAdapter
from ai_employee.infrastructure.testing.trusted_actions import synthetic_account_id
from tests.integration.e2e_backend import (
    child_environment,
    owned_process_reaping,
    service_command,
    stop_owned_process,
)
from tests.integration.m2.test_tool_execution_claim import _Seed, _seed_action

ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class ProcessFacts:
    """仅投影演练断言所需的状态和计数，不读取审批命令或其他用户内容。"""

    task_status: str
    execution_status: str | None
    execution_id: UUID | None
    executions: int
    write_attempts: int
    request_started: bool
    request_started_at: datetime | None
    lease_expires_at: datetime | None
    scheduled_for: datetime | None
    checkpoints: int


async def _facts(sessions: ManagedAsyncSessionMaker, task_id: UUID) -> ProcessFacts:
    """每次用独立新会话读取已提交事实，包含真实 LangGraph checkpoint 的数量。"""
    from sqlalchemy import text

    async with sessions() as session:
        task = await session.get(TaskRunModel, task_id)
        assert task is not None
        executions = tuple((await session.scalars(select(ToolExecutionModel).where(
            ToolExecutionModel.task_id == task_id,
        ))).all())
        checkpoints = 0
        if await session.scalar(text("SELECT to_regclass('public.checkpoints') IS NOT NULL")):
            checkpoints = int(await session.scalar(text(
                "SELECT count(*) FROM checkpoints WHERE thread_id = :task_id"
            ), {"task_id": str(task_id)}) or 0)
        execution = executions[0] if executions else None
        return ProcessFacts(
            task_status=task.status, execution_status=execution.status if execution else None,
            execution_id=execution.id if execution else None, executions=len(executions),
            write_attempts=execution.write_attempt_count if execution else 0,
            request_started=execution is not None and execution.request_started_at is not None,
            request_started_at=execution.request_started_at if execution else None,
            lease_expires_at=task.lease_expires_at, scheduled_for=task.scheduled_for,
            checkpoints=checkpoints,
        )


async def _seed(sessions: ManagedAsyncSessionMaker, database_url: str) -> _Seed:
    """建立真实加密且已批准的夹具；人工审批本身由独立浏览器矩阵验证。

    只在 Worker 启动前补全同一个合成连接的身份与加密凭据，并令审批窗口使用数据库
    当前时间。后续崩溃与恢复不能修改命令、哈希、审批决定或任何执行结果。
    """
    seed = _Seed()
    seed.provider_account_id = synthetic_account_id("google", seed.connection_id)
    async with sessions() as session:
        current = await session.scalar(select(func.clock_timestamp()))
    assert current is not None
    await _seed_action(database_url, seed, task_status=TaskStatus.QUEUED,
                       deadline=current + timedelta(minutes=5))
    async with sessions.begin() as session:
        await session.execute(update(OAuthConnectionModel).where(
            OAuthConnectionModel.id == seed.connection_id,
        ).values(account_email=f"synthetic-{seed.connection_id}@example.test"))
        await session.execute(update(ApprovalRequestModel).where(
            ApprovalRequestModel.id == seed.approval_id,
        ).values(decided_at=current, expires_at=current + timedelta(minutes=10)))
        for kind in ("access_token", "refresh_token"):
            encrypted = AeadCipher(b"x" * 32).encrypt(
                b"synthetic-process-credential", f"{seed.user_id}:{seed.connection_id}:{kind}".encode(),
            )
            session.add(EncryptedCredentialModel(
                user_id=seed.user_id, connection_id=seed.connection_id, credential_kind=kind,
                ciphertext=encrypted.ciphertext, nonce=encrypted.nonce, key_version=encrypted.key_version,
                token_expires_at=current + timedelta(hours=1) if kind == "access_token" else None,
            ))
        session.add(OutboxEventModel(
            topic="task.execute", aggregate_id=seed.task_id,
            deduplication_key=f"synthetic-process:{seed.task_id}", payload={"task_id": str(seed.task_id)},
            available_at=current,
        ))
    return seed


async def exercise_worker_crash(*, database_url: str, redis_url: str, point: str, directory: Path) -> dict[str, object]:
    """运行两次真实 Worker，证明 SIGKILL 前请求/结果已提交而 Redis 消息尚未 ACK。

    Args:
        database_url: 官方 provenance lifecycle 提供的 disposable regular 库。
        redis_url: 只允许本地保留 DB15；该库由本串行故障例清空。
        point: 合成供应商返回后，或 Taskiq 真实 when_executed ACK 前。
        directory: pytest 独占临时目录，保存原始日志及内容无关证据。

    Returns:
        退出、重投、执行次数、最终持久状态与日志字节摘要。任何验证失败仍回收全部自有组。
    """
    redis_url = validate_test_redis_url(redis_url).value
    directory.chmod(0o700)
    key_file = directory / "master-key"
    key_file.write_bytes(base64.urlsafe_b64encode(b"x" * 32))
    key_file.chmod(0o600)
    sessions = build_session_factory(database_url)
    redis = Redis.from_url(redis_url, decode_responses=True)
    sender = RedisStreamBroker(url=redis_url, queue_name="ai_employee_tasks", consumer_group_name="ai_employee_workers", consumer_id="0-0")
    kicker = AsyncKicker("ai_employee.workers.execute_task:execute_task", sender, {})
    relay = OutboxRelay(store=SqlAlchemyOutboxStore(sessions), enqueuer=TaskiqTaskEnqueuer(kicker.kiq),
                        event_publisher=TaskEventPublisher(redis_url), clock=lambda: datetime.now(UTC),
                        claim_ttl=timedelta(seconds=30), retry_base=timedelta(seconds=1), retry_max=timedelta(seconds=2))
    children: list[subprocess.Popen[bytes]] = []
    streams: list[BinaryIO] = []
    report: dict[str, object] = {"point": point}
    try:
        await redis.flushdb()
        seed = await _seed(sessions, database_url)
        adapter = M2FakeActionAdapter(redis_url=redis_url, user_id=seed.user_id, provider="google")
        environment = child_environment({**os.environ, "TEST_REDIS_URL": redis_url}, TestDatabaseUrl(database_url))
        environment.update(APP_MASTER_KEY_FILE=str(key_file), PYTHONPATH=str(ROOT / "backend"),
                           METRICS_ENABLED="false", M2_PROCESS_CRASH_POINT=point,
                           TASK_LEASE_SECONDS="5", TASK_STEP_TIMEOUT_SECONDS="4",
                           TASK_TIMEOUT_SECONDS="90",
                           M2_PROCESS_CRASH_DIRECTORY=str(directory))

        def spawn(label: str) -> subprocess.Popen[bytes]:
            """仅启动自己的新 session；固定真实 argv 后追加窄的测试暂停模块。"""
            path = directory / f"{label}.log"
            stream = path.open("xb")
            path.chmod(0o600)
            streams.append(stream)
            child = subprocess.Popen([*service_command("worker"), "tests.integration.faults.m2_crash_worker"],
                                     cwd=ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            children.append(child)
            return child

        with owned_process_reaping():
            try:
                await relay.dispatch(seed.task_id)
                first = spawn("before-kill")
                boundary_file = directory / "boundary.json"
                for _ in range(200):
                    if boundary_file.exists() or first.poll() is not None:
                        break
                    await asyncio.sleep(0.05)
                assert boundary_file.exists(), "real Worker did not reach the selected crash boundary"
                boundary = json.loads(boundary_file.read_text())
                assert boundary["point"] == point and os.getpgid(boundary["pid"]) == first.pid
                assert boundary["task_lease_seconds"] == 5 and boundary["broker_idle_timeout_ms"] == 1000
                before = await _facts(sessions, seed.task_id)
                observation = await adapter.observations(seed.operation_id)
                pending = await redis.xpending_range("ai_employee_tasks", "ai_employee_workers", "-", "+", 10)
                assert len(pending) == 1 and observation.write_calls == 1
                assert before.executions == before.write_attempts == 1 and before.request_started
                assert before.execution_status == ("executing" if point == "after_provider_return" else "succeeded")
                assert before.task_status == ("running" if point == "after_provider_return" else "succeeded")
                assert before.checkpoints > 0

                os.killpg(first.pid, signal.SIGKILL)
                await asyncio.to_thread(stop_owned_process, first)
                assert first.returncode == -signal.SIGKILL
                after_kill = await _facts(sessions, seed.task_id)
                # SIGKILL 前最后一次合法续租可以晚于暂停点快照，因此用进程组完全退出后
                # 的新会话作为租约基准；请求、结果与 checkpoint 的业务事实仍需保持。
                result_fields = tuple(name for name in asdict(before) if name != "lease_expires_at")
                committed_preserved = all(getattr(after_kill, name) == getattr(before, name) for name in result_fields)
                assert committed_preserved
                pending_observed_at = monotonic()
                pending_after_kill = await redis.xpending_range("ai_employee_tasks", "ai_employee_workers", "-", "+", 10)
                assert len(pending_after_kill) == 1 and pending_after_kill[0]["message_id"] == pending[0]["message_id"]
                original_entry = await redis.xrange("ai_employee_tasks", pending[0]["message_id"], pending[0]["message_id"])
                assert len(original_entry) == 1
                # 不运行 Scheduler，不写数据库期限，也不手工 claim pending。仅等待数据库
                # 的真实时钟超过首次认领时已持久化的短租约，然后启动 replacement。
                async with asyncio.timeout(10):
                    while after_kill.lease_expires_at is not None:
                        async with sessions() as session:
                            current = await session.scalar(select(func.clock_timestamp()))
                        assert current is not None
                        if current > after_kill.lease_expires_at:
                            break
                        await asyncio.sleep(0.05)
                # replacement 启动前只允许真实时间流逝；持久租约不能被测试重写，Redis
                # idle 的增量也不能超过独立单调时钟测得的实际观察窗口。
                assert await _facts(sessions, seed.task_id) == after_kill
                still_pending = await redis.xpending_range(
                    "ai_employee_tasks", "ai_employee_workers", "-", "+", 10,
                )
                assert len(still_pending) == 1 and still_pending[0]["message_id"] == pending[0]["message_id"]
                assert still_pending[0]["consumer"] == pending_after_kill[0]["consumer"]
                assert still_pending[0]["times_delivered"] == pending_after_kill[0]["times_delivered"] == 1
                elapsed_ms = (monotonic() - pending_observed_at) * 1000
                assert still_pending[0]["time_since_delivered"] <= pending_after_kill[0]["time_since_delivered"] + elapsed_ms + 500
                assert await redis.xrange("ai_employee_tasks", pending[0]["message_id"], pending[0]["message_id"]) == original_entry
                message_digest = hashlib.sha256(pending[0]["message_id"].encode()).hexdigest()
                acknowledged_file = directory / f"ack-{message_digest}.json"
                environment["M2_PROCESS_CRASH_POINT"] = "observe_only"
                second = spawn("after-restart")
                final = after_kill
                for _ in range(700):
                    assert second.poll() is None, "replacement Worker exited before durable convergence"
                    final = await _facts(sessions, seed.task_id)
                    queued = await redis.xpending("ai_employee_tasks", "ai_employee_workers")
                    if final.task_status == "succeeded" and queued["pending"] == 0 and acknowledged_file.exists():
                        break
                    await relay.dispatch(seed.task_id)
                    await asyncio.sleep(0.05)
                assert final.task_status == final.execution_status == "succeeded"
                assert (await redis.xpending("ai_employee_tasks", "ai_employee_workers"))["pending"] == 0
                observed = await adapter.observations(seed.operation_id)
                assert final.executions == final.write_attempts == observed.write_calls == 1
                assert observed.reconcile_calls == (1 if point == "after_provider_return" else 0)
                assert final.execution_id == after_kill.execution_id
                assert final.request_started_at == after_kill.request_started_at
                acknowledged = json.loads(acknowledged_file.read_text())
                assert acknowledged["message_sha256"] == message_digest
                assert acknowledged["delivery_count"] == 2 and acknowledged["pending_after_ack"] == 0
                report.update(killed_exit=first.returncode, real_worker_pid=boundary["pid"],
                              pending_before_kill=1, pending_after_recovery=0,
                              request_started_preserved=after_kill.request_started,
                              committed_facts_preserved=committed_preserved,
                              after_exit_until_restart_facts_preserved=True,
                              original_message=acknowledged,
                              before=asdict(before), after_kill=asdict(after_kill), after=asdict(final),
                              write_calls=observed.write_calls, reconcile_calls=observed.reconcile_calls,
                              three_real_write_switches_disabled=True)
            finally:
                for child in reversed(children):
                    await asyncio.to_thread(stop_owned_process, child)
        return report
    finally:
        for stream in streams:
            stream.close()
        report["logs"] = [{"name": path.name, "bytes": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                          for path in sorted(directory.glob("*.log"))]
        output = directory / "process-evidence.json"
        output.write_text(json.dumps(report, default=str, sort_keys=True) + "\n")
        output.chmod(0o600)
        await sender.shutdown()
        await redis.aclose()
        await sessions.dispose()
