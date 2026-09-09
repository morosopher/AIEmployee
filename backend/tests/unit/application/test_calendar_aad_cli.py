"""验证维护 CLI 的无参数和安全异常边界；测试不读取 Secret 或访问网络/数据库。"""

import importlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import URL

from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
from ai_employee.config import Settings
from ai_employee.infrastructure.db.repositories import calendar_aad_preflight as resources
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


@pytest.mark.parametrize("explicit_arguments", [False, True])
def test_calendar_backup_rejects_arguments_before_parent_entry(
    explicit_arguments, monkeypatch, capsys
):
    """兼容入口在父备份流程前拒绝显式或进程参数，不能先触发任何owner工作。"""
    from ai_employee.cli import calendar_aad_backup_0019 as module
    from ai_employee.cli import postgres_backup as parent

    calls = []

    def delegated(arguments):
        """观察父入口是否被错误调用，不替代参数拒绝或输出断言。"""
        calls.append(arguments)
        return 99

    arguments = ["--scope", "synthetic-private-scope"]
    monkeypatch.setattr(sys, "argv", ["calendar-backup", *arguments])
    monkeypatch.setattr(parent, "main", delegated)
    assert module.main(arguments if explicit_arguments else None) == 1
    assert calls == []
    output = capsys.readouterr()
    assert output.err == "calendar_aad_arguments_invalid\n" and output.out == ""


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


@pytest.mark.parametrize(
    "module_name",
    ["calendar_aad_preflight_0019", "calendar_aad_0019", "calendar_aad_backup_0019"],
)
def test_calendar_aad_cli_rejects_fifo_without_writer_and_closes_lease(
    module_name, monkeypatch, tmp_path, capsys
):
    """真实 CLI/asyncio.run 必须在无 FIFO writer 时返回稳定错误并完整退出 lease。"""
    module = importlib.import_module(f"ai_employee.cli.{module_name}")
    closed, disposed, owner_closed = [], [], []
    artifact_file = resources.CalendarAadArtifactFile(tmp_path, BINDING)
    os.mkfifo(artifact_file.path, mode=0o600)

    @contextmanager
    def context(url):
        """只替代物理数据库连接，真实异步 lease adapter 与 artifact reader 保持不变。"""
        try:
            yield object()
        finally:
            closed.append(True)

    async def dispose():
        """显式记录 CLI 所拥有的会话工厂生命周期，不访问任何数据库。"""
        disposed.append(True)

    sessions = SimpleNamespace(
        engine=SimpleNamespace(url=URL.create("postgresql")), dispose=dispose
    )
    # 新增的固定owner事实读取器只构造合法endpoint；FIFO拒绝前绝不读它的Secret或数据库。
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://ai_employee_app@postgres:5432/ai_employee_test"
    )
    monkeypatch.setattr(resources, "calendar_aad_rollout_lease", context)
    if module_name == "calendar_aad_backup_0019":
        from ai_employee.cli import postgres_backup as parent
        from ai_employee.cli import postgres_restore
        from ai_employee.infrastructure.db.postgres_backup_manifest import OPERATIONS_EXECUTABLES

        @contextmanager
        def owned(label, value):
            """仅替代物理owner连接及数据库锁，真实文件锁、父组合和revision lease均保留。"""
            try:
                yield value
            finally:
                owner_closed.append(label)

        holder = SimpleNamespace(shared_schema=lambda: owned("schema", None))
        target = SimpleNamespace(backup_holder=lambda: owned("backup", holder))
        management = SimpleNamespace(acquire_target_lock=lambda: owned("target", target))
        owner = SimpleNamespace(
            acquire_management_lifecycle_lock=lambda: owned("management", management)
        )
        monkeypatch.setattr(
            parent, "_open_maintenance_context", lambda *args: owned("owner", owner)
        )
        monkeypatch.setattr(parent, "_read_secret_file", lambda path: SecretStr("synthetic-only"))
        monkeypatch.setattr(parent, "build_session_factory", lambda url: sessions)
        monkeypatch.setattr(
            parent,
            "image_executable_digests",
            lambda: dict.fromkeys(OPERATIONS_EXECUTABLES, "a" * 64),
        )
        monkeypatch.setattr(postgres_restore, "_host_guard", lambda: None)
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
        monkeypatch.setenv("BACKUP_ARTIFACT_BASENAME", BINDING.basename)
        monkeypatch.setenv("BACKUP_IMMUTABLE_IMAGE_ID", BINDING.immutable_image_id)
        monkeypatch.delenv("BACKUP_RCLONE_REMOTE", raising=False)
        for flag in (
            "EXTERNAL_WRITES_ENABLED",
            "GOOGLE_WRITES_ENABLED",
            "MICROSOFT_WRITES_ENABLED",
        ):
            monkeypatch.setenv(flag, "false")
    else:
        monkeypatch.setattr(module, "rollout_environment", lambda: (tmp_path, BINDING))
        monkeypatch.setattr(
            module,
            "Settings",
            lambda: Settings(
                app_env="test",
                app_test_mode=True,
                task_timeout_seconds=1,
                task_step_timeout_seconds=1,
            ),
        )
        monkeypatch.setattr(module, "build_session_factory", lambda url: sessions)
        monkeypatch.setattr(
            module,
            "build_oauth_security_services",
            lambda **kwargs: SimpleNamespace(coordinator=object(), cipher=object()),
        )
        monkeypatch.setattr(module, "CalendarAadReadAdapters", lambda *args, **kwargs: object())
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(module.main, [])
        try:
            assert pending.result(timeout=0.2) == 1
        finally:
            if not pending.done():
                # 唯一救援对象是本测试的 FIFO；零写入唤醒旧 reader，避免挂住 asyncio.run 收尾。
                descriptor = os.open(artifact_file.path, os.O_WRONLY | os.O_NONBLOCK)
                os.close(descriptor)
            assert pending.result(timeout=3) == 1
    assert closed == [True]
    assert disposed == [True]
    output = capsys.readouterr()
    if module_name == "calendar_aad_backup_0019":
        assert owner_closed == ["schema", "backup", "target", "management", "owner"]
        assert output.err == "postgres_backup_failed\n"
        assert not tuple(tmp_path.glob("*.dump.enc*"))
        assert not tuple(tmp_path.glob(".partial.*"))
    else:
        assert output.err == "calendar_aad_artifact_invalid\n"
    assert output.out == ""
