"""验证 CI 工作流检查读取实际集成选择，并拒绝断线或被排除的日简报评估。"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.integration import integration_suite

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("fault", [None, "ci_disconnected", "comment_only", "eval_removed", "eval_ignored", "ancestor_ignored", "preparation_disconnected", "preparation_comment_only"])
def test_workflow_contract_follows_ci_and_actual_regular_eval_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    """真实内嵌检查使用实际 builder；仅用 Fake 隔离 just 展开和负例选择。

    当前 recipe 通过唯一 orchestrator 收集评估，不包含评估目录文本。正例必须接受
    该真实执行链；负例在断开 CI、只留注释、删除评估或忽略其父目录时仍然拒绝。
    所有文件均为临时合成配置，不执行 CI、连接数据库或读取真实 Secret。
    """
    for relative in ("justfiles/test.just", ".github/workflows/ci.yml"):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((ROOT / relative).read_text())
    monkeypatch.chdir(tmp_path)
    command = "uv run --project backend python scripts/run_integration_tests.py"
    if fault == "ci_disconnected":
        command = "uv run --project backend pytest backend/tests/unit -q"
    elif fault == "comment_only":
        command = "# " + command

    def expanded_ci(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        """模拟 just 的实际展开输出；不把注释当可执行调用，也不递归运行 CI。"""
        assert arguments[:4] == ["just", "--color", "never", "--dry-run"]
        if arguments[4:] == ["ci"]:
            return subprocess.CompletedProcess(arguments, 0, "", command + "\n")
        assert arguments[4:] == ["prepare-ci-database"]
        preparation = "uv run --project backend python scripts/prepare_ci_database.py"
        if fault == "preparation_disconnected":
            preparation = "true"
        elif fault == "preparation_comment_only":
            preparation = "# " + preparation
        return subprocess.CompletedProcess(arguments, 0, "", preparation + "\n")

    monkeypatch.setattr(subprocess, "run", expanded_ci)
    original_arguments = integration_suite.build_regular_pytest_arguments()
    if fault == "eval_removed":
        replacement = tuple(value for value in original_arguments if value != "backend/tests/evals")
        monkeypatch.setattr(integration_suite, "build_regular_pytest_arguments", lambda: replacement)
    elif fault in {"eval_ignored", "ancestor_ignored"}:
        ignored = "backend/tests/evals" if fault == "eval_ignored" else "backend/tests"
        monkeypatch.setattr(integration_suite, "build_regular_pytest_arguments", lambda: (*original_arguments, "--ignore=" + ignored))
    source = (ROOT / "scripts/test-ci-workflow.sh").read_text()
    program = source.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    with pytest.raises(SystemExit) as stopped:
        # 仅执行仓库固定脚本中的检查；fixture 和用户输入不能提供待执行的 Python 源码。
        exec(compile(program, "test-ci-workflow.sh", "exec"), {"__name__": "__main__"})  # noqa: S102
    if fault is None:
        assert stopped.value.code == 0
    elif fault.startswith("preparation_"):
        assert stopped.value.code == "CI must use the approved explicit database preparation recipe"
    else:
        assert stopped.value.code == "just ci must include the approved daily-brief evaluation suite"


@pytest.mark.parametrize("preparation_exit", [0, 31])
def test_workflow_preparation_publishes_environment_only_after_explicit_anchor_recipe(
    tmp_path: Path, preparation_exit: int,
) -> None:
    """执行原 workflow run 块，捕捉遗漏准备、仍旧 bootstrap 或失败后继续发布环境的回归。

    外部服务仅在边界以 Fake just 隔离；真实 shell、变量/Secret 文件生成和旧 bootstrap
    脚本均原样执行。Fake uv/数据库工具一律拒绝，因此单测不接触外部数据库或容器。
    """
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    step = workflow.split("      - name: Create synthetic secret files", 1)[1]
    block = step.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0]
    # 实际 env 模板也必须参与执行：删掉 ID 或错接另一 service 时，Fake recipe 应拒绝。
    expressions = {
        "${{ job.services.postgres.id }}": "b" * 64,
        "${{ github.run_id }}": "30001", "${{ github.run_attempt }}": "1",
    }
    workflow_environment: dict[str, str] = {}
    if "        env:\n" in step.split("        run: |\n", 1)[0]:
        env_block = step.split("        env:\n", 1)[1].split("        run: |\n", 1)[0]
        for line in env_block.splitlines():
            key, separator, value = line.strip().partition(": ")
            if separator:
                workflow_environment[key] = expressions.get(value, value)
    shell_path = tmp_path / "preparation.sh"
    shell_path.write_text(textwrap.dedent(block))
    for relative in ("scripts/init-db-roles.sh", "backend/pyproject.toml"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((ROOT / relative).read_text())
    tools = tmp_path / "bin"
    tools.mkdir()
    program = f"#!{sys.executable}\n" + textwrap.dedent('''\
        import base64
        import os
        from pathlib import Path
        import sys
        tool = Path(sys.argv[0]).name
        if tool == "openssl":
            assert sys.argv[1:] == ["rand", "-base64", "32"]
            print(base64.b64encode(bytes(range(32))).decode())
            raise SystemExit(0)
        if tool == "just":
            assert sys.argv[1:] == ["prepare-ci-database"]
            assert os.environ["CI_DATABASE_CONTAINER_ID"] == "b" * 64
            assert os.environ["CI_DATABASE_RUN_ID"] == "30001"
            assert os.environ["CI_DATABASE_RUN_ATTEMPT"] == "1"
            assert os.environ["CI_DATABASE_JOB"] == "verify"
            assert os.environ["TEST_DATABASE_URL"].endswith("@127.0.0.1:5432/ai_employee_test")
            Path(os.environ["FAKE_READY_FILE"]).write_text("called")
            raise SystemExit(int(os.environ["FAKE_PREPARATION_EXIT"]))
        raise SystemExit(86)
        ''')
    for tool in ("just", "uv", "psql", "docker", "openssl"):
        path = tools / tool
        path.write_text(program)
        path.chmod(0o700)
    result = subprocess.run(
        ["/bin/bash", str(shell_path)], cwd=tmp_path,
        env={
            "PATH": str(tools) + os.pathsep + os.defpath,
            "RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(tmp_path / "github-env"),
            **workflow_environment,
            "FAKE_READY_FILE": str(tmp_path / "recipe-called"),
            "FAKE_PREPARATION_EXIT": str(preparation_exit),
        }, capture_output=True, text=True, check=False,
    )
    assert result.returncode == preparation_exit, "workflow preparation did not follow the explicit anchor recipe"
    assert (tmp_path / "recipe-called").read_text() == "called"
    assert (tmp_path / "github-env").exists() is (preparation_exit == 0)
    for path in (tmp_path / "ai-employee-secrets").iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
