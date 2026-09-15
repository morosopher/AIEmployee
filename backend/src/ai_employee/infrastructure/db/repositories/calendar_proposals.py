"""以加密不可变快照和 PostgreSQL CAS 持久化日历变更提案。"""

import hashlib
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hmac import compare_digest
from typing import cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.use_cases.calendar_proposals import (
    CalendarOperationKind,
    CalendarProposalSnapshot,
    CalendarProposalStateSnapshot,
    CalendarRestoreSourceProjection,
    CalendarRestoreSourceSnapshot,
    CalendarSnapshot,
    CalendarSnapshotKind,
)
from ai_employee.application.use_cases.tasks import CreateTaskResult
from ai_employee.domain.actions import (
    CalendarProposalStatus,
    transition_calendar_proposal,
)
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import JsonValue
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
)
from ai_employee.infrastructure.db.models.sources import CalendarEventModel
from ai_employee.infrastructure.db.repositories.historical_action_bindings import (
    preserve_historical_action_bindings,
)
from ai_employee.infrastructure.db.repositories.identity import lock_active_user
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepository
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.action_payloads import (
    ActionPayloadCipher,
    PreparedActionPayload,
    canonical_action_payload_json,
)

CALENDAR_SNAPSHOT_CONTENT_KIND = "calendar_snapshot"
CALENDAR_SNAPSHOT_ACTION = "calendar.snapshot"
CALENDAR_SNAPSHOT_SCHEMA_VERSION = "calendar_snapshot.v1"


@dataclass(frozen=True, slots=True)
class _PreparedCalendarSnapshot:
    """保存首个数据库 mutation 前已完成的 snapshot 内容准备结果。"""

    snapshot_id: UUID
    user_id: UUID
    version: int
    snapshot_kind: CalendarSnapshotKind
    encrypted: EncryptedValue
    canonical_hash: str
    retain_until: datetime


class SqlAlchemyCalendarProposalRepository:
    """在调用方事务内维护版本化提案和记录级加密快照。

    每个 snapshot 使用自己的行 ID 作为 AAD 记录维度。创建和版本推进会先完成时间、
    规范 JSON、哈希与加密准备，再执行父 INSERT/CAS，因此准备异常即使被调用方捕获并
    正常提交也不会留下半成品。恢复操作始终创建新提案与新密文，不修改历史快照行。
    """

    def __init__(self, session: AsyncSession, cipher: ActionPayloadCipher) -> None:
        """绑定调用方控制的异步会话和操作内容加密器。

        Args:
            session: 外层应用事务拥有的 SQLAlchemy 异步会话。
            cipher: 使用 snapshot 行 ID 构造记录绑定 AAD 的加密器。
        """
        self._session = session
        self._cipher = cipher

    async def get_by_creation_key(
        self,
        *,
        user_id: UUID,
        creation_idempotency_key: str,
    ) -> CalendarProposalSnapshot | None:
        """按用户创建键读取当前提案，供应用层在生成随机 ID 前识别重放。

        Args:
            user_id: 当前认证用户。
            creation_idempotency_key: 已在应用边界验证的创建幂等键。

        Returns:
            当前用户同键提案；不存在或跨用户时返回 ``None``。
        """
        proposal = await self._session.scalar(
            select(CalendarChangeProposalModel).where(
                CalendarChangeProposalModel.user_id == user_id,
                CalendarChangeProposalModel.creation_idempotency_key == creation_idempotency_key,
            )
        )
        return None if proposal is None else await self._proposal_snapshot(proposal)

    async def create(
        self,
        *,
        proposal_id: UUID,
        snapshot_id: UUID,
        user_id: UUID,
        connection_id: UUID,
        creation_idempotency_key: str,
        creation_payload_hash: str,
        calendar_id: str,
        operation_kind: CalendarOperationKind,
        target_event_id: str | None,
        base_etag: str | None,
        retain_until: datetime,
        desired_state: Mapping[str, object],
    ) -> CalendarProposalSnapshot:
        """幂等创建提案头和版本一 desired snapshot。

        Args:
            proposal_id: 新提案稳定 UUID；重放时可被既有提案 ID 取代。
            snapshot_id: 版本一 desired snapshot ID，也是其 AAD 记录维度。
            user_id: 当前认证用户。
            connection_id: 提案固定写入连接。
            creation_idempotency_key: 用户范围内的创建重放键。
            creation_payload_hash: 完整规范创建请求哈希。
            calendar_id: 精确供应商日历标识。
            operation_kind: M2 限定的创建、修改或恢复类别。
            target_event_id: 修改/恢复目标事件；创建可为空。
            base_etag: 修改/恢复生成时观察到的供应商版本；创建可为空。
            retain_until: 日程内容密文保留截止时间；会规范为 UTC。
            desired_state: 只含标准 JSON 值的完整期望状态。

        Returns:
            新建或同键同哈希重放命中的当前提案快照。

        Raises:
            StateConflictError: 同一创建键已绑定不同规范请求哈希。
            ValueError: 操作类别、时间或 JSON 值不符合持久化边界。
        """
        # target的active谓词只证明先前读取；当前用户锁须覆盖父行和desired的原子提交。
        if not await lock_active_user(self._session, user_id=user_id):
            raise StateConflictError(
                error_code="calendar_proposal_not_editable",
                message="calendar proposal is not editable",
            )
        normalized_operation = _operation_kind(operation_kind)
        prepared_snapshot = self._prepare_snapshot(
            snapshot_id=snapshot_id,
            user_id=user_id,
            version=1,
            snapshot_kind="desired",
            content=desired_state,
            retain_until=retain_until,
        )
        inserted_id = await self._session.scalar(
            insert(CalendarChangeProposalModel)
            .values(
                id=proposal_id,
                user_id=user_id,
                connection_id=connection_id,
                creation_idempotency_key=creation_idempotency_key,
                creation_payload_hash=creation_payload_hash,
                calendar_id=calendar_id,
                operation_kind=normalized_operation,
                target_event_id=target_event_id,
                base_etag=base_etag,
                current_version=1,
                status=CalendarProposalStatus.EDITING.value,
                retain_until=prepared_snapshot.retain_until,
            )
            .on_conflict_do_nothing(
                constraint="uq_calendar_change_proposals_user_creation_idempotency_key"
            )
            .returning(CalendarChangeProposalModel.id)
        )
        if inserted_id is None:
            existing = await self._session.scalar(
                select(CalendarChangeProposalModel).where(
                    CalendarChangeProposalModel.user_id == user_id,
                    CalendarChangeProposalModel.creation_idempotency_key
                    == creation_idempotency_key,
                )
            )
            if existing is None:
                raise RuntimeError("calendar proposal idempotency winner is not visible")
            if not compare_digest(existing.creation_payload_hash, creation_payload_hash):
                raise _idempotency_payload_mismatch()
            return await self._proposal_snapshot(existing)

        await self._insert_prepared_snapshot(
            proposal_id=proposal_id,
            prepared=prepared_snapshot,
        )
        proposal = await self._session.scalar(
            select(CalendarChangeProposalModel).where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == user_id,
            )
        )
        if proposal is None:
            raise RuntimeError("inserted calendar proposal is not visible")
        return await self._proposal_snapshot(proposal)

    async def get_current(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> CalendarProposalSnapshot | None:
        """按用户读取提案及精确当前 desired snapshot。

        Args:
            user_id: 当前认证用户。
            proposal_id: 待读取提案 ID。

        Returns:
            当前提案快照；不存在或跨用户时返回 ``None``。

        Raises:
            StateConflictError: 当前内容密文已清除、哈希不匹配或结构损坏。
            cryptography.exceptions.InvalidTag: snapshot 密文或 AAD 被替换。
        """
        proposal = await self._session.scalar(
            select(CalendarChangeProposalModel).where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == user_id,
            )
        )
        return None if proposal is None else await self._proposal_snapshot(proposal)

    async def list_current(
        self,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[CalendarProposalSnapshot, ...]:
        """按用户、更新时间和 UUID 稳定分页读取当前提案。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("calendar proposal limit must be between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("calendar proposal offset must be nonnegative")
        rows = tuple(
            (
                await self._session.scalars(
                    select(CalendarChangeProposalModel)
                    .where(CalendarChangeProposalModel.user_id == user_id)
                    .order_by(
                        CalendarChangeProposalModel.updated_at.desc(),
                        CalendarChangeProposalModel.id.desc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        return tuple([await self._proposal_snapshot(row) for row in rows])

    async def cancel(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> CalendarProposalSnapshot | None:
        """先同步用户屏障再锁定并取消本地提案；inactive沿用不存在时的None语义。"""
        if not await lock_active_user(self._session, user_id=user_id):
            return None
        proposal = await self._session.scalar(
            select(CalendarChangeProposalModel)
            .where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == user_id,
            )
            .with_for_update()
        )
        if proposal is None:
            return None
        current = CalendarProposalStatus(proposal.status)
        transition_calendar_proposal(current, CalendarProposalStatus.CANCELLED)
        proposal.status = CalendarProposalStatus.CANCELLED.value
        await self._session.flush()
        return await self._proposal_snapshot(proposal)

    async def save_next_version(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        desired_state: Mapping[str, object],
        retain_until: datetime,
        connection_id: UUID | None = None,
        calendar_id: str | None = None,
    ) -> CalendarProposalSnapshot | None:
        """以父行 CAS 推进版本，并可原子切换 editing shell 的精确日历目标。

        Args:
            snapshot_id: 新 desired snapshot ID，也是该密文 AAD 记录维度。
            user_id: 当前认证用户。
            proposal_id: 待编辑提案 ID。
            expected_version: 客户端观察到的正整数当前版本。
            desired_state: 新版本完整期望状态。
            retain_until: 新 snapshot 密文保留截止时间。
            connection_id: 显式日历确认选择的新连接；必须与 ``calendar_id`` 同时提供。
            calendar_id: 显式日历确认选择的新目录 ID；必须与 ``connection_id`` 同时提供。

        Returns:
            保存后的当前提案；不存在或跨用户时返回 ``None``。

        Raises:
            StateConflictError: expected version 陈旧，或提案不在可编辑状态。
        """
        # 编辑、显式确认和另开事务的建议保存共享入口，先user再父行/版本/历史摘要。
        if not await lock_active_user(self._session, user_id=user_id):
            return None
        if type(expected_version) is not int or expected_version <= 0:
            raise ValueError("expected_version must be a positive integer")
        if (connection_id is None) != (calendar_id is None):
            raise ValueError("calendar retarget identity must be provided together")
        if calendar_id is not None and (not isinstance(calendar_id, str) or not calendar_id):
            raise ValueError("calendar retarget calendar_id is invalid")
        if connection_id is not None and calendar_id is not None:
            current = await self._session.scalar(
                select(CalendarChangeProposalModel)
                .where(
                    CalendarChangeProposalModel.id == proposal_id,
                    CalendarChangeProposalModel.user_id == user_id,
                )
                .with_for_update()
            )
            if current is None:
                return None
            if current.current_version != expected_version:
                raise _proposal_version_conflict()
            changed_target = (current.connection_id, current.calendar_id) != (
                connection_id,
                calendar_id,
            )
            if changed_target and current.operation_kind != "create":
                raise StateConflictError(
                    error_code="calendar_proposal_binding_immutable",
                    message="calendar proposal source cannot be changed",
                )
            if current.status != CalendarProposalStatus.EDITING.value:
                raise StateConflictError(
                    error_code="calendar_proposal_not_editable",
                    message="calendar proposal is not editable",
                )
            if changed_target:
                await preserve_historical_action_bindings(
                    self._session,
                    self._cipher,
                    user_id=user_id,
                    proposal_kind="calendar_proposal",
                    proposal_id=proposal_id,
                    original_connection_id=current.connection_id,
                    original_version=expected_version,
                    original_calendar_id=current.calendar_id,
                    calendar_proposals=self,
                )
        next_version = expected_version + 1
        prepared_snapshot = self._prepare_snapshot(
            snapshot_id=snapshot_id,
            user_id=user_id,
            version=next_version,
            snapshot_kind="desired",
            content=desired_state,
            retain_until=retain_until,
        )
        update_values: dict[str, object] = {
            "current_version": next_version,
            "retain_until": prepared_snapshot.retain_until,
        }
        if connection_id is not None and calendar_id is not None:
            update_values.update(
                connection_id=connection_id,
                calendar_id=calendar_id,
            )
        updated_id = await self._session.scalar(
            update(CalendarChangeProposalModel)
            .where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == user_id,
                CalendarChangeProposalModel.current_version == expected_version,
                CalendarChangeProposalModel.status == CalendarProposalStatus.EDITING.value,
            )
            .values(**update_values)
            .returning(CalendarChangeProposalModel.id)
        )
        if updated_id is None:
            existing = await self._session.scalar(
                select(CalendarChangeProposalModel).where(
                    CalendarChangeProposalModel.id == proposal_id,
                    CalendarChangeProposalModel.user_id == user_id,
                )
            )
            if existing is None:
                return None
            if existing.current_version != expected_version:
                raise _proposal_version_conflict()
            raise StateConflictError(
                error_code="calendar_proposal_not_editable",
                message="calendar proposal is not editable",
            )

        await self._insert_prepared_snapshot(
            proposal_id=proposal_id,
            prepared=prepared_snapshot,
        )
        proposal = await self._session.scalar(
            select(CalendarChangeProposalModel).where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == user_id,
            )
        )
        if proposal is None:
            raise RuntimeError("updated calendar proposal is not visible")
        return await self._proposal_snapshot(proposal)

    async def save_snapshot(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        version: int,
        snapshot_kind: CalendarSnapshotKind,
        content: Mapping[str, object],
        retain_until: datetime,
    ) -> CalendarSnapshot | None:
        """为当前用户提案保存一个不可变 desired 或 before snapshot。

        相同 ``proposal_id + version + kind`` 的重放仅在规范内容哈希相同时复用既有
        行；哈希不同会拒绝，避免补偿快照被同一逻辑位置静默替换。

        Args:
            snapshot_id: 新 snapshot 行 ID，也是 AAD 记录维度。
            user_id: 当前认证用户。
            proposal_id: snapshot 所属提案。
            version: snapshot 对应的正整数提案版本。
            snapshot_kind: ``desired`` 或 ``before``。
            content: 只含标准 JSON 值的完整日历状态。
            retain_until: 内容密文保留截止时间。

        Returns:
            新建或同哈希重放命中的 snapshot；跨用户或提案不存在时返回 ``None``。
        """
        # 旧desired的重放可只补before，不能依赖create曾经持有的另一个事务的用户锁。
        if not await lock_active_user(self._session, user_id=user_id):
            return None
        return await self._save_snapshot_for_proposal(
            snapshot_id=snapshot_id,
            user_id=user_id,
            proposal_id=proposal_id,
            version=version,
            snapshot_kind=snapshot_kind,
            content=content,
            retain_until=_as_utc(retain_until),
        )

    async def load_snapshot(
        self,
        *,
        user_id: UUID,
        snapshot_id: UUID,
    ) -> CalendarSnapshot | None:
        """按用户读取、认证、解密并校验规范哈希的 snapshot。

        Args:
            user_id: 当前认证用户。
            snapshot_id: 待读取 snapshot 行 ID。

        Returns:
            解密 snapshot；不存在或跨用户时返回 ``None``。

        Raises:
            StateConflictError: AEAD 三元组已清除、规范哈希不匹配或 kind 非法。
            cryptography.exceptions.InvalidTag: 密文、用户、记录或内部协议 AAD 不匹配。
        """
        row = await self._session.scalar(
            select(CalendarChangeSnapshotModel).where(
                CalendarChangeSnapshotModel.id == snapshot_id,
                CalendarChangeSnapshotModel.user_id == user_id,
            )
        )
        return None if row is None else self._decode_snapshot(row)

    async def get_restore_source(
        self,
        *,
        user_id: UUID,
        source_snapshot_id: UUID,
    ) -> CalendarRestoreSourceSnapshot | None:
        """从用户拥有的 before snapshot 解析不可拆分恢复目标身份。

        本查询只返回 source/proposal/connection/calendar/event 标识，不解密历史日程内容，
        供 Worker 在释放首个短事务前确定供应商精确 GET 目标。

        Args:
            user_id: 当前认证用户。
            source_snapshot_id: 历史修改前快照 ID。

        Returns:
            目标身份；不存在、跨用户、不是 before 或没有目标事件时返回 ``None``。
        """
        row = (
            await self._session.execute(
                select(
                    CalendarChangeSnapshotModel.id.label("source_snapshot_id"),
                    CalendarChangeSnapshotModel.proposal_id.label("source_proposal_id"),
                    CalendarChangeProposalModel.connection_id,
                    CalendarChangeProposalModel.calendar_id,
                    CalendarChangeProposalModel.target_event_id,
                )
                .join(
                    CalendarChangeProposalModel,
                    CalendarChangeProposalModel.id == CalendarChangeSnapshotModel.proposal_id,
                )
                .where(
                    CalendarChangeSnapshotModel.id == source_snapshot_id,
                    CalendarChangeSnapshotModel.user_id == user_id,
                    CalendarChangeSnapshotModel.snapshot_kind == "before",
                    CalendarChangeProposalModel.user_id == user_id,
                    CalendarChangeProposalModel.target_event_id.is_not(None),
                )
            )
        ).one_or_none()
        if row is None or row.target_event_id is None:
            return None
        return CalendarRestoreSourceSnapshot(
            source_snapshot_id=row.source_snapshot_id,
            source_proposal_id=row.source_proposal_id,
            connection_id=row.connection_id,
            calendar_id=row.calendar_id,
            provider_event_id=row.target_event_id,
        )

    async def get_eligible_restore_source(
        self,
        *,
        user_id: UUID,
        source_snapshot_id: UUID,
        now: datetime,
    ) -> CalendarRestoreSourceSnapshot | None:
        """为恢复 Worker 一次读取并锁定全部 provider-read 前置事实。

        查询仅投影恢复精确 GET 所需的非敏感标识，但在同一个 SQL 谓词中验证用户归属、
        ``before`` 类别、已应用 ``update`` 生命周期、snapshot 仍在保留期且 AEAD 三元组
        完整，以及本地 CalendarEvent 与提案的 connection/calendar/provider event 三元组
        精确一致。任一事实缺失均返回 ``None``，使 Worker 在解析凭据或访问供应商之前
        fail closed；第二写事务复用本方法并锁行，避免提交网络读取期间已经失效的来源。

        Args:
            user_id: 当前任务持久归属用户。
            source_snapshot_id: 任务输入中的历史 before snapshot ID。
            now: 当前事务观察到的带时区时间，用于严格判断密文保留期。

        Returns:
            可安全跨短事务携带的精确读取身份；任一资格事实不成立时返回 ``None``。
        """
        checked_now = _as_utc(now)
        event_binding = (
            (CalendarEventModel.user_id == user_id)
            & (CalendarEventModel.connection_id == CalendarChangeProposalModel.connection_id)
            & (CalendarEventModel.calendar_id == CalendarChangeProposalModel.calendar_id)
            & (CalendarEventModel.provider_event_id == CalendarChangeProposalModel.target_event_id)
        )
        row = (
            await self._session.execute(
                select(
                    CalendarChangeSnapshotModel.id.label("source_snapshot_id"),
                    CalendarChangeSnapshotModel.proposal_id.label("source_proposal_id"),
                    CalendarChangeProposalModel.connection_id,
                    CalendarChangeProposalModel.calendar_id,
                    CalendarChangeProposalModel.target_event_id,
                )
                .join(
                    CalendarChangeProposalModel,
                    CalendarChangeProposalModel.id == CalendarChangeSnapshotModel.proposal_id,
                )
                .join(CalendarEventModel, event_binding)
                .where(
                    CalendarChangeSnapshotModel.id == source_snapshot_id,
                    CalendarChangeSnapshotModel.user_id == user_id,
                    CalendarChangeSnapshotModel.snapshot_kind == "before",
                    CalendarChangeSnapshotModel.retain_until > checked_now,
                    CalendarChangeSnapshotModel.content_ciphertext.is_not(None),
                    CalendarChangeSnapshotModel.content_nonce.is_not(None),
                    CalendarChangeSnapshotModel.content_key_version.is_not(None),
                    CalendarChangeProposalModel.user_id == user_id,
                    CalendarChangeProposalModel.operation_kind == "update",
                    CalendarChangeProposalModel.status == CalendarProposalStatus.APPLIED.value,
                    CalendarChangeProposalModel.target_event_id.is_not(None),
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None or row.target_event_id is None:
            return None
        return CalendarRestoreSourceSnapshot(
            source_snapshot_id=row.source_snapshot_id,
            source_proposal_id=row.source_proposal_id,
            connection_id=row.connection_id,
            calendar_id=row.calendar_id,
            provider_event_id=row.target_event_id,
        )

    async def get_restore_source_projection(
        self,
        *,
        user_id: UUID,
        source_snapshot_id: UUID,
    ) -> CalendarRestoreSourceProjection | None:
        """锁定并投影 application 恢复资格判断需要的全部持久事实。

        本方法只执行用户范围读取，不解释 snapshot 类别、提案生命周期、保留期或事件
        绑定是否合格。application 在同一事务上下文内完成纯校验后，才可调用
        ``create_restore_prepare_task`` 创建 TaskRun、AuditEvent 与 Outbox。精确事件查询
        使用提案保存的 connection/calendar/provider event 三元组；未找到时仍返回 source
        投影并把事件字段置空，使其稳定映射为 409 而不是伪装成 source 404。

        Args:
            user_id: 当前认证用户；snapshot、proposal 与 event 查询都显式隔离该值。
            source_snapshot_id: 请求指定的历史 snapshot ID。

        Returns:
            锁定的最小非敏感事实；snapshot 或父 proposal 不存在/跨用户时返回 ``None``。
        """
        # enqueue会在同事务创建Task并重获user锁。必须在source/event之前先取user，
        # 与Prepare结果的Task→user→source排序一致；不能先锁source再等结果持有的user。
        if not await lock_active_user(self._session, user_id=user_id):
            return None
        row = (
            await self._session.execute(
                select(
                    CalendarChangeSnapshotModel.id.label("source_snapshot_id"),
                    CalendarChangeSnapshotModel.proposal_id.label("source_proposal_id"),
                    CalendarChangeSnapshotModel.snapshot_kind,
                    CalendarChangeSnapshotModel.retain_until,
                    CalendarChangeSnapshotModel.content_ciphertext,
                    CalendarChangeSnapshotModel.content_nonce,
                    CalendarChangeSnapshotModel.content_key_version,
                    CalendarChangeProposalModel.operation_kind,
                    CalendarChangeProposalModel.status.label("proposal_status"),
                    CalendarChangeProposalModel.connection_id.label("proposal_connection_id"),
                    CalendarChangeProposalModel.calendar_id.label("proposal_calendar_id"),
                    CalendarChangeProposalModel.target_event_id.label("proposal_provider_event_id"),
                )
                .join(
                    CalendarChangeProposalModel,
                    CalendarChangeProposalModel.id == CalendarChangeSnapshotModel.proposal_id,
                )
                .where(
                    CalendarChangeSnapshotModel.id == source_snapshot_id,
                    CalendarChangeSnapshotModel.user_id == user_id,
                    CalendarChangeProposalModel.user_id == user_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            return None

        event = None
        if row.proposal_provider_event_id is not None:
            event = (
                await self._session.execute(
                    select(
                        CalendarEventModel.id.label("target_local_event_id"),
                        CalendarEventModel.connection_id.label("event_connection_id"),
                        CalendarEventModel.calendar_id.label("event_calendar_id"),
                        CalendarEventModel.provider_event_id.label("event_provider_event_id"),
                    )
                    .where(
                        CalendarEventModel.user_id == user_id,
                        CalendarEventModel.connection_id == row.proposal_connection_id,
                        CalendarEventModel.calendar_id == row.proposal_calendar_id,
                        CalendarEventModel.provider_event_id == row.proposal_provider_event_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()

        return CalendarRestoreSourceProjection(
            source_snapshot_id=row.source_snapshot_id,
            source_proposal_id=row.source_proposal_id,
            snapshot_kind=row.snapshot_kind,
            operation_kind=row.operation_kind,
            # 未知数据库状态交给 application 的严格比较归类为冲突，避免在读取边界
            # 抛出未分类 ValueError 并错误地转成 500。
            proposal_status=cast(CalendarProposalStatus, row.proposal_status),
            target_local_event_id=(event.target_local_event_id if event is not None else None),
            proposal_connection_id=row.proposal_connection_id,
            proposal_calendar_id=row.proposal_calendar_id,
            proposal_provider_event_id=row.proposal_provider_event_id,
            event_connection_id=(event.event_connection_id if event is not None else None),
            event_calendar_id=(event.event_calendar_id if event is not None else None),
            event_provider_event_id=(event.event_provider_event_id if event is not None else None),
            retain_until=row.retain_until,
            ciphertext_present=(
                row.content_ciphertext is not None
                and row.content_nonce is not None
                and row.content_key_version is not None
            ),
        )

    async def create_restore_prepare_task(
        self,
        *,
        user_id: UUID,
        source_snapshot_id: UUID,
        creation_idempotency_key: str,
    ) -> CreateTaskResult:
        """在调用方已验证 source 的同一事务中原子创建恢复准备任务。

        Args:
            user_id: 当前认证用户和任务持久归属用户。
            source_snapshot_id: 已在本事务内锁定并验证的历史 before snapshot ID。
            creation_idempotency_key: 恢复提案创建与任务重放共享的稳定用户范围键。

        Returns:
            新建或精确重放的持久任务标识与状态。

        Notes:
            持久输入刻意不包含 REST path ``event_id``、动态时间或别名 ``snapshot_id``；Worker
            只从 PostgreSQL 重新读取并二次验证 source，不能信任队列消息复制的业务事实。
        """
        payload = _restore_prepare_task_payload(
            source_snapshot_id=source_snapshot_id,
            creation_idempotency_key=creation_idempotency_key,
        )
        return await SqlAlchemyTaskRepository(self._session).create_with_outbox(
            user_id=user_id,
            kind="calendar.restore.prepare",
            input_payload=payload,
            idempotency_key=creation_idempotency_key,
        )

    async def get_existing_restore_prepare_task(
        self,
        *,
        user_id: UUID,
        source_snapshot_id: UUID,
        creation_idempotency_key: str,
    ) -> CreateTaskResult | None:
        """在读取任何可变 source 前按精确恢复任务意图识别顺序重放。

        Args:
            user_id: 当前认证用户，参与任务幂等隔离。
            source_snapshot_id: 冻结任务输入中的历史 snapshot ID。
            creation_idempotency_key: 同时写入任务输入与 TaskRun 唯一键的客户端键。

        Returns:
            同用户、同 kind、同完整输入的已有任务；键不存在时返回 ``None``。

        Raises:
            StateConflictError: 同键已绑定到其他任务 kind 或不同恢复输入。
        """
        payload = _restore_prepare_task_payload(
            source_snapshot_id=source_snapshot_id,
            creation_idempotency_key=creation_idempotency_key,
        )
        return await SqlAlchemyTaskRepository(self._session).get_existing(
            user_id=user_id,
            kind="calendar.restore.prepare",
            input_payload=payload,
            idempotency_key=creation_idempotency_key,
        )

    async def mark_stale(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> CalendarProposalStateSnapshot | None:
        """锁定提案并通过纯领域状态机记录 ETag 冲突后的 ``stale`` 状态。

        Args:
            user_id: 当前认证用户。
            proposal_id: 发生供应商版本冲突的提案 ID。

        Returns:
            更新后的无内容状态快照；跨用户或不存在时返回 ``None``。

        Raises:
            StateConflictError: 当前状态不允许进入 ``stale``。
        """
        proposal = await self._session.scalar(
            select(CalendarChangeProposalModel)
            .where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == user_id,
            )
            .with_for_update()
        )
        if proposal is None:
            return None
        target = transition_calendar_proposal(
            CalendarProposalStatus(proposal.status),
            CalendarProposalStatus.STALE,
        )
        proposal.status = target.value
        await self._session.flush()
        return CalendarProposalStateSnapshot(
            proposal_id=proposal.id,
            current_version=proposal.current_version,
            status=target,
        )

    async def create_restore_proposal(
        self,
        *,
        proposal_id: UUID,
        snapshot_id: UUID,
        user_id: UUID,
        source_snapshot_id: UUID,
        connection_id: UUID,
        creation_idempotency_key: str,
        creation_payload_hash: str,
        calendar_id: str,
        target_event_id: str,
        base_etag: str,
        retain_until: datetime,
    ) -> CalendarProposalSnapshot | None:
        """从历史 before snapshot 解密内容并创建全新的恢复提案事实。

        历史行保持只读；新提案使用新 ID、新 snapshot ID 和新 AAD 重新加密相同目标
        状态。当前供应商 ETag 和通知策略等后续业务校验由对应应用用例完成，本方法不
        预实现可用性算法。重放必须先按用户和创建键解析既有恢复提案：同哈希直接复用，
        异哈希立即冲突；只有尚无既有事实时才允许读取历史 source snapshot，因此 source
        在首次成功复制后被保留任务清理也不会破坏创建幂等语义。

        Args:
            proposal_id: 新恢复提案 ID。
            snapshot_id: 新恢复提案 desired snapshot ID。
            user_id: 当前认证用户。
            source_snapshot_id: 历史 ``before`` snapshot ID。
            connection_id: 当前写入连接。
            creation_idempotency_key: 恢复请求创建键。
            creation_payload_hash: 完整规范恢复请求哈希。
            calendar_id: 当前目标日历。
            target_event_id: 当前目标供应商事件。
            base_etag: 生成恢复提案时重新读取的当前 ETag。
            retain_until: 新内容密文保留截止时间。

        Returns:
            新建或同键同哈希重放命中的恢复提案；源 snapshot 跨用户或不存在时返回
            ``None``。

        Raises:
            StateConflictError: 源不是 before snapshot、内容已清除，或创建键哈希冲突。
        """
        existing = await self._session.scalar(
            select(CalendarChangeProposalModel).where(
                CalendarChangeProposalModel.user_id == user_id,
                CalendarChangeProposalModel.creation_idempotency_key == creation_idempotency_key,
            )
        )
        if existing is not None:
            if not compare_digest(existing.creation_payload_hash, creation_payload_hash):
                raise _idempotency_payload_mismatch()
            return await self._proposal_snapshot(existing)

        source = await self.load_snapshot(user_id=user_id, snapshot_id=source_snapshot_id)
        if source is None:
            return None
        if source.snapshot_kind != "before":
            raise _calendar_snapshot_unavailable()
        return await self.create(
            proposal_id=proposal_id,
            snapshot_id=snapshot_id,
            user_id=user_id,
            connection_id=connection_id,
            creation_idempotency_key=creation_idempotency_key,
            creation_payload_hash=creation_payload_hash,
            calendar_id=calendar_id,
            operation_kind="restore",
            target_event_id=target_event_id,
            base_etag=base_etag,
            retain_until=retain_until,
            desired_state=source.content,
        )

    async def _save_snapshot_for_proposal(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        version: int,
        snapshot_kind: CalendarSnapshotKind,
        content: Mapping[str, object],
        retain_until: datetime,
    ) -> CalendarSnapshot | None:
        """验证父提案归属后加密并幂等插入一个 snapshot。"""
        if type(version) is not int or version <= 0:
            raise ValueError("calendar snapshot version must be a positive integer")
        proposal = await self._session.scalar(
            select(CalendarChangeProposalModel).where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == user_id,
            )
        )
        if proposal is None:
            return None
        if version > proposal.current_version:
            raise StateConflictError(
                error_code="calendar_snapshot_version_conflict",
                message="calendar snapshot version is unavailable",
            )
        prepared_snapshot = self._prepare_snapshot(
            snapshot_id=snapshot_id,
            user_id=user_id,
            version=version,
            snapshot_kind=snapshot_kind,
            content=content,
            retain_until=retain_until,
        )
        return await self._insert_prepared_snapshot(
            proposal_id=proposal_id,
            prepared=prepared_snapshot,
        )

    def _prepare_snapshot(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        version: int,
        snapshot_kind: CalendarSnapshotKind,
        content: Mapping[str, object],
        retain_until: datetime,
    ) -> _PreparedCalendarSnapshot:
        """在任何数据库写入前完成 snapshot 的全部纯 Python 内容准备。"""
        normalized_kind = _snapshot_kind(snapshot_kind)
        normalized_retain_until = _as_utc(retain_until)
        prepared_content = canonical_action_payload_json(content)
        canonical_hash = _canonical_hash(prepared_content)
        encrypted = self._cipher.encrypt_prepared_json(
            prepared_content,
            user_id=user_id,
            record_id=snapshot_id,
            content_kind=_snapshot_content_kind(normalized_kind),
            action=CALENDAR_SNAPSHOT_ACTION,
            schema_version=CALENDAR_SNAPSHOT_SCHEMA_VERSION,
        )
        return _PreparedCalendarSnapshot(
            snapshot_id=snapshot_id,
            user_id=user_id,
            version=version,
            snapshot_kind=normalized_kind,
            encrypted=encrypted,
            canonical_hash=canonical_hash,
            retain_until=normalized_retain_until,
        )

    async def _insert_prepared_snapshot(
        self,
        *,
        proposal_id: UUID,
        prepared: _PreparedCalendarSnapshot,
    ) -> CalendarSnapshot:
        """只消费已验证准备结果，幂等插入一个不可变 snapshot。"""
        inserted_id = await self._session.scalar(
            insert(CalendarChangeSnapshotModel)
            .values(
                id=prepared.snapshot_id,
                user_id=prepared.user_id,
                proposal_id=proposal_id,
                version=prepared.version,
                snapshot_kind=prepared.snapshot_kind,
                content_ciphertext=prepared.encrypted.ciphertext,
                content_nonce=prepared.encrypted.nonce,
                content_key_version=prepared.encrypted.key_version,
                canonical_hash=prepared.canonical_hash,
                retain_until=prepared.retain_until,
            )
            .on_conflict_do_nothing(constraint="uq_calendar_change_snapshots_proposal_version_kind")
            .returning(CalendarChangeSnapshotModel.id)
        )
        if inserted_id is None:
            existing = await self._session.scalar(
                select(CalendarChangeSnapshotModel).where(
                    CalendarChangeSnapshotModel.user_id == prepared.user_id,
                    CalendarChangeSnapshotModel.proposal_id == proposal_id,
                    CalendarChangeSnapshotModel.version == prepared.version,
                    CalendarChangeSnapshotModel.snapshot_kind == prepared.snapshot_kind,
                )
            )
            if existing is None:
                raise RuntimeError("calendar snapshot idempotency winner is not visible")
            if not compare_digest(existing.canonical_hash, prepared.canonical_hash):
                raise StateConflictError(
                    error_code="calendar_snapshot_payload_mismatch",
                    message="calendar snapshot is already bound to different content",
                )
            return self._decode_snapshot(existing)
        await self._session.flush()
        row = await self._session.scalar(
            select(CalendarChangeSnapshotModel).where(
                CalendarChangeSnapshotModel.id == prepared.snapshot_id,
                CalendarChangeSnapshotModel.user_id == prepared.user_id,
            )
        )
        if row is None:
            raise RuntimeError("inserted calendar snapshot is not visible")
        return self._decode_snapshot(row)

    async def _proposal_snapshot(
        self,
        proposal: CalendarChangeProposalModel,
    ) -> CalendarProposalSnapshot:
        """读取当前 desired 与提案稳定 before 引用并返回无 ORM 快照。"""
        row = await self._session.scalar(
            select(CalendarChangeSnapshotModel).where(
                CalendarChangeSnapshotModel.user_id == proposal.user_id,
                CalendarChangeSnapshotModel.proposal_id == proposal.id,
                CalendarChangeSnapshotModel.version == proposal.current_version,
                CalendarChangeSnapshotModel.snapshot_kind == "desired",
            )
        )
        if row is None:
            raise _calendar_snapshot_unavailable()
        before_snapshot_id = await self._session.scalar(
            select(CalendarChangeSnapshotModel.id)
            .where(
                CalendarChangeSnapshotModel.user_id == proposal.user_id,
                CalendarChangeSnapshotModel.proposal_id == proposal.id,
                CalendarChangeSnapshotModel.snapshot_kind == "before",
            )
            # before 是提案生成时观察到的稳定事实；后续 desired 编辑不能把查询限制到
            # current_version，否则版本二以后会错误丢失补偿来源。
            .order_by(
                CalendarChangeSnapshotModel.version,
                CalendarChangeSnapshotModel.id,
            )
            .limit(1)
        )
        return CalendarProposalSnapshot(
            proposal_id=proposal.id,
            connection_id=proposal.connection_id,
            calendar_id=proposal.calendar_id,
            operation_kind=_operation_kind(proposal.operation_kind),
            target_event_id=proposal.target_event_id,
            base_etag=proposal.base_etag,
            creation_payload_hash=proposal.creation_payload_hash,
            current_version=proposal.current_version,
            status=CalendarProposalStatus(proposal.status),
            retain_until=proposal.retain_until,
            desired_snapshot=self._decode_snapshot(row),
            before_snapshot_id=before_snapshot_id,
        )

    def _decode_snapshot(self, row: CalendarChangeSnapshotModel) -> CalendarSnapshot:
        """认证、解密并校验一个 snapshot 行的规范哈希。"""
        snapshot_kind = _snapshot_kind(row.snapshot_kind)
        if (
            row.content_ciphertext is None
            or row.content_nonce is None
            or row.content_key_version is None
        ):
            # 合法的保留清理骨架不能被解释成空描述、地点或事件状态。
            raise _calendar_snapshot_unavailable()
        content = self._cipher.decrypt_json(
            EncryptedValue(
                row.content_ciphertext,
                row.content_nonce,
                row.content_key_version,
            ),
            user_id=row.user_id,
            record_id=row.id,
            content_kind=_snapshot_content_kind(snapshot_kind),
            action=CALENDAR_SNAPSHOT_ACTION,
            schema_version=CALENDAR_SNAPSHOT_SCHEMA_VERSION,
        )
        prepared_content = canonical_action_payload_json(content)
        if not compare_digest(_canonical_hash(prepared_content), row.canonical_hash):
            raise _calendar_snapshot_unavailable()
        return CalendarSnapshot(
            snapshot_id=row.id,
            proposal_id=row.proposal_id,
            version=row.version,
            snapshot_kind=snapshot_kind,
            content=cast(dict[str, JsonValue], content),
            canonical_hash=row.canonical_hash,
            retain_until=row.retain_until,
            created_at=row.created_at,
        )


class SqlAlchemyCalendarRestoreEnqueueRepositoryFactory:
    """为恢复 API 提供验证与 TaskRun/Outbox 共用的单一事务。"""

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        cipher: ActionPayloadCipher,
    ) -> None:
        """保存进程级会话工厂与 snapshot cipher，不在构造时占用连接。"""
        self._session_factory = session_factory
        self._cipher = cipher

    @asynccontextmanager
    async def __call__(self):
        """异常时整体回滚 source 锁定、TaskRun、AuditEvent 与 Outbox。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyCalendarProposalRepository(session, self._cipher)


def _restore_prepare_task_payload(
    *,
    source_snapshot_id: UUID,
    creation_idempotency_key: str,
) -> dict[str, JsonValue]:
    """构造 restore prepare 创建与重放比较共享的唯一冻结任务输入。

    Args:
        source_snapshot_id: 历史 before snapshot 的稳定 UUID。
        creation_idempotency_key: 恢复提案创建与 TaskRun 共用的用户范围键。

    Returns:
        只含 ``source_snapshot_id`` 与 ``creation_idempotency_key`` 的新字典；REST path
        event、动态时钟和可变 source 投影绝不进入任务意图。
    """
    return {
        "source_snapshot_id": str(source_snapshot_id),
        "creation_idempotency_key": creation_idempotency_key,
    }


def _canonical_hash(payload: PreparedActionPayload) -> str:
    """对 ActionPayloadCipher 共享的唯一规范 JSON 字节计算 snapshot SHA-256。"""
    return hashlib.sha256(payload.canonical_bytes).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """要求带时区时间并转换为 UTC，禁止宿主机时区参与业务日期。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retain_until must be timezone-aware")
    return value.astimezone(UTC)


def _operation_kind(value: str) -> CalendarOperationKind:
    """把数据库或调用方字符串收窄到 M2 三种提案类别。"""
    if value not in {"create", "update", "restore"}:
        raise ValueError("calendar operation kind is not supported")
    return cast(CalendarOperationKind, value)


def _snapshot_kind(value: str) -> CalendarSnapshotKind:
    """把数据库或调用方字符串收窄到 desired/before 内容类别。"""
    if value not in {"desired", "before"}:
        raise _calendar_snapshot_unavailable()
    return cast(CalendarSnapshotKind, value)


def _snapshot_content_kind(snapshot_kind: CalendarSnapshotKind) -> str:
    """把 desired/before 纳入 AAD 内容类别，阻止同一行被重新解释。"""
    return f"{CALENDAR_SNAPSHOT_CONTENT_KIND}:{snapshot_kind}"


def _idempotency_payload_mismatch() -> StateConflictError:
    """构造不回显创建键或日程内容的稳定幂等冲突。"""
    return StateConflictError(
        error_code="idempotency_key_payload_mismatch",
        message="idempotency key is already bound to different content",
    )


def _proposal_version_conflict() -> StateConflictError:
    """构造 API 可稳定映射的提案版本冲突。"""
    return StateConflictError(
        error_code="proposal_version_conflict",
        message="calendar proposal version changed",
    )


def _calendar_snapshot_unavailable() -> StateConflictError:
    """构造密文清理、结构损坏或哈希不一致时的 fail-closed 错误。"""
    return StateConflictError(
        error_code="calendar_snapshot_unavailable",
        message="calendar snapshot content is unavailable",
    )


__all__ = [
    "CALENDAR_SNAPSHOT_ACTION",
    "CALENDAR_SNAPSHOT_CONTENT_KIND",
    "CALENDAR_SNAPSHOT_SCHEMA_VERSION",
    "CalendarOperationKind",
    "CalendarProposalSnapshot",
    "CalendarProposalStateSnapshot",
    "CalendarSnapshot",
    "CalendarSnapshotKind",
    "SqlAlchemyCalendarProposalRepository",
    "SqlAlchemyCalendarRestoreEnqueueRepositoryFactory",
]
