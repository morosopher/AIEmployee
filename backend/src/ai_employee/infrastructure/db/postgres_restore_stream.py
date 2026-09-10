"""监督单次已获准 restore ordinal 的匿名 pipe 与两个 PostgreSQL 子进程。

本模块不持有 catalog authority，也不决定重试。holder 必须在 ``establish_started`` 回调
中登记 exact backend、CAS started 并 fsync projection；回调正常完成前绝不发送 SQL 或
启动 generator。owner 凭据仅传给固定 psql consumer，SQL 只在有界内存和匿名 pipe 流动。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from ai_employee.infrastructure.calendar_aad_resources import await_calendar_aad_resource

_CHUNK_BYTES = 64 * 1024
_SHUTDOWN_SECONDS = 5
_CONTROL_POLL_SECONDS = 0.05
_GENERATOR_ENVIRONMENT_KEYS = frozenset({"PATH", "LANG", "LC_ALL", "TZ"})

# --single-transaction 只有配合 -f/-c 才适用；显式 --file=- 让 stdin 也属于该事务。
# guard 位于 pg_temp，因此 pg_restore 对 public 的 clean/drop 无法把它提前移除。
_PRELUDE = b"""CREATE TEMP TABLE ai_employee_restore_guard (
    complete boolean NOT NULL
) ON COMMIT DROP;
CREATE FUNCTION pg_temp.ai_employee_restore_check() RETURNS trigger
LANGUAGE plpgsql AS $ai_employee_restore_guard$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_temp.ai_employee_restore_guard WHERE complete) THEN
        RAISE EXCEPTION 'restore stream completion guard rejected';
    END IF;
    RETURN NULL;
END;
$ai_employee_restore_guard$;
CREATE CONSTRAINT TRIGGER ai_employee_restore_complete
AFTER INSERT ON pg_temp.ai_employee_restore_guard
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION pg_temp.ai_employee_restore_check();
INSERT INTO pg_temp.ai_employee_restore_guard VALUES (false);
"""
_TRAILER = b"UPDATE pg_temp.ai_employee_restore_guard SET complete = true;\n"


class RestoreStreamInvariantError(RuntimeError):
    """表示 stream 输入或 child/pipe ownership 不符合固定协议；诊断无 Secret/SQL。"""


@dataclass(frozen=True, slots=True)
class RestoreStreamRequest:
    """绑定已验证私有 dump 路径、attempt 与从未复用的非零 call ordinal。"""

    dump: Path
    attempt_id: UUID
    call_ordinal: int

    def __post_init__(self) -> None:
        """在 spawn 前拒绝未知字段类型、非法序号和非绝对路径。"""
        if (
            not isinstance(self.dump, Path)
            or not self.dump.is_absolute()
            or type(self.attempt_id) is not UUID
            or type(self.call_ordinal) is not int
            or not 1 <= self.call_ordinal <= 999999
        ):
            raise RestoreStreamInvariantError("restore stream input is invalid")

    @property
    def application_name(self) -> str:
        """由冻结 canonical 身份生成不超过 PostgreSQL 63-byte 的应用名。"""
        return f"ai_employee_restore:{self.attempt_id}:{self.call_ordinal}"


@dataclass(frozen=True, slots=True)
class RestoreStreamResult:
    """只返回两个退出码和无内容流证据，结果核对始终由 holder 负责。"""

    generator_exit: int | None
    consumer_exit: int
    sql_bytes_forwarded: int
    trailer_sent: bool


class _Spawner(Protocol):
    """对 asyncio subprocess 的唯一外部适配 seam，用于确定性 process/pipe 测试。"""

    async def __call__(
        self,
        program: str,
        /,
        *arguments: str,
        stdin: int | None,
        stdout: int | None,
        stderr: int | None,
        env: Mapping[str, str],
        close_fds: bool,
    ) -> asyncio.subprocess.Process:
        """创建本 controller 独占 stdin writer 的 child；不启动 shell。"""


class RestoreConsumerProof:
    """仅限本机活 controller 的 child/pipe receipt，永不序列化成恢复 authority。

    holder 仍须在 pg_stat_activity 按 database/role/application 匹配并登记真实 backend。
    这个 receipt 只证明当前调用仍拥有未喂 SQL 的原始 child/stdin；另一 host 或新调用
    无法从 projection 制造它。pid 为操作系统 child ID，不冒充 PostgreSQL backend PID。
    """

    def __init__(self, process: asyncio.subprocess.Process, application_name: str) -> None:
        self._process = process
        self._writer = process.stdin
        self._controller_pid = os.getpid()
        self._sql_bytes = 0
        self.application_name = application_name
        self.child_pid = process.pid

    def owns_unfed_pipe(self) -> bool:
        """只在原进程、原 child/pipe 仍存活且 SQL-byte-zero 时返回真。"""
        return (
            os.getpid() == self._controller_pid
            and self._writer is not None
            and self._process.stdin is self._writer
            and self._process.returncode is None
            and not self._writer.is_closing()
            and self._sql_bytes == 0
        )

    async def _write(
        self,
        value: bytes,
        assert_control_live: Callable[[], Awaitable[None]],
        consumer_exit: asyncio.Task[int] | None = None,
    ) -> None:
        """每笔供流前重证原control，drain阻塞时持续监督；先计数以保留失败byte证据。"""
        await assert_control_live()
        if self._writer is None or self._writer.is_closing():
            raise BrokenPipeError("restore consumer pipe is closed")
        self._sql_bytes += len(value)
        self._writer.write(value)
        await _await_with_control(self._writer.drain(), assert_control_live, consumer_exit)


async def _stop_child(process: asyncio.subprocess.Process) -> None:
    """关闭自身 writer，发送 TERM 并在固定宽限后 KILL；每条分支都收取退出状态。"""
    if process.stdin is not None:
        process.stdin.close()
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_SECONDS)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _cleanup_children(
    children: list[asyncio.Task[asyncio.subprocess.Process]],
) -> None:
    """先接回每个 spawn 的确定结果，再逐一关闭 child；早期失败不跳过另一个。"""
    failure: BaseException | None = None
    for creation in children:
        try:
            process = await creation
            await _stop_child(process)
        except BaseException as error:  # noqa: BLE001 - 收齐所有 child 后原样传播首错。
            if failure is None:
                failure = error
    if failure is not None:
        raise failure


async def _await_with_control[T](
    operation: Awaitable[T],
    assert_control_live: Callable[[], Awaitable[None]],
    consumer_exit: asyncio.Task[int] | None = None,
) -> T:
    """在每个有界等待及成功返回边界重证control，失效后取消并收齐本次waiter。

    operation只承载本调用拥有的异步等待；spawn传入shield以保留已开始child的归属。
    同步holder检查由调用方串行移交线程，不在event loop执行，也不与登记SQL并发。
    已失败的operation保留首错；成功结果必须通过本次活性检查才可进入下一写阶段。
    """
    pending = asyncio.ensure_future(operation)
    watched = (pending,) if consumer_exit is None else (pending, consumer_exit)
    failure: BaseException | None = None
    try:
        while True:
            done, _ = await asyncio.wait(
                watched, timeout=_CONTROL_POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
            if pending in done and (pending.cancelled() or pending.exception() is not None):
                return pending.result()
            await assert_control_live()
            if consumer_exit is not None and consumer_exit in done:
                raise BrokenPipeError("restore consumer exited before stream completion")
            if pending in done:
                return pending.result()
    except BaseException as error:
        failure = error
        raise
    finally:
        if not pending.done():
            pending.cancel()

        async def collect() -> None:
            await asyncio.gather(pending, return_exceptions=True)

        try:
            await await_calendar_aad_resource(asyncio.create_task(collect()))
        except BaseException as cleanup_failure:
            if failure is not None:
                raise failure from cleanup_failure
            raise


async def execute_restore_stream(
    request: RestoreStreamRequest,
    *,
    consumer_environment: Mapping[str, str],
    establish_started: Callable[[RestoreConsumerProof], Awaitable[None]],
    assert_control_live: Callable[[], Awaitable[None]],
    spawn: _Spawner = asyncio.create_subprocess_exec,
) -> RestoreStreamResult:
    """在 started 屏障后执行有完成 guard 的 pg_restore→psql 流。

    Args:
        request: 已批准单次 ordinal 的无 Secret 输入。
        consumer_environment: 只给 psql 的固定 target/owner 凭据环境；不会复制给 generator。
        establish_started: 同一 owner holder 登记 backend、CAS started 和 fsync 的回调。
        assert_control_live: 绑定原物理holder和全部锁的串行异步检查；不得静默重连。
        spawn: 默认为真实 subprocess；测试仅替换最外层进程启动。

    Returns:
        两 child 的确切状态；即使返回双零也必须重新证明 backend exit 和 exact post。

    Raises:
        RestoreStreamInvariantError: 输入或本机 pipe receipt 失效，零 generator。
        BaseException: 原始 holder/取消错误在全部已拥有 child 回收后传播。
    """
    if (
        consumer_environment.get("PGUSER") != "ai_employee_owner"
        or consumer_environment.get("PGAPPNAME") != request.application_name
        or not consumer_environment.get("PGDATABASE")
        or not consumer_environment.get("PGHOST")
    ):
        raise RestoreStreamInvariantError("restore consumer target is invalid")
    generator_environment = {
        key: value
        for key, value in consumer_environment.items()
        if key in _GENERATOR_ENVIRONMENT_KEYS
    }
    children: list[asyncio.Task[asyncio.subprocess.Process]] = []
    first_failure: BaseException | None = None
    control_failure: BaseException | None = None
    result: RestoreStreamResult | None = None

    async def check_control() -> None:
        """区分control失效与child pipe错误；相同异常类型也不能被降格为可核对流结果。"""
        nonlocal control_failure
        try:
            await assert_control_live()
        except BaseException as error:
            control_failure = error
            raise

    try:
        await check_control()
        creation = asyncio.create_task(
            spawn(
                "psql",
                "--no-psqlrc",
                "--quiet",
                "--set=ON_ERROR_STOP=1",
                "--single-transaction",
                "--file=-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=dict(consumer_environment),
                close_fds=True,
            )
        )
        children.append(creation)
        consumer = await _await_with_control(asyncio.shield(creation), check_control)
        proof = RestoreConsumerProof(consumer, request.application_name)
        if not proof.owns_unfed_pipe():
            raise RestoreStreamInvariantError("restore consumer pipe ownership is invalid")
        await _await_with_control(establish_started(proof), check_control)
        if not proof.owns_unfed_pipe():
            raise RestoreStreamInvariantError("restore consumer start fence is invalid")
        await proof._write(_PRELUDE, check_control)
        creation = asyncio.create_task(
            spawn(
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--file=-",
                str(request.dump),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=generator_environment,
                close_fds=True,
            )
        )
        children.append(creation)
        generator = await _await_with_control(asyncio.shield(creation), check_control)
        if generator.stdout is None:
            raise RestoreStreamInvariantError("restore generator stdout is unavailable")
        trailer_sent = False
        consumer_exit_task = asyncio.create_task(consumer.wait())
        generator_exit: int | None = None
        consumer_exit: int | None = None
        try:
            while chunk := await _await_with_control(
                generator.stdout.read(_CHUNK_BYTES), check_control, consumer_exit_task
            ):
                await proof._write(chunk, check_control, consumer_exit_task)
            generator_exit = await _await_with_control(
                generator.wait(), check_control, consumer_exit_task
            )
            if generator_exit == 0:
                # EOF和exit=0不代表control仍持锁；trailer是允许提交的独立写边界。
                await proof._write(_TRAILER, check_control, consumer_exit_task)
                trailer_sent = True
            if consumer.stdin is not None:
                consumer.stdin.close()
            consumer_exit = await _await_with_control(consumer_exit_task, check_control)
        except (BrokenPipeError, ConnectionResetError):
            if control_failure is not None:
                raise
            await _stop_child(generator)
            await _stop_child(consumer)
            generator_exit = generator.returncode
            consumer_exit = consumer.returncode
            if consumer_exit is None:
                raise RestoreStreamInvariantError("restore consumer exit is unknown") from None
        finally:
            # waiter 不是 child owner；取消它不取消进程，child 统一由外层清理后收取状态。
            if not consumer_exit_task.done():
                consumer_exit_task.cancel()
            await asyncio.gather(consumer_exit_task, return_exceptions=True)
        if consumer_exit is None:
            raise RestoreStreamInvariantError("restore consumer exit is unknown")
        result = RestoreStreamResult(
            generator_exit,
            consumer_exit,
            proof._sql_bytes,
            trailer_sent,
        )
    except BaseException as error:  # noqa: BLE001 - finally 完整清理后仍传播原始失败。
        first_failure = error
    finally:
        # spawn 和 pipe cleanup 属于本调用已开始的资源义务；后续 cancel 不能跳过任何
        # child，也不能将第一项失败改写成成功。cleanup 本身从不重新启动 generator。
        cleanup = asyncio.create_task(_cleanup_children(children))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as cancellation:
                if first_failure is None:
                    first_failure = cancellation
            except BaseException as error:  # noqa: BLE001 - 只延迟传播以收齐资源。
                if first_failure is None:
                    first_failure = error
        try:
            cleanup.result()
        except BaseException as error:  # noqa: BLE001 - 首错优先，绝不伪造成功。
            if first_failure is None:
                first_failure = error
    if first_failure is not None:
        raise first_failure
    if result is None:
        raise RestoreStreamInvariantError("restore stream result is unavailable")
    return result
