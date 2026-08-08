"""配置字段固定、输入已脱敏的 JSON 结构化日志。"""

import hashlib
import json
import logging
import traceback
from collections.abc import Callable
from threading import RLock
from typing import Final

from ai_employee.infrastructure.observability.redaction import redact_value

# HTTPX/HTTPCore 的请求日志模板由第三方版本控制，可能把完整 URL query、Authorization
# header 或异常对象拼入 message。记录创建时统一收窄为固定事件，后续无论记录传播到哪个
# handler、是否被 dictConfig 重新配置，或最终落到 lastResort，都不会再携带第三方原文。
_HTTP_CLIENT_LOGGER_NAMES: Final[tuple[str, ...]] = ("httpx", "httpcore")
_HTTP_CLIENT_EVENT: Final[str] = "http_client_event"
_HTTP_CLIENT_LOGGING_LOCK = RLock()
_HTTP_CLIENT_MAKE_RECORD_MARKER: Final[object] = object()
_HTTP_CLIENT_MAKE_RECORD_MARKER_ATTR: Final[str] = "_ai_employee_http_client_make_record_marker"
_HTTP_CLIENT_SAFE_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        # logging.LogRecord 的标准字段保留文件位置、级别和线程维度，但不保留第三方
        # factory 或 LoggerAdapter 注入的任意对象；这些对象可能是 URL、Header 或正文。
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        # Formatter 可能在 format() 中读取这两个标准缓存字段。
        "message",
        "asctime",
    }
)


def _is_http_client_logger(name: str) -> bool:
    """判断记录是否来自 HTTPX 或 HTTPCore 命名空间。"""
    return any(
        name == namespace or name.startswith(f"{namespace}.")
        for namespace in _HTTP_CLIENT_LOGGER_NAMES
    )


def _scrub_http_client_record(record: logging.LogRecord) -> logging.LogRecord:
    """在记录进入任意 handler 前固定 HTTP 客户端字段并清空未知扩展值。

    ``Logger.makeRecord`` 会在全局 ``LogRecordFactory`` 返回后再把 ``extra`` 写入
    ``record.__dict__``。因此仅包装 factory 不能覆盖 ``extra`` 中的 Authorization、URL
    或 token；配置 helper 还会包装 ``Logger.makeRecord``，在该注入步骤完成后再次调用本
    函数。未知字段会被删除；需要固定占位符的宿主 formatter 可通过
    ``logging.Formatter.defaults`` 提供安全默认值。
    """
    if not _is_http_client_logger(record.name):
        return record

    # 只保留稳定事件名；args、异常和 stack_info 可能分别携带 URL、Header、Cookie 或
    # 供应商响应，必须在记录进入任何 logger/handler 前一起清空。
    record.msg = _HTTP_CLIENT_EVENT
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    record.message = _HTTP_CLIENT_EVENT

    # extra 在 factory 之后写入，且自定义 factory 也可能预先挂载任意扩展字段。只保留标准
    # logging 元数据，其余键和值一并删除，避免字典序列化器读取原始对象或把敏感字段名写入
    # 日志；需要固定占位符的宿主 formatter 应通过 logging.Formatter.defaults 提供默认值。
    attributes = record.__dict__
    for key in tuple(attributes):
        if key not in _HTTP_CLIENT_SAFE_RECORD_FIELDS:
            del attributes[key]
    return record


class _HttpClientLogRecordFactory:
    """在保留宿主记录工厂的前提下，固定第三方 HTTP 记录的可观测内容。

    ``logging`` 在创建 ``LogRecord`` 时调用全局 factory；``Logger.makeRecord`` 随后才注入
    ``extra``。factory 与 makeRecord 两个边界都完成收窄后，同一记录经过父/子 logger、重
    配置后的 handler 以及 ``lastResort`` 时都共享安全字段。委托原 factory 可保留宿主已经
    安装的记录类型，不改变应用 ``ai_employee`` 日志的既有行为。
    """

    def __init__(self, delegate: Callable[..., logging.LogRecord]) -> None:
        """保存宿主 factory，避免覆盖其他框架的记录初始化逻辑。"""
        self._delegate = delegate

    def __call__(self, *args: object, **kwargs: object) -> logging.LogRecord:
        """创建记录并在 extra 注入前清除 HTTP 客户端原文和异常上下文。"""
        record = self._delegate(*args, **kwargs)
        return _scrub_http_client_record(record)


def _wrap_http_client_make_record(
    delegate: Callable[..., logging.LogRecord],
) -> Callable[..., logging.LogRecord]:
    """包装宿主 ``Logger.makeRecord``，覆盖 factory 之后的 extra 注入时序。"""

    def wrapped(
        logger: logging.Logger,
        *args: object,
        **kwargs: object,
    ) -> logging.LogRecord:
        """委托原方法后再清理最终记录，保持宿主自定义签名和实现。"""
        record = delegate(logger, *args, **kwargs)
        return _scrub_http_client_record(record)

    setattr(wrapped, _HTTP_CLIENT_MAKE_RECORD_MARKER_ATTR, _HTTP_CLIENT_MAKE_RECORD_MARKER)
    return wrapped


def configure_http_client_logging() -> None:
    """安装 HTTPX/HTTPCore 记录级脱敏边界，且重复调用保持幂等。

    该 helper 只包装当前 ``LogRecordFactory`` 与 ``Logger.makeRecord``，不删除宿主 handler、
    不修改 logger level 或 ``propagate``。后一个边界用于覆盖标准库在 factory 返回后注入
    ``extra`` 的时序；因此宿主可以自由重配第三方 logger，而每条新记录仍会在所有 sink 之
    前被固定为安全事件。应用自己的 ``ai_employee.*`` logger 完全沿用原行为。
    """
    # 两个全局入口必须在同一把锁下按 makeRecord → factory 顺序安装：makeRecord wrapper
    # 先覆盖 factory 之后的 extra 注入步骤，避免另一线程恰好在 factory 已替换、旧
    # makeRecord 仍生效的窗口里把 Authorization/token 写回记录。记录创建本身无需持有该锁，
    # 减少 OAuth 并发请求的额外争用。
    with _HTTP_CLIENT_LOGGING_LOCK:
        current_make_record = logging.Logger.makeRecord
        if (
            getattr(current_make_record, _HTTP_CLIENT_MAKE_RECORD_MARKER_ATTR, None)
            is not _HTTP_CLIENT_MAKE_RECORD_MARKER
        ):
            # 通过类属性替换保留 Python descriptor 绑定语义；wrapper 本身不遍历 loggerDict，
            # 因而未来新建子 logger、dictConfig 和并发 getLogger 都共享同一安全边界。
            type.__setattr__(
                logging.Logger,
                "makeRecord",
                _wrap_http_client_make_record(current_make_record),
            )

        # makeRecord 边界已经覆盖后，再安装 factory 以处理直接调用 factory 的路径；这样
        # 无论初始化线程在两步之间被调度到哪里，HTTP 记录都至少经过一个完整的 scrubber。
        current_factory = logging.getLogRecordFactory()
        if not isinstance(current_factory, _HttpClientLogRecordFactory):
            logging.setLogRecordFactory(_HttpClientLogRecordFactory(current_factory))


def diagnostic_user_digest(user_id: str) -> str:
    """生成不可逆用户诊断维度，避免原始 UUID 或邮箱进入日志。

    Args:
        user_id: 已认证用户的内部 UUID 字符串。

    Returns:
        固定长度 SHA-256 摘要，仅供同进程诊断关联。
    """
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def configure_json_logging(secret_patterns: tuple[str, ...] = ()) -> None:
    """安装仅输出脱敏 JSON 的根日志 handler，重复调用不会叠加 handler。

    HTTPX/HTTPCore 请求记录先由记录级边界固定为安全事件；应用自己的 ``ai_employee``
    logger 仍由根 handler 正常处理，HTTP 请求诊断由已脱敏的 OpenTelemetry span 负责。
    """
    configure_http_client_logging()
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter(secret_patterns))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class _JsonFormatter(logging.Formatter):
    """把标准日志属性与允许的 extras 收窄为稳定 JSON 信封。"""

    def __init__(self, secret_patterns: tuple[str, ...]) -> None:
        super().__init__()
        self._secret_patterns = secret_patterns

    def format(self, record: logging.LogRecord) -> str:
        """省略 Python 异常原文，避免供应商错误文本越过脱敏边界。"""
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname.lower(),
            # 标准 logging 的 message 可能是异常插值或任意调用方字符串；使用 logger 名称
            # 作为稳定事件，而非读取 ``getMessage`` 或 exception 文本。
            "event": record.name if record.name.startswith("ai_employee.") else "application.log",
            "trace_id": getattr(record, "trace_id", None),
            "task_id": getattr(record, "task_id", None),
            "step_id": getattr(record, "step_id", None),
            "provider": getattr(record, "provider", None),
            "error_code": getattr(record, "error_code", None),
        }
        if record.exc_info is not None:
            # 只记录代码位置，刻意不调用 formatException：异常消息和 locals 可能包含请求正文、
            # Cookie、授权头或供应商响应。函数名与行号足以让 trace_id 关联到受控源码版本。
            payload["stack"] = [
                {"function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(
            redact_value(payload, secret_patterns=self._secret_patterns), ensure_ascii=False
        )
