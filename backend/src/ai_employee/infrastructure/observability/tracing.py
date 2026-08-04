"""以可选 OTLP 导出器初始化无敏感 HTTP 属性的 OpenTelemetry 追踪。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from opentelemetry.instrumentation.httpx import RequestInfo
from opentelemetry.trace import Span
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from opentelemetry.trace import TracerProvider


_HTTPX_URL_ATTRIBUTES = ("http.url", "url.full")
_SERVER_URL_ATTRIBUTES = (
    "http.url",
    "url.full",
    "http.target",
    "http.request.target",
    "http.request.url",
)
_SERVER_QUERY_ATTRIBUTES = ("url.query", "url.fragment")


def _sanitize_httpx_request_span(span: Span, request: RequestInfo) -> None:
    """移除 HTTPX 客户端 URL 属性中的 query 与 fragment，不改变实际请求。

    Google 的 ``syncToken``、``pageToken`` 等增量游标位于 query；OpenTelemetry 旧版与新版
    semantic convention 分别可能使用 ``http.url`` 和 ``url.full``，因此两者都写入同一个安全
    的 scheme-host-path 值。hook 只操作刚创建的 span，不触碰 ``RequestInfo`` 或 HTTPX headers，
    所以供应商请求仍携带原有 query 且调用语义不变。
    """
    safe_url = _strip_http_query(str(request.url))
    for attribute_name in _HTTPX_URL_ATTRIBUTES:
        span.set_attribute(attribute_name, safe_url)


async def _sanitize_async_httpx_request_span(span: Span, request: RequestInfo) -> None:
    """为 AsyncHTTPTransport 复用同步的 span URL 脱敏逻辑。"""
    _sanitize_httpx_request_span(span, request)


def _sanitize_fastapi_server_span(span: Span, scope: dict[str, object]) -> None:
    """从 FastAPI server span 移除 OAuth 与同步 query，只保留请求 path。

    ASGI instrumentation 会在创建 span 时把 ``scope.query_string`` 拼入旧版 ``http.url``，
    并可能随 semantic convention 写入 ``url.full``、``http.target`` 或新命名的完整 URL 属性。
    这些字段可包含 OAuth ``code/state``、Gmail/Calendar 增量游标，因此 hook 统一覆盖为没有
    query/fragment 的 path；方法、路由和响应状态仍由 instrumentation 原生记录。
    """
    raw_path = scope.get("path", "/")
    path = _strip_http_query(raw_path) if isinstance(raw_path, str) else "/"
    for attribute_name in _SERVER_URL_ATTRIBUTES:
        span.set_attribute(attribute_name, path)
    for attribute_name in _SERVER_QUERY_ATTRIBUTES:
        span.set_attribute(attribute_name, "")


def _strip_http_query(raw_url: str) -> str:
    """保留 HTTP URL 的可诊断定位部分，删除不安全的 query 和 fragment。"""
    parsed = urlsplit(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def initialize_tracing(
    *,
    app: FastAPI | None,
    async_engine: AsyncEngine | None = None,
    engine: Engine | None = None,
    enabled: bool,
    service_name: str,
    endpoint: str,
    tracer_provider: TracerProvider | None = None,
) -> None:
    """按配置一次性安装 FastAPI、SQLAlchemy 与 HTTPX instrumentation。

    Args:
        app: 可选 FastAPI 应用，仅 API 进程传入。
        async_engine: API/Worker 使用的异步 SQLAlchemy engine。
        engine: 仅供同步 SQLAlchemy 边界或 in-memory 集成测试使用的 engine。
        enabled: 是否启用追踪；关闭时完全 no-op。
        service_name: OTEL service.name 资源属性。
        endpoint: 可选 OTLP HTTP collector 地址。
        tracer_provider: 可注入 provider；测试用它隔离全局 OpenTelemetry 状态。

    Raises:
        ValueError: 同时传入同步和异步 engine，无法确定唯一的 SQL instrumentation 目标。

    未配置 endpoint 时仍安装 SDK provider，但不联网导出。headers 与 request body 从不作为
    instrumentation 参数开启。注入 provider 时绝不修改全局 provider，避免同一测试进程中
    的应用工厂或其他测试互相覆盖 exporter；生产路径才注册一次进程级 provider。
    """
    if not enabled:
        return
    if async_engine is not None and engine is not None:
        raise ValueError("tracing accepts either async_engine or engine, not both")

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = tracer_provider
    if provider is None:
        configured_provider = SdkTracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            configured_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
        # OpenTelemetry 只允许设置一次 provider。若宿主已安装 provider，后续进程初始化复用
        # 它而不重复注册 exporter 或 instrumentation。
        trace.set_tracer_provider(configured_provider)
        provider = trace.get_tracer_provider()

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            excluded_urls="/api/v1/system/health,/metrics",
            server_request_hook=_sanitize_fastapi_server_span,
        )
    target_engine = async_engine.sync_engine if async_engine is not None else engine
    if target_engine is not None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=target_engine, tracer_provider=provider)
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument(
        tracer_provider=provider,
        request_hook=_sanitize_httpx_request_span,
        async_request_hook=_sanitize_async_httpx_request_span,
    )
