"""通过真实 Bash producer 与合成可执行程序验证备份 hook、失败和取消资源边界。"""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
from ai_employee.cli.calendar_aad_backup_0019 import _SCRIPT, run_backup_shell


@pytest.fixture
def synthetic_backup_commands(tmp_path, monkeypatch):
    """只替换 pg_dump/openssl 可执行程序；真实生产 shell 和异步进程管理保持不变。"""
    directory = tmp_path / "backup"
    directory.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pg_dump = fake_bin / "pg_dump"
    pg_dump.write_text("""#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path
Path(os.environ["SYNTHETIC_BACKUP_PID"]).write_text(str(os.getpid()))
if os.environ.get("SYNTHETIC_BACKUP_BLOCK") == "yes":
    time.sleep(120)
if os.environ.get("SYNTHETIC_BACKUP_FAIL") == "yes":
    raise SystemExit(1)
Path(next(item.split("=", 1)[1] for item in sys.argv if item.startswith("--file="))).write_bytes(b"synthetic-dump")
""")
    openssl = fake_bin / "openssl"
    openssl.write_text("""#!/usr/bin/env python3
import sys
from pathlib import Path
args = sys.argv
Path(args[args.index("-out") + 1]).write_bytes(b"synthetic-encrypted:" + Path(args[args.index("-in") + 1]).read_bytes())
""")
    for executable in (pg_dump, openssl):
        executable.chmod(0o700)
    passphrase = tmp_path / "synthetic-passphrase"
    passphrase.write_text("synthetic-test-only")
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("BACKUP_PASSPHRASE_FILE", str(passphrase))
    monkeypatch.setenv("BACKUP_DIR", str(directory))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SYNTHETIC_BACKUP_PID", str(tmp_path / "producer.pid"))
    monkeypatch.delenv("BACKUP_RCLONE_REMOTE", raising=False)
    monkeypatch.delenv("BACKUP_ARTIFACT_BASENAME", raising=False)
    return directory, tmp_path / "producer.pid"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_calendar_aad_backup_uses_shared_shell_producer(
    synthetic_backup_commands, monkeypatch, failed
):
    """成功产出合成加密数据，dump 失败时清理明文；没有第二套 Python dump/encryption 实现。"""
    directory, _ = synthetic_backup_commands
    target = directory / "synthetic.dump.enc"
    if failed:
        monkeypatch.setenv("SYNTHETIC_BACKUP_FAIL", "yes")
        with pytest.raises(CalendarAadRolloutError) as failure:
            await run_backup_shell("produce", target)
        assert failure.value.error_code == "calendar_aad_backup_failed"
        assert list(directory.iterdir()) == []
    else:
        await run_backup_shell("produce", target)
        assert target.read_bytes() == b"synthetic-encrypted:synthetic-dump"
        assert list(directory.iterdir()) == [target]


@pytest.mark.asyncio
async def test_calendar_aad_backup_cancellation_stops_actual_child_group(
    synthetic_backup_commands, monkeypatch
):
    """等待实际 pg_dump 替身启动后取消，返回时该子进程不得仍在执行。"""
    directory, pid_file = synthetic_backup_commands
    monkeypatch.setenv("SYNTHETIC_BACKUP_BLOCK", "yes")
    task = asyncio.create_task(run_backup_shell("produce", directory / "synthetic.dump.enc"))
    try:
        async with asyncio.timeout(3):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        producer_pid = int(pid_file.read_text())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    process_state = Path(f"/proc/{producer_pid}/stat")
    assert not process_state.exists() or process_state.read_text().split()[2] == "Z"
    assert not list(directory.glob("*.dump.enc"))


def test_calendar_aad_backup_keeps_ordinary_script_entry(synthetic_backup_commands):
    """未指定 rollout basename 的原公开脚本仍生成时间戳 dump/checksum，保持普通备份路径。"""
    directory, _ = synthetic_backup_commands
    result = subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    artifacts = list(directory.glob("ai_employee-*.dump.enc"))
    assert len(artifacts) == 1
    assert artifacts[0].read_bytes() == b"synthetic-encrypted:synthetic-dump"
    assert artifacts[0].with_suffix(".enc.sha256").is_file()
