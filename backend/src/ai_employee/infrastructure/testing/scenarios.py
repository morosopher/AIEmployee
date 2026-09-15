"""连接测试专用一次性场景与离线 Fake；控制面只保存枚举、标识和调用计数。

场景消费与外部 Fake ledger 位于 Redis，它模拟供应商进程之外的观察事实，不替代
PostgreSQL 的任务、审批或执行状态。生产组合根不会构造本模块的写适配器。
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from redis.asyncio import Redis

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
    ApprovalWarningCode,
    ExecutionReference,
    ProviderWriteOutcome,
)
from ai_employee.domain.actions import ProviderWriteOutcomeKind
from ai_employee.domain.calendar_actions import (
    CalendarCreateCommand,
    CalendarRestoreCommand,
    CalendarUpdateCommand,
    NotificationPolicy,
)
from ai_employee.domain.mail_actions import MailSendCommand
from ai_employee.integrations.microsoft.calendar_write import validate_graph_notification_policy
from ai_employee.integrations.microsoft.timezones import to_windows_timezone

TEST_SCENARIO_KEY_PREFIX = "ai_employee:test-scenario:"
TEST_SCENARIO_TTL_SECONDS = 600


class TestScenario(StrEnum):
    """封闭的合成故障枚举；不接受供应商响应、地址、正文或任意配置。"""

    OAUTH_REVOKED = "oauth_revoked"
    GMAIL_429 = "gmail_429"
    CALENDAR_5XX = "calendar_5xx"
    MODEL_INVALID_TWICE = "model_invalid_twice"
    PARTIAL_SOURCE = "partial_source"
    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"
    TIMEOUT_AFTER_ACCEPT = "timeout_after_accept"
    AMBIGUOUS_5XX = "ambiguous_5xx"
    DELAYED_RECONCILIATION_SUCCESS = "delayed_reconciliation_success"
    NEVER_RESOLVED = "never_resolved"
    ETAG_CONFLICT = "etag_conflict"
    CAPABILITY_REVOKED = "capability_revoked"
    DUPLICATE_DELIVERY = "duplicate_delivery"


M2_WRITE_SCENARIOS = frozenset(
    {
        TestScenario.CONFIRMED_APPLIED,
        TestScenario.CONFIRMED_NOT_APPLIED,
        TestScenario.TIMEOUT_AFTER_ACCEPT,
        TestScenario.AMBIGUOUS_5XX,
        TestScenario.DELAYED_RECONCILIATION_SUCCESS,
        TestScenario.NEVER_RESOLVED,
        TestScenario.ETAG_CONFLICT,
        TestScenario.CAPABILITY_REVOKED,
        TestScenario.DUPLICATE_DELIVERY,
    }
)

# GETDEL 与首次 ledger 建立在同一原子脚本内，进程在返回前崩溃也不能把旧场景施加到
# 下一项操作。每次真实 execute 都累计一次，不在 Fake 内去重，以免掩盖 Worker 重发。
_RECORD_WRITE = """
local scenario = redis.call('HGET', KEYS[2], 'scenario')
if not scenario then
    scenario = redis.call('GETDEL', KEYS[1]) or 'confirmed_applied'
    redis.call('HSET', KEYS[2], 'scenario', scenario, 'action', ARGV[1],
               'write_calls', 0, 'reconcile_calls', 0)
end
if redis.call('HGET', KEYS[2], 'action') ~= ARGV[1] then
    return redis.error_reply('synthetic operation binding conflict')
end
local writes = redis.call('HINCRBY', KEYS[2], 'write_calls', 1)
redis.call('EXPIRE', KEYS[2], ARGV[2])
if ARGV[3] ~= '' and (scenario == 'confirmed_applied' or scenario == 'duplicate_delivery'
    or scenario == 'timeout_after_accept' or scenario == 'delayed_reconciliation_success') then
    redis.call('SET', KEYS[3], ARGV[3], 'EX', ARGV[2])
end
return {scenario, writes, redis.call('HGET', KEYS[2], 'reconcile_calls')}
"""

# 只读核对只能查阅已经存在的外部 ledger；缺失时返回 unknown，不能凭默认成功造结果。
_RECORD_RECONCILIATION = """
local scenario = redis.call('HGET', KEYS[1], 'scenario')
if not scenario then return {'never_resolved', 0, 0} end
if redis.call('HGET', KEYS[1], 'action') ~= ARGV[1] then
    return redis.error_reply('synthetic operation binding conflict')
end
local reads = redis.call('HINCRBY', KEYS[1], 'reconcile_calls', 1)
redis.call('EXPIRE', KEYS[1], ARGV[2])
return {scenario, redis.call('HGET', KEYS[1], 'write_calls'), reads}
"""


@dataclass(frozen=True, slots=True)
class M2FakeObservation:
    """外部 Fake 的内容无关旁证；零调用与缺失 ledger 具有相同安全计数。"""

    write_calls: int
    reconcile_calls: int


class M2FakeActionAdapter:
    """为四种类型化动作提供独立于 Worker 生命周期的可控离线结果。

    Args:
        redis_url: 本地测试 Redis；由双开关组合根或显式测试注入。
        user_id: 当前认证用户，用于隔离场景和操作 ledger。
        provider: 仅允许 Google/Microsoft；不能通过测试载荷注册其他供应商。

    Notes:
        不包含 HTTP 客户端、Token 或供应商写实现。调用计数不会自动去重，重复实际
        execute 必须暴露给故障断言；幂等责任始终在生产 ToolExecution 路径。
    """

    def __init__(self, *, redis_url: str, user_id: UUID, provider: str) -> None:
        if provider not in {"google", "microsoft"}:
            raise ValueError("synthetic provider is unsupported")
        self.provider = provider
        self._redis_url = redis_url
        self._user_id = user_id

    def _key(self, operation_id: UUID) -> str:
        """把用户、供应商与操作绑定到不含正文的短期测试键。"""
        return f"ai_employee:test-m2:{self._user_id}:{self.provider}:{operation_id}"

    def validate_for_approval(self, command: TrustedCommand) -> ApprovalPreflightResult:
        """纯本地验证可表示性，保留两家日历通知策略差异；绝不读取/消费场景。"""
        if isinstance(command, MailSendCommand):
            return ApprovalPreflightResult()
        if self.provider == "microsoft":
            validate_graph_notification_policy(
                attendees=command.attendees, policy=command.notification_policy
            )
            to_windows_timezone(command.timezone)
        elif command.notification_policy is NotificationPolicy.NONE:
            return ApprovalPreflightResult(
                warnings=(ApprovalWarningCode.GOOGLE_SEND_UPDATES_NONE_EXTERNAL_SYNC,)
            )
        return ApprovalPreflightResult()

    async def execute(self, command: TrustedCommand) -> ProviderWriteOutcome:
        """原子登记一次外部调用，再按固定场景返回三态结果；不会隐藏重复写入。"""
        client = Redis.from_url(self._redis_url, decode_responses=True)
        try:
            calendar_key = self._key(command.operation_id)
            if not isinstance(command, MailSendCommand):
                event_id = command.client_event_id if isinstance(command, CalendarCreateCommand) else command.provider_event_id
                calendar_key = self._calendar_key(command.connection_id, command.calendar_id, event_id)
            raw: object = await client.eval(
                _RECORD_WRITE, 3, f"{TEST_SCENARIO_KEY_PREFIX}{self._user_id}",
                self._key(command.operation_id), calendar_key,
                command.action, TEST_SCENARIO_TTL_SECONDS,
                "" if isinstance(command, MailSendCommand) else str(command.operation_id),
            )
        finally:
            await client.aclose()
        scenario, _, _ = _ledger_result(raw)
        return self._outcome(command, scenario=scenario, reconcile_calls=0)

    async def reconcile(
        self, command: TrustedCommand, execution: ExecutionReference
    ) -> ProviderWriteOutcome:
        """只读精确操作的外部事实；应用崩溃后的重复只读不会产生第二次写入。"""
        if execution.operation_id != command.operation_id or execution.provider != self.provider:
            raise ValueError("synthetic reconciliation binding conflict")
        client = Redis.from_url(self._redis_url, decode_responses=True)
        try:
            raw: object = await client.eval(
                _RECORD_RECONCILIATION, 1, self._key(command.operation_id),
                command.action, TEST_SCENARIO_TTL_SECONDS,
            )
        finally:
            await client.aclose()
        scenario, _, reads = _ledger_result(raw)
        return self._outcome(command, scenario=scenario, reconcile_calls=reads)

    async def observations(self, operation_id: UUID) -> M2FakeObservation:
        """读取外部 ledger 计数；不向调用方返回场景原文或业务载荷。"""
        client = Redis.from_url(self._redis_url, decode_responses=True)
        try:
            values = await client.hmget(self._key(operation_id), "write_calls", "reconcile_calls")
        finally:
            await client.aclose()
        return M2FakeObservation(int(values[0] or 0), int(values[1] or 0))

    def _calendar_key(self, connection_id: UUID, calendar_id: str, event_id: str) -> str:
        """外部当前事件索引只保存身份摘要和 operation ID，避免正文进入控制面。"""
        identity = json.dumps([str(connection_id), calendar_id, event_id], separators=(",", ":")).encode()
        return f"ai_employee:test-m2-event:{self._user_id}:{self.provider}:{hashlib.sha256(identity).hexdigest()}"

    async def current_calendar_operation(self, *, connection_id: UUID, calendar_id: str, event_id: str) -> UUID | None:
        """只接受原子写入的外部应用标记；Redis 丢失不能回退为本地缓存成功。"""
        client = Redis.from_url(self._redis_url, decode_responses=True)
        try:
            raw = await client.get(self._calendar_key(connection_id, calendar_id, event_id))
            if not isinstance(raw, str):
                return None
            try:
                operation = UUID(raw)
            except ValueError:
                return None
            scenario = await client.hget(self._key(operation), "scenario")
            if scenario not in {"confirmed_applied", "duplicate_delivery", "timeout_after_accept", "delayed_reconciliation_success"}:
                return None
            return operation
        finally:
            await client.aclose()

    def _outcome(
        self, command: TrustedCommand, *, scenario: TestScenario, reconcile_calls: int
    ) -> ProviderWriteOutcome:
        """将外部观察映射为封闭三态；没有证据的场景始终保留 UNKNOWN。"""
        applied = scenario in {TestScenario.CONFIRMED_APPLIED, TestScenario.DUPLICATE_DELIVERY}
        applied |= scenario is TestScenario.TIMEOUT_AFTER_ACCEPT and reconcile_calls >= 1
        applied |= scenario is TestScenario.DELAYED_RECONCILIATION_SUCCESS and reconcile_calls >= 2
        rejected = scenario in {
            TestScenario.CONFIRMED_NOT_APPLIED, TestScenario.ETAG_CONFLICT,
            TestScenario.CAPABILITY_REVOKED,
        }
        kind = (
            ProviderWriteOutcomeKind.CONFIRMED_APPLIED if applied else
            ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED if rejected else
            ProviderWriteOutcomeKind.UNKNOWN
        )
        code = None
        if scenario is TestScenario.ETAG_CONFLICT:
            code = "calendar_event_version_conflict"
        elif scenario is TestScenario.CAPABILITY_REVOKED:
            code = "connection_scope_missing"
        elif rejected:
            code = "provider_write_confirmed_not_applied"
        elif not applied:
            code = "provider_write_outcome_unknown"
        identifier = f"synthetic-{command.operation_id}"
        if isinstance(command, CalendarCreateCommand):
            identifier = command.client_event_id
        elif isinstance(command, (CalendarUpdateCommand, CalendarRestoreCommand)):
            identifier = command.provider_event_id
        return ProviderWriteOutcome(
            kind=kind, retryable=False, retry_after_seconds=None,
            provider_resource_id=identifier if applied else None,
            provider_request_id=identifier, correlation_id=str(command.operation_id),
            provider_url=f"https://example.test/{identifier}" if applied else None,
            error_code=code,
        )


def _ledger_result(raw: object) -> tuple[TestScenario, int, int]:
    """收窄 Redis 脚本返回值，错误只能报告固定原因，不回显控制面原值。"""
    if not isinstance(raw, list) or len(raw) != 3:
        raise ValueError("synthetic ledger result is invalid")
    try:
        scenario = TestScenario(raw[0])
        writes, reads = int(raw[1]), int(raw[2])
    except (ValueError, TypeError):
        raise ValueError("synthetic ledger result is invalid") from None
    if scenario not in M2_WRITE_SCENARIOS or writes < 0 or reads < 0:
        raise ValueError("synthetic ledger result is invalid")
    return scenario, writes, reads


async def consume_test_scenario(*, redis_url: str, user_id: UUID) -> str | None:
    """原子消费当前用户的测试故障并立即关闭短生命周期 Redis 客户端。

    该函数只由 ``APP_TEST_MODE`` 下的组合根传入 fake；生产适配器既不导入也不调用它。
    ``GETDEL`` 保证同一场景不会被重试或并发 fake 重复施加。
    """
    client = Redis.from_url(redis_url)
    try:
        value = await client.getdel(f"{TEST_SCENARIO_KEY_PREFIX}{user_id}")
    finally:
        await client.aclose()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value if isinstance(value, str) else None
