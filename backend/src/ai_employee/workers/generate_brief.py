"""为 daily_brief 任务提供独立的持久化 worker 边界。"""

from uuid import UUID

from ai_employee.application.use_cases.briefs import PersistDailyBriefUseCase
from ai_employee.domain.briefs import DailyBriefContent


async def persist_generated_brief(*, use_case: PersistDailyBriefUseCase, user_id: UUID, task_id: UUID, content: DailyBriefContent, markdown: str) -> UUID:
    """把已由 Graph 生成并验证的结果写入事务；Graph 网络调用不在事务中执行。"""
    return await use_case.execute(user_id=user_id, task_id=task_id, content=content, markdown=markdown)
