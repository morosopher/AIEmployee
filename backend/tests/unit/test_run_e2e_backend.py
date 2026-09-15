"""验证 E2E 只在受管临时库启动子进程，并保留固定 anchor 环境与退出语义。"""

import ctypes
import importlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as DatabaseUrl


def _module():
    """缺少新编排入口时给出明确行为 RED，避免收集阶段 ImportError 掩盖目标。"""
    path = Path(__file__).parents[1] / "integration" / "e2e_backend.py"
    if not path.exists():
        pytest.fail("E2E managed lifecycle entry is missing", pytrace=False)
    return importlib.import_module("tests.integration.e2e_backend")


def test_e2e_children_use_one_disposable_database_and_keep_parent_anchor() -> None:
    """业务 ORM 与 checkpoint 共用临时目标，外层 TEST_DATABASE_URL 保持原值。"""
    module = _module()
    original = {"TEST_DATABASE_URL": "synthetic-anchor", "TEST_REDIS_URL": "redis://127.0.0.1:56379/15"}
    value = DatabaseUrl("postgresql+asyncpg://owner:synthetic@127.0.0.1:55443/ai_employee_suite_test")
    actual = module.child_environment(original, value)
    assert original == {"TEST_DATABASE_URL": "synthetic-anchor", "TEST_REDIS_URL": "redis://127.0.0.1:56379/15"}
    assert actual["TEST_DATABASE_URL"] == original["TEST_DATABASE_URL"]
    assert actual["DATABASE_URL"] == value
    assert actual["CHECKPOINT_DATABASE_URL"] == value.replace("postgresql+asyncpg://", "postgresql://")
    assert actual["APP_ENV"] == "test" and actual["APP_TEST_MODE"] == "true"
    assert all(actual[key] == "false" for key in ("EXTERNAL_WRITES_ENABLED", "GOOGLE_WRITES_ENABLED", "MICROSOFT_WRITES_ENABLED"))
    assert actual["WRITE_TEST_ACCOUNT_ALLOWLIST"] == "[]"


@pytest.mark.parametrize("role", ["api", "worker"])
def test_e2e_processes_use_actual_no_access_log_and_single_worker_commands(role: str) -> None:
    """真正执行的 argv 自带安全日志/单 Worker 参数，不能由注释字符串代替。"""
    module = _module()
    command = module.service_command(role)
    assert command[:2] == [sys.executable, "-m"]
    if role == "api":
        assert command[2:] == ["uvicorn", "ai_employee.main:app", "--host", "127.0.0.1", "--port", "8000", "--no-access-log"]
    else:
        assert command[2:] == ["taskiq", "worker", "--workers", "1", "--ack-type", "when_executed", "ai_employee.infrastructure.queue.broker:broker", "ai_employee.workers.execute_task", "ai_employee.workers.schedules"]


def test_e2e_worker_registers_trusted_action_consumer_in_clean_process() -> None:
    """真实解析 E2E argv 的模块列表；空 broker 不能冒充能消费队列的 Worker。"""
    command = _module().service_command("worker")
    broker_path = "ai_employee.infrastructure.queue.broker:broker"
    modules = command[command.index(broker_path) + 1:]
    result = subprocess.run(
        [sys.executable, "-c", """
import json
import sys
from taskiq.cli.utils import import_object, import_tasks

broker = import_object(sys.argv[1])
broker.is_worker_process = True
import_tasks(sys.argv[2:], ["**/tasks.py"], False)
print(json.dumps(sorted(broker.get_all_tasks())))
""", broker_path, *modules],
        capture_output=True, text=True, check=True, timeout=15,
    )
    assert "ai_employee.workers.execute_task:execute_task" in json.loads(result.stdout)
    assert "ai_employee.workers.schedules:relay_outbox" in json.loads(result.stdout)


def test_sensitive_observation_uses_real_app_wrapper_without_changing_normal_argv() -> None:
    """仅显式敏感输出测试选择 exporter wrapper，原服务日志和 Worker argv 保持真实。"""
    module = _module()
    ordinary = module.service_command("api")
    observed = module.service_command("api", observe=True)
    assert observed[:3] == ordinary[:3]
    assert observed[3] == "tests.integration.sensitive_output_app:app"
    assert "--no-access-log" in observed and "--app-dir" in observed
    assert module.service_command("worker", observe=True) == module.service_command("worker")


def test_e2e_child_failure_is_returned_only_after_both_owned_processes_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """API 非零退出仍回收 Worker；仅关闭其亲自创建的进程，不扫描或杀死共享服务。"""
    module = _module()
    events: list[str] = []

    class Process:
        """记录有界进程清理顺序的端口 Fake。"""

        def __init__(self, name: str) -> None:
            self.name = name
            self.pid = 987654 if name == "worker" else 987655
            self.returncode = None if name == "worker" else 7

        def poll(self):
            return self.returncode

        def wait(self, *, timeout: float):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("synthetic child", timeout)
            return self.returncode

    created: list[Process] = []

    def spawn(argv, **kwargs):
        assert kwargs["start_new_session"] is True
        child = Process("worker" if "taskiq" in argv else "api")
        created.append(child)
        return child

    def stop(child):
        events.append(f"stop:{child.name}")
        child.returncode = child.returncode or 0

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module, "stop_owned_process", stop)
    code = module.run_services(Path.cwd(), {}, lambda: events.append("verify"), lambda: False)
    assert code == 7
    assert events[-2:] == ["stop:api", "stop:worker"]
    assert all(child.returncode is not None for child in created)


def test_e2e_cleanup_attempts_each_owned_child_after_one_stop_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个退出证明失败也必须尝试回收另一个孩子，随后向数据库边界传播失败。"""
    module = _module()
    events: list[str] = []

    class Process:
        """只记录有明确所有权的服务，用固定退出值进入收尾流程。"""

        def __init__(self, name: str) -> None:
            self.name = name

        def poll(self) -> int:
            return 7

    def spawn(argv, **_kwargs):
        return Process("worker" if "taskiq" in argv else "api")

    def stop(child):
        events.append(child.name)
        if child.name == "api":
            raise TimeoutError("synthetic owned-child exit proof unavailable")

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module, "stop_owned_process", stop)
    with pytest.raises(ExceptionGroup, match="E2E owned-process cleanup failed"):
        module.run_services(Path.cwd(), {}, lambda: None, lambda: False)
    assert events == ["api", "worker"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux 自有孤儿进程回收交错")
def test_e2e_stops_owned_group_after_direct_parent_has_already_exited() -> None:
    """真实创建一个自有组，父进程先退后仍回收孙进程；不接触共享服务。

    测试临时成为 subreaper 以回收自己制造的孤儿，避免容器 PID 1 的收尸行为
    影响断言。退出证据包括组消失，而非只看已退出父进程的 returncode。
    """
    module = _module()
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    parent = subprocess.Popen(
        [sys.executable, "-c",
         ("import subprocess,sys; "
         "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'],"
         "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
         "print(child.pid,flush=True)")],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
    )
    child_pid: int | None = None
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline())
        assert parent.wait(timeout=5) == 0
        os.killpg(parent.pid, 0)
        module.stop_owned_process(parent)
        with pytest.raises(ProcessLookupError):
            os.killpg(parent.pid, 0)
    finally:
        try:
            os.killpg(parent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        parent.wait(timeout=5)
        if child_pid is not None:
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass
        if parent.stdout is not None:
            parent.stdout.close()
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0
