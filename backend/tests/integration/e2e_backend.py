"""在既有 suite/provenance 生命周期内运行 E2E API 与 Worker。

输入库只作为 roles-absent anchor。业务进程使用独立 UUID 临时库；正常结束、浏览器
失败或 SIGTERM 都先回收亲自创建的进程，再由原 helper 清理并复核完整 anchor。
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl,
    ValidatedTestDatabaseUrl,
    validate_test_database_url,
)
from ai_employee.infrastructure.queue.redis_url import validate_test_redis_url
from tests.integration.disposable_database import IntegrationSuiteLockLease
from tests.integration.integration_suite import (
    IntegrationSuiteInvariantError,
    SqlAlchemyIntegrationSuiteResources,
    managed_cycle5_regular_database,
)


class _E2EResources(SqlAlchemyIntegrationSuiteResources):
    """只暴露当前 suite lease 验证，所有创建/清理/anchor 规则继承原实现。"""

    _lease: IntegrationSuiteLockLease | None = None

    @contextmanager
    def acquire_suite_lock(self, anchor: ValidatedTestDatabaseUrl) -> Iterator[IntegrationSuiteLockLease]:
        """复用同一 management holder；浏览器运行中持续验证其连续性。"""
        with super().acquire_suite_lock(anchor) as lease:
            self._lease = lease
            try:
                yield lease
            finally:
                self._lease = None

    def verify(self) -> None:
        """holder 缺失或连接丢失时 fail closed，不能继续运行依赖它的服务。"""
        if self._lease is None:
            raise IntegrationSuiteInvariantError("E2E suite lease is absent")
        self._lease.verify()


def child_environment(base: Mapping[str, str], database_url: TestDatabaseUrl) -> dict[str, str]:
    """建立业务与 checkpoint 同库环境，保留外层固定 anchor，真实写开关全部关闭。

    Args:
        base: 调用者配置；不会修改，Secret 不进入日志或 argv。
        database_url: 原 provenance helper 刚创建并迁移的临时目标。

    Returns:
        双测试开关开启且全部真实写开关关闭的独立环境副本。
    """
    environment = dict(base)
    environment.update(
        DATABASE_URL=database_url,
        CHECKPOINT_DATABASE_URL=database_url.replace("postgresql+asyncpg://", "postgresql://", 1),
        REDIS_URL=base["TEST_REDIS_URL"], APP_ENV="test", APP_TEST_MODE="true",
        EXTERNAL_WRITES_ENABLED="false", GOOGLE_WRITES_ENABLED="false",
        MICROSOFT_WRITES_ENABLED="false", WRITE_TEST_ACCOUNT_ALLOWLIST="[]",
    )
    return environment


def service_command(role: str, *, observe: bool = False) -> list[str]:
    """返回真实固定 argv；观测测试只替换导出真实 main.app 的测试侧 wrapper。

    普通入口保持原字节参数；显式 observe 由独立 scanner 配置传入，wrapper 还会校验
    双测试开关、私有输入权限与真实写关闭状态。两种入口都禁用 access log。
    """
    if role == "api":
        command = [sys.executable, "-m", "uvicorn", "ai_employee.main:app", "--host", "127.0.0.1",
                   "--port", "8000", "--no-access-log"]
        if observe:
            command[3] = "tests.integration.sensitive_output_app:app"
            command.extend(("--app-dir", str(Path(__file__).resolve().parents[2])))
        return command
    if role == "worker":
        # Taskiq 的 broker import 不会自动登记生产任务；必须与 just/Compose 一样显式
        # 载入执行和调度模块，否则进程存活却会丢弃未知任务，无法验证真实队列消费。
        return [sys.executable, "-m", "taskiq", "worker", "--workers", "1", "--ack-type",
                "when_executed", "ai_employee.infrastructure.queue.broker:broker",
                "ai_employee.workers.execute_task", "ai_employee.workers.schedules"]
    raise ValueError("E2E service role is invalid")


@contextmanager
def owned_process_reaping() -> Iterator[None]:
    """在 Linux 临时接收自有孤儿后代，避免容器 PID 1 不收尸使组退出无法证明。

    只改变当前测试编排进程；恢复原标志前必须已停止并回收全部亲自创建的组。
    其他 POSIX 平台由系统 init 回收孤儿，仍使用同一有界组消失证明。
    """
    if sys.platform != "linux":
        yield
        return
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError("E2E child ownership query failed")
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError("E2E child ownership setup failed")
    try:
        yield
    finally:
        if libc.prctl(36, previous.value, 0, 0, 0) != 0:
            raise OSError("E2E child ownership restoration failed")


def _wait_owned_group(process: subprocess.Popen[bytes], timeout: float) -> bool:
    """同时等待直接孩子与同组后代消失；仅 reap 指定负 PGID，不遍历其他进程。"""
    deadline = time.monotonic() + timeout
    while True:
        # Popen 必须先认领自己的退出值，随后才能收回已被 subreaper 接收的同组孤儿。
        direct_exited = process.poll() is not None
        if direct_exited:
            while True:
                try:
                    pid, _ = os.waitpid(-process.pid, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return direct_exited
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def stop_owned_process(process: subprocess.Popen[bytes]) -> None:
    """有界回收本函数创建的独立进程组，避免 Taskiq 孙进程仍持有数据库会话。

    ``start_new_session=True`` 保证组 ID 为直接子进程 ID；不扫描系统进程或按名称 kill。
    直接父进程已退出时仍向该自有组发信号；退出证明同时要求 Popen 退出和组消失，
    Taskiq 孙进程不能因主进程先退而继续占用数据库会话。
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if _wait_owned_group(process, 8):
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if not _wait_owned_group(process, 5):
        raise TimeoutError("E2E owned process group did not exit")


def run_services(
    root: Path,
    environment: Mapping[str, str],
    verify: Callable[[], None],
    stopping: Callable[[], bool],
) -> int:
    """启动真实服务并观察退出；关闭全部已创建子进程后才释放数据库上下文。"""
    children: list[subprocess.Popen[bytes]] = []
    try:
        for role in ("worker", "api"):
            verify()
            if stopping():
                return 0
            command = service_command(role, observe=bool(environment.get("M2_SENSITIVE_OUTPUT_DIRECTORY")))
            children.append(subprocess.Popen(command, cwd=root,
                                             env=dict(environment), start_new_session=True))
        worker, api = children
        while not stopping():
            verify()
            for process in (api, worker):
                code = process.poll()
                if code is not None:
                    return code if code != 0 else 1
            try:
                api.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
        return 0
    finally:
        failures: list[Exception] = []
        for process in reversed(children):
            try:
                stop_owned_process(process)
            except Exception as error:  # noqa: BLE001 - 收尾须继续尝试每个自有子进程。
                failures.append(error)
        if failures:
            raise ExceptionGroup("E2E owned-process cleanup failed", failures)


def main() -> int:
    """验证本地输入，由唯一既有管理器拥有测试库，再启动 API/Worker。

    错误输出固定且脱敏；未知 DBAPI 不离开工具边界。Playwright 的 SIGTERM
    gracefulShutdown 必须给 finally 留出完成角色/会话/lease/anchor 证明的时间。
    """
    root = Path(__file__).resolve().parents[3]
    stop_requested = False

    def stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous = {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        anchor = validate_test_database_url(os.environ["TEST_DATABASE_URL"])
        validate_test_redis_url(os.environ["TEST_REDIS_URL"])
        for name in ("E2E_ADMIN_EMAIL", "E2E_ADMIN_PASSWORD_FILE", "APP_MASTER_KEY_FILE"):
            if not os.environ.get(name):
                raise ValueError("E2E required setting is absent")
        resources = _E2EResources(root)
        with owned_process_reaping(), managed_cycle5_regular_database(anchor=anchor, resources=resources) as temporary:
            environment = child_environment(os.environ, temporary)
            resources.verify()
            if stop_requested:
                return 0
            subprocess.run([sys.executable, "-m", "ai_employee.cli.create_admin", "--email",
                            environment["E2E_ADMIN_EMAIL"], "--password-file",
                            environment["E2E_ADMIN_PASSWORD_FILE"], "--if-absent"],
                           cwd=root, env=environment, check=True, timeout=30)
            result = run_services(root, environment, resources.verify, lambda: stop_requested)
        print("E2E owned services stopped; disposable database cleaned; anchor unchanged")
        return result
    except Exception:  # noqa: BLE001 - 工具最外层禁止输出 DSN、Secret 或 catalog 内容。
        print("E2E managed backend failed; inspect content-free test evidence", file=sys.stderr)
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
