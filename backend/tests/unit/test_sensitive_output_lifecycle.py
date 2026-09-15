"""真实进程验证敏感输出只能在所有自有生产者退出后冻结，包含退出末尾写入。"""

import importlib
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("browser_code", [0, 7])
def test_scanning_boundary_includes_final_output_from_every_owned_producer(tmp_path: Path, browser_code: int) -> None:
    """浏览器先退后，两个自有服务收到 SIGTERM 才写最后一行；成功/失败都必须等到它们退出。"""
    name = "tests.integration.sensitive_output_runner"
    assert importlib.util.find_spec(name) is not None, "raw output producer supervisor is absent"
    module = importlib.import_module(name)
    program = (
        "import signal,time\n"
        "stopping=False\n"
        "def stop(*args):\n global stopping\n stopping=True\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "print('ready',flush=True)\n"
        "while not stopping: time.sleep(.01)\n"
        "print('final-owned-output',flush=True)\n"
    )
    commands = {role: (sys.executable, "-u", "-c", program) for role in ("backend", "web")}

    def ready(role: str, process: subprocess.Popen[bytes]) -> None:
        """只等精确进程产生的 ready 字节；不以固定 sleep 代替启动或退出事实。"""
        path = tmp_path / ("services.stdout.log" if role == "backend" else "web.stdout.log")
        deadline = time.monotonic() + 5
        while b"ready\n" not in path.read_bytes():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.01)

    result = module.run_observed_processes(
        root=tmp_path, raw=tmp_path,
        browser_command=(sys.executable, "-c", f"raise SystemExit({browser_code})"),
        service_commands=commands, wait_ready=ready,
    )
    assert result.code == browser_code
    assert {producer.role for producer in result.producers} == {"backend", "web", "playwright"}
    assert all(producer.group_exited for producer in result.producers)
    for filename in ("services.stdout.log", "web.stdout.log"):
        assert (tmp_path / filename).read_bytes() == b"ready\nfinal-owned-output\n"
