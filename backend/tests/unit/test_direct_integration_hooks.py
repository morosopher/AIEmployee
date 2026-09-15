"""以真实 pytest 子进程验证直接集成入口；仅替换创建数据库的外部边界。

这些用例保护 pytest 收集、筛选、重入、退出与报告契约。测试沙盒复制实际 conftest，
调用记录替身只在 orchestrator 边界停止数据库工作；不伪造 TestReport 或通过数量。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
REGULAR_FILE = "backend/tests/integration/api/test_hook_selection.py"
LIFECYCLE_FILE = "backend/tests/integration/db/test_migrations.py"
IGNORED_FILE = "backend/tests/integration/api/test_hook_ignored.py"
REGULAR_NODES = tuple(f"{REGULAR_FILE}::test_chosen_regular[{number}]" for number in (0, 1))
LIFECYCLE_NODES = (f"{LIFECYCLE_FILE}::test_chosen_lifecycle",)
FIXED_DATABASE_URL = (
    "postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test"
)


@dataclass(frozen=True)
class HookSandbox:
    """持有一次真实 pytest 调用的私有路径，不读取工作区数据库或配置。"""

    root: Path

    def run(self, *arguments: str, exit_code: int = 0) -> subprocess.CompletedProcess[str]:
        """继承 Python 依赖但清除 pytest 隐式参数，返回真实 OS 退出和全部终端输出。"""
        environment = dict(os.environ)
        environment.pop("PYTEST_ADDOPTS", None)
        environment.pop("PYTEST_PLUGINS", None)
        environment.update(
            PYTHONPATH=str(ROOT / "backend"),
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
            TEST_DATABASE_URL=FIXED_DATABASE_URL,
            HOOK_SANDBOX=str(self.root),
            HOOK_CHILD_EXIT=str(exit_code),
        )
        return subprocess.run(
            [sys.executable, "-m", "pytest", *arguments],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def calls(self) -> list[dict[str, object]]:
        """仅读取脱敏选择事实，缺文件代表尚未到达数据库编排边界。"""
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def executed(self) -> list[str]:
        """记录真正进入测试函数的节点；父委派与错误收集都必须保持为空。"""
        path = self.root / "executed.txt"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def hook_sandbox(tmp_path: Path) -> HookSandbox:
    """建立三个合成文件和同字节入口，保留 hook、pytest 收集及执行本身。"""
    backend = tmp_path / "backend"
    backend.mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = synthetic: fixed hook contract\n")
    entry = (ROOT / "backend/conftest.py").read_text()
    entry += '''

# 沙盒只替换外部数据库编排边界；真实 hook 与其生成的选择保持原样。
import json

def _recorded_orchestrator(**kwargs):
    plan = kwargs["selected"]
    record = {"arguments": list(plan.arguments), "cwd": str(plan.cwd),
              "regular": list(plan.regular_nodes), "lifecycle": list(plan.lifecycle_nodes)}
    with (Path(os.environ["HOOK_SANDBOX"]) / "calls.jsonl").open("a") as output:
        output.write(json.dumps(record) + "\\n")
    return int(os.environ["HOOK_CHILD_EXIT"])

orchestrate_integration_tests = _recorded_orchestrator
SqlAlchemyIntegrationSuiteResources = lambda repository_root: object()
'''
    (backend / "conftest.py").write_text(entry)
    common = '''import os
from pathlib import Path
import pytest

def record(value):
    with (Path(os.environ["HOOK_SANDBOX"]) / "executed.txt").open("a") as output:
        output.write(value + "\\n")

'''
    sources = {
        REGULAR_FILE: common + '''@pytest.mark.synthetic
@pytest.mark.parametrize("number", [0, 1])
def test_chosen_regular(number):
    record("regular-" + str(number))

def test_unselected_regular():
    record("unselected")
''',
        LIFECYCLE_FILE: common + '''@pytest.mark.synthetic
def test_chosen_lifecycle():
    record("lifecycle")
''',
        IGNORED_FILE: common + '''@pytest.mark.synthetic
def test_chosen_ignored():
    record("ignored")
''',
    }
    for name, source in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return HookSandbox(tmp_path)


def test_collect_only_does_not_enter_database_or_fixtures(hook_sandbox: HookSandbox) -> None:
    """收集成功只展示真实节点，不能因固定 URL 创建 disposable 数据库。"""
    result = hook_sandbox.run("backend", "--collect-only", "-q")
    assert result.returncode == 0, result.stderr
    assert "5 tests collected" in result.stdout
    assert hook_sandbox.calls() == [] and hook_sandbox.executed() == []


def test_empty_selection_preserves_pytest_exit_five(hook_sandbox: HookSandbox) -> None:
    """-k 排空全部测试时没有可委派工作，不能把 pytest 的退出5改成成功。"""
    result = hook_sandbox.run("backend", "-k", "absent_case", "-q")
    assert result.returncode == 5
    assert hook_sandbox.calls() == [] and hook_sandbox.executed() == []


@pytest.mark.parametrize("continue_after_errors", [False, True])
def test_collection_error_stops_before_any_fixture_or_database(
    hook_sandbox: HookSandbox, continue_after_errors: bool,
) -> None:
    """收集不完整无法绑定有序选择；即使请求继续，也不得在原 anchor 执行剩余测试。"""
    (hook_sandbox.root / "backend/tests/integration/api/test_broken.py").write_text("def invalid(:\n")
    extra = ("--continue-on-collection-errors",) if continue_after_errors else ()
    result = hook_sandbox.run("backend", "-q", *extra)
    assert result.returncode == 2, result.stdout + result.stderr
    assert hook_sandbox.calls() == [] and hook_sandbox.executed() == []


def test_real_collection_filters_and_original_arguments_are_preserved(hook_sandbox: HookSandbox) -> None:
    """-k、marker、ignore 与参数化由 pytest 真实应用，只有剩余节点被一次委派。"""
    arguments = ("backend", "-q", "-k", "chosen", "-m", "synthetic", f"--ignore={IGNORED_FILE}")
    result = hook_sandbox.run(*arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    assert hook_sandbox.calls() == [{
        "arguments": list(arguments), "cwd": str(hook_sandbox.root),
        "regular": list(REGULAR_NODES), "lifecycle": list(LIFECYCLE_NODES),
    }]
    assert hook_sandbox.executed() == []
    assert "Delegated 3 selected nodes; child phases succeeded; anchor verified" in result.stdout
    assert "3 passed" not in result.stdout


@pytest.mark.parametrize("phase,nodes,executed", [
    ("regular", REGULAR_NODES, ["regular-0", "regular-1"]),
    ("lifecycle", LIFECYCLE_NODES, ["lifecycle"]),
])
def test_child_reentry_runs_only_verified_selected_phase(
    hook_sandbox: HookSandbox, phase: str, nodes: tuple[str, ...], executed: list[str],
) -> None:
    """内部 phase 抑制再次建库；摘要匹配后仅运行所属真实节点，不重复父进程。"""
    # 独立 JSON/SHA 计算只构造命令输入；预期执行名单是上面手工列出的三个测试函数。
    digest = hashlib.sha256(json.dumps(nodes, separators=(",", ":")).encode()).hexdigest()
    result = hook_sandbox.run(
        "backend", "-q", "-k", "chosen", "-m", "synthetic", f"--ignore={IGNORED_FILE}",
        f"--ai-employee-integration-child={phase}", f"--ai-employee-integration-selection={digest}",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert hook_sandbox.calls() == [] and hook_sandbox.executed() == executed
    assert f"{len(executed)} passed" in result.stdout


@pytest.mark.parametrize("extra", [
    ("--ai-employee-integration-selection=" + "0" * 64,),
    ("--ai-employee-integration-child=regular", "--ai-employee-integration-selection=" + "0" * 64),
    ("--ai-employee-integration-child=lifecycle",),
])
def test_child_metadata_mismatch_rejects_before_fixture(hook_sandbox: HookSandbox, extra: tuple[str, ...]) -> None:
    """缺 phase、摘要被篡改或无摘要混相均返回UsageError，不吞掉未运行节点。"""
    result = hook_sandbox.run("backend", "-q", *extra)
    assert result.returncode == 4, result.stdout + result.stderr
    assert hook_sandbox.calls() == [] and hook_sandbox.executed() == []


@pytest.mark.parametrize("code", [1, 2, 3, 5])
def test_real_parent_preserves_nonzero_child_exit(hook_sandbox: HookSandbox, code: int) -> None:
    """数据库边界报告任何 child 非零时父进程都保留退出状态，且不转为本地执行。"""
    result = hook_sandbox.run("backend", "-q", exit_code=code)
    assert result.returncode == code, result.stdout + result.stderr
    assert len(hook_sandbox.calls()) == 1 and hook_sandbox.executed() == []
    assert "child phases succeeded" not in result.stdout


@pytest.mark.parametrize("report_argument", ["--junitxml=report.xml", "--junit-xml=report.xml", "--cov=ai_employee"])
def test_direct_report_arguments_fail_before_database(hook_sandbox: HookSandbox, report_argument: str) -> None:
    """共享报告路径不能被两个子进程覆盖；不支持的coverage插件参数也保持真实解析错误。"""
    result = hook_sandbox.run("backend", "-q", report_argument)
    assert result.returncode == 4, result.stdout + result.stderr
    assert hook_sandbox.calls() == [] and hook_sandbox.executed() == []
