"""证明直接 pytest 选择在既有两阶段生命周期内不丢失、不放宽且保留失败状态。"""

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from tests.integration import integration_suite
from tests.unit.test_run_integration_tests import _FakeResources, _validated_anchor

REGULAR = "backend/tests/integration/api/test_connections.py::test_synthetic_regular"
LIFECYCLE = "backend/tests/integration/db/test_migrations.py::test_synthetic_lifecycle"
UNIT = "backend/tests/unit/application/test_calendar_event_aad.py::test_synthetic_vector"


def _selection_module() -> ModuleType:
    """实现缺失时给出行为契约 RED，避免把导入失败误记为数据库 guard 缺陷。"""
    name = "tests.integration.pytest_selection"
    assert importlib.util.find_spec(name) is not None, "direct selection adapter is absent"
    return importlib.import_module(name)


def test_selected_partition_is_an_ordered_disjoint_cover_of_actual_collection() -> None:
    """参数化 node、两个固定空库 node 与普通单测都只进入其所属 phase 一次。"""
    module = _selection_module()
    selected = (REGULAR + "[one]", LIFECYCLE, UNIT, *integration_suite.LIFECYCLE_NODE_TARGETS)
    regular, lifecycle = module.partition_nodes(selected)
    assert regular == (selected[0], UNIT)
    assert lifecycle == (LIFECYCLE, *integration_suite.LIFECYCLE_NODE_TARGETS)
    assert set(regular).isdisjoint(lifecycle)
    assert set(regular) | set(lifecycle) == set(selected)


@pytest.mark.parametrize("mutation", ["missing", "extra", "reordered", "wrong_phase"])
def test_child_cannot_change_the_parent_selection(mutation: str) -> None:
    """摘要同时绑定所选 nodeid 和顺序；过滤、增加或挪动用例不能得到绿色退出。"""
    module = _selection_module()
    expected = (REGULAR, UNIT)
    candidates = {
        "missing": (REGULAR,),
        "extra": (*expected, REGULAR + "[extra]"),
        "reordered": (UNIT, REGULAR),
        "wrong_phase": (LIFECYCLE,),
    }
    with pytest.raises(ValueError, match="selection"):
        module.verify_child_selection(candidates[mutation], "regular", module.selection_digest(expected))
    assert module.verify_child_selection((*expected, LIFECYCLE), "regular", module.selection_digest(expected)) == expected


@pytest.mark.parametrize("child_phase", [None, "regular", "lifecycle"])
def test_delegation_requires_exact_release_environment_and_no_child_reentry(child_phase: str | None) -> None:
    """其他环境、纯单测、纯 lifecycle 和带内部 phase 的孩子都不会再次创建数据库。"""
    module = _selection_module()
    exact = module.DIRECT_RELEASE_DATABASE_URL
    assert module.should_delegate((REGULAR, LIFECYCLE), exact, child_phase) is (child_phase is None)
    assert not module.should_delegate((REGULAR,), "different-synthetic-target", child_phase)
    assert not module.should_delegate((UNIT,), exact, child_phase)
    assert not module.should_delegate((LIFECYCLE,), exact, child_phase)


@pytest.mark.parametrize("failed_phase,code", [("regular", 0), ("regular", 1), ("regular", 2), ("regular", 5), ("lifecycle", 3)])
def test_selected_orchestrator_preserves_cli_cwd_database_boundaries_and_exit_code(failed_phase: str, code: int) -> None:
    """任何非零退出都先经过原 cleanup；原 CLI、cwd、环境选择与 child 计数不被重写。"""
    module = _selection_module()
    events: list[str] = []
    invocations: list[integration_suite.PytestInvocation] = []
    arguments = (REGULAR.split("::")[0], LIFECYCLE.split("::")[0], "-q", "-k", "synthetic", "--tb=short")
    plan = module.selected_plan((REGULAR, LIFECYCLE), arguments, Path("/synthetic/invocation"))

    def run_child(invocation: integration_suite.PytestInvocation, lease: object) -> int:
        """以确定性退出替代 OS 子进程，lease 与 DB fixture 仍使用既有端口。"""
        del lease
        invocations.append(invocation)
        events.append(f"child-{invocation.phase}")
        return code if invocation.phase == failed_phase else 0

    result = integration_suite.orchestrate_integration_tests(
        anchor=_validated_anchor(), base_environment={"SYNTHETIC_SENTINEL": "preserved"},
        resources=_FakeResources(events), run_child=run_child,
        python_executable="/synthetic/python", repository_root=Path("/synthetic/repository"),
        selected=plan,
    )
    assert result == code
    assert "regular-cleanup" in events and events[-1] == "suite-lock-release"
    if failed_phase == "regular" and code:
        assert len(invocations) == 1
    else:
        assert [item.phase for item in invocations] == ["regular", "lifecycle"]
    for invocation in invocations:
        assert invocation.argv[3:3 + len(arguments)] == arguments
        assert invocation.cwd == plan.cwd
        assert invocation.environment["SYNTHETIC_SENTINEL"] == "preserved"
        assert invocation.environment["DATABASE_URL"] == invocation.environment["TEST_DATABASE_URL"]
        assert f"--ai-employee-integration-child={invocation.phase}" in invocation.argv
        expected = (REGULAR,) if invocation.phase == "regular" else (LIFECYCLE,)
        assert f"--ai-employee-integration-selection={module.selection_digest(expected)}" in invocation.argv


def test_regular_only_selection_never_runs_an_empty_lifecycle_child() -> None:
    """实际选择只有 regular 时不会用空 lifecycle 的退出码5掩盖真实结果。"""
    module = _selection_module()
    invocations: list[integration_suite.PytestInvocation] = []

    def run_child(invocation: integration_suite.PytestInvocation, lease: object) -> int:
        del lease
        invocations.append(invocation)
        return 0

    result = integration_suite.orchestrate_integration_tests(
        anchor=_validated_anchor(), base_environment={}, resources=_FakeResources([]),
        run_child=run_child, python_executable="/synthetic/python", repository_root=Path("/synthetic/repository"),
        selected=module.selected_plan((REGULAR,), (REGULAR, "-q"), Path("/synthetic/invocation")),
    )
    assert result == 0 and [item.phase for item in invocations] == ["regular"]
