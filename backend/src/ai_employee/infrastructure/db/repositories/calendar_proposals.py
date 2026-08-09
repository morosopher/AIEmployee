"""以加密不可变快照和 PostgreSQL CAS 持久化日历变更提案。"""

import hashlib
from collections.abc import Mapping
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
    CalendarRestoreSourceSnapshot,
    CalendarSnapshot,
    CalendarSnapshotKind,
)
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
                CalendarChangeProposalModel.creation_idempotency_key
                == creation_idempotency_key,
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

    async def save_next_version(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        desired_state: Mapping[str, object],
        retain_until: datetime,
    ) -> CalendarProposalSnapshot | None:
        """以父行 CAS 推进版本，并由赢家插入下一条 desired snapshot。

        Args:
            snapshot_id: 新 desired snapshot ID，也是该密文 AAD 记录维度。
            user_id: 当前认证用户。
            proposal_id: 待编辑提案 ID。
            expected_version: 客户端观察到的正整数当前版本。
            desired_state: 新版本完整期望状态。
            retain_until: 新 snapshot 密文保留截止时间。

        Returns:
            保存后的当前提案；不存在或跨用户时返回 ``None``。

        Raises:
            StateConflictError: expected version 陈旧，或提案不在可编辑状态。
        """
        if type(expected_version) is not int or expected_version <= 0:
            raise ValueError("expected_version must be a positive integer")
        next_version = expected_version + 1
        prepared_snapshot = self._prepare_snapshot(
            snapshot_id=snapshot_id,
            user_id=user_id,
            version=next_version,
            snapshot_kind="desired",
            content=desired_state,
            retain_until=retain_until,
        )
        updated_id = await self._session.scalar(
            update(CalendarChangeProposalModel)
            .where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == user_id,
                CalendarChangeProposalModel.current_version == expected_version,
                CalendarChangeProposalModel.status == CalendarProposalStatus.EDITING.value,
            )
            .values(
                current_version=next_version,
                retain_until=prepared_snapshot.retain_until,
            )
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
                    CalendarChangeProposalModel.id
                    == CalendarChangeSnapshotModel.proposal_id,
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
]
