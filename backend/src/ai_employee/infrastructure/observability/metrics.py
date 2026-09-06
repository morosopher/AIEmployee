"""集中注册 Prometheus 指标，并提供 API 进程安全暴露入口。"""

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)
from starlette.responses import Response

from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability

METRIC_NAMESPACE = "ai_employee"
HEARTBEAT_INTERVAL_SECONDS = 30
M2_PROVIDERS = ("google", "microsoft")
M2_ACTIONS = ("mail.send", "calendar.create", "calendar.update", "calendar.restore")
M2_OUTCOMES = ("confirmed_applied", "confirmed_not_applied", "unknown")


class Metrics:
    """拥有 M1 与 M2 所有稳定指标，禁止在业务模块临时创建名称或用户维度。

    Args:
        registry: 可注入 registry 以供测试隔离；生产为每个应用实例创建独立 registry，
            防止 ASGI 测试反复构造应用时注册重复时序列。
    """

    def __init__(
        self, registry: CollectorRegistry | None = None, *, monotonic_clock: Callable[[], float] | None = None
    ) -> None:
        """以固定名称、标签和单位创建任务、依赖、模型及 API 指标。"""
        target_registry = registry or CollectorRegistry(auto_describe=True)
        self._registry = target_registry
        self._monotonic_clock = monotonic_clock or monotonic
        self._heartbeat_at: dict[str, float] = {}
        self._stuck_kinds: set[str] = set()
        self.tasks_total = Counter("ai_employee_tasks_total", "任务终态计数", ["kind", "status"], registry=target_registry)
        self.task_duration = Histogram("ai_employee_task_duration_seconds", "任务耗时", ["kind", "status"], registry=target_registry)
        self.task_retries = Counter("ai_employee_task_retries_total", "任务重试次数", ["kind"], registry=target_registry)
        self.queue_wait = Histogram("ai_employee_queue_wait_seconds", "任务排队时间", ["kind"], registry=target_registry)
        self.stuck_tasks = Gauge("ai_employee_stuck_tasks", "疑似卡住任务数", ["kind"], registry=target_registry)
        self.sync_age = Gauge("ai_employee_sync_age_seconds", "同步新鲜度", ["provider", "resource"], registry=target_registry)
        self.provider_errors = Counter("ai_employee_provider_errors_total", "供应商错误", ["provider", "error_code"], registry=target_registry)
        self.model_schema_repairs = Counter("ai_employee_model_schema_repairs_total", "模型结构修复", ["model", "outcome"], registry=target_registry)
        self.model_tokens = Counter("ai_employee_model_tokens_total", "模型 token", ["model", "direction"], registry=target_registry)
        self.model_cost = Counter("ai_employee_model_cost_usd_total", "模型估算成本", ["model"], registry=target_registry)
        self.model_latency = Histogram("ai_employee_model_latency_seconds", "模型延迟", ["model"], registry=target_registry)
        self.api_errors = Counter("ai_employee_api_errors_total", "API 错误", ["route", "error_code"], registry=target_registry)
        self.sse_connections = Gauge("ai_employee_sse_connections", "SSE 连接数", ["state"], registry=target_registry)
        self.heartbeat_age = Gauge("ai_employee_process_heartbeat_age_seconds", "进程心跳年龄", ["process"], registry=target_registry)
        self.dependency_health = Gauge("ai_employee_dependency_health", "依赖健康状态", ["dependency"], registry=target_registry)
        self.provider_writes = Counter(
            "ai_employee_provider_write_requests_total",
            "实际供应商写请求结果",
            ["provider", "action", "outcome"],
            registry=target_registry,
        )
        self.approval_decisions = Counter(
            "ai_employee_approval_decisions_total",
            "已提交审批决定",
            ["action", "decision"],
            registry=target_registry,
        )
        self.approval_expired = Counter(
            "ai_employee_approval_expired_total",
            "已提交审批到期",
            ["action"],
            registry=target_registry,
        )
        self.reconciliations = Counter(
            "ai_employee_tool_reconciliation_total",
            "实际只读核对结果",
            ["provider", "action", "outcome"],
            registry=target_registry,
        )
        self.reconciliation_age = Gauge(
            "ai_employee_tool_reconciliation_age_seconds",
            "最老未决写入的年龄",
            ["provider", "action"],
            registry=target_registry,
        )
        self.needs_attention = Gauge(
            "ai_employee_needs_attention_tasks",
            "等待人工确认的任务数",
            ["provider", "action"],
            registry=target_registry,
        )
        self.calendar_conflicts = Counter(
            "ai_employee_calendar_version_conflicts_total",
            "供应商日历版本冲突",
            ["provider"],
            registry=target_registry,
        )
        self.capability_state = Gauge(
            "ai_employee_connection_capability_state",
            "各能力状态的连接数",
            ["provider", "capability", "state"],
            registry=target_registry,
        )
        self.write_kill_switch = Gauge(
            "ai_employee_write_kill_switch_state",
            "有效供应商写入开关，1为允许",
            ["provider"],
            registry=target_registry,
        )

    def record_provider_write(self, *, provider: str, action: str, outcome: str) -> None:
        """只在 request-start CAS 赢家实际调用 adapter 后计数；标签严格封闭。"""
        _bounded(provider, M2_PROVIDERS)
        _bounded(action, M2_ACTIONS)
        _bounded(outcome, M2_OUTCOMES)
        self.provider_writes.labels(provider=provider, action=action, outcome=outcome).inc()

    def record_reconciliation(self, *, provider: str, action: str, outcome: str) -> None:
        """记录只读调用结果；与供应商写请求使用不同计数器。"""
        _bounded(provider, M2_PROVIDERS)
        _bounded(action, M2_ACTIONS)
        _bounded(outcome, M2_OUTCOMES)
        self.reconciliations.labels(provider=provider, action=action, outcome=outcome).inc()

    def record_approval_decision(self, *, action: str, decision: str) -> None:
        """在决定事务成功提交后记录一次批准或拒绝，不记录无效重复请求。"""
        _bounded(action, M2_ACTIONS)
        _bounded(decision, ("approved", "rejected"))
        self.approval_decisions.labels(action=action, decision=decision).inc()

    def record_approval_expired(self, *, action: str) -> None:
        """记录已提交的 M2 过期状态；M1 fake.write 不进入该指标。"""
        _bounded(action, M2_ACTIONS)
        self.approval_expired.labels(action=action).inc()

    def record_calendar_version_conflict(self, *, provider: str) -> None:
        """记录规范化的 ETag 冲突，不保存日历 ID 或供应商错误原文。"""
        _bounded(provider, M2_PROVIDERS)
        self.calendar_conflicts.labels(provider=provider).inc()

    def record_duplicate_provider_call_attempt(self, *, provider: str) -> None:
        """用既有错误族记录被 request-start CAS 阻断的重复写企图。"""
        _bounded(provider, M2_PROVIDERS)
        self.record_provider_error(provider=provider, error_code="duplicate_provider_call_attempt")

    def record_action_state(
        self,
        *,
        provider: str,
        action: str,
        needs_attention: int,
        age_seconds: float,
        unresolved: bool = True,
    ) -> None:
        """设置数据库聚合，并让直曝 registry 的每次 scrape 计算未决年龄。

        Gauge 的回调只读单调时钟，绝不在 Prometheus 线程执行数据库 I/O。扫描重启后
        重新读取持久 request-start，终态时显式清零且停止年龄增长。
        """
        _bounded(provider, M2_PROVIDERS)
        _bounded(action, M2_ACTIONS)
        self.needs_attention.labels(provider=provider, action=action).set(max(needs_attention, 0))
        observed_at = self._monotonic_clock()
        age = max(age_seconds, 0)
        self.reconciliation_age.labels(provider=provider, action=action).set_function(
            lambda: max(age + self._monotonic_clock() - observed_at, 0) if unresolved else 0
        )

    def record_capability_state(
        self, *, provider: str, capability: str, state: str, count: int
    ) -> None:
        """仅按两供应商、四能力及固定状态聚合，禁止连接或账户标签。"""
        _bounded(provider, M2_PROVIDERS)
        _bounded(capability, tuple(item.value for item in ConnectionCapability))
        _bounded(state, tuple(item.value for item in CapabilityStatus))
        self.capability_state.labels(provider=provider, capability=capability, state=state).set(
            max(count, 0)
        )

    def record_write_kill_switch(self, *, provider: str, enabled: bool) -> None:
        """只表达当前进程配置的全局×供应商有效开关，不更改策略。"""
        _bounded(provider, M2_PROVIDERS)
        self.write_kill_switch.labels(provider=provider).set(int(enabled))

    def render(self) -> Response:
        """返回 Prometheus 文本，指标从不按用户或内容添加标签。"""
        self._refresh_heartbeat_ages()
        return Response(generate_latest(self._registry), media_type=CONTENT_TYPE_LATEST)

    def record_task_outcome(self, *, kind: str, status: str, duration_seconds: float) -> None:
        """记录一次已完成任务的终态和耗时。

        Args:
            kind: 受控任务种类，不能使用请求或用户输入的自由文本。
            status: 受控终态或 ``retry_scheduled`` 状态。
            duration_seconds: 从持久任务开始到本次状态写入的非负秒数。
        """
        self.tasks_total.labels(kind=kind, status=status).inc()
        self.task_duration.labels(kind=kind, status=status).observe(max(duration_seconds, 0.0))

    def record_task_retry(self, *, kind: str) -> None:
        """记录一次由持久状态机安排的重试，不读取队列消息详情。"""
        self.task_retries.labels(kind=kind).inc()

    def record_queue_wait(self, *, kind: str, seconds: float) -> None:
        """记录任务从创建到首次获得 Worker 租约的等待时间。"""
        self.queue_wait.labels(kind=kind).observe(max(seconds, 0.0))

    def record_stuck_tasks(self, *, kind: str, count: int) -> None:
        """设置一次扫描发现的卡住任务数量，避免把用户标识放入标签。"""
        self._stuck_kinds.add(kind)
        self.stuck_tasks.labels(kind=kind).set(max(count, 0))

    def clear_stuck_tasks(self, *, active_kinds: set[str]) -> None:
        """将本轮扫描未出现的历史 kind 归零，防止恢复后遗留告警。"""
        for kind in self._stuck_kinds - active_kinds:
            self.stuck_tasks.labels(kind=kind).set(0)

    def record_sync_age(self, *, provider: str, resource: str, seconds: float) -> None:
        """用最新数据库聚合或成功观测替换年龄基准，直曝 registry 同样持续增长。

        单次同步成功不能永久遮住数据库中另一来源的更旧时间；每次持久聚合都替换
        回调闭包中的基准，不再由 API render 的进程内成功记录反向覆盖。
        """
        completed_at = self._monotonic_clock() - max(seconds, 0.0)
        self.sync_age.labels(provider=provider, resource=resource).set_function(
            lambda: max(self._monotonic_clock() - completed_at, 0)
        )

    def record_sync_success(self, *, provider: str, resource: str) -> None:
        """记录最近一次同步成功的单调时刻，供后续 scrape 计算实际年龄。"""
        self.record_sync_age(provider=provider, resource=resource, seconds=0.0)

    def record_provider_error(self, *, provider: str, error_code: str) -> None:
        """记录已规范化供应商错误码，绝不使用原始响应文本。"""
        self.provider_errors.labels(provider=provider, error_code=error_code).inc()

    def record_model_schema_repair(self, *, model: str, outcome: str) -> None:
        """记录一次结构化输出修复尝试的安全结果。"""
        self.model_schema_repairs.labels(model=model, outcome=outcome).inc()

    def record_model_response(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
        latency_seconds: float,
    ) -> None:
        """记录模型用量和延迟，参数均为不含提示词的聚合数值。"""
        self.model_tokens.labels(model=model, direction="input").inc(max(input_tokens, 0))
        self.model_tokens.labels(model=model, direction="output").inc(max(output_tokens, 0))
        self.model_cost.labels(model=model).inc(max(estimated_cost_usd, 0.0))
        self.model_latency.labels(model=model).observe(max(latency_seconds, 0.0))

    def record_sse_connection(self, *, connected: bool) -> None:
        """在 SSE 建立和关闭时增减活动连接 Gauge。"""
        self.sse_connections.labels(state="active").inc(1 if connected else -1)

    def record_heartbeat(self, *, process: str, age_seconds: float) -> None:
        """更新进程心跳年龄，正常运行路径应持续写入零。"""
        self._heartbeat_at[process] = self._monotonic_clock() - max(age_seconds, 0.0)
        self._refresh_heartbeat_ages()
        recorded_at = self._heartbeat_at[process]
        self.heartbeat_age.labels(process=process).set_function(
            lambda: max(self._monotonic_clock() - recorded_at, 0)
        )

    def record_dependency_health(self, *, dependency: str, healthy: bool) -> None:
        """把依赖探测结果映射为 1 或 0，不输出 DSN 或连接错误原文。"""
        self.dependency_health.labels(dependency=dependency).set(1 if healthy else 0)

    def start_internal_listener(self, *, port: int) -> None:
        """启动仅供容器内部网络抓取的 Prometheus HTTP 监听器。

        API 进程使用路由统一暴露指标；Worker 与 Scheduler 没有 ASGI 服务，因此由本方法
        使用独立 registry 提供相同文本格式。部署层不得把该端口发布给主机或 Caddy。
        """
        start_http_server(port=port, addr="0.0.0.0", registry=self._registry)

    def _refresh_heartbeat_ages(self) -> None:
        """在每次 scrape 前按单调时钟刷新所有进程心跳年龄。"""
        now = self._monotonic_clock()
        for process, recorded_at in self._heartbeat_at.items():
            self.heartbeat_age.labels(process=process).set(max(now - recorded_at, 0.0))


def _bounded(value: str, allowed: tuple[str, ...]) -> None:
    """在创建标签前拒绝任意用户文本，错误消息也不回显传入值。"""
    if value not in allowed:
        raise ValueError("metric label is outside the bounded vocabulary")


def create_metrics() -> Metrics:
    """构造 API、Worker、Scheduler 可共享的默认指标集合。"""
    return Metrics()


async def run_periodic_heartbeat(
    *,
    metrics: Metrics,
    process: str,
    interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    on_tick: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """在空闲进程中持续写入心跳，并可在同一安全 tick 执行无副作用探针。

    任务由各进程生命周期创建并在 shutdown 取消，绝不持久化或向队列投递任何事实。
    """
    while True:
        metrics.record_heartbeat(process=process, age_seconds=0)
        if on_tick is not None:
            await on_tick()
        await asyncio.sleep(interval_seconds)
