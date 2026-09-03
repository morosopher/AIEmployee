"""定义统一操作视图及未知结果的人工/用户核对用例。

本模块只暴露稳定的标识、状态和枚举；邮件正文、地址、日程描述以及供应商原始响应均
不经过这些接口。具体锁序、CAS、审计和 Outbox 原子写入由基础设施层的 action-view
repository 完成。
"""

import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_employee.application.use_cases.task_execution import utc_instant

POSTGRESQL_BIGINT_MAX = 2**63 - 1
_CANONICAL_CURSOR = re.compile(r"^(0|[1-9][0-9]*)$")
MANUAL_RESOLUTIONS = frozenset({"confirmed_executed", "confirmed_not_executed"})


def parse_canonical_cursor(value: object, *, field: str = "task_version") -> int:
    """验证并解析 PostgreSQL BIGINT 范围内的规范十进制游标。

    Args:
        value: API/浏览器传入的字符串；整数、前导零和符号形式都拒绝。
        field: 错误消息中使用的稳定字段名。

    Returns:
        已通过格式与范围检查的 Python ``int``。

    Raises:
        TypeError: 输入不是字符串。
        ValueError: 不是规范非负十进制，或超出 BIGINT 上限。
    """
    if type(value) is not str:
        raise TypeError(f"{field} must be a canonical decimal string")
    if _CANONICAL_CURSOR.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical decimal string")
    max_text = str(POSTGRESQL_BIGINT_MAX)
    if len(value) > len(max_text) or (len(value) == len(max_text) and value > max_text):
        raise ValueError(f"{field} exceeds the supported cursor range")
    # 只在正则与固定长度检查之后转换，避免不受控的大整数文本触发运行时限制。
    return int(value)


def canonical_cursor(value: object, *, field: str = "task_version") -> str:
    """返回经过统一规则验证的游标原文。

    ``ActionSnapshot.event_cursor`` 与 ``task_version`` 使用同一表示；该函数用于输出
    端口也用于输入端，确保前端不会因 JavaScript number 精度而改变 BIGINT。
    """
    parsed = parse_canonical_cursor(value, field=field)
    return str(parsed)


# 便于调用方按语义命名而不复制 parser；所有别名共享同一范围/前导零规则。
validate_task_version = canonical_cursor
parse_task_version = parse_canonical_cursor


@dataclass(frozen=True, slots=True)
class ActionSnapshot:
    """统一操作视图的最小稳定投影。

    这是 Task 26 API 的前向兼容 DTO：当前任务只需要返回状态、错误、尝试次数和同一
    审计游标，后续可在不改变人工 CAS 契约的情况下扩展脱敏 provider link。
    """

    task_id: UUID
    status: str
    error_code: str | None
    event_cursor: str
    task_version: str
    reconciliation_attempt_count: int = 0
    provider_url: str | None = None

    def __post_init__(self) -> None:
        """要求两个公开游标均为同一规范字符串。"""
        event_cursor = canonical_cursor(self.event_cursor, field="event_cursor")
        task_version = canonical_cursor(self.task_version, field="task_version")
        if event_cursor != task_version:
            raise ValueError("event_cursor and task_version must match")
        if (
            type(self.reconciliation_attempt_count) is not int
            or self.reconciliation_attempt_count < 0
        ):
            raise ValueError("reconciliation_attempt_count must be non-negative")
        object.__setattr__(self, "event_cursor", event_cursor)
        object.__setattr__(self, "task_version", task_version)


class ActionViewTransaction(Protocol):
    """定义人工结果与用户触发核对的事务端口。"""

    async def resolve_manual(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        task_version: int,
        resolution: str,
        resolved_at: datetime,
    ) -> int:
        """在固定锁序内执行人工 CAS，并返回新审计 ID。"""

    async def request_reconciliation(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        requested_at: datetime,
    ) -> UUID:
        """把 needs-attention 重新打开为只读核对并返回原 ToolExecution ID。"""


class ActionViewTransactionFactory(Protocol):
    """为一次操作视图 mutation 创建自动提交/回滚事务。"""

    def __call__(self) -> AbstractAsyncContextManager[ActionViewTransaction]:
        """返回短事务上下文。"""


class ManualResolutionUseCase:
    """记录人工确认结果，并保证不会重新调用供应商写接口。"""

    def __init__(self, transactions: ActionViewTransactionFactory) -> None:
        """注入负责锁序与持久化的事务工厂。"""
        self._transactions = transactions

    async def execute(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        task_version: str,
        resolution: str,
        now: datetime,
    ) -> str:
        """以审计游标 CAS 原子记录人工结论。

        Args:
            user_id: 当前认证用户；repository 仍会再次显式过滤归属。
            task_id: needs-attention 可信动作任务。
            task_version: 当前操作快照的 canonical decimal-string 游标。
            resolution: 仅 ``confirmed_executed`` 或 ``confirmed_not_executed``。
            now: 带时区人工确认时间。

        Returns:
            新 ``tool.manually_resolved`` 审计 ID 的 canonical decimal-string。

        Raises:
            ValueError/TypeError: 枚举、游标或时间不符合边界。
            StateConflictError: 游标过期、任务已被自动核对或跨用户不可见。
        """
        if type(resolution) is not str or resolution not in MANUAL_RESOLUTIONS:
            raise ValueError("resolution must be confirmed_executed or confirmed_not_executed")
        parsed_version = parse_canonical_cursor(task_version)
        resolved_at = utc_instant(now, field="now")
        async with self._transactions() as transaction:
            audit_id = await transaction.resolve_manual(
                user_id=user_id,
                task_id=task_id,
                task_version=parsed_version,
                resolution=resolution,
                resolved_at=resolved_at,
            )
        if type(audit_id) is not int or audit_id <= 0 or audit_id > POSTGRESQL_BIGINT_MAX:
            raise RuntimeError("manual resolution returned an invalid audit id")
        return str(audit_id)


class RequestActionReconciliationUseCase:
    """响应用户“再次核对”请求，只建立只读任务，不打开任何写权限。"""

    def __init__(self, transactions: ActionViewTransactionFactory) -> None:
        """注入负责状态/CAS/Outbox 原子写入的事务工厂。"""
        self._transactions = transactions

    async def execute(self, *, user_id: UUID, task_id: UUID, now: datetime) -> UUID:
        """把 needs-attention 任务转回 reconciling 并返回原执行 ID。"""
        requested_at = utc_instant(now, field="now")
        async with self._transactions() as transaction:
            return await transaction.request_reconciliation(
                user_id=user_id,
                task_id=task_id,
                requested_at=requested_at,
            )


__all__ = [
    "MANUAL_RESOLUTIONS",
    "POSTGRESQL_BIGINT_MAX",
    "ActionSnapshot",
    "ActionViewTransaction",
    "ActionViewTransactionFactory",
    "ManualResolutionUseCase",
    "RequestActionReconciliationUseCase",
    "canonical_cursor",
    "parse_canonical_cursor",
    "parse_task_version",
    "validate_task_version",
]
