"""拥有敏感输出的全部生产者，并在退出/flush/数据库清理完成后才返回扫描器。

Playwright 的 shell webServer 可能先退出而后台管理器仍在收尾，因此本测试侧模块
亲自拥有 backend manager、Vite 和 Playwright 三个组，复用 E2E 的 subreaper/组退出
证明。数据库仍完全委托原 E2E 生命周期；此处不创建、迁移或清理第二套数据库。
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FrameType
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from tests.integration.e2e_backend import owned_process_reaping, stop_owned_process


@dataclass(frozen=True, slots=True)
class ProducerExit:
    """来自自有 Popen 与组消失证明的退出事实，不包含环境或请求内容。"""

    role: str
    pid: int
    returncode: int
    group_exited: bool


@dataclass(frozen=True, slots=True)
class ObservedResult:
    """浏览器原始退出码及所有输出生产者的最终退出事实。"""

    code: int
    producers: tuple[ProducerExit, ...]


def _wait_ready(role: str, process: subprocess.Popen[bytes]) -> None:
    """仅探测固定 loopback 服务；每次轮询都检查自有进程仍存活且总时间有界。"""
    url = "http://127.0.0.1:8000/api/v1/system/health" if role == "backend" else "http://127.0.0.1:5173"
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 90
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=.5) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError):
            pass
        time.sleep(.05)
    raise RuntimeError("owned observable service did not become ready")


def run_observed_processes(
    *,
    root: Path,
    raw: Path,
    browser_command: Sequence[str],
    service_commands: Mapping[str, Sequence[str]] | None = None,
    wait_ready: Callable[[str, subprocess.Popen[bytes]], None] = _wait_ready,
) -> ObservedResult:
    """在同一所有权边界运行并关闭所有输出生产者，返回前已关闭日志文件句柄。

    Args:
        root: 仓库根；argv 与 cwd 不含 Secret。
        raw: 当前 scanner 私有输出目录。
        browser_command: 独立配置的真实 Playwright argv。
        service_commands: 仅单元测试替换进程端口；默认执行原 E2E backend 与真实 Vite。
        wait_ready: 可替换的就绪端口；退出证明始终使用真实 Popen/进程组。

    Returns:
        浏览器原始状态；即使浏览器失败也在全部服务退出后返回。

    Raises:
        ExceptionGroup: 任一自有组退出或 backend 数据库清理无法证明。
    """
    commands = service_commands or {
        "backend": (sys.executable, str(root / "scripts/run_e2e_backend.py")),
        "web": ("pnpm", "--dir", "frontend", "dev", "--host", "127.0.0.1", "--strictPort"),
    }
    children: list[tuple[str, subprocess.Popen[bytes]]] = []
    exits: list[ProducerExit] = []
    code = 1
    with owned_process_reaping(), ExitStack() as files:
        try:
            for role in ("backend", "web", "playwright"):
                stem = "services" if role == "backend" else role
                stdout = files.enter_context((raw / f"{stem}.stdout.log").open("xb"))
                stderr = files.enter_context((raw / f"{stem}.stderr.log").open("xb"))
                command = browser_command if role == "playwright" else commands[role]
                process = subprocess.Popen(list(command), cwd=root, stdout=stdout, stderr=stderr, start_new_session=True)
                children.append((role, process))
                if role != "playwright":
                    wait_ready(role, process)
            browser = children[-1][1]
            while True:
                if any(process.poll() is not None for role, process in children if role != "playwright"):
                    raise RuntimeError("owned observable service exited during browser flow")
                try:
                    code = browser.wait(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            failures: list[Exception] = []
            for role, process in reversed(children):
                try:
                    if role == "backend" and process.poll() is None:
                        # backend manager 必须完成自己的 API/Worker 组退出与 provenance
                        # 清理；先向该直接孩子请求正常结束，再证明整个自有组已消失。
                        process.send_signal(signal.SIGTERM)
                        try:
                            process.wait(timeout=55)
                        except subprocess.TimeoutExpired:
                            failures.append(TimeoutError("observable backend cleanup deadline exceeded"))
                    stop_owned_process(process)
                    status = process.poll()
                    if status is None or (role == "backend" and status != 0):
                        raise RuntimeError("observable backend cleanup did not complete")
                    exits.append(ProducerExit(role, process.pid, status, True))
                except Exception as error:  # noqa: BLE001 - 尝试回收每个自有孩子后统一失败。
                    failures.append(error)
            if failures:
                raise ExceptionGroup("observable output producer cleanup failed", failures)
    return ObservedResult(code, tuple(exits))


def main() -> int:
    """拒绝复用未知监听者，运行自有进程，所有输出关闭后记录退出证明。"""
    root = Path(__file__).resolve().parents[3]
    raw = Path(os.environ["M2_SENSITIVE_OUTPUT_DIRECTORY"])
    config = os.environ["M2_SENSITIVE_PLAYWRIGHT_CONFIG"]

    def interrupted(_number: int, _frame: FrameType | None) -> None:
        """外层超时请求仍进入 finally 回收子进程，不留下后台输出生产者。"""
        raise InterruptedError("observable output run interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        for port in (8000, 5173):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError("observable loopback port is already occupied")
        result = run_observed_processes(
            root=root, raw=raw,
            browser_command=("pnpm", "--dir", "frontend", "exec", "playwright", "test", "--config", config),
        )
        # 已退出 with: 所有自有进程组消失、backend 原生命周期结束、文件句柄关闭。
        # 根 scanner 还将等此 supervisor 退出，之后才读取原始字节并计算最终摘要。
        with (raw / "producers-stopped.json").open("x", encoding="utf-8") as output:
            json.dump({"code": result.code, "producers": [asdict(item) for item in result.producers]}, output)
        return result.code
    except Exception:  # noqa: BLE001 - 进程/数据库边界不向终端输出未知环境或驱动文本。
        print("observable output lifecycle failed", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
