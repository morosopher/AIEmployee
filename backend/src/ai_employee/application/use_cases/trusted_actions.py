"""把本地不可变版本冻结为单操作、哈希绑定且记录加密的审批任务。

用例先在短事务内复用提交幂等事实，再锁定草稿或提案并重新验证写入门禁、连接能力、
目录权限、收件人/参会人上限与 ETag。只有全部纯校验通过后才生成稳定 ID、执行无副作用
provider preflight、规范化命令、计算哈希并加密；所有持久 mutation 由同一事务端口完成。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import cast
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag

from ai_employee.application.commands import (
    TrustedCommand,
    TrustedCommandValidationError,
    canonical_command_json,
    parse_trusted_command,
    trusted_command_hash,
)
from ai_employee.application.ports.encryption import (
    EncryptionBoundaryError,
    EncryptionKeyVersionError,
)
from ai_employee.application.ports.trusted_actions import (
    CalendarProposalSubmissionSnapshot,
    ExistingTrustedActionSubmission,
    MailDraftSubmissionSnapshot,
    ProviderWriteOutcome,
    RequestStartDisposition,
    TrustedActionAdapterRegistry,
    TrustedActionCommandCipher,
    TrustedActionDispatchSnapshot,
    TrustedActionExecutionSnapshot,
    TrustedActionPreflightRegistry,
    TrustedActionRequestStartAuthorization,
    TrustedActionRisk,
    TrustedActionSubmission,
    TrustedActionSubmissionResult,
    TrustedActionSubmissionTransactionFactory,
    TrustedActionWritePolicy,
    durable_retry_summary_is_valid,
    trusted_action_idempotency_key,
    trusted_execution_binding_matches,
)
from ai_employee.domain.actions import (
    CalendarProposalStatus,
    MailDraftStatus,
    ProviderWriteOutcomeKind,
    ToolExecutionStatus,
)
from ai_employee.domain.calendar_actions import NotificationPolicy, calendar_client_event_id
from ai_employee.domain.connections import CapabilityStatus, canonical_provider_identity_key
from ai_employee.domain.errors import StateConflictError, TransientProviderError
from ai_employee.domain.mail_actions import MailMode
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus
from ai_employee.infrastructure.security.action_payloads import ActionPayloadFormatError

APPROVAL_COMMAND_CONTENT_KIND = "approval_command"
APPROVAL_TTL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class TrustedActionGraphFacts:
    """LangGraph 可 checkpoint 的审批哈希与持久决定投影。"""

    payload_hash: str
    decision: str | None


class CalendarProposalSubmissionNotFoundError(Exception):
    """表示日历提交事务按用户锁定时未找到提案，供 API 统一隐藏归属。"""


class _TrustedActionIntegrityError(Exception):
    """仅在进程内标记冻结命令与持久标识不一致，且永不携带命令内容。"""


class TrustedActionAttemptAbandoned(Exception):
    """表示当前 Worker 不得完成任务，后续投递只能从持久事实恢复。"""


class TrustedActionExecutionUseCase:
    """以 PostgreSQL claim 事实驱动一次精确真实写入或只读结果核对。

    首次 claim 在同一事务重查用户、租约、审批、五分钟截止、哈希、三层写开关、
    连接能力与规范账户身份；一旦 ToolExecution 已存在，后续恢复只依赖该持久授权事实，
    不会因审批截止已过而撤销一个已经按时完成的 claim。完整命令只在 claim 提交后解密，
    并在独立 request-start 事务之前重新验证哈希及标识绑定。
    """

    def __init__(
        self,
        *,
        transactions: TrustedActionSubmissionTransactionFactory,
        adapters: TrustedActionAdapterRegistry,
        write_policy: TrustedActionWritePolicy,
        clock: Callable[[], datetime],
        id_factory: Callable[[], UUID] = uuid4,
        dispose: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """保存短事务工厂、固定 adapter、写门禁与可替换时钟/ID 来源。"""
        self._transactions = transactions
        self._adapters = adapters
        self._write_policy = write_policy
        self._clock = clock
        self._id_factory = id_factory
        self._dispose = dispose

    async def load_graph_facts(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
    ) -> TrustedActionGraphFacts:
        """读取 identifier-only Graph 事实，并拒绝 checkpoint 哈希被替换。

        初次调用允许 ``expected_payload_hash`` 为空，由数据库填入冻结哈希；恢复调用必须
        使用规范 SHA-256 并做常量时间比较。审批决定始终来自 PostgreSQL，而非 resume 值。
        """
        async with self._transactions() as transaction:
            facts = await transaction.load_graph_facts(
                task_id=task_id,
                approval_id=approval_id,
                operation_id=operation_id,
            )
        if facts is None:
            raise _trusted_action_unavailable()
        payload_hash, decision = facts
        if not _canonical_payload_hash(payload_hash) or (
            expected_payload_hash and not _payload_hashes_match(payload_hash, expected_payload_hash)
        ):
            raise _trusted_action_unavailable()
        return TrustedActionGraphFacts(payload_hash=payload_hash, decision=decision)

    async def claim(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        lease_owner: str,
    ) -> None:
        """原子创建唯一 ToolExecution，或安全复用先前已提交的 claim。

        授权失败需要与任务/审批状态原子持久化时，方法先正常退出事务使失败事实提交，
        再向 Graph 抛出稳定冲突；因此不会因异常触发上下文回滚而遗失失效记录。
        """
        _validate_lease_owner(lease_owner)
        deferred_error: StateConflictError | None = None
        async with self._transactions() as transaction:
            snapshot = await transaction.lock_execution(
                task_id=task_id,
                approval_id=approval_id,
                operation_id=operation_id,
            )
            if snapshot is None:
                raise _trusted_action_unavailable()
            # 首次授权必须使用全部行锁取得后的 PostgreSQL 权威时间。应用时钟可能在
            # 等锁期间跨过租约或审批截止点，不能作为创建外部写授权事实的依据。
            now = _utc_now(snapshot.database_now)
            if not _payload_hashes_match(snapshot.payload_hash, expected_payload_hash):
                deferred_error = _trusted_action_unavailable()
            elif snapshot.execution is not None:
                _validate_existing_claim(snapshot, lease_owner=lease_owner, now=now)
                return
            elif not _current_task_lease(snapshot, lease_owner=lease_owner, now=now):
                raise _trusted_action_unavailable()
            elif snapshot.approval_status != ApprovalStatus.APPROVED.value:
                raise _approval_conflict()
            else:
                deferred_error = self._new_claim_error(snapshot, now=now)

            if deferred_error is None:
                # adapter 必须在 claim 前已经由固定组合根组装；否则不能留下一个永远无法
                # dispatch、却已把本地对象推进 executing 的半授权 ToolExecution。
                try:
                    self._adapters.trusted_action_adapter(
                        provider=snapshot.provider,
                        action=snapshot.action,
                    )
                except StateConflictError as error:
                    deferred_error = error
                else:
                    await transaction.create_tool_claim(
                        snapshot=snapshot,
                        execution_id=self._id_factory(),
                        idempotency_key=_tool_idempotency_key(snapshot),
                        claimed_at=now,
                    )
                    return

            await transaction.fail_unclaimed_action(
                snapshot=snapshot,
                error_code=deferred_error.error_code,
                failed_at=now,
            )
        if deferred_error is None:  # pragma: no cover - 上述分支已经穷尽。
            raise _trusted_action_unavailable()
        raise deferred_error

    async def execute_or_reconcile(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        lease_owner: str,
        may_retry_write: bool = True,
    ) -> None:
        """从持久 ToolExecution 选择首次写、明确安全重试、只读核对或终态复用。

        ``request_started_at`` 在 adapter 写调用前独立提交。并发 Graph 重放即使同时读到
        ``claimed``，也只有该 CAS 的赢家能调用 ``execute``；``executing``、
        ``reconciling`` 与异常的 claimed-with-start 只能调用只读 ``reconcile``。
        """
        _validate_lease_owner(lease_owner)
        if type(may_retry_write) is not bool:
            raise TypeError("may_retry_write must be bool")
        snapshot: TrustedActionDispatchSnapshot | None = None
        async with self._transactions() as transaction:
            snapshot = await transaction.load_dispatch(
                task_id=task_id,
                approval_id=approval_id,
                operation_id=operation_id,
            )
        if snapshot is None:
            raise _trusted_action_unavailable()
        status = snapshot.execution.status
        if not _dispatch_execution_binding_is_valid(snapshot):
            raise _trusted_action_unavailable()
        if status in {
            ToolExecutionStatus.SUCCEEDED,
            ToolExecutionStatus.CONFIRMED_FAILED,
            ToolExecutionStatus.NEEDS_ATTENTION,
        }:
            # 终态是已经提交的外部副作用结论；即使命令内容已按保留策略清除，或当前
            # Worker 没有组装写 adapter，也只能复用结论，不能解密或改写既有事实。
            return
        if status is ToolExecutionStatus.RETRYABLE_FAILED and not (
            snapshot.execution.request_started_at is not None
            and snapshot.execution.write_attempt_count > 0
            and durable_retry_summary_is_valid(snapshot.execution.result_summary)
        ):
            raise _trusted_action_unavailable()
        try:
            async with self._transactions() as transaction:
                command_payload = await transaction.load_command(
                    user_id=snapshot.user_id,
                    approval_id=snapshot.approval_id,
                )
                if command_payload is None:
                    raise _TrustedActionIntegrityError
            command = _validated_dispatch_command(
                command_payload,
                snapshot=snapshot,
                expected_payload_hash=expected_payload_hash,
            )
        except (
            ActionPayloadFormatError,
            EncryptionBoundaryError,
            EncryptionKeyVersionError,
            InvalidTag,
            TrustedCommandValidationError,
            _TrustedActionIntegrityError,
            TypeError,
            ValueError,
        ):
            if snapshot.execution.request_started_at is not None:
                await self._preserve_started_attempt(
                    snapshot=snapshot,
                    lease_owner=lease_owner,
                )
                raise TrustedActionAttemptAbandoned from None
            await self._persist_integrity_failure(
                snapshot=snapshot,
                failed_at=_utc_now(self._clock()),
                lease_owner=lease_owner,
            )
            raise _trusted_action_unavailable() from None
        except StateConflictError as error:
            if error.error_code != "trusted_action_unavailable":
                raise
            if snapshot.execution.request_started_at is not None:
                await self._preserve_started_attempt(
                    snapshot=snapshot,
                    lease_owner=lease_owner,
                )
                raise TrustedActionAttemptAbandoned from None
            await self._persist_integrity_failure(
                snapshot=snapshot,
                failed_at=_utc_now(self._clock()),
                lease_owner=lease_owner,
            )
            raise _trusted_action_unavailable() from None
        except BaseException:
            if snapshot.execution.request_started_at is not None:
                await self._preserve_started_attempt(
                    snapshot=snapshot,
                    lease_owner=lease_owner,
                )
            raise

        from_reconciliation = status in {
            ToolExecutionStatus.EXECUTING,
            ToolExecutionStatus.RECONCILING,
        } or (
            status is ToolExecutionStatus.CLAIMED
            and snapshot.execution.request_started_at is not None
        )
        try:
            adapter = self._adapters.trusted_action_adapter(
                provider=snapshot.provider,
                action=snapshot.action,
            )
        except BaseException:
            if snapshot.execution.request_started_at is not None:
                await self._preserve_started_attempt(
                    snapshot=snapshot,
                    lease_owner=lease_owner,
                )
            raise

        if (
            status
            in {
                ToolExecutionStatus.CLAIMED,
                ToolExecutionStatus.RETRYABLE_FAILED,
            }
            and not from_reconciliation
        ):
            request_start_error_code: str | None = None
            try:
                async with self._transactions() as transaction:
                    request_start = await transaction.mark_request_started(
                        snapshot=snapshot,
                        lease_owner=lease_owner,
                        authorize=self._request_start_authorization_error,
                    )
                    if request_start.disposition is RequestStartDisposition.INVALIDATED:
                        request_start_error_code = request_start.error_code
            except BaseException:
                # request-start 提交后的 ACK 丢失与真实 rollback 对调用方形状相同；用
                # dispatch 冻结投影做幂等 abandon，数据库只会释放确已开始的同一尝试。
                await self._preserve_started_attempt(
                    snapshot=snapshot,
                    lease_owner=lease_owner,
                )
                raise
            if request_start_error_code is not None:
                raise StateConflictError(
                    error_code=request_start_error_code,
                    message="trusted action authorization changed before request start",
                )
            if request_start.disposition is not RequestStartDisposition.STARTED:
                # 租约已丢失或另一调用已经提交 request-start 时，本调用绝不能释放赢家
                # 的 live lease，也不能让 Graph/Runner 把 loser 当作成功完成。
                raise TrustedActionAttemptAbandoned
        elif not from_reconciliation:
            raise _trusted_action_unavailable()

        try:
            outcome = (
                await adapter.reconcile(command, snapshot.execution)
                if from_reconciliation
                else await adapter.execute(command)
            )
            if type(outcome) is not ProviderWriteOutcome:
                raise TypeError("trusted action adapter returned an invalid outcome")
            completed_at = _utc_now(self._clock())
            async with self._transactions() as transaction:
                await transaction.persist_provider_outcome(
                    snapshot=snapshot,
                    outcome=outcome,
                    completed_at=completed_at,
                    from_reconciliation=from_reconciliation,
                    may_retry_write=may_retry_write,
                    lease_owner=lease_owner,
                )
        except BaseException:
            # 只有本调用已赢得 request-start 或正持有恢复租约时才走到这里；provider、
            # 结果 CAS 或 commit-ACK 异常均先释放精确未决尝试，再保留原控制流。
            await self._preserve_started_attempt(
                snapshot=snapshot,
                lease_owner=lease_owner,
            )
            raise
        if outcome.kind is ProviderWriteOutcomeKind.UNKNOWN:
            # Repository 已保留 request-start 与 UNKNOWN 摘要并释放租约；专属 TaskStep
            # 将该控制流映射为 Runner no-finish，禁止通用成功/失败终态覆盖未决事实。
            raise TrustedActionAttemptAbandoned
        if (
            outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
            and outcome.retryable
            and may_retry_write
        ):
            # 先提交明确“未应用”结果，再让 DurableTaskRunner 写入唯一 retry_scheduled
            # Outbox。只有该持久状态允许下一轮重新进入 adapter.execute。
            raise TransientProviderError(
                error_code=outcome.error_code or "provider_write_retryable",
                message="provider confirmed that the trusted action was not applied",
                retry_after=outcome.retry_after_seconds,
            )

    async def finalize(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        decision: str,
        lease_owner: str,
    ) -> None:
        """完成拒绝分支；批准分支只验证持久决定，不覆盖工具终态。"""
        _validate_lease_owner(lease_owner)
        if decision == ApprovalStatus.REJECTED.value:
            async with self._transactions() as transaction:
                rejected_facts = await transaction.load_graph_facts(
                    task_id=task_id,
                    approval_id=approval_id,
                    operation_id=operation_id,
                )
                if (
                    rejected_facts is None
                    or rejected_facts[1] != ApprovalStatus.REJECTED.value
                    or not _payload_hashes_match(rejected_facts[0], expected_payload_hash)
                ):
                    raise _approval_conflict()
                await transaction.finalize_rejected(
                    task_id=task_id,
                    approval_id=approval_id,
                    operation_id=operation_id,
                    lease_owner=lease_owner,
                    finished_at=_utc_now(self._clock()),
                )
            return
        if decision != ApprovalStatus.APPROVED.value:
            raise _approval_conflict()
        approved_facts = await self.load_graph_facts(
            task_id=task_id,
            approval_id=approval_id,
            operation_id=operation_id,
            expected_payload_hash=expected_payload_hash,
        )
        if approved_facts.decision != ApprovalStatus.APPROVED.value:
            raise _approval_conflict()

    async def abandon_started_attempt(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        lease_owner: str,
    ) -> bool:
        """从标识重建并认证冻结投影后释放中断租约，供 Graph 外层故障恢复。

        Args:
            task_id: 当前可信动作任务 ID。
            approval_id: 冻结审批 ID。
            operation_id: 稳定操作 ID。
            expected_payload_hash: Graph 中唯一保存的内容无关冻结哈希。
            lease_owner: 当前 DurableTaskRunner owner。

        Returns:
            Repository 已确认并保留同一未决尝试时为 ``True``；请求尚未开始、事实已
            终结或租约已被另一个 owner 接管时为 ``False``。
        """
        _validate_lease_owner(lease_owner)
        if not _canonical_payload_hash(expected_payload_hash):
            return False
        try:
            async with self._transactions() as transaction:
                snapshot = await transaction.load_dispatch(
                    task_id=task_id,
                    approval_id=approval_id,
                    operation_id=operation_id,
                )
            if (
                snapshot is None
                or not _dispatch_execution_binding_is_valid(snapshot)
                or not _payload_hashes_match(snapshot.payload_hash, expected_payload_hash)
                or snapshot.execution.request_started_at is None
                or snapshot.execution.status
                not in {
                    ToolExecutionStatus.EXECUTING,
                    ToolExecutionStatus.RECONCILING,
                }
            ):
                return False
            # Graph 外异常没有 request-start 前的进程内 snapshot，因此必须重新认证
            # AEAD 命令并绑定 action/schema/operation/connection/proposal/calendar 后才可
            # 使用当前投影。完整命令只在本地内存短暂存在，绝不写入 checkpoint。
            async with self._transactions() as transaction:
                command_payload = await transaction.load_command(
                    user_id=snapshot.user_id,
                    approval_id=snapshot.approval_id,
                )
            if command_payload is None:
                return False
            _validated_dispatch_command(
                command_payload,
                snapshot=snapshot,
                expected_payload_hash=expected_payload_hash,
            )
        except (
            ActionPayloadFormatError,
            EncryptionBoundaryError,
            EncryptionKeyVersionError,
            InvalidTag,
            StateConflictError,
            TrustedCommandValidationError,
            _TrustedActionIntegrityError,
            TypeError,
            ValueError,
        ):
            return False
        return await self._abandon_snapshot(snapshot=snapshot, lease_owner=lease_owner)

    async def _abandon_snapshot(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        lease_owner: str,
    ) -> bool:
        """把未 checkpoint 化的 request-start 前投影交给锁内 abandon CAS。"""
        async with self._transactions() as transaction:
            return await transaction.abandon_started_attempt(
                snapshot=snapshot,
                lease_owner=lease_owner,
            )

    async def _preserve_started_attempt(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        lease_owner: str,
    ) -> None:
        """在异常传播前屏蔽当前调用并提交精确 request-start abandon CAS。

        Repository 返回 ``False`` 表示请求未开始、冻结绑定已变化或 lease 已失效；这些
        情况都不能回退覆盖。若持久保护本身异常，普通异常转换为放弃完成，避免 Runner
        尝试通用终态；任务取消则仍保留原取消语义，由到期租约允许后续恢复。
        """
        try:
            await asyncio.shield(
                self._abandon_snapshot(
                    snapshot=snapshot,
                    lease_owner=lease_owner,
                )
            )
        except Exception:  # noqa: BLE001 - 保护边界失败时禁止退回通用终态。
            current_task = asyncio.current_task()
            if current_task is None or not current_task.cancelling():
                raise TrustedActionAttemptAbandoned from None

    async def dispose(self) -> None:
        """释放本用例独占的会话工厂；共享组合根可省略该回调。"""
        if self._dispose is not None:
            await self._dispose()

    def _new_claim_error(
        self,
        snapshot: TrustedActionExecutionSnapshot,
        *,
        now: datetime,
    ) -> StateConflictError | None:
        """返回首次 claim 的稳定失败，不读取或返回任何命令内容。"""
        if not snapshot.user_is_active:
            return _trusted_action_unavailable()
        if snapshot.approval_version <= 0 or snapshot.approved_execution_deadline_at is None:
            return _approval_conflict()
        deadline = _utc_now(snapshot.approved_execution_deadline_at)
        if deadline <= now:
            return StateConflictError(
                error_code="approval_execution_deadline_expired",
                message="approved trusted action execution deadline expired",
            )
        if not self._write_policy.provider_writes_enabled(snapshot.provider):
            return StateConflictError(
                error_code="external_writes_disabled",
                message="external writes are disabled",
            )
        capability_error = _execution_capability_error(snapshot)
        if capability_error is not None:
            return capability_error
        try:
            provider_identity_key = canonical_provider_identity_key(
                snapshot.provider,
                snapshot.provider_tenant_id,
                snapshot.provider_account_id,
            )
        except ValueError:
            return _external_write_account_not_allowed()
        if not self._write_policy.write_account_allowed(provider_identity_key):
            return _external_write_account_not_allowed()
        return None

    def _request_start_authorization_error(
        self,
        authorization: TrustedActionRequestStartAuthorization,
    ) -> str | None:
        """在 Repository 持有固定行锁时复用当前应用写策略并返回稳定错误码。"""
        if not authorization.user_is_active:
            return "trusted_action_unavailable"
        if not self._write_policy.provider_writes_enabled(authorization.provider):
            return "external_writes_disabled"
        capability_error = _capability_error_code(
            connection_status=authorization.connection_status,
            read_capability_status=authorization.read_capability_status,
            write_capability_status=authorization.write_capability_status,
            write_capability_error_code=authorization.write_capability_error_code,
            calendar_can_write=authorization.calendar_can_write,
        )
        if capability_error is not None:
            return capability_error
        try:
            provider_identity_key = canonical_provider_identity_key(
                authorization.provider,
                authorization.provider_tenant_id,
                authorization.provider_account_id,
            )
        except ValueError:
            return "external_write_account_not_allowed"
        if not self._write_policy.write_account_allowed(provider_identity_key):
            return "external_write_account_not_allowed"
        return None

    async def _persist_integrity_failure(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        failed_at: datetime,
        lease_owner: str,
    ) -> None:
        """用新事务提交零 provider 调用的认证失败终态。"""
        async with self._transactions() as transaction:
            await transaction.fail_claimed_integrity(
                snapshot=snapshot,
                failed_at=failed_at,
                lease_owner=lease_owner,
            )


class SubmitMailDraftUseCase:
    """冻结当前邮件草稿版本为精确 ``mail.send`` 加密审批。"""

    def __init__(
        self,
        *,
        transactions: TrustedActionSubmissionTransactionFactory,
        preflights: TrustedActionPreflightRegistry,
        write_policy: TrustedActionWritePolicy,
        command_cipher: TrustedActionCommandCipher,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        """保存提交事务、固定适配器注册表、安全门禁、加密器与显式 ID 来源。"""
        self._transactions = transactions
        self._preflights = preflights
        self._write_policy = write_policy
        self._command_cipher = command_cipher
        self._id_factory = id_factory

    async def execute(
        self,
        *,
        user_id: UUID,
        draft_id: UUID,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> TrustedActionSubmissionResult:
        """提交一个当前编辑态草稿版本，重放同键时返回原任务。

        Args:
            user_id: 当前认证用户。
            draft_id: 待冻结本地草稿。
            expected_version: 客户端观察到的不可变版本。
            idempotency_key: 用户范围内提交幂等键。
            now: 带时区提交瞬间，也是邮件 ``Date`` 与审批期限基准。

        Returns:
            不含正文、地址或密文的任务与审批标识。

        Raises:
            StateConflictError: 版本、状态、能力、写门禁或线程绑定已失效。
            ValueError: 时间、幂等键或严格可信命令不合法。
        """
        checked_now = _utc_now(now)
        _validate_request(expected_version=expected_version, idempotency_key=idempotency_key)
        async with self._transactions() as transaction:
            existing = await transaction.find_existing_submission(
                user_id=user_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                return _reuse_existing(
                    existing,
                    proposal_kind="mail_draft",
                    proposal_id=draft_id,
                    proposal_version=expected_version,
                )
            snapshot = await transaction.lock_mail_draft(user_id=user_id, draft_id=draft_id)
            if snapshot is None:
                raise _version_conflict("draft_version_conflict")
            _validate_mail_snapshot(snapshot, expected_version=expected_version)
            _validate_write_gate(
                provider=snapshot.provider,
                provider_identity_key=snapshot.provider_identity_key,
                write_policy=self._write_policy,
            )
            if await transaction.proposal_version_is_consumed(
                user_id=user_id,
                proposal_kind="mail_draft",
                proposal_id=draft_id,
                proposal_version=expected_version,
            ):
                raise _version_conflict("draft_version_conflict")

            operation_id, task_id, step_id, approval_id = self._new_ids()
            command_payload = _mail_command_payload(
                snapshot=snapshot,
                operation_id=operation_id,
                now=checked_now,
            )
            submission = self._freeze(
                user_id=user_id,
                task_id=task_id,
                step_id=step_id,
                approval_id=approval_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                proposal_kind="mail_draft",
                proposal_id=draft_id,
                proposal_version=expected_version,
                provider=snapshot.provider,
                risk_level=TrustedActionRisk.HIGH,
                command_payload=command_payload,
                now=checked_now,
            )
            await transaction.create_submission(submission)
        return _result(submission)

    def _new_ids(self) -> tuple[UUID, UUID, UUID, UUID]:
        """按 operation、task、step、approval 固定顺序生成四个稳定 ID。"""
        return (
            self._id_factory(),
            self._id_factory(),
            self._id_factory(),
            self._id_factory(),
        )

    def _freeze(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        step_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        idempotency_key: str,
        proposal_kind: str,
        proposal_id: UUID,
        proposal_version: int,
        provider: str,
        risk_level: TrustedActionRisk,
        command_payload: Mapping[str, object],
        now: datetime,
    ) -> TrustedActionSubmission:
        """执行严格规范化、纯 preflight、哈希和记录绑定加密。"""
        canonical = _canonical_command(command_payload)
        command = parse_trusted_command(canonical)
        action = _command_text(canonical, "action")
        schema_version = _command_text(canonical, "schema_version")
        preflight = self._preflights.trusted_action_preflight(
            provider=provider,
            action=action,
        )
        if preflight.provider != provider:
            raise _provider_action_unavailable()
        preflight_result = preflight.validate_for_approval(command)
        encrypted = self._command_cipher.encrypt_json(
            canonical,
            user_id=user_id,
            record_id=approval_id,
            content_kind=APPROVAL_COMMAND_CONTENT_KIND,
            action=action,
            schema_version=schema_version,
        )
        return TrustedActionSubmission(
            user_id=user_id,
            task_id=task_id,
            step_id=step_id,
            approval_id=approval_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            action=action,
            schema_version=schema_version,
            risk_level=risk_level,
            proposal_kind=proposal_kind,
            proposal_id=proposal_id,
            proposal_version=proposal_version,
            payload={"storage": "encrypted", "schema_version": schema_version},
            payload_hash=trusted_command_hash(canonical),
            encrypted_command=encrypted,
            warnings=preflight_result.warnings,
            expires_at=now + APPROVAL_TTL,
            available_at=now,
        )


class SubmitCalendarProposalUseCase:
    """冻结当前日历提案版本为 create、update 或 restore 单操作审批。"""

    def __init__(
        self,
        *,
        transactions: TrustedActionSubmissionTransactionFactory,
        preflights: TrustedActionPreflightRegistry,
        write_policy: TrustedActionWritePolicy,
        command_cipher: TrustedActionCommandCipher,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        """复用同一提交冻结器，日历用例只保留自己的锁定与命令映射流程。"""
        self._submission_support = SubmitMailDraftUseCase(
            transactions=transactions,
            preflights=preflights,
            write_policy=write_policy,
            command_cipher=command_cipher,
            id_factory=id_factory,
        )

    async def execute(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> TrustedActionSubmissionResult:
        """锁定并提交一个确认完整的当前日历提案版本。"""
        checked_now = _utc_now(now)
        _validate_request(expected_version=expected_version, idempotency_key=idempotency_key)
        async with self._submission_support._transactions() as transaction:
            existing = await transaction.find_existing_submission(
                user_id=user_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                return _reuse_existing(
                    existing,
                    proposal_kind="calendar_proposal",
                    proposal_id=proposal_id,
                    proposal_version=expected_version,
                )
            snapshot = await transaction.lock_calendar_proposal(
                user_id=user_id,
                proposal_id=proposal_id,
            )
            if snapshot is None:
                # 不存在与跨用户都由用户范围锁查询收敛到同一信号；API 可以据此返回
                # 404，而无需在独立原子提交事务外再持有一个请求级 CRUD 事务。
                raise CalendarProposalSubmissionNotFoundError
            _validate_calendar_snapshot(snapshot, expected_version=expected_version)
            _validate_write_gate(
                provider=snapshot.provider,
                provider_identity_key=snapshot.provider_identity_key,
                write_policy=self._submission_support._write_policy,
            )
            if await transaction.proposal_version_is_consumed(
                user_id=user_id,
                proposal_kind="calendar_proposal",
                proposal_id=proposal_id,
                proposal_version=expected_version,
            ):
                raise _version_conflict("proposal_version_conflict")

            operation_id, task_id, step_id, approval_id = self._submission_support._new_ids()
            command_payload = _calendar_command_payload(snapshot, operation_id=operation_id)
            risk = (
                TrustedActionRisk.MEDIUM
                if not snapshot.attendees
                and snapshot.notification_policy is NotificationPolicy.NONE
                else TrustedActionRisk.HIGH
            )
            submission = self._submission_support._freeze(
                user_id=user_id,
                task_id=task_id,
                step_id=step_id,
                approval_id=approval_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                proposal_kind="calendar_proposal",
                proposal_id=proposal_id,
                proposal_version=expected_version,
                provider=snapshot.provider,
                risk_level=risk,
                command_payload=command_payload,
                now=checked_now,
            )
            await transaction.create_submission(submission)
        return _result(submission)


def _validated_dispatch_command(
    payload: Mapping[str, object],
    *,
    snapshot: TrustedActionDispatchSnapshot,
    expected_payload_hash: str,
) -> TrustedCommand:
    """重新解析解密命令，并同时绑定审批、Graph、操作、连接及本地版本。"""
    command = parse_trusted_command(payload)
    canonical_hash = trusted_command_hash(payload)
    if (
        not _payload_hashes_match(canonical_hash, snapshot.payload_hash)
        or not _payload_hashes_match(canonical_hash, expected_payload_hash)
        or command.operation_id != snapshot.operation_id
        or command.connection_id != snapshot.connection_id
        or command.action != snapshot.action
        or command.schema_version != snapshot.schema_version
    ):
        raise _TrustedActionIntegrityError
    if snapshot.proposal_kind == "mail_draft":
        if (
            command.action != "mail.send"
            or getattr(command, "draft_id", None) != snapshot.proposal_id
            or getattr(command, "draft_version", None) != snapshot.proposal_version
        ):
            raise _TrustedActionIntegrityError
    elif snapshot.proposal_kind == "calendar_proposal":
        if (
            command.action
            not in {
                "calendar.create",
                "calendar.update",
                "calendar.restore",
            }
            or getattr(command, "calendar_id", None) != snapshot.calendar_id
        ):
            raise _TrustedActionIntegrityError
    else:
        raise _TrustedActionIntegrityError
    return command


def _validate_existing_claim(
    snapshot: TrustedActionExecutionSnapshot,
    *,
    lease_owner: str,
    now: datetime,
) -> None:
    """验证既有 ToolExecution 的恢复资格，但不重新套用首次审批截止。"""
    execution = snapshot.execution
    if execution is None:
        raise _trusted_action_unavailable()
    if (
        execution.task_id != snapshot.task_id
        or execution.approval_id != snapshot.approval_id
        or execution.operation_id != snapshot.operation_id
        or execution.provider != snapshot.provider
    ):
        raise _trusted_action_unavailable()
    if execution.status in {
        ToolExecutionStatus.SUCCEEDED,
        ToolExecutionStatus.CONFIRMED_FAILED,
        ToolExecutionStatus.NEEDS_ATTENTION,
    }:
        return
    if not snapshot.user_is_active or not _current_task_lease(
        snapshot,
        lease_owner=lease_owner,
        now=now,
    ):
        raise _trusted_action_unavailable()


def _current_task_lease(
    snapshot: TrustedActionExecutionSnapshot,
    *,
    lease_owner: str,
    now: datetime,
) -> bool:
    """要求当前调用者仍持有严格晚于本次判断时刻的 RUNNING 租约。"""
    expires_at = snapshot.task_lease_expires_at
    return (
        snapshot.task_status is TaskStatus.RUNNING
        and snapshot.task_lease_owner == lease_owner
        and expires_at is not None
        and _utc_now(expires_at) > now
    )


def _execution_capability_error(
    snapshot: TrustedActionExecutionSnapshot,
) -> StateConflictError | None:
    """把 claim 时的新鲜连接/能力事实映射为稳定、无内容失败。"""
    error_code = _capability_error_code(
        connection_status=snapshot.connection_status,
        read_capability_status=snapshot.read_capability_status,
        write_capability_status=snapshot.write_capability_status,
        write_capability_error_code=snapshot.write_capability_error_code,
        calendar_can_write=snapshot.calendar_can_write,
    )
    if error_code == "connection_scope_missing":
        return StateConflictError(
            error_code="connection_scope_missing",
            message="provider write capability requires reauthorization",
        )
    if error_code is not None:
        return _connection_capability_disabled()
    return None


def _capability_error_code(
    *,
    connection_status: str,
    read_capability_status: CapabilityStatus,
    write_capability_status: CapabilityStatus,
    write_capability_error_code: str | None,
    calendar_can_write: bool | None,
) -> str | None:
    """把 claim 与 request-start 共用的最新能力事实映射为稳定错误码。"""
    if write_capability_error_code == "connection_scope_missing" or (
        write_capability_status in {CapabilityStatus.ACTION_REQUIRED, CapabilityStatus.REVOKED}
    ):
        return "connection_scope_missing"
    if (
        connection_status != "connected"
        or read_capability_status is not CapabilityStatus.ENABLED
        or write_capability_status is not CapabilityStatus.ENABLED
        or calendar_can_write is False
    ):
        return "connection_capability_disabled"
    return None


def _tool_idempotency_key(snapshot: TrustedActionExecutionSnapshot) -> str:
    """生成计划冻结的精确 action/task/approval/version/operation 幂等键。"""
    return trusted_action_idempotency_key(
        action=snapshot.action,
        task_id=snapshot.task_id,
        approval_id=snapshot.approval_id,
        approval_version=snapshot.approval_version,
        operation_id=snapshot.operation_id,
    )


def _dispatch_execution_binding_is_valid(snapshot: TrustedActionDispatchSnapshot) -> bool:
    """在选择 execute/reconcile 前重证 dispatch DTO 的全部持久绑定。"""
    execution = snapshot.execution
    return execution.approval_id == snapshot.approval_id and trusted_execution_binding_matches(
        execution_task_id=execution.task_id,
        expected_task_id=snapshot.task_id,
        execution_step_id=execution.step_id,
        expected_step_id=snapshot.step_id,
        execution_operation_id=execution.operation_id,
        expected_operation_id=snapshot.operation_id,
        execution_provider=execution.provider,
        expected_provider=snapshot.provider,
        execution_tool_name=execution.tool_name,
        expected_action=snapshot.action,
        execution_idempotency_key=execution.idempotency_key,
        approval_id=snapshot.approval_id,
        approval_version=snapshot.approval_version,
        execution_payload_hash=execution.request_payload_hash,
        expected_payload_hash=snapshot.payload_hash,
    )


def _validate_lease_owner(value: str) -> None:
    """拒绝空白或换行 owner，且不把它写入 Graph checkpoint。"""
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\r" in value
        or "\n" in value
        or len(value) > 255
    ):
        raise _trusted_action_unavailable()


def _canonical_payload_hash(value: object) -> bool:
    """判断值是否为 canonical lowercase SHA-256，供 compare_digest 前收窄。"""
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _payload_hashes_match(left: object, right: object) -> bool:
    """只对两个规范 ASCII 哈希执行常量时间比较，异常形状直接 fail closed。"""
    return (
        _canonical_payload_hash(left)
        and _canonical_payload_hash(right)
        and compare_digest(cast(str, left), cast(str, right))
    )


def _mail_command_payload(
    *,
    snapshot: MailDraftSubmissionSnapshot,
    operation_id: UUID,
    now: datetime,
) -> dict[str, object]:
    """把锁定草稿投影显式映射为标准 JSON ``mail.send`` 命令。"""
    thread_headers: dict[str, object] | None = None
    if snapshot.thread_headers is not None:
        thread_headers = {
            "in_reply_to": snapshot.thread_headers.in_reply_to,
            "references": list(snapshot.thread_headers.references),
        }
    return {
        "schema_version": "mail_send.v1",
        "action": "mail.send",
        "operation_id": str(operation_id),
        "connection_id": str(snapshot.connection_id),
        "draft_id": str(snapshot.draft_id),
        "draft_version": snapshot.current_version,
        "message_date": _rfc3339(now),
        "mode": snapshot.mode.value,
        "source_thread_id": snapshot.source_thread_id,
        "source_message_id": snapshot.source_message_id,
        "to": list(snapshot.to),
        "cc": list(snapshot.cc),
        "bcc": list(snapshot.bcc),
        "subject": snapshot.subject,
        "body_text": snapshot.body_text,
        "thread_headers": thread_headers,
    }


def _calendar_command_payload(
    snapshot: CalendarProposalSubmissionSnapshot,
    *,
    operation_id: UUID,
) -> dict[str, object]:
    """按提案种类构造完整期望状态及其条件写入绑定。"""
    common: dict[str, object] = {
        "operation_id": str(operation_id),
        "connection_id": str(snapshot.connection_id),
        "calendar_id": snapshot.calendar_id,
        "title": snapshot.title,
        "description": snapshot.description,
        "location": snapshot.location,
        "starts_at": snapshot.starts_at,
        "ends_at": snapshot.ends_at,
        "timezone": snapshot.timezone,
        "all_day": snapshot.all_day,
        "attendees": list(snapshot.attendees),
        "notification_policy": (
            snapshot.notification_policy.value if snapshot.notification_policy is not None else None
        ),
    }
    if snapshot.operation_kind == "create":
        return {
            "schema_version": "calendar_create.v1",
            "action": "calendar.create",
            **common,
            "client_event_id": calendar_client_event_id(operation_id),
        }
    action = f"calendar.{snapshot.operation_kind}"
    return {
        "schema_version": f"calendar_{snapshot.operation_kind}.v1",
        "action": action,
        **common,
        "provider_event_id": snapshot.target_event_id,
        "base_etag": snapshot.base_etag,
        "before_snapshot_id": (
            str(snapshot.before_snapshot_id) if snapshot.before_snapshot_id is not None else None
        ),
        "changed_fields": list(snapshot.changed_fields),
    }


def _validate_mail_snapshot(
    snapshot: MailDraftSubmissionSnapshot,
    *,
    expected_version: int,
) -> None:
    """重新验证草稿版本、编辑态、连接能力和回复来源绑定。"""
    if (
        snapshot.current_version != expected_version
        or snapshot.status is not MailDraftStatus.EDITING
    ):
        raise _version_conflict("draft_version_conflict")
    _validate_connection_capabilities(
        connection_status=snapshot.connection_status,
        read_status=snapshot.read_capability_status,
        write_status=snapshot.write_capability_status,
        write_error_code=snapshot.write_capability_error_code,
    )
    if snapshot.mode is not MailMode.NEW and snapshot.thread_headers is None:
        raise StateConflictError(
            error_code="mail_thread_binding_conflict",
            message="mail reply source binding is unavailable or inconsistent",
        )


def _validate_calendar_snapshot(
    snapshot: CalendarProposalSubmissionSnapshot,
    *,
    expected_version: int,
) -> None:
    """重新验证提案完整性、能力、目录权限与条件更新版本。"""
    if (
        snapshot.current_version != expected_version
        or snapshot.status is not CalendarProposalStatus.EDITING
        or not snapshot.submission_ready
    ):
        raise _version_conflict("proposal_version_conflict")
    _validate_connection_capabilities(
        connection_status=snapshot.connection_status,
        read_status=snapshot.read_capability_status,
        write_status=snapshot.write_capability_status,
        write_error_code=snapshot.write_capability_error_code,
    )
    if not snapshot.calendar_can_write:
        raise _connection_capability_disabled()
    if snapshot.operation_kind not in {"create", "update", "restore"}:
        raise _provider_action_unavailable()
    if snapshot.operation_kind == "create":
        return
    if snapshot.target_event_recurring:
        raise StateConflictError(
            error_code="calendar_recurring_event_unsupported",
            message="recurring calendar events are not supported",
        )
    if (
        snapshot.target_event_can_edit is not True
        or snapshot.target_event_status is None
        or snapshot.target_event_status.casefold() == "cancelled"
        or snapshot.base_etag is None
        or snapshot.current_provider_etag is None
        or snapshot.base_etag != snapshot.current_provider_etag
    ):
        raise StateConflictError(
            error_code="calendar_event_version_conflict",
            message="calendar event version changed",
        )


def _validate_connection_capabilities(
    *,
    connection_status: str,
    read_status: CapabilityStatus,
    write_status: CapabilityStatus,
    write_error_code: str | None,
) -> None:
    """要求连接、读依赖与写能力均处于当前可执行状态。"""
    if write_error_code == "connection_scope_missing" or write_status in {
        CapabilityStatus.ACTION_REQUIRED,
        CapabilityStatus.REVOKED,
    }:
        raise StateConflictError(
            error_code="connection_scope_missing",
            message="provider write capability requires reauthorization",
        )
    if (
        connection_status != "connected"
        or read_status is not CapabilityStatus.ENABLED
        or write_status is not CapabilityStatus.ENABLED
    ):
        raise _connection_capability_disabled()


def _validate_write_gate(
    *,
    provider: str,
    provider_identity_key: str,
    write_policy: TrustedActionWritePolicy,
) -> None:
    """在任何 ID、密文或持久事实产生前执行三层静态写入门禁。"""
    if not write_policy.provider_writes_enabled(provider) or not write_policy.write_account_allowed(
        provider_identity_key
    ):
        raise StateConflictError(
            error_code="external_writes_disabled",
            message="external writes are disabled",
        )


def _reuse_existing(
    existing: ExistingTrustedActionSubmission,
    *,
    proposal_kind: str,
    proposal_id: UUID,
    proposal_version: int,
) -> TrustedActionSubmissionResult:
    """只在同键仍绑定相同本地版本时返回既有提交。"""
    if (
        existing.proposal_kind != proposal_kind
        or existing.proposal_id != proposal_id
        or existing.proposal_version != proposal_version
    ):
        raise StateConflictError(
            error_code="idempotency_key_payload_mismatch",
            message="idempotency key is already bound to another trusted action",
        )
    return TrustedActionSubmissionResult(
        task_id=existing.task_id,
        approval_id=existing.approval_id,
        operation_id=existing.operation_id,
        status=existing.status,
    )


def _result(submission: TrustedActionSubmission) -> TrustedActionSubmissionResult:
    """从新提交记录生成无敏感返回值。"""
    return TrustedActionSubmissionResult(
        task_id=submission.task_id,
        approval_id=submission.approval_id,
        operation_id=submission.operation_id,
        status=TaskStatus.QUEUED,
    )


def _canonical_command(payload: Mapping[str, object]) -> dict[str, object]:
    """使用唯一可信命令规范器返回独立标准 JSON object。"""
    decoded: object = json.loads(canonical_command_json(payload).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise _provider_action_unavailable()
    return dict(cast(dict[str, object], decoded))


def _command_text(payload: Mapping[str, object], field: str) -> str:
    """读取规范命令固定文本字段，异常形状统一 fail closed。"""
    value = payload.get(field)
    if type(value) is not str:
        raise _provider_action_unavailable()
    return value


def _validate_request(*, expected_version: int, idempotency_key: str) -> None:
    """验证持久版本和数据库幂等键边界。"""
    if type(expected_version) is not int or expected_version <= 0:
        raise ValueError("expected_version must be a positive integer")
    if (
        type(idempotency_key) is not str
        or not idempotency_key
        or idempotency_key != idempotency_key.strip()
        or len(idempotency_key) > 255
        or "\r" in idempotency_key
        or "\n" in idempotency_key
    ):
        raise ValueError("idempotency_key is invalid")


def _utc_now(value: datetime) -> datetime:
    """要求显式带时区时间并规范为 UTC。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _rfc3339(value: datetime) -> str:
    """把 UTC datetime 编码为命令规范器接受的 RFC3339 文本。"""
    return value.isoformat().replace("+00:00", "Z")


def _version_conflict(error_code: str) -> StateConflictError:
    """构造不回显资源存在性或内容的版本冲突。"""
    return StateConflictError(error_code=error_code, message="trusted action version changed")


def _connection_capability_disabled() -> StateConflictError:
    """构造连接或目录不再允许真实写入的稳定冲突。"""
    return StateConflictError(
        error_code="connection_capability_disabled",
        message="provider write capability is not enabled",
    )


def _provider_action_unavailable() -> StateConflictError:
    """构造精确 provider/action 未组装或异常时的脱敏错误。"""
    return StateConflictError(
        error_code="provider_action_unavailable",
        message="provider action is unavailable",
    )


def _trusted_action_unavailable() -> StateConflictError:
    """构造不泄露审批存在性、命令内容或持久篡改维度的失败。"""
    return StateConflictError(
        error_code="trusted_action_unavailable",
        message="trusted action is unavailable",
    )


def _approval_conflict() -> StateConflictError:
    """构造审批决定或版本不再允许当前 Graph 分支的稳定冲突。"""
    return StateConflictError(
        error_code="approval_conflict",
        message="approval is unavailable",
    )


def _external_write_account_not_allowed() -> StateConflictError:
    """构造非生产规范账户身份未通过 allowlist 的稳定失败。"""
    return StateConflictError(
        error_code="external_write_account_not_allowed",
        message="external write account is not allowed",
    )


__all__ = [
    "CalendarProposalSubmissionNotFoundError",
    "SubmitCalendarProposalUseCase",
    "SubmitMailDraftUseCase",
    "TrustedActionExecutionUseCase",
    "TrustedActionGraphFacts",
]
