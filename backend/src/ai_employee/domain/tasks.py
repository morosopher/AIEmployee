"""定义可信任务状态、确定性迁移规则与冻结审批提案。"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final, cast

from ai_employee.domain.errors import StateConflictError

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class TaskStatus(StrEnum):
    """任务从创建、执行、等待到终止的可持久化状态。"""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    """任务步骤在执行时间线中的可持久化状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApprovalStatus(StrEnum):
    """审批请求从待决定到终止的可持久化状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class InvalidTaskTransition(StateConflictError):
    """表示当前任务状态不允许迁移到请求目标状态。"""

    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        """构造不含业务载荷的稳定任务状态冲突。

        Args:
            current: 任务当前状态。
            target: 调用方请求的目标状态。
        """
        super().__init__(
            error_code="invalid_task_transition",
            message=f"{current.value} cannot transition to {target.value}",
        )


# 外层只读映射与内层 frozenset 共同保证运行期不能改写状态机白名单。
ALLOWED_TRANSITIONS: Final[Mapping[TaskStatus, frozenset[TaskStatus]]] = MappingProxyType(
    {
        TaskStatus.CREATED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
        TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
        TaskStatus.RUNNING: frozenset(
            {
                TaskStatus.WAITING_APPROVAL,
                TaskStatus.RETRY_SCHEDULED,
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.WAITING_APPROVAL: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
        TaskStatus.RETRY_SCHEDULED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
        TaskStatus.SUCCEEDED: frozenset(),
        TaskStatus.FAILED: frozenset(),
        TaskStatus.CANCELLED: frozenset(),
    }
)


def transition_task(current: TaskStatus, target: TaskStatus) -> TaskStatus:
    """校验并返回一次纯任务状态迁移。

    该函数不执行持久化或 I/O；应用层后续负责把合法迁移与审计、事件写入
    同一事务。终态和自迁移没有白名单条目，因此统一按状态冲突拒绝。

    Args:
        current: 任务当前状态。
        target: 请求迁移到的目标状态。

    Returns:
        已通过白名单校验的目标状态。

    Raises:
        InvalidTaskTransition: 当前到目标的迁移不在批准状态图中。
    """
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTaskTransition(current, target)
    return target


def _validate_json_value(
    value: object,
    active_container_ids: set[int] | None = None,
) -> None:
    """递归校验审批载荷只含稳定标准 JSON 值。

    Python 编码器会把 tuple 等非 JSON 容器隐式转换成数组，也默认允许 NaN 与
    Infinity。审批哈希必须跨进程和语言保持一致，因此这里先以精确运行时类型
    拒绝这些扩展，再执行规范化序列化。

    Args:
        value: 当前需要校验的根值或嵌套值。
        active_container_ids: 当前递归路径上的容器标识，仅供内部循环检测使用。

    Raises:
        TypeError: 值、容器或对象键不是受支持的标准 JSON 类型。
        ValueError: 浮点值为 NaN/无穷值，或容器包含循环引用。
    """
    if value is None or type(value) in {str, int, bool}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("approval payload JSON numbers must be finite")
        return
    if type(value) not in {list, dict}:
        raise TypeError("approval payload must contain only standard JSON values")

    container_ids = active_container_ids if active_container_ids is not None else set()
    container_id = id(value)
    if container_id in container_ids:
        raise ValueError("approval payload must not contain cyclic JSON containers")

    # 只记录当前祖先链，允许同一个无环子对象在不同分支重复出现并得到相同 JSON。
    container_ids.add(container_id)
    try:
        if type(value) is list:
            for item in cast(list[object], value):
                _validate_json_value(item, container_ids)
        else:
            for key, item in cast(dict[object, object], value).items():
                if type(key) is not str:
                    raise TypeError("approval payload JSON object keys must be strings")
                _validate_json_value(item, container_ids)
    finally:
        container_ids.remove(container_id)


def _canonicalize_payload(payload: dict[str, JsonValue]) -> str:
    """把合法审批载荷编码为唯一、可哈希的 JSON 文本。

    Args:
        payload: 以 JSON 对象为根的待审批参数。

    Returns:
        UTF-8 语义下保留 Unicode、排序键且无多余空白的 JSON 文本。

    Raises:
        TypeError: 载荷包含非 JSON 数据或非字符串对象键。
        ValueError: 载荷包含 NaN、无穷值或循环容器。
    """
    _validate_json_value(payload)
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class ApprovalProposal:
    """绑定操作名、精确冻结载荷、哈希与初始审批状态的提案。

    ``frozen=True`` 只能阻止字段重新赋值，不能冻结字段内部的 ``dict`` 或
    ``list``。因此对象内部只保存规范 JSON 字符串这一不可变值，公开
    :attr:`payload` 每次解码为新的 JSON 对象。这样调用方修改原始容器或公开
    副本都不会改变内部事实，同时 Task 6 可直接使用 ``payload`` 返回值写入
    JSONB，无需引入通用序列化框架。
    """

    action: str
    payload_hash: str
    status: ApprovalStatus
    _canonical_payload: str = field(repr=False)

    @property
    def payload(self) -> dict[str, JsonValue]:
        """返回可供 JSONB 持久化的独立载荷副本。

        Returns:
            从内部规范 JSON 解码得到的新字典；修改它不会影响提案内部状态。
        """
        # 规范文本只由 ``create`` 在递归校验后生成，因此此处收窄解析边界是安全的。
        return cast(dict[str, JsonValue], json.loads(self._canonical_payload))

    @classmethod
    def create(
        cls,
        action: str,
        payload: dict[str, JsonValue],
    ) -> "ApprovalProposal":
        """冻结待审批载荷并计算稳定 SHA-256 十六进制哈希。

        Args:
            action: 稳定英文工具动作名。
            payload: 只包含标准 JSON 值的完整待执行参数。

        Returns:
            状态为 ``PENDING`` 且与精确规范载荷绑定的不可变审批提案。

        Raises:
            TypeError: 载荷包含非 JSON 数据或非字符串对象键。
            ValueError: 载荷包含 NaN、无穷值或循环容器。
        """
        canonical_payload = _canonicalize_payload(payload)
        payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        return cls(
            action=action,
            payload_hash=payload_hash,
            status=ApprovalStatus.PENDING,
            _canonical_payload=canonical_payload,
        )
