"""定义隐私删除请求的窄用例，确保 API 不执行长时间删除且重试保持幂等。"""

import hashlib
from uuid import UUID

from ai_employee.application.use_cases.tasks import CreateTaskResult, CreateTaskUseCase

_ALL_DATA_DELETION_REQUEST_ID_DOMAIN = b"AIEMPLOYEE/privacy-delete-all-data-request-id/v1\x00"


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
