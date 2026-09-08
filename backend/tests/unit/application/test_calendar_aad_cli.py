"""验证维护 CLI 的无参数和安全异常边界；测试不读取 Secret 或访问网络/数据库。"""

import importlib
from pathlib import Path

import httpx
import pytest

from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
from ai_employee.config import Settings
from tests.unit.application.test_calendar_aad_rollout import BINDING


@pytest.fixture
def fixed_rollout_state(monkeypatch):
    """模拟批准的 migration/resync invocation，仅传固定 state 路径与实际 image ID。"""
    state = f"/backups/{BINDING.basename}.calendar-aad-preflight.json"
    monkeypatch.setenv("CALENDAR_AAD_ROLLOUT_STATE", state)
    monkeypatch.setenv("CALENDAR_AAD_IMMUTABLE_IMAGE_ID", BINDING.immutable_image_id)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.delenv("BACKUP_ARTIFACT_BASENAME", raising=False)
    return state


@pytest.mark.parametrize("explicit_binding", [False, True])
def test_calendar_aad_fixed_state_derives_directory_and_basename(
    monkeypatch, fixed_rollout_state, explicit_binding
):
    """批准的 state-only 环境可以绑定目录/basename，无需私有替代环境字段。"""
    from ai_employee.cli.calendar_aad_preflight_0019 import rollout_environment

    if explicit_binding:
        monkeypatch.setenv("BACKUP_DIR", "/backups")
        monkeypatch.setenv("BACKUP_ARTIFACT_BASENAME", BINDING.basename)
    assert rollout_environment() == (Path("/backups").resolve(), BINDING)


def test_calendar_aad_fixed_mount_rejects_physical_root(monkeypatch, fixed_rollout_state):
    """即使 state 词法绑定合法，实际 /backups 指向根目录也必须在 artifact I/O 前拒绝。"""
    from ai_employee.cli.calendar_aad_preflight_0019 import rollout_environment

    def resolve_mount(path):
        """仅替换文件系统解析边界，模拟容器挂载目录解析为物理根目录。"""
        assert path == Path("/backups")
        return Path("/")

    monkeypatch.setattr(Path, "resolve", resolve_mount)
    with pytest.raises(CalendarAadRolloutError) as failure:
        rollout_environment()
    assert failure.value.error_code == "calendar_aad_artifact_invalid"


@pytest.mark.parametrize(
    "state",
    [
        "",
        "/other/synthetic.calendar-aad-preflight.json",
        "/backups/nested/synthetic.calendar-aad-preflight.json",
        "/backups/../synthetic.calendar-aad-preflight.json",
        "/backups/synthetic.json",
        "//backups/synthetic.calendar-aad-preflight.json",
        "/backups/synthetic.calendar-aad-preflight.json/",
    ],
)
def test_calendar_aad_fixed_state_rejects_noncanonical_path(
    monkeypatch, fixed_rollout_state, state
):
    """固定路径协议不能被其它目录、路径别名、后缀或空值替代；旧环境不能掩盖错误。"""
    from ai_employee.cli.calendar_aad_preflight_0019 import rollout_environment

    monkeypatch.setenv("CALENDAR_AAD_ROLLOUT_STATE", state)
    monkeypatch.setenv("BACKUP_DIR", "/backups")
    monkeypatch.setenv("BACKUP_ARTIFACT_BASENAME", BINDING.basename)
    with pytest.raises(CalendarAadRolloutError) as failure:
        rollout_environment()
    assert failure.value.error_code == "calendar_aad_artifact_invalid"


@pytest.mark.parametrize("override", ["directory", "basename"])
def test_calendar_aad_fixed_state_rejects_explicit_binding_disagreement(
    monkeypatch, fixed_rollout_state, tmp_path, override
):
    """显式目录或 basename 必须与冻结 state 一致，不能悄悄选择另一 artifact。"""
    from ai_employee.cli.calendar_aad_preflight_0019 import rollout_environment

    monkeypatch.setenv("BACKUP_DIR", str(tmp_path) if override == "directory" else "/backups")
    monkeypatch.setenv(
        "BACKUP_ARTIFACT_BASENAME",
        "synthetic-other" if override == "basename" else BINDING.basename,
    )
    with pytest.raises(CalendarAadRolloutError) as failure:
        rollout_environment()
    assert failure.value.error_code == "calendar_aad_artifact_invalid"


@pytest.mark.parametrize("alias", ["parent", "symlink"])
def test_calendar_aad_cli_rejects_physical_root(monkeypatch, tmp_path, alias):
    """CLI 同样拒绝指向物理根目录的路径，且解析期间不读取 artifact 或 Secret。"""
    from ai_employee.cli.calendar_aad_preflight_0019 import rollout_environment

    directory = Path("/tmp/..")
    if alias == "symlink":
        directory = tmp_path / "root link"
        directory.symlink_to(Path("/"), target_is_directory=True)
    monkeypatch.setenv("BACKUP_DIR", str(directory))
    monkeypatch.setenv("BACKUP_ARTIFACT_BASENAME", BINDING.basename)
    monkeypatch.setenv("CALENDAR_AAD_IMMUTABLE_IMAGE_ID", BINDING.immutable_image_id)
    monkeypatch.setenv(
        "CALENDAR_AAD_ROLLOUT_STATE", f"/backups/{BINDING.basename}.calendar-aad-preflight.json"
    )
    with pytest.raises(CalendarAadRolloutError) as failure:
        rollout_environment()
    assert failure.value.error_code == "calendar_aad_artifact_invalid"


@pytest.mark.parametrize(
    "module_name",
    [
        "calendar_aad_preflight_0019",
        "calendar_aad_0019",
        "calendar_aad_migrate_0019",
        "calendar_aad_backup_0019",
    ],
)
def test_calendar_aad_no_argument_cli_rejects_without_echoing_input(module_name, capsys):
    """未知参数不能被 argparse 回显为可能含原始日历标识的诊断。"""
    module = importlib.import_module(f"ai_employee.cli.{module_name}")
    assert module.main(["--scope", "synthetic-private-scope"]) == 1
    output = capsys.readouterr()
    assert output.err == "calendar_aad_arguments_invalid\n"
    assert output.out == ""


@pytest.mark.parametrize("module_name", ["calendar_aad_preflight_0019", "calendar_aad_0019"])
def test_calendar_aad_cli_http_exception_is_content_free(
    module_name, monkeypatch, tmp_path, capsys
):
    """原始 HTTP 异常的 URL/response 不进入 CLI stderr，未知失败也不能伪装成可重试。"""
    module = importlib.import_module(f"ai_employee.cli.{module_name}")
    monkeypatch.setattr(module, "rollout_environment", lambda: (tmp_path, BINDING))
    monkeypatch.setattr(module, "Settings", lambda: Settings(app_env="test", app_test_mode=True))

    async def failed(*args):
        """仅替代最外部执行边界，模拟带敏感 query 的 HTTP 异常。"""
        request = httpx.Request("GET", "https://calendar.example.test/?synthetic-private-scope")
        raise httpx.HTTPStatusError(
            "synthetic-private-response",
            request=request,
            response=httpx.Response(401, request=request),
        )

    monkeypatch.setattr(module, "_run", failed)
    assert module.main([]) == 1
    output = capsys.readouterr()
    assert output.err == "calendar_aad_rollout_failed\n"
    assert output.out == ""
