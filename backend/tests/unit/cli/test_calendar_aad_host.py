"""验证 0019 宿主边界；Docker 仅由进程内 Fake 提供 metadata，不启动容器。"""

import json
import runpy
import subprocess
from pathlib import Path

import pytest

_HOST_SCRIPT = Path(__file__).resolve().parents[4] / "scripts/run-calendar-aad-0019.py"
_IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def host_module(monkeypatch):
    """载入固定宿主脚本并隔离 operator 环境，避免继承本机 image override。"""
    monkeypatch.setenv("APP_IMAGE_TAG", "synthetic-0019")
    monkeypatch.setenv("BACKUP_ARTIFACT_BASENAME", "synthetic..0019")
    monkeypatch.delenv("CALENDAR_AAD_IMMUTABLE_IMAGE_ID", raising=False)
    monkeypatch.delenv("CALENDAR_AAD_ROLLOUT_STATE", raising=False)
    return runpy.run_path(str(_HOST_SCRIPT), run_name="calendar_aad_host_test")


@pytest.mark.parametrize("alias", ["parent", "symlink"])
def test_calendar_aad_host_rejects_physical_root_before_docker(
    host_module, monkeypatch, tmp_path, alias
):
    """词法 parent 与测试自有 symlink 都不能将宿主根目录带到 Docker 查询边界。"""
    directory = Path("/tmp/..")
    if alias == "symlink":
        directory = tmp_path / "root link"
        directory.symlink_to(Path("/"), target_is_directory=True)
    monkeypatch.setenv("BACKUP_DIR", str(directory))
    calls = []

    def forbidden_docker(*arguments):
        """在首个边界终止，不允许测试中的根目录成为真实容器挂载源。"""
        calls.append(arguments)
        raise AssertionError("physical root reached Docker")

    run = host_module["run"]
    monkeypatch.setitem(run.__globals__, "_docker", forbidden_docker)
    with pytest.raises(
        host_module["RolloutHostError"], match="^calendar_aad_backup_directory_invalid$"
    ):
        run("preflight")
    assert calls == []


def test_calendar_aad_host_freezes_the_validated_physical_directory(
    host_module, monkeypatch, tmp_path
):
    """校验后的 symlink 改指根目录不能改变 mount；正常空格和点仍保留在合法物理目录中。"""
    physical = tmp_path / "backup output..0019"
    physical.mkdir()
    alias = tmp_path / "backup link"
    alias.symlink_to(physical, target_is_directory=True)
    monkeypatch.setenv("BACKUP_DIR", str(alias))
    services = {
        name: {
            "image": f"synthetic-{name}:synthetic-0019",
            "environment": {},
            "volumes": [],
        }
        for name in ("worker", "migration", "api", "scheduler")
    }

    def fake_docker(*arguments):
        """只返回合成 metadata，并在目录 admission 之后替换测试自有链接。"""
        if arguments == ("compose", "config", "--format", "json"):
            alias.unlink()
            alias.symlink_to(Path("/"), target_is_directory=True)
            return json.dumps({"services": services})
        if arguments == ("compose", "ps", "--all", "--format", "json"):
            return "[]"
        assert arguments[:2] == ("image", "inspect")
        return _IMAGE

    mounted_sources = []

    def fake_oneoff(command, **kwargs):
        """只读临时 Compose 来观察真实渲染值，绝不调用 Docker 或任何维护命令。"""
        config = json.loads(Path(command[command.index("-f") + 1]).read_text())
        mounted_sources.extend(
            volume["source"] for volume in config["services"]["worker"]["volumes"]
        )
        output = (
            "20260809_0018\n"
            if command[-1].endswith("ai_employee.cli.calendar_aad_revision_0019")
            else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    run = host_module["run"]
    monkeypatch.setitem(run.__globals__, "_docker", fake_docker)
    monkeypatch.setattr(subprocess, "run", fake_oneoff)
    run("preflight")
    assert mounted_sources == [str(physical)]


@pytest.fixture
def docker_probe(host_module, monkeypatch, tmp_path):
    """记录宿主实际调用顺序与渲染配置，只在 Docker 进程边界提供有界合成响应。"""
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))

    class Probe:
        """仅模拟 metadata 与 one-off 退出结果，不替换任何宿主校验函数。"""

        def __init__(self):
            """建立固定合成镜像/Secret 配置与可控的 screen 后漂移。"""
            self.events = []
            self.calls = []
            self.screen_output = "20260809_0018\n"
            self.screen_status = 0
            self.screened = False
            self.changed_after_screen = None
            self.services = {
                name: {
                    "image": f"synthetic-{name}:synthetic-0019",
                    "environment": {},
                    "volumes": [],
                    "secrets": [
                        {
                            "source": "postgres_bootstrap_password"
                            if name == "migration"
                            else "app_database_password"
                        }
                    ],
                }
                for name in ("worker", "migration", "api", "scheduler", "backup")
            }
            self.services["worker"]["environment"]["DATABASE_URL"] = (
                "postgresql+psycopg://synthetic_app@postgres:5432/synthetic_test"
            )

        def query(self, *arguments):
            """按实际查询参数返回 metadata；需要时只在 screen 完成后改变观察结果。"""
            self.events.append(arguments)
            if arguments == ("compose", "config", "--format", "json"):
                return json.dumps({"services": self.services})
            if arguments == ("compose", "ps", "--all", "--format", "json"):
                if self.screened and self.changed_after_screen == "service":
                    return '[{"Service":"api","State":"running"}]'
                return "[]"
            assert arguments[:2] == ("image", "inspect")
            if self.screened and self.changed_after_screen == "image":
                return "sha256:" + "b" * 64
            return _IMAGE

        def oneoff(self, command, **kwargs):
            """记录临时配置与命令并返回合成退出结果，不执行容器内部脚本。"""
            config = json.loads(Path(command[command.index("-f") + 1]).read_text())
            service = command[-3]
            screen = command[-1].endswith("ai_employee.cli.calendar_aad_revision_0019")
            self.calls.append((service, command[-1], config["services"][service]))
            self.events.append(("screen" if screen else "operation",))
            if screen:
                self.screened = True
                return subprocess.CompletedProcess(
                    command,
                    self.screen_status,
                    stdout=self.screen_output,
                    stderr="calendar_aad_revision_screen_failed\n" if self.screen_status else "",
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    probe = Probe()
    monkeypatch.setitem(host_module["run"].__globals__, "_docker", probe.query)
    monkeypatch.setattr(subprocess, "run", probe.oneoff)
    return probe


@pytest.mark.parametrize("operation", ["preflight", "migrate", "resync", "backup"])
def test_calendar_aad_host_preserves_fixed_state_and_secret_wrapper(
    host_module, docker_probe, operation
):
    """正式命令保留固定 state/image binding，migration 显式使用批准的 owner Secret wrapper。"""
    docker_probe.screen_output = "20260809_0019\n" if operation == "resync" else "20260809_0018\n"
    host_module["run"](operation)
    _, command, selected = docker_probe.calls[-1]
    assert selected["environment"]["CALENDAR_AAD_ROLLOUT_STATE"] == (
        "/backups/synthetic..0019.calendar-aad-preflight.json"
    )
    assert selected["environment"]["CALENDAR_AAD_IMMUTABLE_IMAGE_ID"] == _IMAGE
    secret = "postgres_bootstrap_password" if operation == "migrate" else "app_database_password"
    assert command.startswith(f'export PGPASSWORD="$(cat /run/secrets/{secret})"; exec ')


def test_calendar_aad_migration_uses_the_prescribed_owner_secret_wrapper(host_module, docker_probe):
    """单独证明 owner wrapper 的固定值，不让缺失 state 的早期断言掩盖该兼容性错误。"""
    host_module["run"]("migrate")
    assert docker_probe.calls[-1][1] == (
        'export PGPASSWORD="$(cat /run/secrets/postgres_bootstrap_password)"; '
        "exec uv run --no-sync python -m ai_employee.cli.calendar_aad_migrate_0019"
    )


@pytest.mark.parametrize("operation", ["preflight", "migrate", "resync", "backup"])
def test_calendar_aad_host_screens_then_rechecks_before_formal_operation(
    host_module, docker_probe, operation
):
    """先只读筛查，重查实际服务/镜像后才开始正式 one-off；screen 无备份或源码挂载。"""
    docker_probe.screen_output = "20260809_0019\n" if operation == "resync" else "20260809_0018\n"
    host_module["run"](operation)
    assert len(docker_probe.calls) == 2
    service, command, screen = docker_probe.calls[0]
    assert service == "migration" and command.endswith("ai_employee.cli.calendar_aad_revision_0019")
    assert screen["image"] == _IMAGE and screen["volumes"] == []
    assert screen["secrets"] == [{"source": "postgres_bootstrap_password"}]
    assert "CALENDAR_AAD_ROLLOUT_STATE" not in screen["environment"]
    assert "BACKUP_DIR" not in screen["environment"]
    screen_index = docker_probe.events.index(("screen",))
    operation_index = docker_probe.events.index(("operation",))
    between = docker_probe.events[screen_index + 1 : operation_index]
    assert ("compose", "ps", "--all", "--format", "json") in between
    assert any(
        event[:2] == ("image", "inspect") and "synthetic-worker:" in event[-1] for event in between
    )
    assert any(
        event[:2] == ("image", "inspect") and "synthetic-migration:" in event[-1]
        for event in between
    )


@pytest.mark.parametrize(
    "failure", ["screen_error", "wrong_revision", "extra_output", "service", "image"]
)
def test_calendar_aad_host_rejects_failed_or_stale_screen_before_operation(
    host_module, docker_probe, failure
):
    """screen 失败、版本不符或其后停服/镜像变化，都不能启动正式 artifact/provider 操作。"""
    expected_code = "calendar_aad_revision_mismatch"
    if failure == "screen_error":
        docker_probe.screen_status = 1
        expected_code = "calendar_aad_revision_screen_failed"
    elif failure == "wrong_revision":
        docker_probe.screen_output = "20260809_0019\n"
    elif failure == "extra_output":
        docker_probe.screen_output = "20260809_0018\nsynthetic-private-diagnostic\n"
    else:
        docker_probe.changed_after_screen = failure
        expected_code = (
            "calendar_aad_services_running"
            if failure == "service"
            else "calendar_aad_image_mismatch"
        )
    with pytest.raises(host_module["RolloutHostError"], match=f"^{expected_code}$"):
        host_module["run"]("preflight")
    assert len(docker_probe.calls) == 1
    assert docker_probe.calls[0][1].endswith("ai_employee.cli.calendar_aad_revision_0019")
