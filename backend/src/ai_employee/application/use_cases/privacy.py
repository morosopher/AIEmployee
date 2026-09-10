"""定义隐私删除请求的窄用例，确保 API 不执行长时间删除且重试保持幂等。"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_employee.application.ports.trusted_actions import (
    ProviderWriteOutcome,
    TrustedActionDispatchSnapshot,
)
from ai_employee.application.use_cases.tasks import CreateTaskResult, CreateTaskUseCase
from ai_employee.domain.tasks import JsonValue

_ALL_DATA_DELETION_REQUEST_ID_DOMAIN = b"AIEMPLOYEE/privacy-delete-all-data-request-id/v1\x00"
PRIVACY_DELETION_STARTED_EVENT_TYPE = "privacy.deletion_started"
PRIVACY_DELETION_STARTED_SCHEMA_VERSION = "privacy_deletion_started.v1"


@dataclass(frozen=True, slots=True)
class PrivacyDeletionStartedFact:
    """提供不含 ORM/内容的删除屏障事实，保留外层归属以防 metadata 冒充授权。"""

    event_type: str
    user_id: UUID
    task_id: UUID | None
    event_metadata: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class PrivacyDeletionStartedAuthority:
    """表示唯一 true→false CAS 赢家绑定的不可变用户、任务及原始删除请求。"""

    user_id: UUID
    task_id: UUID
    request_id: str


@dataclass(frozen=True, slots=True)
class PrivacyDeletionBinding:
    """保存一次删除尝试的租约身份；它不替代数据库中完整 started 集和活租约检查。"""

    user_id: UUID
    task_id: UUID
    request_id: str
    lease_owner: str


@dataclass(frozen=True, slots=True)
class PrivacyReconciliationTarget:
    """只传递冻结执行投影与已持久化的最小资源定位，不携带审批命令或内容。"""

    dispatch: TrustedActionDispatchSnapshot
    resource_id: str | None


class PrivacyReconciliationReader(Protocol):
    """已获单次资格的隐私读取端口；没有写、refresh、授权码或命令解密入口。"""

    async def reconcile(
        self,
        *,
        binding: PrivacyDeletionBinding,
        target: PrivacyReconciliationTarget,
    ) -> ProviderWriteOutcome:
        """重验精确用户/执行/连接，至多一个有界 GET；不完整证据只能返回 unknown。"""


class PrivacyCheckpointCleaner(Protocol):
    """通过已有 app/checkpointer 权限删除规范任务 thread 的窄能力。"""

    async def clear_thread(
        self,
        *,
        binding: PrivacyDeletionBinding,
        task_id: UUID,
        now: datetime,
    ) -> None:
        """同事务证明赢家活租约和 thread 归属，再显式删除三张数据表；失败保留父任务。"""


class ExpiredTaskCheckpointCleaner(Protocol):
    """普通历史清理持有 Task→active user 锁期间使用的 app 三表删除能力。

    只由历史 Repository 的同一未提交事务调用，直到父任务删除提交前不能释放锁。
    实现不得重获调用者持有的行锁，也不能删除业务父表或扩大 retention 权限。
    """

    async def clear_expired_thread(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        cutoff: datetime,
    ) -> None:
        """精确只读重验归属、终态、截止及 active/authority，再提交原生三表删除。"""


def parse_privacy_deletion_started_authority(
    facts: Sequence[PrivacyDeletionStartedFact],
    *,
    expected_user_id: UUID,
    expected_task_id: UUID,
    expected_request_id: str,
) -> PrivacyDeletionStartedAuthority | None:
    """严格解析完整 per-user started 集合；不择优、不强转、不吞掉冲突事实。

    Args:
        facts: 调用者按用户读取的全部 started 事实，不得预先按 task/request 过滤。
        expected_user_id: 当前锁定的任务所属用户。
        expected_task_id: 当前锁定的预屏障删除任务。
        expected_request_id: 原任务输入中的非空删除请求标识。

    Returns:
        只有单行、外层归属及关闭 metadata 全部精确匹配时返回授权；其余返回 None。
        本纯函数是屏障、租约、恢复扫描和最终删除的共享判定，SQL 预筛不能替代它。
    """
    if len(facts) != 1 or not expected_request_id:
        return None
    fact = facts[0]
    expected_metadata: dict[str, JsonValue] = {
        "schema_version": PRIVACY_DELETION_STARTED_SCHEMA_VERSION,
        "request_id": expected_request_id,
    }
    if (
        fact.event_type != PRIVACY_DELETION_STARTED_EVENT_TYPE
        or fact.user_id != expected_user_id
        or fact.task_id != expected_task_id
        or fact.event_metadata != expected_metadata
    ):
        return None
    return PrivacyDeletionStartedAuthority(
        user_id=expected_user_id,
        task_id=expected_task_id,
        request_id=expected_request_id,
    )


class RequestSourceCacheDeletionUseCase:
    """创建仅清除可重新同步来源缓存的耐久任务。"""

    def __init__(self, creator: CreateTaskUseCase) -> None:
        """注入统一任务创建用例，复用事务与 Outbox 幂等保证。"""
        self._creator = creator

    async def execute(self, *, user_id: UUID, idempotency_key: str) -> CreateTaskResult:
        """提交 source cache 删除意图，不读取或暴露来源内容。"""
        return await self._creator.execute(
            user_id=user_id,
            kind="privacy.clear_source_cache",
            input_payload={},
            idempotency_key=idempotency_key,
        )


class RequestAllDataDeletionUseCase:
    """创建不可逆全数据删除任务；确认短语只在 API 验证，绝不持久化。"""

    def __init__(self, creator: CreateTaskUseCase) -> None:
        """注入统一任务创建用例，避免路由直接接触数据库。"""
        self._creator = creator

    async def execute(self, *, user_id: UUID, idempotency_key: str) -> CreateTaskResult:
        """提交可重试的全量删除请求，仅持久化不含身份内容的 opaque request ID。

        ``deletion_request_id`` 必须在同一用户携带同一 ``Idempotency-Key`` 重试时保持不变，
        否则严格的任务输入绑定会把正常重试误判为载荷冲突。请求 ID 由稳定的用户范围输入
        派生，而不是在路由层生成随机 UUID；``deletion_request_id`` 字段只保存摘要，不会
        在任务载荷或错误响应中复制原始用户标识和客户端键（任务本身仍按既有契约保存用户归属
        与客户端幂等键列）。
        """
        request_id = _derive_all_data_deletion_request_id(
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
        return await self._creator.execute(
            user_id=user_id,
            kind="privacy.delete_all_data",
            input_payload={"deletion_request_id": request_id},
            idempotency_key=idempotency_key,
        )


def _derive_all_data_deletion_request_id(*, user_id: UUID, idempotency_key: str) -> str:
    """从用户和幂等键确定性派生全数据删除请求 ID。

    两个输入使用固定 domain separation 和独立的 ``uint32`` 大端长度帧，避免简单分隔符
    拼接产生的边界碰撞（例如一个字段的后缀被误当作另一个字段的前缀）。UUID 使用其
    canonical 二进制表示，幂等键使用严格 UTF-8；最终只返回 SHA-256 的小写十六进制摘要，
    使持久化的 request ID 既稳定又不直接暴露输入内容。

    Args:
        user_id: 当前认证用户的 UUID，作为幂等作用域的一部分。
        idempotency_key: 当前请求的客户端幂等键；API 已在路由边界验证其非空和长度。

    Returns:
        固定 64 个字符的小写十六进制 SHA-256 摘要。
    """
    user_bytes = user_id.bytes
    key_bytes = idempotency_key.encode("utf-8", errors="strict")
    framed = b"".join(len(raw).to_bytes(4, "big") + raw for raw in (user_bytes, key_bytes))
    return hashlib.sha256(_ALL_DATA_DELETION_REQUEST_ID_DOMAIN + framed).hexdigest()
