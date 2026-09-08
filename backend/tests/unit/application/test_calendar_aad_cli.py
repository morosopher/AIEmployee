"""验证维护 CLI 的无参数和安全异常边界；测试不读取 Secret 或访问网络/数据库。"""

import importlib

import httpx
import pytest

from ai_employee.config import Settings
from tests.unit.application.test_calendar_aad_rollout import BINDING


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
