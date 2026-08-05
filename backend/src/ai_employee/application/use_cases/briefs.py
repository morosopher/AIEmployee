"""定义每日简报版本化持久化用例及其应用层端口。"""

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol
from uuid import UUID

from ai_employee.domain.briefs import DailyBriefContent


class DailyBriefPersistenceStore(Protocol):
    """定义一次简报事务中必须共同完成的持久化能力。"""

    async def persist(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        content: DailyBriefContent,
        markdown: str,
        email_analyses: tuple[dict[str, Any], ...],
        model_invocations: tuple[dict[str, Any], ...],
    ) -> UUID:
        """原子写入版本、条目、判断、模型审计和任务结果。"""


class DailyBriefPersistenceStoreFactory(Protocol):
    """为一次用例调用提供提交或回滚受控的简报事务。"""

    def __call__(self) -> AbstractAsyncContextManager[DailyBriefPersistenceStore]:
        """创建仅覆盖当前写入意图的事务上下文。"""


class PersistDailyBriefUseCase:
    """协调简报持久化意图，不依赖 ORM、SQL 或具体数据库会话。"""

    def __init__(self, stores: DailyBriefPersistenceStoreFactory) -> None:
        """注入隐藏 SQLAlchemy 的事务型持久化端口。"""
        self._stores = stores

    async def execute(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        content: DailyBriefContent,
        markdown: str,
        email_analyses: tuple[dict[str, Any], ...] = (),
        model_invocations: tuple[dict[str, Any], ...] = (),
    ) -> UUID:
        """写入下一版本或复用同一任务已经持久化的简报。

        手动刷新与重复 Worker 恢复均使用稳定 ``task_id``；端口实现负责在同一事务内
        分配版本、写入完整结果和审计事件，避免至少一次投递产生第二份业务事实。
        """
        async with self._stores() as store:
            return await store.persist(
                user_id=user_id,
                task_id=task_id,
                content=content,
                markdown=markdown,
                email_analyses=email_analyses,
                model_invocations=model_invocations,
            )
