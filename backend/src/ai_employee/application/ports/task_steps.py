"""Agent 节点写入持久任务时间线的应用层端口。"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TaskStepEvent:
    """不含正文或凭据的节点生命周期事件。"""

    task_run_id: str
    step_name: str
    status: str


class TaskStepEventSink(Protocol):
    """由 Worker 基础设施实现的持久时间线写入边界。"""

    def record(self, event: TaskStepEvent) -> None:
        """持久记录事件；实现负责事务与幂等，不接收源正文。"""
