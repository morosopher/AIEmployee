"""以两次短事务持久化本人日历可用性建议。"""

from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag
from pydantic import ValidationError
from sqlalchemy import select

from ai_employee.application.use_cases.calendar_editor import (
    BeforeStatus,
    CalendarEditorRead,
    CalendarReprepareSourceProjection,
    editor_calendar_fields,
)
from ai_employee.application.use_cases.calendar_proposals import (
    CalendarAvailabilityRead,
    CalendarProposalContent,
    CalendarProposalSnapshot,
    CalendarProposalUseCase,
)
from ai_employee.domain.actions import CalendarProposalStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.sources import CalendarEventModel
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

    async def load_editor(
        self, *, user_id: UUID, proposal_id: UUID, observed_at: datetime
    ) -> CalendarEditorRead | None:
        """在单次本人短读中获取原 before 和完整编辑区间，不使用当前事件替代历史。

        同时返回上下文 DTO；冲突算法由应用层在本方法退出、事务释放后执行。
        未完整输入不读取冲突上下文，避免把空数组误认为无冲突。
        """
        async with self._session_factory.begin() as session:
            repository = SqlAlchemyCalendarProposalRepository(session, self._action_cipher)
            source = SqlAlchemyCalendarSyncRepository(session)
            proposal = await CalendarProposalUseCase(
                proposals=repository, calendar=source, clock=lambda: observed_at
            ).get(user_id=user_id, proposal_id=proposal_id)
            before = None
            before_content = None
            before_status: BeforeStatus = (
                "not_applicable" if proposal.operation_kind == "create" else "unavailable"
            )
            if proposal.operation_kind != "create" and proposal.before_snapshot_id is not None:
                try:
                    snapshot = await repository.load_snapshot(
                        user_id=user_id, snapshot_id=proposal.before_snapshot_id
                    )
                    if (
                        snapshot is not None
                        and snapshot.proposal_id == proposal_id
                        and snapshot.snapshot_kind == "before"
                        and snapshot.retain_until > observed_at
                    ):
                        before_content = CalendarProposalContent.model_validate(snapshot.content)
                        before = editor_calendar_fields(before_content)
                        if before is not None:
                            before_status = "available"
                except (InvalidTag, ValidationError, ValueError):
                    before = None
                except StateConflictError as error:
                    if error.error_code != "calendar_snapshot_unavailable":
                        raise
            context = None
            target_event = None
            if proposal.operation_kind != "create":
                # 同一次有界查询复用于冲突排除与重新准备。严格绑定本人和原三元身份，
                # 只读本地 UUID/ETag，不取供应商内容、不把当前事件充当原 before。
                target_event = (
                    await session.execute(
                        select(CalendarEventModel.id, CalendarEventModel.etag).where(
                            CalendarEventModel.user_id == user_id,
                            CalendarEventModel.connection_id == proposal.connection_id,
                            CalendarEventModel.calendar_id == proposal.calendar_id,
                            CalendarEventModel.provider_event_id == proposal.target_event_id,
                        )
                    )
                ).one_or_none()
            fields = editor_calendar_fields(proposal.content)
            if fields is not None:
                zone = ZoneInfo(fields.timezone)
                start = (
                    datetime.combine(date.fromisoformat(fields.starts_at), time(), zone)
                    if fields.all_day
                    else datetime.fromisoformat(fields.starts_at)
                )
                end = (
                    datetime.combine(date.fromisoformat(fields.ends_at), time(), zone)
                    if fields.all_day
                    else datetime.fromisoformat(fields.ends_at)
                )
                context = await source.get_availability_context(
                    user_id=user_id,
                    observed_at=observed_at,
                    search_start=start,
                    horizon_days=(end.astimezone(UTC) - start.astimezone(UTC)).days + 1,
                    excluded_event_id=target_event.id if target_event is not None else None,
                )
            restore_projection = None
            if (
                proposal.operation_kind == "update"
                and proposal.status is CalendarProposalStatus.APPLIED
                and before_status == "available"
                and proposal.before_snapshot_id is not None
            ):
                # 复用入队的本人来源读取；短事务内仅锁定现有事实，不访问供应商或创建任务。
                restore_projection = await repository.get_restore_source_projection(
                    user_id=user_id, source_snapshot_id=proposal.before_snapshot_id
                )
            reprepare_projection = (
                CalendarReprepareSourceProjection(
                    event_id=target_event.id,
                    current_etag=target_event.etag,
                    before_operation_id=before_content.operation_id,
                    before_source_event_ids=before_content.source_event_ids,
                )
                if target_event is not None
                and before_status == "available"
                and before_content is not None
                else None
            )
            return CalendarEditorRead(
                proposal, before, before_status, context, restore_projection, reprepare_projection
            )

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
            context = await SqlAlchemyCalendarSyncRepository(session).get_availability_context(
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
