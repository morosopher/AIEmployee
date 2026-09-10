"""执行隐私删除的 Worker 步骤，所有删除均在 PostgreSQL 有界事务内完成。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Final, Protocol
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidTag
from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.encryption import (
    EncryptedValue,
    EncryptionBoundaryError,
    EncryptionKeyVersionError,
)
from ai_employee.application.ports.oauth import OAuthRevocationResult
from ai_employee.application.use_cases.auth import Clock
from ai_employee.application.use_cases.privacy import (
    PRIVACY_DELETION_STARTED_EVENT_TYPE,
    PRIVACY_DELETION_STARTED_SCHEMA_VERSION,
    PrivacyCheckpointCleaner,
    PrivacyDeletionBinding,
    PrivacyReconciliationReader,
)
from ai_employee.application.use_cases.task_execution import LeasedTask, TaskLeaseMode
from ai_employee.config import Settings, get_settings
from ai_employee.domain.errors import DomainError, InternalInvariantError, StateConflictError
from ai_employee.domain.settings import WeeklyWorkingHours
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
    MailDraftModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.db.models.briefs import (
    ConversationModel,
    DailyBriefItemModel,
    DailyBriefModel,
    LLMInvocationModel,
    MessageModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    EmailAnalysisModel,
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.oauth_lifecycle import (
    lock_cleanup_refresh_events,
    lock_oauth_cleanup_identity,
    oauth_cleanup_lock_contended,
)
from ai_employee.infrastructure.db.repositories.privacy_checkpoints import (
    PostgresPrivacyCheckpointCleaner,
)
from ai_employee.infrastructure.db.repositories.privacy_deletion import (
    deletion_unavailable,
    lock_deletion_binding,
)
from ai_employee.infrastructure.db.repositories.privacy_reconciliation import (
    SqlAlchemyPrivacyReconciliationStore,
)
from ai_employee.infrastructure.db.repositories.task_execution import (
    load_deletion_started_authority,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.oauth import GoogleOAuthAdapter
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter
from ai_employee.integrations.privacy import FakePrivacyRevoker, PrivacyProviderReader
from ai_employee.workers.action_lifecycle import ActionCleanupMode, ActionLifecycleCleanup

DEFAULT_PRIVACY_BATCH_SIZE: Final[int] = 100


class OAuthTokenRevoker(Protocol):
    """定义全数据删除可调用的最小供应商撤销边界。"""

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """尽力撤销单个 OAuth token，失败由删除 Worker 本地处理。"""
        ...


class _PrivacyClock:
    """为生产提供 UTC 时间，测试通过同一 Clock 端口注入固定时刻。"""

    def now(self) -> datetime:
        """返回当前 UTC 瞬间，不借宿主机时区推断业务期限。"""
        return datetime.now(UTC)


class AllDataDeletionCompleted(BaseException):
    """表示当前任务已被删除，Worker 入口必须直接确认消息且不再写 TaskRun。

    该哨兵故意不继承 ``Exception``，避免 ``DurableTaskRunner`` 的通用错误收敛逻辑在
    已删除任务上再次写入失败状态；只允许 ``execute_task`` 的外层组合边界捕获它。
    """


class PrivacyDeletionWorker:
    """以用户范围执行可重试的来源缓存或全数据删除。"""

    name = "privacy_deletion"

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        *,
        credential_cipher: AeadCipher | None = None,
        oauth_adapters: Mapping[str, OAuthTokenRevoker] | None = None,
        privacy_reader: PrivacyReconciliationReader | None = None,
        checkpoint_cleaner: PrivacyCheckpointCleaner | None = None,
        clock: Clock | None = None,
    ) -> None:
        """注入专用会话、可选凭据解密器与供应商撤销端口。

        Args:
            session_factory: 由调用方拥有生命周期的 retention 专用会话工厂。
            credential_cipher: 生产组合根提供的 AEAD 解密器；仅 source-cache 测试可省略。
            oauth_adapters: 仅固定 Google/Microsoft 的 Task25 撤销端口；缺失时无网络。
            privacy_reader: 固定 M2 的无命令 GET 端口；生产必须装配，测试可用 Fake。
            checkpoint_cleaner: 仅具有 app 原有 checkpoint 删除能力的窄端口。
            clock: 显式 UTC Clock，所有屏障/阶段租约/最终时间使用同一个来源。
        """
        self._session_factory = session_factory
        self._credential_cipher = credential_cipher
        adapters = dict(oauth_adapters or {})
        if set(adapters) - {"google", "microsoft"}:
            raise ValueError("privacy supports only Google and Microsoft")
        self._oauth_adapters = MappingProxyType(adapters)
        self._clock = clock or _PrivacyClock()
        self._privacy_reader = privacy_reader
        self._checkpoint_cleaner = checkpoint_cleaner

    async def execute(self, task: LeasedTask) -> None:
        """按可信任务种类执行删除，未知种类明确拒绝。"""
        if task.user_id is None:
            raise ValueError("privacy deletion requires a user id")
        if task.kind == "privacy.clear_source_cache":
            await self.clear_source_cache(user_id=task.user_id, batch_size=DEFAULT_PRIVACY_BATCH_SIZE)
            return
        if task.kind == "privacy.delete_all_data":
            request_id = task.input_payload.get("deletion_request_id")
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("privacy all-data deletion requires an opaque request id")
            await self.delete_all_data(
                user_id=task.user_id,
                request_id=request_id,
                task_id=task.task_id,
                lease_owner=task.lease_owner,
                lease_mode=task.lease_mode,
                batch_size=DEFAULT_PRIVACY_BATCH_SIZE,
            )
            raise AllDataDeletionCompleted
        raise ValueError("unsupported privacy task kind")

    async def clear_source_cache(self, *, user_id: UUID, batch_size: int) -> None:
        """删除可重新同步来源，并把 Gmail/Calendar 同步状态复位为首次同步。"""
        self._validate_batch_size(batch_size)
        await ActionLifecycleCleanup(self._session_factory, clock=self._clock).clean_user(
            user_id=user_id,
            now=self._clock.now(),
            batch_size=batch_size,
            mode=ActionCleanupMode.SOURCE_CACHE,
        )
        await self._delete_user_rows(EmailAnalysisModel, user_id, batch_size)
        await self._delete_user_rows(EmailMessageModel, user_id, batch_size)
        await self._delete_user_rows(CalendarEventModel, user_id, batch_size)
        await self._delete_user_rows(EmailThreadModel, user_id, batch_size)
        # 简报只来源于 M1 的只读邮箱与日历缓存；清理缓存后它不再是可审计的有效派生物。
        await self._delete_briefs(user_id=user_id, batch_size=batch_size)
        async with self._session_factory.begin() as session:
            connection_ids = select(OAuthConnectionModel.id).where(OAuthConnectionModel.user_id == user_id)
            await session.execute(
                update(SyncCursorModel)
                .where(SyncCursorModel.connection_id.in_(connection_ids))
                .values(cursor=None, last_success_at=None, last_attempt_at=None, last_error_code=None)
            )

    async def delete_all_data(
        self,
        *,
        user_id: UUID,
        request_id: str,
        task_id: UUID,
        lease_owner: str | None,
        lease_mode: TaskLeaseMode,
        batch_size: int,
    ) -> None:
        """按唯一屏障赢家分阶段删除；只有最终事务消费恢复 TaskRun 与 authority。

        每个已提交阶段均可从同一过期 RUNNING 任务恢复。租约丢失、表锁竞争或异常只
        中止本次尝试，通用 Runner 不得把已 inactive 的精确赢家写成终态或重排队。
        """
        self._validate_batch_size(batch_size)
        if not request_id or not lease_owner:
            raise self._unavailable()
        binding = PrivacyDeletionBinding(user_id, task_id, request_id, lease_owner)
        await self._establish_barrier(binding, lease_mode=lease_mode)
        await self._after_deletion_phase(phase="barrier")
        await self._assert_winner(binding)
        await ActionLifecycleCleanup(self._session_factory, clock=self._clock).clean_user(
            user_id=user_id,
            now=self._clock.now(),
            batch_size=batch_size,
            mode=ActionCleanupMode.ALL_DATA,
            deletion_binding=binding,
        )
        await self._after_deletion_phase(phase="unclaimed_actions")
        await self._assert_winner(binding)
        await self._reconcile_claimed_actions(binding, batch_size=batch_size)
        await self._after_deletion_phase(phase="reconciliation")
        await self._delete_connection_credentials(binding)
        await self._delete_local_rows(binding, batch_size=batch_size)
        await self._after_deletion_phase(phase="local_rows_deleted")
        await self._clear_checkpoint(binding, task_id=binding.task_id)
        await self._finalize_deleted_user(binding=binding)

    @staticmethod
    def _unavailable() -> StateConflictError:
        """统一返回无内容冲突，拒绝泄露另一 task/request 或把 loser 标成删除成功。"""
        return deletion_unavailable()

    async def _lock_binding(
        self,
        session: AsyncSession,
        binding: PrivacyDeletionBinding,
    ) -> tuple[TaskRunModel, UserModel]:
        """先锁 TaskRun 再锁用户，锁后用当前时钟重验精确 request、owner 与未过期租约。"""
        return await lock_deletion_binding(
            session,
            binding=binding,
            clock=self._clock,
            require_winner=False,
        )

    async def _lock_winner(
        self,
        session: AsyncSession,
        binding: PrivacyDeletionBinding,
    ) -> tuple[TaskRunModel, UserModel]:
        """每个后续删除事务都重验完整 per-user authority 集合，不只依赖进程内绑定。"""
        return await lock_deletion_binding(session, binding=binding, clock=self._clock)

    async def _assert_winner(self, binding: PrivacyDeletionBinding) -> None:
        """在阶段边界拒绝已失去租约的旧 Worker；不写新 lease 或任何业务结果。"""
        async with self._session_factory.begin() as session:
            await self._lock_winner(session, binding)

    async def _establish_barrier(
        self,
        binding: PrivacyDeletionBinding,
        *,
        lease_mode: TaskLeaseMode,
    ) -> None:
        """唯一 true→false CAS 与精确 started 同事务；false 只接受 typed recovery 赢家。"""
        async with self._session_factory.begin() as session:
            task, user = await self._lock_binding(session, binding)
            if not user.is_active:
                if lease_mode is not TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY:
                    raise self._unavailable()
                if await load_deletion_started_authority(session, task=task) is None:
                    raise self._unavailable()
                return
            # 活动用户若已有 started，也不能靠追加第二条或 cutoff 删除来挑选赢家。
            prior = await session.scalar(
                select(AuditEventModel.id)
                .where(
                    AuditEventModel.user_id == binding.user_id,
                    AuditEventModel.event_type == PRIVACY_DELETION_STARTED_EVENT_TYPE,
                )
                .limit(1)
            )
            if prior is not None or lease_mode is not TaskLeaseMode.NORMAL:
                raise self._unavailable()
            changed = await session.scalar(
                update(UserModel)
                .where(
                    UserModel.id == binding.user_id,
                    UserModel.is_active.is_(True),
                )
                .values(is_active=False, updated_at=self._clock.now())
                .returning(UserModel.id)
            )
            if changed is None:
                raise self._unavailable()
            session.add(
                AuditEventModel(
                    user_id=binding.user_id,
                    task_id=binding.task_id,
                    event_type=PRIVACY_DELETION_STARTED_EVENT_TYPE,
                    actor_type="system",
                    actor_id=None,
                    event_metadata={
                        "schema_version": PRIVACY_DELETION_STARTED_SCHEMA_VERSION,
                        "request_id": binding.request_id,
                    },
                    created_at=self._clock.now(),
                )
            )
            await session.flush()
            if await load_deletion_started_authority(session, task=task) is None:
                raise self._unavailable()

    async def _reconcile_claimed_actions(
        self, binding: PrivacyDeletionBinding, *, batch_size: int
    ) -> None:
        """在删除前保留有界只读核对阶段；不解密可信命令也不制造未执行证明。

        现有执行 adapter 需要完整命令，不可用于 inactive 恢复。这里只读取最小执行
        事实；能安全定位的供应商只读核对由专用注入端口完成，结果不改变删除授权。
        """
        store = SqlAlchemyPrivacyReconciliationStore(self._session_factory, clock=self._clock)
        after: UUID | None = None
        while True:
            task_ids = await store.candidates(
                user_id=binding.user_id, after=after, limit=batch_size
            )
            if not task_ids:
                return
            for task_id in task_ids:
                target = await store.qualify(
                    binding=binding, task_id=task_id, now=self._clock.now()
                )
                if target is not None and self._privacy_reader is not None:
                    await self._privacy_reader.reconcile(binding=binding, target=target)
            after = task_ids[-1]

    async def _delete_connection_credentials(self, binding: PrivacyDeletionBinding) -> None:
        """每连接至多一个 token：本地凭据删除提交后才允许一次 best-effort revoke。

        表锁 NOWAIT 冲突整笔回滚并终止本次尝试，绝不能跳过未清理连接最终完成。
        崩溃发生在删除提交后时，恢复看到无凭据便不再 revoke；不另存 token 或重建凭据。
        """
        while True:
            async with self._session_factory() as session:
                connection_id = await session.scalar(
                    select(OAuthConnectionModel.id)
                    .where(
                        OAuthConnectionModel.user_id == binding.user_id,
                        exists(
                            select(EncryptedCredentialModel.id).where(
                                EncryptedCredentialModel.user_id == binding.user_id,
                                EncryptedCredentialModel.connection_id == OAuthConnectionModel.id,
                            )
                        ),
                    )
                    .order_by(OAuthConnectionModel.id)
                    .limit(1)
                )
            if connection_id is None:
                return
            token: str | None = None
            provider: str | None = None
            try:
                try:
                    async with self._session_factory.begin() as session:
                        await self._lock_winner(session, binding)
                        identity = await lock_oauth_cleanup_identity(
                            session,
                            user_id=binding.user_id,
                            connection_id=connection_id,
                        )
                        if identity is None:
                            continue
                        await lock_cleanup_refresh_events(
                            session, user_id=binding.user_id, connection_id=connection_id
                        )
                        provider = await session.scalar(
                            select(OAuthConnectionModel.provider).where(
                                OAuthConnectionModel.user_id == binding.user_id,
                                OAuthConnectionModel.id == connection_id,
                            )
                        )
                        chosen_id = identity.refresh_id or identity.access_id
                        if (
                            chosen_id is not None
                            and self._credential_cipher is not None
                            and provider in self._oauth_adapters
                        ):
                            credential = await session.scalar(
                                select(EncryptedCredentialModel).where(
                                    EncryptedCredentialModel.user_id == binding.user_id,
                                    EncryptedCredentialModel.connection_id == connection_id,
                                    EncryptedCredentialModel.id == chosen_id,
                                )
                            )
                            if credential is not None:
                                try:
                                    token = self._credential_cipher.decrypt(
                                        EncryptedValue(
                                            credential.ciphertext,
                                            credential.nonce,
                                            credential.key_version,
                                        ),
                                        self._credential_aad(
                                            binding.user_id,
                                            connection_id,
                                            credential.credential_kind,
                                        ),
                                    ).decode("utf-8")
                                except (
                                    InvalidTag,
                                    UnicodeDecodeError,
                                    EncryptionKeyVersionError,
                                    EncryptionBoundaryError,
                                ):
                                    token = None
                        deleted = (
                            await session.scalars(
                                delete(EncryptedCredentialModel)
                                .where(
                                    EncryptedCredentialModel.user_id == binding.user_id,
                                    EncryptedCredentialModel.connection_id == connection_id,
                                )
                                .returning(EncryptedCredentialModel.id)
                            )
                        ).all()
                except DBAPIError as error:
                    if not oauth_cleanup_lock_contended(error):
                        raise
                    raise self._unavailable() from None
                await self._after_user_row_batch(
                    model=EncryptedCredentialModel,
                    user_id=binding.user_id,
                    deleted_count=len(deleted),
                )
                await self._after_deletion_phase(phase="credentials_deleted")
                if token and provider in self._oauth_adapters:
                    try:
                        async with asyncio.timeout(10):
                            await self._oauth_adapters[provider].revoke(token)
                    except (httpx.HTTPError, DomainError, TimeoutError):
                        # Task25 的撤销 outcome/网络失败均不改变已经提交的本地删除事实。
                        pass
                await self._after_deletion_phase(phase="revoke_attempted")
            finally:
                token = None

    async def _delete_local_rows(self, binding: PrivacyDeletionBinding, *, batch_size: int) -> None:
        """显式子到父、有界提交所有 M2/M1 本地行，同时保留当前任务与全部 started。"""
        for model in (
            MailDraftVersionModel,
            CalendarChangeSnapshotModel,
            EmailAnalysisModel,
            EmailMessageModel,
            CalendarEventModel,
            EmailThreadModel,
            MessageModel,
            ConversationModel,
            UserSessionModel,
        ):
            await self._delete_user_rows(model, binding.user_id, batch_size, binding=binding)
        await self._delete_briefs(user_id=binding.user_id, batch_size=batch_size, binding=binding)
        await self._delete_task_graph(
            user_id=binding.user_id, batch_size=batch_size, binding=binding
        )
        for parent_model in (
            MailDraftModel,
            CalendarChangeProposalModel,
            ConnectionCapabilityModel,
            ProviderCalendarModel,
            OAuthAttemptModel,
        ):
            await self._delete_user_rows(parent_model, binding.user_id, batch_size, binding=binding)
        while True:
            async with self._session_factory.begin() as session:
                await self._lock_winner(session, binding)
                ids = (
                    await session.scalars(
                        select(SyncCursorModel.id)
                        .where(
                            SyncCursorModel.connection_id.in_(
                                select(OAuthConnectionModel.id).where(
                                    OAuthConnectionModel.user_id == binding.user_id,
                                )
                            ),
                        )
                        .limit(batch_size)
                    )
                ).all()
                if not ids:
                    break
                await session.execute(delete(SyncCursorModel).where(SyncCursorModel.id.in_(ids)))
        async with self._session_factory.begin() as session:
            _, user = await self._lock_winner(session, binding)
            # 这三个延迟归属 FK 没有 ON DELETE SET NULL。先在有效 winner 下解除引用，
            # 才能逐连接提交删除；完整匿名默认值仍由最后唯一事务再次写入。
            user.default_mail_connection_id = None
            user.default_calendar_connection_id = None
            user.default_calendar_id = None
        await self._delete_user_rows(
            OAuthConnectionModel, binding.user_id, batch_size, binding=binding
        )

    async def _after_deletion_phase(self, *, phase: str) -> None:
        """提供明确提交/回滚边界的故障注入观察点，生产不记录内容或增加外部副作用。"""
        del phase

    @staticmethod
    def _credential_aad(user_id: UUID, connection_id: UUID, kind: str) -> bytes:
        """复用 OAuth 回调和同步步骤的记录绑定 AAD 格式。"""
        return f"{user_id}:{connection_id}:{kind}".encode("ascii")

    async def _delete_briefs(
        self,
        *,
        user_id: UUID,
        batch_size: int,
        binding: PrivacyDeletionBinding | None = None,
    ) -> None:
        """先删简报条目再删父简报；全数据分支每批都重验同一赢家租约。"""
        while True:
            async with self._session_factory.begin() as session:
                if binding is not None:
                    await self._lock_winner(session, binding)
                brief_ids = (
                    await session.scalars(
                        select(DailyBriefModel.id)
                        .where(
                            DailyBriefModel.user_id == user_id,
                        )
                        .limit(batch_size)
                    )
                ).all()
                if not brief_ids:
                    return
                await session.execute(
                    delete(DailyBriefItemModel).where(DailyBriefItemModel.brief_id.in_(brief_ids))
                )
                await session.execute(
                    delete(DailyBriefModel).where(
                        DailyBriefModel.user_id == user_id,
                        DailyBriefModel.id.in_(brief_ids),
                    )
                )

    async def _delete_task_dependents(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        task_ids: Sequence[UUID],
        final: bool = False,
    ) -> None:
        """显式删除 Tool→approval→step 与审计/Outbox，最终阶段才可消费 started。

        TaskRun.user_id 已在父查询重验；无直接 user_id 的子表经这些任务 ID 限定归属。
        业务聚合/内容在调用前或后按独立外键拓扑清理，不靠 TaskRun cascade 掩盖残留。
        """
        await session.execute(
            delete(LLMInvocationModel).where(
                LLMInvocationModel.user_id == user_id,
                LLMInvocationModel.task_id.in_(task_ids),
            )
        )
        for model in (ToolExecutionModel, ApprovalRequestModel, TaskStepModel):
            await session.execute(delete(model).where(model.task_id.in_(task_ids)))
        await session.execute(
            delete(OutboxEventModel).where(OutboxEventModel.aggregate_id.in_(task_ids))
        )
        audit_query = delete(AuditEventModel).where(
            AuditEventModel.user_id == user_id,
            AuditEventModel.task_id.in_(task_ids),
        )
        if not final:
            audit_query = audit_query.where(
                AuditEventModel.event_type != PRIVACY_DELETION_STARTED_EVENT_TYPE
            )
        await session.execute(audit_query)

    async def _delete_task_graph(
        self,
        *,
        user_id: UUID,
        batch_size: int,
        binding: PrivacyDeletionBinding,
    ) -> None:
        """其他任务图有界子到父删除；当前赢家及完整 started 集一直保留到最终事务。"""
        while True:
            async with self._session_factory() as session:
                task_ids = (
                    await session.scalars(
                        select(TaskRunModel.id)
                        .where(
                            TaskRunModel.user_id == user_id,
                            TaskRunModel.id != binding.task_id,
                        )
                        .order_by(TaskRunModel.id)
                        .limit(batch_size)
                    )
                ).all()
            if not task_ids:
                break
            # app 清理与 retention 父行删除各自提交；inactive 保存门禁阻止间隙复活。
            # 不持 retention 的 Task/user 锁调用 app 连接，否则同线程会等待自己。
            for task_id in task_ids:
                await self._clear_checkpoint(binding, task_id=task_id)
            async with self._session_factory.begin() as session:
                await lock_deletion_binding(
                    session,
                    binding=binding,
                    clock=self._clock,
                    task_ids=task_ids,
                )
                await self._delete_task_dependents(session, user_id=user_id, task_ids=task_ids)
                await session.execute(
                    delete(TaskRunModel).where(
                        TaskRunModel.user_id == user_id,
                        TaskRunModel.id.in_(task_ids),
                    )
                )
        while True:
            async with self._session_factory.begin() as session:
                await self._lock_winner(session, binding)
                ids = (
                    await session.scalars(
                        select(AuditEventModel.id)
                        .where(
                            AuditEventModel.user_id == user_id,
                            AuditEventModel.event_type != PRIVACY_DELETION_STARTED_EVENT_TYPE,
                            (
                                AuditEventModel.task_id.is_(None)
                                | (AuditEventModel.task_id != binding.task_id)
                            ),
                        )
                        .order_by(AuditEventModel.id)
                        .limit(batch_size)
                    )
                ).all()
                if not ids:
                    return
                await session.execute(
                    delete(AuditEventModel).where(
                        AuditEventModel.user_id == user_id,
                        AuditEventModel.id.in_(ids),
                    )
                )

    async def _clear_checkpoint(self, binding: PrivacyDeletionBinding, *, task_id: UUID) -> None:
        """缺失 app 清理装配时 fail closed，不能把残留 checkpoint 当作已删除的用户数据。"""
        if self._checkpoint_cleaner is None:
            raise InternalInvariantError(
                error_code="privacy_checkpoint_cleaner_unavailable",
                message="Checkpoint cleanup is unavailable",
            )
        await self._checkpoint_cleaner.clear_thread(
            binding=binding, task_id=task_id, now=self._clock.now()
        )

    async def _finalize_deleted_user(self, *, binding: PrivacyDeletionBinding) -> None:
        """在唯一最终事务删除当前任务/authority、重置全部设置并追加单条完成事实。

        TaskRun→user 串行化与共享严格 parser 是唯一准入。最后的删除、匿名化与 INSERT
        不得拆成提交；中途异常使原 RUNNING/authority 回滚保留，ACK replay 则由缺失
        TaskRun 的既有获取路径直接确认，不再次进入本方法。
        """
        async with self._session_factory.begin() as session:
            task, user = await self._lock_winner(session, binding)
            # 任何残留连接意味着前一凭据/子图阶段尚未完成，绝不能提交虚假的完成审计。
            remaining_connection = await session.scalar(
                select(OAuthConnectionModel.id)
                .where(
                    OAuthConnectionModel.user_id == binding.user_id,
                )
                .limit(1)
            )
            if remaining_connection is not None:
                raise self._unavailable()
            await self._delete_task_dependents(
                session, user_id=binding.user_id, task_ids=(task.id,), final=True
            )
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == binding.user_id)
            )
            await session.delete(task)
            await session.flush()
            await self._after_deletion_phase(phase="before_final_commit")
            now = self._clock.now()
            user.email = f"deleted-{binding.user_id}@invalid.local"
            user.display_name = "Deleted User"
            user.password_hash = None
            user.is_active = False
            user.default_mail_connection_id = None
            user.default_calendar_connection_id = None
            user.default_calendar_id = None
            user.timezone = "UTC"
            user.locale = "zh-CN"
            user.brief_time = time(8)
            user.email_body_retention_days = 30
            user.source_metadata_retention_days = 180
            user.workspace_history_retention_days = 365
            user.working_hours = WeeklyWorkingHours.default().to_mapping()
            user.meeting_buffer_minutes = 10
            user.updated_at = now
            session.add(
                AuditEventModel(
                    user_id=binding.user_id,
                    task_id=None,
                    event_type="privacy.deletion_completed",
                    actor_type="system",
                    actor_id=None,
                    event_metadata={
                        "operation": "all_data_deletion",
                        "request_id": binding.request_id,
                        "completed_at": now.isoformat(),
                        "trace_id": None,
                    },
                    created_at=now,
                )
            )

    async def _delete_user_rows(
        self,
        model: type[Any],
        user_id: UUID,
        batch_size: int,
        *,
        binding: PrivacyDeletionBinding | None = None,
    ) -> None:
        """用主键子查询删除一个拥有 ``user_id`` 的表，保证每批独立提交。

        每次循环的事务仅包含一批记录，提交成功后才进入下一批。该明确的提交边界既限制
        删除时的锁范围，也让任务在进程中断后可以从剩余记录继续执行。

        Args:
            model: 仅在基础设施删除边界使用的 SQLAlchemy ORM 模型。
            user_id: 待删除数据所属的用户标识。
            batch_size: 单个事务允许删除的最大记录数。
        """
        identifier = model.id
        ownership = model.user_id
        while True:
            async with self._session_factory.begin() as session:
                if binding is not None:
                    await self._lock_winner(session, binding)
                ids = (await session.scalars(select(identifier).where(ownership == user_id).limit(batch_size))).all()
                if not ids:
                    return
                await session.execute(
                    delete(model).where(ownership == user_id, identifier.in_(ids))
                )  # type: ignore[arg-type]
            # 只在事务成功提交后暴露批次边界，测试可以在这里模拟崩溃而不回滚已删除记录。
            await self._after_user_row_batch(
                model=model,
                user_id=user_id,
                deleted_count=len(ids),
            )

    async def _after_user_row_batch(
        self,
        *,
        model: type[Any],
        user_id: UUID,
        deleted_count: int,
    ) -> None:
        """提供已提交删除批次的测试观察点，生产实现不引入额外副作用。

        Args:
            model: 刚完成删除的 ORM 模型，用于测试精确选择崩溃阶段。
            user_id: 当前删除任务的用户标识。
            deleted_count: 已在本次事务中持久删除的记录数量。
        """
        del model, user_id, deleted_count

    @staticmethod
    def _validate_batch_size(batch_size: int) -> None:
        """拒绝零、负数或过大的删除批量，防止错误配置扩大锁范围。"""
        if not 1 <= batch_size <= 1_000:
            raise ValueError("privacy batch_size must be between 1 and 1000")


@lru_cache
def build_privacy_deletion_worker() -> PrivacyDeletionWorker:
    """使用 retention 专用 DSN 构建删除器，并把连接池所有权交给 Worker 入口。

    专用 DSN 必须由 Secret 文件注入；开发/测试未挂载该文件时只回退已验证的应用 DSN，
    避免为隐私任务读取第二套未受控配置。
    """
    settings = get_settings()
    if not settings.retention_database_url_file.is_file():
        if settings.app_env == "production":
            raise RuntimeError("RETENTION_DATABASE_URL_FILE is required in production")
        database_url = settings.database_url
    else:
        database_url = settings.read_secret_file(settings.retention_database_url_file).get_secret_value()
    cipher = AeadCipher.from_file(settings.app_master_key_file)
    adapters = _build_oauth_revokers(settings)
    sessions = build_session_factory(database_url)
    clock = _PrivacyClock()
    # test mode 的 MockTransport 没有网络回退；即使存在合成 access 也只能得到 unknown。
    transport = (
        httpx.MockTransport(lambda _request: httpx.Response(204))
        if settings.app_test_mode
        else None
    )
    return PrivacyDeletionWorker(
        sessions,
        credential_cipher=cipher,
        oauth_adapters=adapters,
        clock=clock,
        privacy_reader=PrivacyProviderReader(
            store=SqlAlchemyPrivacyReconciliationStore(sessions, clock=clock),
            cipher=cipher,
            clock=clock,
            transport=transport,
        ),
        checkpoint_cleaner=PostgresPrivacyCheckpointCleaner(
            settings.checkpoint_database_url, clock=clock
        ),
    )


def _build_oauth_revokers(settings: Settings) -> Mapping[str, OAuthTokenRevoker]:
    """固定两家 Task25 撤销适配器，配置不完整的供应商没有撤销能力；测试仅使用 Fake。"""
    if settings.app_test_mode:
        return {
            provider: FakePrivacyRevoker(provider=provider) for provider in ("google", "microsoft")
        }
    adapters: dict[str, OAuthTokenRevoker] = {}
    for provider, adapter_type, client_id, redirect_uri, secret_file in (
        (
            "google",
            GoogleOAuthAdapter,
            settings.google_client_id,
            settings.google_redirect_uri,
            settings.google_client_secret_file,
        ),
        (
            "microsoft",
            MicrosoftOAuthAdapter,
            settings.microsoft_client_id,
            settings.microsoft_redirect_uri,
            settings.microsoft_client_secret_file,
        ),
    ):
        if client_id and redirect_uri:
            secret = settings.read_secret_file(secret_file).get_secret_value()
            adapters[provider] = adapter_type(client_id, secret, redirect_uri)
    return adapters
