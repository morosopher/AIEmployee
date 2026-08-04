"""定义隐私删除请求的窄用例，确保 API 不执行长时间删除。"""

from uuid import UUID

from ai_employee.application.use_cases.tasks import CreateTaskResult, CreateTaskUseCase


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

    async def execute(self, *, user_id: UUID, idempotency_key: str, request_id: str) -> CreateTaskResult:
        """提交可重试的全量删除请求，仅持久化不含身份内容的 opaque request ID。"""
        return await self._creator.execute(
            user_id=user_id,
            kind="privacy.delete_all_data",
            input_payload={"deletion_request_id": request_id},
            idempotency_key=idempotency_key,
        )
