"""以两次短事务持久化本人日历可用性建议。"""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from ai_employee.application.use_cases.calendar_proposals import (
    CalendarAvailabilityRead,
    CalendarProposalSnapshot,
)
from ai_employee.infrastructure.db.repositories.calendar import (
    SqlAlchemyCalendarSyncRepository,
)
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher


class SqlAlchemyCalendarAvailabilityRepository:
    """在独立短读/短写事务间传递冻结应用 DTO。

    本 adapter 不运行候选算法，也不把 ORM 实例带出事务。读阶段获取当前 proposal/version、
    用户设置、连接能力、目录、游标和最小事件投影；应用层关闭事务后计算；写阶段只通过
    ``expected_version`` CAS 保存新的完整 desired snapshot。
    """

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        action_cipher: ActionPayloadCipher,
    ) -> None:
        """绑定短事务会话工厂与日历 snapshot AEAD 加密器。

        Args:
            session_factory: 每次 ``begin`` 创建独立事务的受管工厂。
            action_cipher: 与日历提案仓储共享的记录绑定 payload cipher。
        """
        self._session_factory = session_factory
        self._action_cipher = action_cipher

    async def load_suggestion(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        observed_at: datetime,
        search_start: datetime,
        horizon_days: int,
    ) -> CalendarAvailabilityRead | None:
        """在一个短读事务中冻结 proposal 与本人可用性事实。

        Returns:
            用户拥有的冻结 DTO；提案或用户不存在时返回 ``None``。上下文离开本方法时
            事务已提交，调用方可以安全执行任意纯 CPU 计算。
        """
        async with self._session_factory.begin() as session:
            proposal = await SqlAlchemyCalendarProposalRepository(
                session, self._action_cipher
            ).get_current(user_id=user_id, proposal_id=proposal_id)
            if proposal is None:
                return None
            context = await SqlAlchemyCalendarSyncRepository(
                session
            ).get_availability_context(
                user_id=user_id,
                observed_at=observed_at,
                search_start=search_start,
                horizon_days=horizon_days,
            )
            if context is None:
                return None
            return CalendarAvailabilityRead(proposal=proposal, context=context)

    async def save_suggestion(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        desired_state: Mapping[str, object],
        retain_until: datetime,
    ) -> CalendarProposalSnapshot | None:
        """在新短写事务中以 expected-version CAS 保存候选缓存。

        ``SqlAlchemyCalendarProposalRepository`` 在版本已变化时抛出稳定
        ``proposal_version_conflict``；本层不重读、重算或静默覆盖并发编辑。
        """
        async with self._session_factory.begin() as session:
            return await SqlAlchemyCalendarProposalRepository(
                session, self._action_cipher
            ).save_next_version(
                snapshot_id=snapshot_id,
                user_id=user_id,
                proposal_id=proposal_id,
                expected_version=expected_version,
                desired_state=desired_state,
                retain_until=retain_until,
            )


__all__ = ["SqlAlchemyCalendarAvailabilityRepository"]
