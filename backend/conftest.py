"""在 fixture 执行前把固定 release 选择交给原两阶段 integration orchestrator。

保留 pytest 的参数化、marker、-k、ignore 和失败收集语义。子进程原样继承 CLI/cwd，
仅增加 phase 与有序选择摘要；内部标记不是数据库或业务 guard 的替代品。
"""

import os
import sys
from pathlib import Path
from typing import cast

import pytest

from ai_employee.infrastructure.db.database_url import validate_test_database_url
from tests.integration.integration_suite import (
    IntegrationPhase,
    SqlAlchemyIntegrationSuiteResources,
    orchestrate_integration_tests,
    run_pytest_child,
)
from tests.integration.pytest_selection import (
    node_phase,
    selected_plan,
    should_delegate,
    verify_child_selection,
)

ROOT = Path(__file__).resolve().parent.parent


def pytest_addoption(parser: pytest.Parser) -> None:
    """仅声明测试编排元数据，不提供跳过测试、角色修补或生产门禁旁路。"""
    parser.addoption("--ai-employee-integration-child", choices=("regular", "lifecycle"), default=None)
    parser.addoption("--ai-employee-integration-selection", default=None)


def _node_id(item: pytest.Item) -> str:
    """把 pytest rootdir-relative nodeid 转为稳定仓库路径，保留类/参数化后缀。"""
    path = item.path.resolve().relative_to(ROOT).as_posix()
    _, separator, suffix = item.nodeid.partition("::")
    return path + separator + suffix


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """child 在正常收集筛选完成后核对完整分区；不匹配即拒绝且尚未开始 fixture。"""
    phase = config.getoption("ai_employee_integration_child")
    expected = config.getoption("ai_employee_integration_selection")
    if phase is None:
        if expected is not None:
            raise pytest.UsageError("integration selection metadata has no child phase")
        return
    try:
        verify_child_selection(tuple(_node_id(item) for item in items), cast(IntegrationPhase, phase), expected)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from None
    deselected = [item for item in items if node_phase(_node_id(item)) != phase]
    items[:] = [item for item in items if node_phase(_node_id(item)) == phase]
    if deselected:
        config.hook.pytest_deselected(items=deselected)


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session: pytest.Session) -> bool | None:
    """父进程不执行 fixture；真实 child 退出经过原 cleanup/anchor 复核后原样传递。"""
    config = session.config
    if config.option.collectonly:
        return None
    nodes = tuple(_node_id(item) for item in session.items)
    raw_database_url = os.environ.get("TEST_DATABASE_URL")
    if not should_delegate(nodes, raw_database_url, config.getoption("ai_employee_integration_child")):
        return None
    if session.testsfailed:
        # 收集错误使 parent 无法冻结完整选择；pytest 的 continue 选项会直接执行剩余
        # fixture，落回未隔离的原 anchor，因此本入口在任何建库或 fixture 前停止。
        pytest.exit("direct integration collection failed before isolation", returncode=2)
    # 这些输出会被原 CLI 的多个进程写到同一路径，不能给用户一个被覆盖的报告。当前
    # release 直接命令只使用标准终端输出；需要 JUnit/coverage 时应调用既有分阶段入口。
    if config.getoption("xmlpath", default=None) or config.getoption("cov_source", default=None):
        raise pytest.UsageError("direct isolated integration requires separate phase report paths")
    assert raw_database_url is not None
    plan = selected_plan(nodes, config.invocation_params.args, config.invocation_params.dir)
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"Isolated integration selection: {len(plan.regular_nodes)} regular, {len(plan.lifecycle_nodes)} lifecycle")
        reporter.write_line("Parent runs no test functions; outcomes are reported by child phases")
    try:
        code = orchestrate_integration_tests(
            anchor=validate_test_database_url(raw_database_url), base_environment=os.environ,
            resources=SqlAlchemyIntegrationSuiteResources(ROOT), run_child=run_pytest_child,
            python_executable=sys.executable, repository_root=ROOT, selected=plan,
        )
    except Exception:  # noqa: BLE001 - 未知 DBAPI 文本不能进入 pytest 的真实数据库错误输出。
        pytest.exit("direct integration lifecycle verification failed", returncode=1)
    if code != 0:
        pytest.exit("isolated integration child returned a failure", returncode=code)
    if reporter is not None:
        # 选择数含 skip/xfail；只有 child 的真实 TestReport 能说明通过数量，parent 不造报告。
        reporter.write_line(f"Delegated {len(nodes)} selected nodes; child phases succeeded; anchor verified")
    return True
