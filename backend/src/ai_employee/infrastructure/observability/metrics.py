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

METRIC_NAMESPACE = "ai_employee"
HEARTBEAT_INTERVAL_SECONDS = 30


class Metrics:
    """拥有 M1 所有稳定指标，禁止在业务模块临时创建名称或用户维度。

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
        self._sync_completed_at: dict[tuple[str, str], float] = {}
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

    def render(self) -> Response:
        """返回 Prometheus 文本，指标从不按用户或内容添加标签。"""
        self._refresh_heartbeat_ages()
        self._refresh_sync_ages()
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
        """设置脱敏同步新鲜度年龄。"""
        self.sync_age.labels(provider=provider, resource=resource).set(max(seconds, 0.0))

    def record_sync_success(self, *, provider: str, resource: str) -> None:
        """记录最近一次同步成功的单调时刻，供后续 scrape 计算实际年龄。"""
        self._sync_completed_at[(provider, resource)] = self._monotonic_clock()
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

    def _refresh_sync_ages(self) -> None:
        """按单调时钟刷新所有已成功同步资源的实际年龄。"""
        now = self._monotonic_clock()
        for (provider, resource), completed_at in self._sync_completed_at.items():
            self.record_sync_age(provider=provider, resource=resource, seconds=now - completed_at)


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
