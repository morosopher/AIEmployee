"""定义 0019 exact marker recovery 的闭合任务输入与单次 ordinal planner。

恢复 ordinal 与 DurableTaskRunner 的投递 attempt_count 独立；旧终态只读、活动尝试复用，
下一 ordinal 只能由后续一次显式 planner 调用在仍有 marker 时创建。
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_employee.application.calendar_aad_digests import calendar_pair_digest_v1
from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadArtifact,
    CalendarAadGuard,
    CalendarAadPair,
    CalendarAadRolloutError,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.tasks import JsonValue, TaskStatus

CALENDAR_AAD_RECOVERY_KIND = "calendar.aad_0019.resync"
CALENDAR_AAD_RECOVERY_MARKER = "calendar_event_resync_required"
_ACTIVE = frozenset(
    {TaskStatus.CREATED, TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RETRY_SCHEDULED}
)


@dataclass(frozen=True, slots=True)
class CalendarAadRecoveryInput:
    """五个精确字段的任务协议；原 scope 只在受控内存和用户归属的 TaskRun 中保存。"""

    connection_id: UUID
    scope_key: str = field(repr=False)
    pair_digest: str
    recovery_attempt_ordinal: int

    def __post_init__(self) -> None:
        """阻止 bool/非正 ordinal、非 canonical digest 和任何目录/宽 scope 进入执行。"""
        try:
            digest = calendar_pair_digest_v1(self.connection_id, self.scope_key)
        except (ValueError, TypeError):
            raise CalendarAadRolloutError("calendar_aad_recovery_input_invalid") from None
        if (
            self.pair_digest != digest
            or type(self.recovery_attempt_ordinal) is not int
            or self.recovery_attempt_ordinal < 1
        ):
            raise CalendarAadRolloutError("calendar_aad_recovery_input_invalid")

    @classmethod
    def parse(cls, payload: dict[str, JsonValue]) -> "CalendarAadRecoveryInput":
        """在解析 credential/adapter 前验证 exact keys、UUID、revision 和固定 digest。"""
        if type(payload) is not dict or set(payload) != {
            "connection_id",
            "scope_key",
            "recovery_revision",
            "pair_digest",
            "recovery_attempt_ordinal",
        }:
            raise CalendarAadRolloutError("calendar_aad_recovery_input_invalid")
        connection, scope, digest, ordinal = (
            payload["connection_id"],
            payload["scope_key"],
            payload["pair_digest"],
            payload["recovery_attempt_ordinal"],
        )
        if (
            type(connection) is not str
            or type(scope) is not str
            or type(digest) is not str
            or type(ordinal) is not int
            or payload["recovery_revision"] != "20260809_0019"
        ):
            raise CalendarAadRolloutError("calendar_aad_recovery_input_invalid")
        try:
            connection_id = UUID(connection)
            if str(connection_id) != connection:
                raise ValueError("non-canonical UUID")
        except ValueError:
            raise CalendarAadRolloutError("calendar_aad_recovery_input_invalid") from None
        return cls(connection_id, scope, digest, ordinal)

    @property
    def idempotency_key(self) -> str:
        """键仅含 pair digest 与 ordinal，不携带 raw calendar ID。"""
        return f"calendar-aad-0019:{self.pair_digest}:attempt:{self.recovery_attempt_ordinal}"

    def payload(self) -> dict[str, JsonValue]:
        """返回唯一允许持久化的精确任务输入，无 provider credential 或内容。"""
        return {
            "connection_id": str(self.connection_id),
            "scope_key": self.scope_key,
            "recovery_revision": "20260809_0019",
            "pair_digest": self.pair_digest,
            "recovery_attempt_ordinal": self.recovery_attempt_ordinal,
        }


@dataclass(frozen=True, slots=True)
class CalendarAadTaskBinding:
    """绑定已认领 TaskRun 的用户、精确五字段和当前 runner lease owner。"""

    task_id: UUID
    user_id: UUID
    input: CalendarAadRecoveryInput
    lease_owner: str

    @classmethod
    def from_task(cls, task: LeasedTask) -> "CalendarAadTaskBinding":
        """只允许真实 recovery kind 和非空所有权；不能复用普通 sync_calendar 宽 scope。"""
        if (
            task.kind != CALENDAR_AAD_RECOVERY_KIND
            or not isinstance(task.user_id, UUID)
            or not task.lease_owner
        ):
            raise CalendarAadRolloutError("calendar_aad_recovery_input_invalid")
        return cls(
            task.task_id,
            task.user_id,
            CalendarAadRecoveryInput.parse(task.input_payload),
            task.lease_owner,
        )


@dataclass(frozen=True, slots=True)
class MarkedCalendarScopeState:
    """冻结 provider read 前的 generation、exact cursor 身份与观察时间，供最终 CAS 使用。"""

    pair: CalendarAadPair
    calendar_row_id: UUID
    cursor_id: UUID
    last_attempt_at: datetime | None


@dataclass(frozen=True, slots=True)
class CalendarAadRecoveryAttempt:
    """仓储已验证 kind/user/payload/key 后提供的一个历史 ordinal 事实。"""

    task_id: UUID
    input: CalendarAadRecoveryInput
    status: TaskStatus


@dataclass(frozen=True, slots=True)
class CalendarAadPlannedTask:
    """仅含安全标识的 planner 结果，精确限定 one-off 可分派与执行的任务。"""

    task_id: UUID
    pair_digest: str
    recovery_attempt_ordinal: int


class CalendarAadRecoveryStore(Protocol):
    """在同一应用事务中扫描/锁定 exact marker 并复用既有任务原子创建边界。"""

    async def marked_pairs(self) -> tuple[CalendarAadPair, ...]:
        """只选择非目录 calendar_event_resync_required 游标，并从 owning connection 推导用户。"""
        ...

    async def lock_marked_pair(self, pair: CalendarAadPair) -> bool:
        """锁定精确 cursor 并重验 marker；并发已完成返回 False，不创建猜测游标。"""
        ...

    async def attempts(self, pair: CalendarAadPair) -> tuple[CalendarAadRecoveryAttempt, ...]:
        """验证该用户精确 pair 的全部 ordinal 输入、键和状态，不接受宽松历史行。"""
        ...

    async def create(self, pair: CalendarAadPair, input: CalendarAadRecoveryInput) -> UUID:
        """同事务创建 TaskRun、无内容 audit 与 initial Outbox；唯一冲突验证相同意图。"""
        ...


class CalendarAadRecoveryStoreFactory(Protocol):
    """应用拥有事务生命周期，网络/queue 调用永远在该 context 外执行。"""

    def __call__(self) -> AbstractAsyncContextManager[CalendarAadRecoveryStore]: ...


class CalendarAadRecoveryUseCase:
    """一次调用每个 marker pair 最多返回一个活动 ordinal 或创建一个新 ordinal。"""

    def __init__(
        self,
        *,
        stores: CalendarAadRecoveryStoreFactory,
        guard: CalendarAadGuard,
        artifact: CalendarAadArtifact,
    ) -> None:
        """绑定同一 rollout；不提供 user/connection/calendar filter 或自动重新规划入口。"""
        self._stores, self._guard, self._artifact = stores, guard, artifact

    async def plan(self) -> tuple[CalendarAadPlannedTask, ...]:
        """在创建前与临提交前重证 guard；任何失败回滚本轮 TaskRun/Audit/Outbox。"""
        await self._guard.verify()
        planned: list[CalendarAadPlannedTask] = []
        async with self._stores() as store:
            for pair in await store.marked_pairs():
                if pair.digest not in self._artifact.pair_digests:
                    raise CalendarAadRolloutError("calendar_aad_affected_set_changed")
                if not await store.lock_marked_pair(pair):
                    continue
                attempts = await store.attempts(pair)
                ordinals = [attempt.input.recovery_attempt_ordinal for attempt in attempts]
                active = [attempt for attempt in attempts if attempt.status in _ACTIVE]
                if (
                    len(set(ordinals)) != len(ordinals)
                    or len(active) > 1
                    or any(
                        attempt.status not in _ACTIVE | {TaskStatus.FAILED, TaskStatus.CANCELLED}
                        for attempt in attempts
                    )
                ):
                    raise CalendarAadRolloutError("calendar_aad_recovery_invariant")
                if active:
                    winner = active[0]
                    task_id, ordinal = winner.task_id, winner.input.recovery_attempt_ordinal
                else:
                    ordinal = max(ordinals, default=0) + 1
                    input = CalendarAadRecoveryInput(
                        pair.connection_id, pair.calendar_id, pair.digest, ordinal
                    )
                    task_id = await store.create(pair, input)
                planned.append(CalendarAadPlannedTask(task_id, pair.digest, ordinal))
            await self._guard.verify()
        return tuple(planned)
