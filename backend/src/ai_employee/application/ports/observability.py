"""定义任务执行层可选使用的无内容可观测性端口。"""

from typing import Protocol


class TaskMetricsObserver(Protocol):
    """收敛耐久任务执行可安全发布的聚合指标。

    该端口刻意不接收用户标识、任务载荷、步骤输出或异常文本，使 application 层在不依赖
    Prometheus 的前提下仍可报告执行健康度。基础设施中的 ``Metrics`` 实现同名方法。
    """

    def record_task_outcome(self, *, kind: str, status: str, duration_seconds: float) -> None:
        """记录任务状态转换后的聚合耗时。"""

    def record_task_retry(self, *, kind: str) -> None:
        """记录一次已持久化的临时错误重试。"""

    def record_queue_wait(self, *, kind: str, seconds: float) -> None:
        """记录首次获得租约前的排队耗时。"""
