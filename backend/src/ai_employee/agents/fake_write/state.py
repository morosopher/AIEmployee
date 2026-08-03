"""定义假写审批 Graph 的跨节点状态。"""

from typing import TypedDict


class FakeWriteState(TypedDict):
    """在 checkpoint 中保存假写提案、决定与无敏感执行标记。"""

    task_id: str
    proposal_payload: dict[str, object]
    approval_decision: str | None
    tool_called: bool
    messages: list[str]
