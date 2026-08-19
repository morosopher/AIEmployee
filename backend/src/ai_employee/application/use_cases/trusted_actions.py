"""把本地不可变版本冻结为单操作、哈希绑定且记录加密的审批任务。

用例先在短事务内复用提交幂等事实，再锁定草稿或提案并重新验证写入门禁、连接能力、
目录权限、收件人/参会人上限与 ETag。只有全部纯校验通过后才生成稳定 ID、执行无副作用
provider preflight、规范化命令、计算哈希并加密；所有持久 mutation 由同一事务端口完成。
"""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from ai_employee.application.commands import (
    canonical_command_json,
    parse_trusted_command,
    trusted_command_hash,
)
from ai_employee.application.ports.trusted_actions import (
    CalendarProposalSubmissionSnapshot,
    ExistingTrustedActionSubmission,
    MailDraftSubmissionSnapshot,
    TrustedActionCommandCipher,
    TrustedActionPreflightRegistry,
    TrustedActionRisk,
    TrustedActionSubmission,
    TrustedActionSubmissionResult,
    TrustedActionSubmissionTransactionFactory,
    TrustedActionWritePolicy,
)
from ai_employee.domain.actions import CalendarProposalStatus, MailDraftStatus
from ai_employee.domain.calendar_actions import NotificationPolicy, calendar_client_event_id
from ai_employee.domain.connections import CapabilityStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode
from ai_employee.domain.tasks import TaskStatus

APPROVAL_COMMAND_CONTENT_KIND = "approval_command"
APPROVAL_TTL = timedelta(minutes=10)


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
                raise _version_conflict("proposal_version_conflict")
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
            snapshot.notification_policy.value
            if snapshot.notification_policy is not None
            else None
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


__all__ = ["SubmitCalendarProposalUseCase", "SubmitMailDraftUseCase"]
