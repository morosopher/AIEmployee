"""为固定 Task13 直接调用划分已收集节点，复用唯一 integration suite 生命周期。"""

from collections.abc import Sequence
from pathlib import Path

from tests.integration.integration_suite import (
    LIFECYCLE_FILE_TARGETS,
    LIFECYCLE_NODE_TARGETS,
    IntegrationPhase,
    SelectedPytestPlan,
    selection_digest,
)

# 与 release wrapper 的准入常量一致；这里不是生产数据库选择器，也不更改任何环境。
DIRECT_RELEASE_DATABASE_URL = (
    "postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test"
)


def node_phase(node: str) -> IntegrationPhase:
    """只按原 fixed lifecycle catalog 分类；普通单测与 contract/eval 归入 regular。"""
    path = node.partition("::")[0]
    if path in LIFECYCLE_FILE_TARGETS or any(
        node == exact or node.startswith(exact + "[") for exact in LIFECYCLE_NODE_TARGETS
    ):
        return "lifecycle"
    return "regular"


def partition_nodes(nodes: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """稳定划分实际选择，保留参数化后顺序及重复项，不自行扩大目录或测试范围。"""
    return (tuple(node for node in nodes if node_phase(node) == "regular"),
            tuple(node for node in nodes if node_phase(node) == "lifecycle"))


def should_delegate(nodes: Sequence[str], database_url: str | None, child_phase: str | None) -> bool:
    """仅固定 release anchor 的直接 regular 集成选择需要 parent 拥有临时数据库。"""
    return child_phase is None and database_url == DIRECT_RELEASE_DATABASE_URL and any(
        node.startswith("backend/tests/integration/") and node_phase(node) == "regular"
        for node in nodes
    )


def selected_plan(nodes: Sequence[str], arguments: Sequence[str], cwd: Path) -> SelectedPytestPlan:
    """保存原 CLI/cwd 和已经过 pytest 全部选择规则的实际节点；拒绝空 regular。"""
    regular, lifecycle = partition_nodes(nodes)
    if not regular:
        raise ValueError("direct integration selection has no regular nodes")
    return SelectedPytestPlan(tuple(arguments), cwd, regular, lifecycle)


def verify_child_selection(nodes: Sequence[str], phase: IntegrationPhase, expected: str | None) -> tuple[str, ...]:
    """重入标记只选择 phase；缺失/增加/乱序均在测试执行前拒绝，绝不隐藏测试失败。

    原完整 suite 已通过固定 --ignore/--deselect 划分，不带摘要时还必须没有异相节点。
    直接调用则保留原完整参数，在此按同一 catalog 划分并核对 parent 的有序摘要。
    """
    selected = tuple(node for node in nodes if node_phase(node) == phase)
    if (expected is None and len(selected) != len(nodes)) or (
        expected is not None and selection_digest(selected) != expected
    ):
        raise ValueError("integration child selection differs from parent")
    return selected
