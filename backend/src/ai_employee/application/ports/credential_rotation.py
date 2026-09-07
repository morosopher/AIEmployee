"""定义唯一 credential CAS 边界及 append-only OAuth 双通道的关闭结果协议。

历史闭合仅由本模块的严格 schema/关联/时间匹配证明，绝不比较 current credential；
调用方另做 current-readiness。retention 与 ACK-loss 恢复必须复用此 parser，不能宽松猜测。
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ai_employee.application.calendar_aad_digests import canonical_utc, connection_digest_v1
from ai_employee.domain.tasks import JsonValue

if TYPE_CHECKING:
    from ai_employee.application.ports.oauth import OAuthTokenSet
    from ai_employee.application.ports.oauth_refresh import OAuthRefreshClaim

AutomaticRefreshSource = Literal["calendar_aad_preflight", "provider_refresh"]
RefreshTokenDisposition = Literal["missing", "same", "different"]
HexDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Generation = Annotated[int, Field(ge=0)]
KeyVersion = Annotated[int, Field(gt=0)]


@dataclass(frozen=True, slots=True)
class OAuthRefreshAuditRecord:
    """以供应商无关投影表达一条已持久化审计；不接受 ORM 穿透应用层。

    metadata 只在受控应用边界匹配，repr 不包含 fingerprint。event_id 是真实 AuditEvent
    cursor，不是另建的 UUID 或迁移表；task_id=NULL 的事实不进入任务 SSE。
    """

    event_id: int
    user_id: UUID
    event_type: str
    created_at: datetime
    metadata: Mapping[str, JsonValue] = field(repr=False)
    task_id: UUID | None = None
    actor_type: str = "system"


class _ClosedMetadata(BaseModel):
    """所有字段严格校验且禁止 extra；验证错误不携带原输入。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    @field_validator("*", mode="after")
    @classmethod
    def _canonical_identifier(cls, value: object, info: ValidationInfo) -> object:
        """审计 JSON 的 UUID 与 event cursor 必须已经是规范字符串。"""
        name = info.field_name
        if name in {"refresh_attempt_id", "oauth_attempt_id", "recovery_oauth_attempt_id"} and (
            not isinstance(value, str) or str(UUID(value)) != value
        ):
            raise ValueError("OAuth attempt ID is not canonical")
        if name == "recovery_authorization_started_event_id" and (
            not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value)
        ):
            raise ValueError("OAuth audit cursor is not canonical")
        if name in {"token_expires_at", "rollout_deadline_candidate"} and value is not None:
            if not isinstance(value, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value
            ):
                raise ValueError("OAuth result timestamp is not canonical")
            if canonical_utc(datetime.fromisoformat(value)) != value:
                raise ValueError("OAuth result timestamp is not canonical")
        return value

    def metadata(self) -> dict[str, JsonValue]:
        """返回仅含固定 JSON 字段的持久投影；该投影仍不得输出到日志或 API。"""
        return dict(self.model_dump(mode="json"))


class _FenceBinding(_ClosedMetadata):
    """自动与恢复事件共同冻结原 attempt、两份物理摘要和旧 keyed identity。"""

    refresh_attempt_id: str
    connection_digest: HexDigest
    fence_generation: Generation
    pre_credential_snapshot_digest_v1: HexDigest
    pre_refresh_credential_snapshot_digest_v1: HexDigest
    old_refresh_token_identity_v1: HexDigest = Field(repr=False)
    refresh_token_identity_key_version: KeyVersion


class _AutomaticBinding(_FenceBinding):
    """自动来源仅有普通 Worker 与 0019 preflight，普通事件显式使用 NULL tuple。"""

    source: AutomaticRefreshSource
    source_revision: Literal["20260809_0018"] | None
    target_revision: Literal["20260809_0019"] | None
    rollout_digest_v1: HexDigest | None

    @model_validator(mode="after")
    def _revision_binding(self) -> Self:
        """拒绝普通刷新夹带 rollout 身份或 preflight 缺少固定 revision/digest。"""
        values = (self.source_revision, self.target_revision, self.rollout_digest_v1)
        if self.source == "provider_refresh" and values != (None, None, None):
            raise ValueError("ordinary refresh must use null rollout fields")
        if self.source == "calendar_aad_preflight" and any(value is None for value in values):
            raise ValueError("preflight refresh requires rollout binding")
        return self


class OAuthRefreshStartedV1(_AutomaticBinding):
    """provider refresh grant 前已提交的唯一自动 admission fence。"""

    fence_schema_version: Literal["oauth_refresh_fence.v1"]
    result_code: Literal["oauth_refresh_started"]


class OAuthRecoveryStartedV1(_FenceBinding):
    """一次显式 OAuthAttempt 与原自动 fence 的固定 F/S/T 关联；不发出 refresh grant。"""

    recovery_schema_version: Literal["oauth_refresh_recovery_authorization.v1"]
    source: Literal["progressive_recovery"]
    oauth_attempt_id: str
    started_source: AutomaticRefreshSource
    source_generation: Generation
    target_generation: Generation
    result_code: Literal["oauth_refresh_recovery_authorization_started"]

    @model_validator(mode="after")
    def _generations(self) -> Self:
        """恢复 start 只递增 S→T 一次，原 fence 代际 F 不得晚于 S。"""
        if (
            self.fence_generation > self.source_generation
            or self.target_generation != self.source_generation + 1
        ):
            raise ValueError("recovery generation binding is invalid")
        return self


class ConfirmedV1(_AutomaticBinding):
    """只关闭自己的 automatic started；missing/same 必须保留旧 refresh identity。"""

    result_schema_version: Literal["oauth_refresh_confirmed.v1"]
    started_source: AutomaticRefreshSource
    source_generation: Generation
    pre_generation: Generation
    post_generation: Generation
    post_credential_snapshot_digest_v1: HexDigest
    post_refresh_credential_snapshot_digest_v1: HexDigest
    new_refresh_token_identity_v1: HexDigest = Field(repr=False)
    new_refresh_token_identity_key_version: KeyVersion
    token_expires_at: str
    refresh_token_disposition: RefreshTokenDisposition
    refresh_identity_changed: bool
    rollout_deadline_candidate: str | None
    result_code: Literal["oauth_refresh_confirmed"]

    @model_validator(mode="after")
    def _confirmed_invariants(self) -> Self:
        """在闭合前重证 G/G/G、固定 key、disposition/flag/equality 与历史 deadline。"""
        if (
            self.source != self.started_source
            or len(
                {
                    self.fence_generation,
                    self.source_generation,
                    self.pre_generation,
                    self.post_generation,
                }
            )
            != 1
        ):
            raise ValueError("automatic refresh generation or source is inconsistent")
        changed = not secrets.compare_digest(
            self.old_refresh_token_identity_v1, self.new_refresh_token_identity_v1
        )
        if self.refresh_identity_changed != changed or changed != (
            self.refresh_token_disposition == "different"
        ):
            raise ValueError("refresh disposition and identity change disagree")
        if self.refresh_token_identity_key_version != self.new_refresh_token_identity_key_version:
            raise ValueError("OAuth identity key version cannot change")
        if self.source == "provider_refresh":
            if self.rollout_deadline_candidate is not None:
                raise ValueError("ordinary refresh has no rollout deadline")
        else:
            expected = canonical_utc(
                datetime.fromisoformat(self.token_expires_at) - timedelta(seconds=900)
            )
            if self.rollout_deadline_candidate != expected:
                raise ValueError("preflight historical deadline is inconsistent")
        return self


class _RecoveryResultBinding(_FenceBinding):
    """恢复结果精确引用原 fence 与一次已开始的 OAuthAttempt，不读取 mutable current rows。"""

    source: Literal["progressive_recovery"]
    started_source: AutomaticRefreshSource
    recovery_oauth_attempt_id: str
    recovery_authorization_started_event_id: str
    source_generation: Generation
    target_generation: Generation

    @model_validator(mode="after")
    def _generations(self) -> Self:
        """验证 F≤S 与 T=S+1；不能用任意 generation 改变来解除旧 fence。"""
        if (
            self.fence_generation > self.source_generation
            or self.target_generation != self.source_generation + 1
        ):
            raise ValueError("recovery generation binding is invalid")
        return self


class RecoveryUnsatisfiedV1(_RecoveryResultBinding):
    """仅关闭恢复 OAuthAttempt，绝不证明 credential 更新或消费原 automatic fence。"""

    result_schema_version: Literal["oauth_refresh_recovery_unsatisfied.v1"]
    requested_capabilities: list[
        Literal["calendar.read", "calendar.write", "mail.read", "mail.send"]
    ]
    capability_transition: Literal["action_required", "stale_target_noop"]
    error_code: Literal[
        "oauth_authorization_failed",
        "microsoft_admin_consent_required",
        "microsoft_reauthorization_required",
        "google_reauthorization_required",
        "oauth_credential_state_conflict",
        "oauth_refresh_recovery_unsatisfied",
    ]
    result_code: Literal["oauth_refresh_recovery_unsatisfied"]

    @model_validator(mode="after")
    def _requested_capabilities(self) -> Self:
        """持久数组必须非空、排序且唯一；不从 raw scope 或 provider error 推导结果。"""
        if not self.requested_capabilities or self.requested_capabilities != sorted(
            set(self.requested_capabilities)
        ):
            raise ValueError("requested capabilities are not canonical")
        return self


class CredentialReplacedV1(_RecoveryResultBinding):
    """唯一可同时关闭恢复 started 与原 unknown automatic fence 的不同-token proof。"""

    proof_schema_version: Literal["oauth_refresh_credential_replaced.v1"]
    pre_generation: Generation
    post_generation: Generation
    post_credential_snapshot_digest_v1: HexDigest
    post_refresh_credential_snapshot_digest_v1: HexDigest
    new_refresh_token_identity_v1: HexDigest = Field(repr=False)
    new_refresh_token_identity_key_version: KeyVersion
    refresh_identity_changed: Literal[True]
    token_expires_at: str
    result_code: Literal["oauth_refresh_credential_replaced"]

    @field_validator("refresh_identity_changed", mode="before")
    @classmethod
    def _literal_boolean(cls, value: object) -> object:
        """Literal 在 Python 把 1 视同 True；审计 JSON 必须保持严格布尔类型。"""
        if type(value) is not bool:
            raise ValueError("replacement flag must be a JSON boolean")
        return value

    @model_validator(mode="after")
    def _replacement_invariants(self) -> Self:
        """拒绝同 plaintext 重加密、key version 替换或 callback 再次递增 generation。"""
        if (
            self.pre_generation != self.target_generation
            or self.post_generation != self.target_generation
        ):
            raise ValueError("replacement must preserve target generation")
        if self.refresh_token_identity_key_version != self.new_refresh_token_identity_key_version:
            raise ValueError("OAuth identity key version cannot change")
        if secrets.compare_digest(
            self.old_refresh_token_identity_v1, self.new_refresh_token_identity_v1
        ):
            raise ValueError("replacement requires a different refresh identity")
        return self


OAuthRefreshResultV1 = ConfirmedV1 | RecoveryUnsatisfiedV1 | CredentialReplacedV1


def _outer_valid(record: OAuthRefreshAuditRecord, user_id: UUID, event_type: str) -> bool:
    """拒绝其他用户、任务事件、非 system 或非法时间，保留 content-free OAuth 边界。"""
    return (
        record.user_id == user_id
        and record.event_type == event_type
        and record.task_id is None
        and record.actor_type == "system"
        and type(record.event_id) is int
        and record.event_id > 0
        and isinstance(record.created_at, datetime)
        and record.created_at.tzinfo is not None
        and record.created_at.utcoffset() == UTC.utcoffset(record.created_at)
    )


def parse_automatic_started(
    record: OAuthRefreshAuditRecord, *, user_id: UUID, connection_id: UUID, key_version: int
) -> OAuthRefreshStartedV1 | None:
    """严格读取自动 fence；无法证明 schema/归属/固定 root 版本时不允许 provider admission。"""
    if not _outer_valid(record, user_id, "oauth.refresh_started"):
        return None
    try:
        value = OAuthRefreshStartedV1.model_validate(record.metadata)
    except ValidationError:
        return None
    if (
        value.connection_digest != connection_digest_v1(connection_id)
        or value.refresh_token_identity_key_version != key_version
    ):
        return None
    return value


def _same_fence(first: _FenceBinding, second: _FenceBinding) -> bool:
    """匹配原 fence 的全部 frozen 字段，identity 使用恒定时间比较。"""
    fields = (
        "refresh_attempt_id",
        "connection_digest",
        "fence_generation",
        "pre_credential_snapshot_digest_v1",
        "pre_refresh_credential_snapshot_digest_v1",
        "refresh_token_identity_key_version",
    )
    return all(
        getattr(first, name) == getattr(second, name) for name in fields
    ) and secrets.compare_digest(
        first.old_refresh_token_identity_v1, second.old_refresh_token_identity_v1
    )


def parse_recovery_started(
    record: OAuthRefreshAuditRecord,
    *,
    automatic: OAuthRefreshAuditRecord,
    user_id: UUID,
    connection_id: UUID,
    key_version: int,
) -> OAuthRecoveryStartedV1 | None:
    """只解析共享恢复关联，不实现 start/callback 或 state 消费。"""
    original = parse_automatic_started(
        automatic, user_id=user_id, connection_id=connection_id, key_version=key_version
    )
    if (
        original is None
        or not _outer_valid(record, user_id, "oauth.refresh_recovery_authorization_started")
        or record.created_at <= automatic.created_at
    ):
        return None
    try:
        value = OAuthRecoveryStartedV1.model_validate(record.metadata)
    except ValidationError:
        return None
    if not _same_fence(original, value) or value.started_source != original.source:
        return None
    return value


def parse_oauth_refresh_result(
    record: OAuthRefreshAuditRecord,
    *,
    automatic: OAuthRefreshAuditRecord,
    recovery: OAuthRefreshAuditRecord | None = None,
    user_id: UUID,
    connection_id: UUID,
    key_version: int,
) -> OAuthRefreshResultV1 | None:
    """返回合法 versioned result union；非法 metadata 永远不能关闭任何 attempt。

    Args:
        record: 待验证的 append-only result。
        automatic: 精确原自动 started；即使是 unsatisfied 也必须绑定它。
        recovery: 恢复结果必需的 authorization started；confirmed 不使用它。
        user_id: 显式查询归属。
        connection_id: 用于重算固定 connection digest。
        key_version: 当前与 AEAD 相同的固定 root 版本。

    Returns:
        合法的 ConfirmedV1、RecoveryUnsatisfiedV1、CredentialReplacedV1，或 None。
        此函数不接受 current rows，因此 later refresh/reauth 不能复活已闭合 attempt。
    """
    original = parse_automatic_started(
        automatic, user_id=user_id, connection_id=connection_id, key_version=key_version
    )
    if original is None or not _outer_valid(record, user_id, record.event_type):
        return None
    if record.created_at <= automatic.created_at:
        return None
    value: OAuthRefreshResultV1
    try:
        if record.event_type == "oauth.refresh_confirmed":
            value = ConfirmedV1.model_validate(record.metadata)
            if not _same_fence(original, value):
                return None
            if (
                value.source,
                value.source_revision,
                value.target_revision,
                value.rollout_digest_v1,
            ) != (
                original.source,
                original.source_revision,
                original.target_revision,
                original.rollout_digest_v1,
            ):
                return None
            return value
        if record.event_type == "oauth.refresh_recovery_unsatisfied":
            value = RecoveryUnsatisfiedV1.model_validate(record.metadata)
        elif record.event_type == "oauth.refresh_credential_replaced":
            value = CredentialReplacedV1.model_validate(record.metadata)
        else:
            return None
    except (ValidationError, ValueError):
        return None
    if recovery is None:
        return None
    recovery_value = parse_recovery_started(
        recovery,
        automatic=automatic,
        user_id=user_id,
        connection_id=connection_id,
        key_version=key_version,
    )
    if (
        recovery_value is None
        or record.created_at <= recovery.created_at
        or not _same_fence(original, value)
    ):
        return None
    if (
        value.started_source != original.source
        or value.recovery_oauth_attempt_id != recovery_value.oauth_attempt_id
        or value.recovery_authorization_started_event_id != str(recovery.event_id)
        or value.source_generation != recovery_value.source_generation
        or value.target_generation != recovery_value.target_generation
    ):
        return None
    return value


class CredentialRotationRepository(Protocol):
    """在调用方短事务内执行完整 snapshot CAS 和 matching confirmed 追加。

    自动 repository 不提交事务、不执行 provider I/O，也不提供无条件 upsert。
    显式恢复将在同一个 CAS helper 上扩展；它不能复用自动 grant 创建第二个 started。
    """

    async def confirm(
        self, claim: OAuthRefreshClaim, tokens: OAuthTokenSet, *, completed_at: datetime
    ) -> ConfirmedV1:
        """重检连接→access→refresh→started，并原子更新密文与本次 confirmed。"""
        ...
