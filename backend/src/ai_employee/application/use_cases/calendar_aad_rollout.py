"""定义 0019 sealed rollout 的闭合 artifact、精确集合与只能收紧的当前截止线。

本模块不读取文件、数据库或供应商。所有时间和事实由适配器显式提供；历史 refresh
deadline candidate 永远不是当前 readiness/deadline 的输入。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from ai_employee.application.calendar_aad_digests import (
    CALENDAR_AAD_ROLLOUT_SAFETY_MARGIN_SECONDS,
    calendar_pair_digest_v1,
    canonical_utc,
    connection_digest_v1,
    rollout_digest_v1,
)
from ai_employee.domain.errors import UserActionRequiredError

CalendarAadRevision = Literal["20260809_0018", "20260809_0019"]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]


class CalendarAadRolloutError(UserActionRequiredError):
    """以稳定、无账户/日历/凭据内容的错误阻止继续 rollout。"""

    def __init__(self, error_code: str) -> None:
        """错误分类只描述安全边界，不包含原异常、provider response 或原始 scope。"""
        super().__init__(error_code=error_code, message="Calendar AAD rollout requires attention")


@dataclass(frozen=True, slots=True)
class CalendarAadBinding:
    """绑定安全 basename 与宿主 Compose 实际解析的不可变镜像内容 ID。"""

    basename: str
    immutable_image_id: str

    def __post_init__(self) -> None:
        """构造时立即验证身份，不修正用户输入或接受可变 tag 代替内容 ID。"""
        try:
            rollout_digest_v1(self.basename, self.immutable_image_id)
        except (ValueError, TypeError):
            raise CalendarAadRolloutError("calendar_aad_artifact_invalid") from None


@dataclass(frozen=True, slots=True)
class CalendarAadPair:
    """承载从数据库 owner 推导的精确可恢复 pair；原日历 ID 不进入 repr/审计。"""

    user_id: UUID
    connection_id: UUID
    calendar_id: str = field(repr=False)
    provider: str
    timezone: str
    authorization_generation: int

    @property
    def digest(self) -> str:
        """返回共享规范 digest，禁止运维/Worker 自行发明另一种 pair 编码。"""
        return calendar_pair_digest_v1(self.connection_id, self.calendar_id)


@dataclass(frozen=True, slots=True)
class CalendarAadFacts:
    """封装一次实际扫描的集合和当前 access expiry，不含 credential 明文或历史截止线。"""

    revision: CalendarAadRevision
    pairs: tuple[CalendarAadPair, ...]
    access_expiries: tuple[tuple[UUID, datetime | None], ...]

    @property
    def pair_digests(self) -> tuple[str, ...]:
        """返回排序去重的精确 pair 集合；重复事实由 repository admission 拒绝。"""
        return tuple(sorted({pair.digest for pair in self.pairs}))

    @property
    def connection_digests(self) -> tuple[str, ...]:
        """只输出哈希连接标识，绝不泄漏 provider/calendar 或 token 内容。"""
        return tuple(sorted({connection_digest_v1(pair.connection_id) for pair in self.pairs}))


class _ArtifactBase(BaseModel):
    """固定 v1 artifact 公共字段；禁止额外键、隐式类型转换或后续原地篡改。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["calendar_aad_0019_preflight.v1"] = "calendar_aad_0019_preflight.v1"
    source_revision: Literal["20260809_0018"] = "20260809_0018"
    target_revision: Literal["20260809_0019"] = "20260809_0019"
    rollout_digest_v1: Digest
    backup_artifact_basename: str
    immutable_image_id: str
    affected_connection_count: Annotated[int, Field(ge=0)]
    affected_pair_count: Annotated[int, Field(ge=0)]
    connection_digests: tuple[Digest, ...]
    pair_digests: tuple[Digest, ...]
    safety_margin_seconds: Literal[900] = 900

    @model_validator(mode="after")
    def validate_binding_and_counts(self) -> Self:
        """重算 v1 身份并严格绑定排序、唯一性和 count；不接受零分支伪装。"""
        if self.rollout_digest_v1 != rollout_digest_v1(
            self.backup_artifact_basename, self.immutable_image_id
        ):
            raise ValueError("rollout binding is invalid")
        for count, digests in (
            (self.affected_connection_count, self.connection_digests),
            (self.affected_pair_count, self.pair_digests),
        ):
            if count != len(digests) or digests != tuple(sorted(set(digests))):
                raise ValueError("rollout counts are invalid")
        if self.affected_connection_count > self.affected_pair_count:
            raise ValueError("rollout connection count is invalid")
        return self


class CalendarAadNonemptyArtifact(_ArtifactBase):
    """非空受影响集：原始截止线与 earliest expiry 都必须为真实 UTC 时间。"""

    affected_connection_count: Annotated[int, Field(gt=0)]
    affected_pair_count: Annotated[int, Field(gt=0)]
    earliest_token_expires_at: datetime
    rollout_deadline: datetime
    result_code: Literal["calendar_aad_preflight_passed"] = "calendar_aad_preflight_passed"

    @model_validator(mode="after")
    def validate_deadline(self) -> Self:
        """900 秒是固定协议值，既不能从 settings 推导，也不能由运维手填延長。"""
        canonical_utc(self.earliest_token_expires_at)
        canonical_utc(self.rollout_deadline)
        if self.rollout_deadline != self.earliest_token_expires_at - timedelta(seconds=900):
            raise ValueError("rollout deadline is invalid")
        return self


class CalendarAadZeroArtifact(_ArtifactBase):
    """显式无操作分支：所有 count/array 为空且时间严格为 null，仍需每次重证空集。"""

    affected_connection_count: Literal[0] = 0
    affected_pair_count: Literal[0] = 0
    connection_digests: tuple[()] = ()
    pair_digests: tuple[()] = ()
    earliest_token_expires_at: None = None
    rollout_deadline: None = None
    result_code: Literal["calendar_aad_preflight_zero"] = "calendar_aad_preflight_zero"


CalendarAadArtifact = CalendarAadNonemptyArtifact | CalendarAadZeroArtifact
_ARTIFACT: TypeAdapter[CalendarAadArtifact] = TypeAdapter(
    Annotated[CalendarAadArtifact, Field(discriminator="result_code")]
)


def _unique_json_object(items: list[tuple[str, object]]) -> dict[str, object]:
    """在 JSON 边界拒绝重复键，不能让 last-key-wins 抹除冲突事实。"""
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate artifact key")
        result[key] = value
    return result


def parse_rollout_artifact(raw: bytes, binding: CalendarAadBinding) -> CalendarAadArtifact:
    """从有界文件字节验证闭合 schema 和当前 host image/basename，错误始终脱敏。"""
    try:
        if type(raw) is not bytes or len(raw) > 1_048_576:
            raise ValueError("artifact size is invalid")
        payload: object = json.loads(raw, object_pairs_hook=_unique_json_object)
        if not isinstance(payload, dict) or set(payload) != set(
            CalendarAadZeroArtifact.model_fields
        ):
            raise ValueError("artifact keys are invalid")
        if any(
            type(payload[key]) is not int
            for key in ("affected_connection_count", "affected_pair_count", "safety_margin_seconds")
        ):
            raise ValueError("artifact integer is invalid")
        artifact = _ARTIFACT.validate_json(raw)
        if (
            artifact.backup_artifact_basename != binding.basename
            or artifact.immutable_image_id != binding.immutable_image_id
        ):
            raise ValueError("artifact binding changed")
    except (ValidationError, ValueError, TypeError):
        raise CalendarAadRolloutError("calendar_aad_artifact_invalid") from None
    return artifact


def serialize_rollout_artifact(artifact: CalendarAadArtifact) -> bytes:
    """只序列化封闭模型的规范 JSON；字段中没有明文、scope 或完整 provider response。"""
    return json.dumps(
        artifact.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def current_earliest_expiry(facts: CalendarAadFacts) -> datetime:
    """独立读取当前 expiry 投影，要求每个 affected connection 恰有一个非空 UTC 值。"""
    ids = {pair.connection_id for pair in facts.pairs}
    if (
        not ids
        or len(facts.access_expiries) != len(ids)
        or {connection_id for connection_id, _ in facts.access_expiries} != ids
    ):
        raise CalendarAadRolloutError("calendar_aad_local_recoverability_failed")
    expiries: list[datetime] = []
    for _, expiry in facts.access_expiries:
        if expiry is None:
            raise CalendarAadRolloutError("calendar_aad_local_recoverability_failed")
        canonical_utc(expiry)
        expiries.append(expiry)
    return min(expiries)


def create_rollout_artifact(
    binding: CalendarAadBinding, facts: CalendarAadFacts
) -> CalendarAadArtifact:
    """首次发布仅使用当前持久 expiry；后续调用必须保留原 artifact 而非延长窗口。"""
    digest = rollout_digest_v1(binding.basename, binding.immutable_image_id)
    if not facts.pairs:
        return CalendarAadZeroArtifact(
            rollout_digest_v1=digest,
            backup_artifact_basename=binding.basename,
            immutable_image_id=binding.immutable_image_id,
        )
    earliest = current_earliest_expiry(facts)
    return CalendarAadNonemptyArtifact(
        rollout_digest_v1=digest,
        backup_artifact_basename=binding.basename,
        immutable_image_id=binding.immutable_image_id,
        affected_connection_count=len(facts.connection_digests),
        affected_pair_count=len(facts.pair_digests),
        connection_digests=facts.connection_digests,
        pair_digests=facts.pair_digests,
        earliest_token_expires_at=earliest,
        rollout_deadline=earliest - timedelta(seconds=CALENDAR_AAD_ROLLOUT_SAFETY_MARGIN_SECONDS),
    )


def verify_rollout(
    artifact: CalendarAadArtifact, facts: CalendarAadFacts, *, now: datetime
) -> datetime | None:
    """在每个关键边界重证集合及 current expiry；零态不会以 null 绕过检查。

    Returns:
        非空返回 min(original deadline, current min expiry − 900 秒)，零态返回 None。

    Raises:
        CalendarAadRolloutError: 任一集合漂移、当前 expiry 缺失或 now 已到有效截止线。
    """
    canonical_utc(now)
    if (
        artifact.pair_digests != facts.pair_digests
        or artifact.connection_digests != facts.connection_digests
    ):
        raise CalendarAadRolloutError("calendar_aad_affected_set_changed")
    if isinstance(artifact, CalendarAadZeroArtifact):
        if facts.pairs or facts.access_expiries:
            raise CalendarAadRolloutError("calendar_aad_affected_set_changed")
        return None
    deadline = min(
        artifact.rollout_deadline, current_earliest_expiry(facts) - timedelta(seconds=900)
    )
    if now >= deadline:
        raise CalendarAadRolloutError("calendar_aad_rollout_deadline_exceeded")
    return deadline


class CalendarAadGuard(Protocol):
    """planner、Worker 与 artifact publication 共用的当前事实/lease 检查端口。"""

    async def verify(self) -> datetime | None:
        """重新验证固定 artifact、精确集合与当前 expiry；不能把历史结果缓存当成证据。"""
        ...
