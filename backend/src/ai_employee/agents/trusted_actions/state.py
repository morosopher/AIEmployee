"""定义可信动作 Graph 可进入 PostgreSQL checkpoint 的最小状态。"""

from typing import Literal, TypedDict


class TrustedActionState(TypedDict):
    """仅保存稳定标识、哈希、审批决定和无内容流程消息。

    完整命令、密文、正文、地址、标题、日程描述和租约 owner 都不得进入该类型。
    Worker 在每次图调用时从进程内依赖注入当前 owner，并仅在 claim 后紧邻供应商
    dispatch 的受控内存中解密命令。
    """

    task_id: str
    approval_id: str
    operation_id: str
    payload_hash: str
    decision: Literal["approved", "rejected"] | None
    messages: list[str]


__all__ = ["TrustedActionState"]
