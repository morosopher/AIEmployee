"""验证 API tracing 生成安全 span，且不采集健康探针。"""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from sqlalchemy import create_engine, text

from ai_employee.infrastructure.observability.tracing import initialize_tracing


def test_tracing_exports_fastapi_and_sqlalchemy_spans_without_request_secrets() -> None:
    """业务请求应生成 HTTP/SQL span；headers、cookies 和 body 永远不写属性。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "telemetry-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    engine = create_engine("sqlite://")
    app = FastAPI()

    @app.get("/query")
    def query() -> dict[str, bool]:
        """执行合成 SQL，证明 SQLAlchemy instrumentation 已连接到同一 provider。"""
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"ok": True}

    @app.get("/api/v1/system/health")
    def health() -> dict[str, bool]:
        """提供需要从追踪排除的合成健康端点。"""
        return {"ok": True}

    @app.get("/metrics")
    def metrics() -> dict[str, bool]:
        """提供需要从追踪排除的合成指标端点。"""
        return {"ok": True}

    initialize_tracing(
        app=app,
        engine=engine,
        enabled=True,
        service_name="telemetry-test",
        endpoint="",
        tracer_provider=provider,
    )

    client = TestClient(app)
    response = client.request(
        "GET",
        "/query",
        headers={"Authorization": "Bearer synthetic-secret", "Cookie": "session=synthetic"},
        content="synthetic-body",
    )
    assert response.status_code == 200
    before_excluded = tuple(exporter.get_finished_spans())
    assert any(span.attributes.get("http.route") == "/query" for span in before_excluded)
    assert any(span.name.startswith("SELECT") for span in before_excluded)
    for span in before_excluded:
        serialized = repr(dict(span.attributes)).casefold()
        assert "authorization" not in serialized
        assert "cookie" not in serialized
        assert "synthetic-secret" not in serialized
        assert "synthetic-body" not in serialized

    client.get("/api/v1/system/health")
    client.get("/metrics")
    assert tuple(exporter.get_finished_spans()) == before_excluded
    engine.dispose()


def test_fastapi_server_spans_strip_oauth_and_sync_query_values() -> None:
    """入站 server span 不得导出 OAuth code/state 或 Google 增量游标。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "telemetry-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = FastAPI()

    @app.get("/oauth/callback")
    def oauth_callback() -> dict[str, bool]:
        """以合成回调路径验证 tracing 不读取 query 作为 span 属性。"""
        return {"ok": True}

    initialize_tracing(
        app=app,
        enabled=True,
        service_name="telemetry-test",
        endpoint="",
        tracer_provider=provider,
    )
    response = TestClient(app).get(
        "/oauth/callback?code=synthetic-oauth-code&state=synthetic-oauth-state"
        "&syncToken=synthetic-sync-token&pageToken=synthetic-page-token"
    )

    assert response.status_code == 200
    server_span = next(span for span in exporter.get_finished_spans() if span.kind is SpanKind.SERVER)
    serialized = repr(dict(server_span.attributes))
    for secret in (
        "synthetic-oauth-code",
        "synthetic-oauth-state",
        "synthetic-sync-token",
        "synthetic-page-token",
        "code=",
        "state=",
        "syncToken=",
        "pageToken=",
    ):
        assert secret not in serialized
    assert "/oauth/callback" in serialized
    assert server_span.attributes.get(
        "http.method", server_span.attributes.get("http.request.method")
    ) == "GET"
    assert server_span.attributes.get(
        "http.status_code", server_span.attributes.get("http.response.status_code")
    ) == 200


@pytest.mark.asyncio
async def test_httpx_client_spans_strip_google_sync_query_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTPX 客户端 span 不得导出 Gmail/Calendar 增量游标，但保留方法和状态诊断。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "telemetry-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    HTTPXClientInstrumentor().uninstrument()
    observed_urls: list[str] = []

    async def synthetic_send(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        """替代网络传输，以带 request 的合成响应驱动真实 HTTPX instrumentation。"""
        observed_urls.append(str(request.url))
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", synthetic_send)
    initialize_tracing(
        app=None,
        enabled=True,
        service_name="telemetry-test",
        endpoint="",
        tracer_provider=provider,
    )
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events"
                "?syncToken=synthetic-sync-token&pageToken=synthetic-page-token"
            )
    finally:
        HTTPXClientInstrumentor().uninstrument()

    assert response.status_code == 200
    assert observed_urls == [
        (
            "https://www.googleapis.com/calendar/v3/calendars/primary/events"
            "?syncToken=synthetic-sync-token&pageToken=synthetic-page-token"
        )
    ]
    client_span = next(span for span in exporter.get_finished_spans() if span.kind is SpanKind.CLIENT)
    serialized = repr(dict(client_span.attributes))
    assert "synthetic-sync-token" not in serialized
    assert "synthetic-page-token" not in serialized
    assert "syncToken=" not in serialized
    assert "pageToken=" not in serialized
    assert client_span.attributes.get("http.method", client_span.attributes.get("http.request.method")) == "GET"
    assert client_span.attributes.get(
        "http.status_code", client_span.attributes.get("http.response.status_code")
    ) == 200


def test_sync_httpx_client_span_strips_query_without_changing_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步 HTTPX 路径也必须脱敏 span，且仍将原始游标发给供应商。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "telemetry-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    HTTPXClientInstrumentor().uninstrument()
    observed_urls: list[str] = []

    def synthetic_send(
        _transport: httpx.HTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        """用合成同步响应驱动被 instrumentation 包裹的 HTTPTransport。"""
        observed_urls.append(str(request.url))
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", synthetic_send)
    initialize_tracing(
        app=None,
        enabled=True,
        service_name="telemetry-test",
        endpoint="",
        tracer_provider=provider,
    )
    try:
        response = httpx.Client().get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages"
            "?syncToken=synthetic-sync-token&pageToken=synthetic-page-token"
        )
    finally:
        HTTPXClientInstrumentor().uninstrument()

    assert response.status_code == 200
    assert observed_urls == [
        (
            "https://gmail.googleapis.com/gmail/v1/users/me/messages"
            "?syncToken=synthetic-sync-token&pageToken=synthetic-page-token"
        )
    ]
    client_span = next(span for span in exporter.get_finished_spans() if span.kind is SpanKind.CLIENT)
    serialized = repr(dict(client_span.attributes))
    assert "synthetic-sync-token" not in serialized
    assert "synthetic-page-token" not in serialized
    assert "syncToken=" not in serialized
    assert "pageToken=" not in serialized


def test_disabled_tracing_does_not_emit_httpx_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭 tracing 时不得安装 HTTPX hook 或留下任何 span。"""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "telemetry-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    HTTPXClientInstrumentor().uninstrument()

    def synthetic_send(
        _transport: httpx.HTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        """替代网络调用，确保本测试不访问外部地址。"""
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", synthetic_send)
    initialize_tracing(
        app=None,
        enabled=False,
        service_name="telemetry-test",
        endpoint="",
        tracer_provider=provider,
    )
    response = httpx.Client().get("https://gmail.googleapis.com/gmail/v1/users/me/messages?syncToken=unchanged")

    assert response.status_code == 200
    assert tuple(exporter.get_finished_spans()) == ()
