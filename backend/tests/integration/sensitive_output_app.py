"""仅测试侧导出真实应用与原始 OTEL span，验证实际收到的敏感请求字段。

不替换路由、用例、数据库或日志 redactor。Token/Cookie 比较只产生布尔到达回执；
exporter 原样写出 SDK span，任何泄漏都由外层 scanner 对原始文件判定。
"""

import asyncio
import json
import os
import stat
import threading
from collections.abc import Sequence
from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from starlette.types import ASGIApp, Receive, Scope, Send

from ai_employee.infrastructure.observability.tracing import initialize_tracing
from ai_employee.main import app as real_app


def _private_input() -> tuple[Path, str, str]:
    """只接受 scanner 创建的私有目录/输入和完全关闭真实写入的双测试环境。"""
    settings = real_app.state.auth_settings
    directory = Path(os.environ["M2_SENSITIVE_OUTPUT_DIRECTORY"])
    source = Path(os.environ["M2_SENSITIVE_INPUT_FILE"])
    if (settings.app_env != "test" or not settings.app_test_mode
        or settings.external_writes_enabled or settings.google_writes_enabled
        or settings.microsoft_writes_enabled or settings.otel_enabled
        or not settings.metrics_enabled):
        raise RuntimeError("sensitive observation requires isolated test settings")
    for path, mode in ((directory, 0o700), (source, 0o600)):
        attributes = path.stat()
        if path.is_symlink() or stat.S_IMODE(attributes.st_mode) != mode or attributes.st_uid != os.getuid():
            raise RuntimeError("sensitive observation input permissions are invalid")
    if source.parent != directory.parent or source.name != "input.json":
        raise RuntimeError("sensitive observation input binding is invalid")
    values = json.loads(source.read_text())
    if not isinstance(values, dict) or not isinstance(values.get("token"), str) or not isinstance(values.get("cookie"), str):
        raise TypeError("sensitive observation transport input is invalid")
    return directory, values["token"], values["cookie"]


class RawSpanExporter(SpanExporter):
    """直接写 SDK 的结构化 span JSON，不进行替换、白名单筛选或事后脱敏。"""

    def __init__(self, destination: Path) -> None:
        self._output = destination.open("x", encoding="utf-8")
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """同步 exporter 在 span 完成时保留原始字节，并立即 flush 供独立扫描。"""
        with self._lock:
            for span in spans:
                self._output.write(span.to_json(indent=None) + "\n")
            self._output.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """SDK 退出时关闭本 exporter 亲自创建的文件，不触碰其他输出。"""
        with self._lock:
            self._output.close()


class TransportProof:
    """透明转发真实 ASGI 请求，仅证明独立随机 Token/Cookie 确已到达 API 进程。"""

    def __init__(self, application: ASGIApp, directory: Path, token: str, cookie: str) -> None:
        self._application = application
        self._directory = directory
        self._token = ("Bearer " + token).encode("ascii")
        self._cookie = ("m2_sensitive_canary=" + cookie).encode("ascii")
        self._reached = {"token": False, "cookie": False}
        self._recorded = False

    def _record(self) -> None:
        """一次性写仅含布尔值的到达事实；磁盘 I/O 由调用方移出事件循环。"""
        with (self._directory / "transport-reached.json").open("x", encoding="utf-8") as output:
            json.dump(self._reached, output)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """不读取/改写请求正文或响应；真实应用照常执行认证、CSRF 和业务边界。"""
        if scope["type"] == "http":
            headers = dict(scope.get("headers", ()))
            self._reached["token"] |= headers.get(b"authorization") == self._token
            self._reached["cookie"] |= self._cookie in [part.strip() for part in headers.get(b"cookie", b"").split(b";")]
            if all(self._reached.values()) and not self._recorded:
                self._recorded = True
                await asyncio.to_thread(self._record)
        await self._application(scope, receive, send)


directory, token, cookie = _private_input()
provider = TracerProvider(resource=Resource.create({"service.name": "ai-employee-sensitive-test"}))
provider.add_span_processor(SimpleSpanProcessor(RawSpanExporter(directory / "traces.jsonl")))
initialize_tracing(app=real_app, async_engine=real_app.state.auth_session_factory.engine,
                   enabled=True, service_name="ai-employee-sensitive-test", endpoint="",
                   tracer_provider=provider)
app = TransportProof(real_app, directory, token, cookie)
