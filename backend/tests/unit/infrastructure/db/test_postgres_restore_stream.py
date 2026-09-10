"""以确定性进程和 pipe 替身验证 started 屏障、completion trailer 与清理归属。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import UUID

import pytest

ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000601")
APPLICATION_NAME = "ai_employee_restore:00000000-0000-0000-0000-000000000601:1"


def _module() -> ModuleType:
    """模块缺失时以明确业务契约失败，避免 collection error 冒充 RED。"""
    assert (
        importlib.util.find_spec("ai_employee.infrastructure.db.postgres_restore_stream")
        is not None
    ), "restore has no supervised stream executor for the deferred completion guard"
    return importlib.import_module("ai_employee.infrastructure.db.postgres_restore_stream")


class _Writer:
    """保留合成 SQL bytes 和 EOF 事实，模拟唯一 controller pipe write end。"""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.closed_event = asyncio.Event()

    def write(self, value: bytes) -> None:
        assert not self.closed
        self.data.extend(value)

    async def drain(self) -> None:
        """让取消有机会在真实 await 边界到达。"""
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True
        self.closed_event.set()

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self.closed


class _Process:
    """只建模 stream 协议依赖的可见子进程生命周期。"""

    def __init__(self, *, generator: bool, exit_code: int = 0) -> None:
        self.pid = 12002 if generator else 12001
        self.stdin = None if generator else _Writer()
        self.stdout = asyncio.StreamReader() if generator else None
        self.returncode: int | None = None
        self.exit_code = exit_code
        self.terminated = False
        self.waited = False
        if self.stdout is not None:
            self.stdout.feed_data(b"SELECT 1;\n")
            self.stdout.feed_eof()

    async def wait(self) -> int:
        self.waited = True
        if self.stdin is not None:
            await self.stdin.closed_event.wait()
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9


def _consumer_environment() -> dict[str, str]:
    """凭据仅为合成 sentinel；结果和诊断不输出它的字节。"""
    return {
        "PATH": "/usr/bin:/bin",
        "PGHOST": "synthetic-isolated",
        "PGDATABASE": "ai_employee_restore_test",
        "PGUSER": "ai_employee_owner",
        "PGPASSWORD": "synthetic-owner-only",
        "PGAPPNAME": APPLICATION_NAME,
        "DATABASE_URL": "must-not-reach-generator",
        "APP_MASTER_KEY_FILE": "must-not-reach-generator",
        "UNRELATED_SECRET": "must-not-reach-generator",
    }


async def _synthetic_control_live() -> None:
    """孤立process测试的控制边界；真实holder注入与活性失败由下方组合回归独立覆盖。"""


def _bound_stream_holder(monkeypatch: pytest.MonkeyPatch, assert_live: Callable[[], None]):
    """只模拟DB边界，保留真实holder.execute、canonical phase转换与stream回调组合。

    gate排空与数据库authority在独立PostgreSQL回归验证；本fixture仅提供合法前置事实，
    使每个流故障都能到达真实父函数，避免未接入新callback的孤立stream测试伪造GREEN。
    """
    from ai_employee.infrastructure.db import database_maintenance as maintenance
    from tests.unit.infrastructure.db import test_database_restore_parser as fixture

    holder = object.__new__(maintenance.RestoreMaintenanceHolder)
    holder.connection = SimpleNamespace(in_transaction=lambda: False)
    holder.claim = maintenance.RestoreClaim(
        "generic",
        fixture.SOURCE_DIGEST,
        maintenance.RestoreFingerprint("20260809_0018", fixture.EXPECTED_FINGERPRINT),
    )
    current = maintenance.DatabaseRestoreFacts(
        fixture._gate(), fixture._call("gate_established", "empty"), None
    )
    pre = maintenance.RestoreFingerprint("20260809_0018", fixture.PRE_FINGERPRINT)

    def read_facts():
        assert_live()
        return current

    def start_call(facts, reader, evidence):
        nonlocal current
        assert_live()
        current = holder._advance(
            facts,
            maintenance._RestoreCallPhase.RESTORE_BACKEND_STARTING,
            {7: "1", 11: fixture.CALL_STARTED_AT, 14: pre.revision, 15: pre.digest},
        )
        return current

    def register(facts, proof, evidence):
        nonlocal current
        assert_live()
        assert proof.owns_unfed_pipe()
        ready = holder._advance(
            facts,
            maintenance._RestoreCallPhase.RESTORE_BACKEND_READY,
            {12: "4321", 13: fixture.BACKEND_STARTED_AT},
        )
        current = holder._advance(ready, maintenance._RestoreCallPhase.RESTORE_STARTED)
        return current

    monkeypatch.setattr(holder, "read_facts", read_facts)
    monkeypatch.setattr(holder, "admit", read_facts)
    monkeypatch.setattr(holder, "assert_live", assert_live)
    monkeypatch.setattr(holder, "_drain_gate_sessions", assert_live)
    monkeypatch.setattr(holder, "read_only", lambda reader: reader(holder.connection))
    monkeypatch.setattr(holder, "_start_call", start_call)
    monkeypatch.setattr(holder, "_register_and_start", register)
    return holder, pre


@pytest.mark.parametrize(
    "boundary,rollback_fails",
    (
        ("chunk_ready", False),
        ("read_wait", False),
        ("drain_wait", False),
        ("generator_wait", False),
        ("before_trailer", False),
        ("consumer_wait", False),
        ("chunk_ready", True),
    ),
)
def test_restore_holder_stops_stream_when_original_control_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, rollback_fails: bool
) -> None:
    """原holder在供流/阻塞/退出各边界失效必须停止、收齐child且保持首错与未知结果。

    chunk-ready和trailer前反例可完成整个旧流，阻塞反例由测试超时明确暴露无监督。
    control检查必须离开event loop；数据库rollback再次失败也不能抹去首个控制失效。
    """
    from ai_employee.infrastructure.db import database_maintenance as maintenance

    module = _module()
    control_live = True
    bytes_after_loss = 0
    checks_after_loss = 0
    stream_results = []
    projections = []
    failure = ConnectionResetError("synthetic original control lost")
    cleanup_failure = OSError("synthetic rollback failure")

    def lose_control():
        nonlocal control_live
        control_live = False

    def assert_live():
        nonlocal checks_after_loss
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AssertionError("synchronous holder SQL ran on the stream event loop")
        if not control_live:
            checks_after_loss += 1
            raise failure

    class Writer(_Writer):
        """在已接收body后的drain边界注入失效，记录失效后的任何新供流。"""

        def write(self, value: bytes) -> None:
            nonlocal bytes_after_loss
            if not control_live:
                bytes_after_loss += len(value)
            self.last_value = value
            super().write(value)

        async def drain(self) -> None:
            if boundary == "drain_wait" and self.last_value == b"SELECT 1;\n":
                lose_control()
                await asyncio.Future()
            await super().drain()

    class Source:
        """分别模拟已读到body与仍无输出的等待；不输出或落盘SQL内容。"""

        sent = False

        async def read(self, limit: int) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            if boundary in {"chunk_ready", "read_wait"}:
                lose_control()
            if boundary == "read_wait":
                await asyncio.Future()
            return b"SELECT 1;\n"

    class Process(_Process):
        """等待consumer退出时也必须持续检查control；child回收可唤醒所有waiter。"""

        def __init__(self, *, generator: bool) -> None:
            self.pid = 12002 if generator else 12001
            self.stdin = None if generator else Writer()
            self.stdout = Source() if generator else None
            self.returncode = None
            self.terminated = self.waited = False
            self.is_generator = generator
            self.exited = asyncio.Event()

        async def wait(self) -> int:
            self.waited = True
            if self.stdin is not None:
                await self.stdin.closed_event.wait()
            if self.returncode is None:
                if self.is_generator and boundary in {"generator_wait", "before_trailer"}:
                    lose_control()
                if (
                    self.is_generator
                    and boundary == "generator_wait"
                    or not self.is_generator
                    and boundary == "consumer_wait"
                ):
                    lose_control()
                    await self.exited.wait()
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def terminate(self) -> None:
            super().terminate()
            self.exited.set()

        def kill(self) -> None:
            super().kill()
            self.exited.set()

    consumer = Process(generator=False)
    generator = Process(generator=True)

    async def spawn(program, *arguments, **options):
        return consumer if program == "psql" else generator

    async def execute(request, **options):
        result = await asyncio.wait_for(
            module.execute_restore_stream(request, **options, spawn=spawn), timeout=1
        )
        stream_results.append(result)
        return result

    holder, pre = _bound_stream_holder(monkeypatch, assert_live)
    if rollback_fails:

        def rollback():
            raise cleanup_failure

        holder.connection = SimpleNamespace(
            in_transaction=lambda: True, commit=lambda: None, rollback=rollback
        )
    monkeypatch.setattr(maintenance, "execute_restore_stream", execute)
    observed = None
    try:
        holder.execute(
            dump=tmp_path / "synthetic.dump",
            reader=lambda connection: pre,
            verifier=lambda *args: None,
            evidence=SimpleNamespace(
                publish=lambda facts, result: projections.append((facts, result))
            ),
            consumer_environment=_consumer_environment(),
            before_stream=assert_live,
        )
    except OSError as caught:
        observed = caught
    assert not control_live
    assert observed is failure, "control loss must be detected before a blocked stream times out"
    assert checks_after_loss >= 1 and bytes_after_loss == 0
    assert stream_results == [] and consumer.waited and generator.waited
    assert consumer.terminated
    assert consumer.stdin is not None and consumer.stdin.closed
    assert (b"SET complete = true" in consumer.stdin.data) is (boundary == "consumer_wait")
    assert len(projections) == 1 and projections[0][1] == "restore_outcome_unknown"
    assert holder._call(projections[0][0]).phase.value == "restore_started"
    if rollback_fails:
        assert observed.__cause__ is cleanup_failure


def test_restore_stream_has_controller_owned_process_boundary() -> None:
    """冻结的 SQL-byte-zero 协议需要镜像内专用 stream executor，不能使用 shell pipeline。"""
    _module()


@pytest.mark.asyncio
async def test_started_barrier_precedes_all_sql_and_generator_spawn(tmp_path: Path) -> None:
    """未完成 holder CAS+fsync 回调时 consumer 仅连接，SQL 和 generator 都必须为零。"""
    module = _module()
    consumer = _Process(generator=False)
    generator = _Process(generator=True)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    barrier_finished = False

    async def spawn(*arguments: str, **options: object) -> _Process:
        calls.append((arguments, options))
        if arguments[0] == "psql":
            assert options["env"] == _consumer_environment()
            return consumer
        assert barrier_finished
        assert arguments == (
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "--file=-",
            str(tmp_path / "synthetic.dump"),
        )
        assert options["env"] == {"PATH": "/usr/bin:/bin"}
        return generator

    async def establish_started(proof: object) -> None:
        nonlocal barrier_finished
        assert consumer.stdin is not None and consumer.stdin.data == b""
        assert len(calls) == 1
        assert proof.owns_unfed_pipe()  # type: ignore[attr-defined]
        await asyncio.sleep(0)
        assert consumer.stdin.data == b""
        barrier_finished = True

    result = await module.execute_restore_stream(
        module.RestoreStreamRequest(tmp_path / "synthetic.dump", ATTEMPT_ID, 1),
        consumer_environment=_consumer_environment(),
        establish_started=establish_started,
        assert_control_live=_synthetic_control_live,
        spawn=spawn,
    )
    assert calls[0][0] == (
        "psql",
        "--no-psqlrc",
        "--quiet",
        "--set=ON_ERROR_STOP=1",
        "--single-transaction",
        "--file=-",
    )
    assert consumer.stdin is not None and consumer.stdin.closed
    assert b"DEFERRABLE INITIALLY DEFERRED" in consumer.stdin.data
    assert consumer.stdin.data.index(b"SELECT 1;\n") > 0
    assert consumer.stdin.data.endswith(
        b"UPDATE pg_temp.ai_employee_restore_guard SET complete = true;\n"
    )
    assert result.generator_exit == result.consumer_exit == 0
    assert result.trailer_sent and consumer.waited and generator.waited


@pytest.mark.asyncio
async def test_start_barrier_failure_closes_unfed_child_without_generator(tmp_path: Path) -> None:
    """backend 尚未登记或 started 的 fsync 失败只能回收自身 child，不得发送 prelude。"""
    module = _module()
    consumer = _Process(generator=False)
    spawn_count = 0

    async def spawn(*arguments: str, **options: object) -> _Process:
        nonlocal spawn_count
        del arguments, options
        spawn_count += 1
        return consumer

    async def refuse(_proof: object) -> None:
        raise RuntimeError("synthetic-started-fsync-failure")

    with pytest.raises(RuntimeError, match="^synthetic-started-fsync-failure$"):
        await module.execute_restore_stream(
            module.RestoreStreamRequest(tmp_path / "synthetic.dump", ATTEMPT_ID, 1),
            consumer_environment=_consumer_environment(),
            establish_started=refuse,
            assert_control_live=_synthetic_control_live,
            spawn=spawn,
        )
    assert spawn_count == 1
    assert consumer.stdin is not None and consumer.stdin.closed and consumer.stdin.data == b""
    assert consumer.waited and consumer.terminated


@pytest.mark.asyncio
async def test_consumer_exit_stops_a_generator_that_has_not_reached_eof(tmp_path: Path) -> None:
    """consumer 提前退出时不能永远阻塞在 generator stdout；必须监督并回收两个 child。"""
    module = _module()

    class EarlyConsumer(_Process):
        async def wait(self) -> int:
            self.waited = True
            self.returncode = 17
            return 17

    consumer = EarlyConsumer(generator=False)
    generator = _Process(generator=True)
    generator.stdout = asyncio.StreamReader()

    async def spawn(*arguments: str, **options: object) -> _Process:
        del options
        return consumer if arguments[0] == "psql" else generator

    async def started(_proof: object) -> None:
        return None

    result = await asyncio.wait_for(
        module.execute_restore_stream(
            module.RestoreStreamRequest(tmp_path / "synthetic.dump", ATTEMPT_ID, 1),
            consumer_environment=_consumer_environment(),
            establish_started=started,
            assert_control_live=_synthetic_control_live,
            spawn=spawn,
        ),
        timeout=0.2,
    )
    assert result.consumer_exit == 17 and not result.trailer_sent
    assert generator.terminated and generator.waited and consumer.waited


@pytest.mark.asyncio
async def test_generator_failure_never_sends_completion_trailer(tmp_path: Path) -> None:
    """generator 部分 SQL 后非零必须保持 guard 未完成并收回 consumer，不能因 EOF 提交。"""
    module = _module()
    consumer = _Process(generator=False)
    generator = _Process(generator=True, exit_code=3)

    async def spawn(*arguments: str, **options: object) -> _Process:
        del options
        return consumer if arguments[0] == "psql" else generator

    async def started(_proof: object) -> None:
        return None

    result = await module.execute_restore_stream(
        module.RestoreStreamRequest(tmp_path / "synthetic.dump", ATTEMPT_ID, 1),
        consumer_environment=_consumer_environment(),
        establish_started=started,
        assert_control_live=_synthetic_control_live,
        spawn=spawn,
    )
    assert result.generator_exit == 3 and not result.trailer_sent
    assert consumer.stdin is not None and b"SET complete = true" not in consumer.stdin.data
    assert consumer.stdin.closed and consumer.waited and generator.waited


@pytest.mark.asyncio
async def test_cancelled_start_barrier_drains_spawn_and_child(tmp_path: Path) -> None:
    """controller 在可见登记前取消仍须关闭唯一 writer 并收回已创建 child。"""
    module = _module()
    consumer = _Process(generator=False)
    entered = asyncio.Event()

    async def spawn(*arguments: str, **options: object) -> _Process:
        del arguments, options
        return consumer

    async def started(_proof: object) -> None:
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(
        module.execute_restore_stream(
            module.RestoreStreamRequest(tmp_path / "synthetic.dump", ATTEMPT_ID, 1),
            consumer_environment=_consumer_environment(),
            establish_started=started,
            assert_control_live=_synthetic_control_live,
            spawn=spawn,
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert consumer.stdin is not None and consumer.stdin.closed and consumer.stdin.data == b""
    assert consumer.waited and consumer.terminated
