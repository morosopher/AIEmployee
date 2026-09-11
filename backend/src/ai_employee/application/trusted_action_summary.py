"""定义可信动作步骤的内容无关展示索引；该投影从不参与执行授权。

只接受现行四字段或历史精确两字段。任何损坏都显式不可用，不能以当前编辑对象或
尚存的命令密文修补新格式。JSON 边界采用精确类型检查，防止 bool/字符串冒充版本。
"""

from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from ai_employee.domain.tasks import JsonValue

type TrustedActionName = Literal[
    "mail.send", "calendar.create", "calendar.update", "calendar.restore"
]
SUMMARY_VERSION = "trusted_action_step.v1"
_ACTIONS = frozenset({"mail.send", "calendar.create", "calendar.update", "calendar.restore"})
_LEGACY_FIELDS = {"action", "proposal_version"}
_INDEXED_FIELDS = _LEGACY_FIELDS | {"summary_version", "frozen_connection_id"}


@dataclass(frozen=True, slots=True)
class TrustedActionStepSummary:
    """保存已严格解析的展示身份；无 connection 只表示精确旧格式。

    ``frozen_connection_id`` 不是审批凭据。新写入必须携带它；历史升级前只有旧格式可以
    使用尚未换绑的本地对象账户。步骤随原 TaskRun 及隐私清理生命周期删除。
    """

    action: TrustedActionName
    proposal_version: int
    frozen_connection_id: UUID | None

    def as_json(self) -> dict[str, JsonValue]:
        """生成固定字段集合，不携带地址、内容或额外执行元数据。"""
        result: dict[str, JsonValue] = {
            "action": self.action,
            "proposal_version": self.proposal_version,
        }
        if self.frozen_connection_id is not None:
            result.update(
                summary_version=SUMMARY_VERSION,
                frozen_connection_id=str(self.frozen_connection_id),
            )
        return result


def parse_trusted_action_step_summary(value: object) -> TrustedActionStepSummary | None:
    """解析数据库 JSONB 边界；缺字段、额外字段、坏类型或未知版本返回不可用。

    Args:
        value: 原始 TaskStep.input_summary，不能先删除未知字段或做宽松类型转换。

    Returns:
        严格索引/精确 legacy DTO，或 ``None``。UUID 要求规范字符串，SQL 可使用安全
        字符串等值匹配，绝不对任意 JSON 字符串进行可能抛异常的数据库 cast。
    """
    if not isinstance(value, dict) or set(value) not in (_LEGACY_FIELDS, _INDEXED_FIELDS):
        return None
    action, version = value.get("action"), value.get("proposal_version")
    if type(action) is not str or action not in _ACTIONS or type(version) is not int or version < 1:
        return None
    connection_id = None
    if set(value) == _INDEXED_FIELDS:
        raw_connection = value.get("frozen_connection_id")
        if value.get("summary_version") != SUMMARY_VERSION or type(raw_connection) is not str:
            return None
        try:
            connection_id = UUID(raw_connection)
        except ValueError:
            return None
        if str(connection_id) != raw_connection:
            return None
    return TrustedActionStepSummary(cast(TrustedActionName, action), version, connection_id)
