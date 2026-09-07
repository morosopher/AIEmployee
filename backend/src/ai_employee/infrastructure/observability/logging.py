"""配置字段固定、输入已脱敏的 JSON 结构化日志。"""

import hashlib
import json
import logging
import re
import traceback
from collections.abc import Callable
from threading import RLock
from typing import Final
from weakref import WeakKeyDictionary

from ai_employee.infrastructure.observability.redaction import redact_value

# 固定诊断字段只能携带短标识符，不能把字典或 URL/request-target 放入合法键绕过 allowlist。
_DIAGNOSTIC_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_DIAGNOSTIC_FIELDS: Final[tuple[str, ...]] = (
    "trace_id",
    "task_id",
    "step_id",
    "provider",
    "error_code",
)


def _stable_diagnostic_value(value: object) -> str | None:
    """仅保留标识符标量；query、路径、任意对象及嵌套 URL 字段统一省略为 NULL。"""
    return value if type(value) is str and _DIAGNOSTIC_VALUE.fullmatch(value) else None


# HTTPX/HTTPCore 的请求日志模板由第三方版本控制，可能把完整 URL query、Authorization
# header 或异常对象拼入 message。记录创建时统一收窄为固定事件，后续无论记录传播到哪个
# handler、是否被 dictConfig 重新配置，或最终落到 lastResort，都不会再携带第三方原文。
_HTTP_CLIENT_LOGGER_NAMES: Final[tuple[str, ...]] = ("httpx", "httpcore")
_HTTP_CLIENT_EVENT: Final[str] = "http_client_event"
_HTTP_CLIENT_LOGGING_LOCK = RLock()
_HTTP_CLIENT_RECORD_SOURCES_LOCK = RLock()
_HTTP_CLIENT_MAKE_RECORD_MARKER: Final[object] = object()
_HTTP_CLIENT_MAKE_RECORD_MARKER_ATTR: Final[str] = "_ai_employee_http_client_make_record_marker"
_HTTP_CLIENT_HANDLE_MARKER: Final[object] = object()
_HTTP_CLIENT_HANDLE_MARKER_ATTR: Final[str] = "_ai_employee_http_client_handle_marker"
_HTTP_CLIENT_CALL_HANDLERS_MARKER: Final[object] = object()
_HTTP_CLIENT_CALL_HANDLERS_MARKER_ATTR: Final[str] = "_ai_employee_http_client_call_handlers_marker"
_HTTP_CLIENT_HANDLER_FILTER_MARKER: Final[object] = object()
_HTTP_CLIENT_HANDLER_FILTER_MARKER_ATTR: Final[str] = (
    "_ai_employee_http_client_handler_filter_marker"
)
_HTTP_CLIENT_RECORD_SOURCE_FALLBACK_ATTR: Final[str] = "_ai_employee_http_client_record_source"
_HTTP_CLIENT_RECORD_SOURCES: WeakKeyDictionary[logging.LogRecord, str] = WeakKeyDictionary()
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


def _is_http_client_logger(name: object) -> bool:
    """判断记录是否来自 HTTPX 或 HTTPCore 命名空间。

    ``logging.makeLogRecord`` 会先用 ``name=None`` 创建占位记录，再把调用方字典更新
    到记录上；反序列化输入也可能提供其他非字符串值。此处先收窄类型，避免日志安全
    入口因元数据异常抛错，同时让未知来源继续沿用应用日志行为。
    """
    if not isinstance(name, str):
        return False
    return any(
        name == namespace or name.startswith(f"{namespace}.")
        for namespace in _HTTP_CLIENT_LOGGER_NAMES
    )


def _canonical_http_client_name(source_name: str) -> str:
    """把可信 HTTP logger 名称收窄到不含动态子段的稳定命名空间。"""
    for namespace in _HTTP_CLIENT_LOGGER_NAMES:
        if source_name == namespace or source_name.startswith(f"{namespace}."):
            return namespace
    return _HTTP_CLIENT_EVENT


def _remember_http_client_source(record: logging.LogRecord, source_name: str) -> None:
    """在弱引用表中记录 HTTP 来源，避免把可变 ``record.name`` 当作信任根。"""
    canonical_name = _canonical_http_client_name(source_name)
    with _HTTP_CLIENT_RECORD_SOURCES_LOCK:
        try:
            _HTTP_CLIENT_RECORD_SOURCES[record] = canonical_name
        except TypeError:
            # 宿主自定义 LogRecord 可能实现 __eq__ 而失去 hash；仅在此边界使用固定供应商
            # 名称作为兼容回退，绝不把原始 URL、Header 或正文写入 marker。
            setattr(record, _HTTP_CLIENT_RECORD_SOURCE_FALLBACK_ATTR, canonical_name)


def _http_client_source_for_record(record: logging.LogRecord) -> str | None:
    """读取记录的 HTTP 来源；记录回收后弱引用表自动清理，不保留业务副本。"""
    with _HTTP_CLIENT_RECORD_SOURCES_LOCK:
        try:
            source_name = _HTTP_CLIENT_RECORD_SOURCES.get(record)
        except TypeError:
            source_name = None
        if source_name is not None:
            return source_name
        fallback = getattr(record, _HTTP_CLIENT_RECORD_SOURCE_FALLBACK_ATTR, None)
        return fallback if isinstance(fallback, str) else None


def _resolve_http_client_source(record: logging.LogRecord) -> str | None:
    """解析已登记来源；无来源但 name 声称 HTTP 客户端时按最小命名空间 fail-closed。"""
    source_name = _http_client_source_for_record(record)
    if source_name is not None:
        return source_name
    if not _is_http_client_logger(record.name):
        return None

    # Handler.filter 是五层安装中的第一层，可能先于 provenance-producing wrapper 生效。
    # 对未证明来源但自称 HTTPX/HTTPCore 的反序列化或手工记录按安全边界处理，只登记
    # canonical namespace，绝不信任可能包含 token 的动态 name 后缀。
    source_name = _canonical_http_client_name(record.name)
    _remember_http_client_source(record, source_name)
    return source_name


def _scrub_http_client_record(record: logging.LogRecord) -> logging.LogRecord:
    """在记录进入任意 handler 前固定 HTTP 客户端字段并清空未知扩展值。

    ``Logger.makeRecord`` 会在全局 ``LogRecordFactory`` 返回后再把 ``extra`` 写入
    ``record.__dict__``。因此仅包装 factory 不能覆盖 ``extra`` 中的 Authorization、URL
    或 token；配置 helper 还会包装 ``Logger.makeRecord``，在该注入步骤完成后再次调用本
    函数。该入口根据记录创建时的 ``record.name`` 判断来源；经过 logger filter 后若记录
    被改名，应改用 ``_scrub_http_client_record_from_logger``，以免把可变字段当作可信来源。
    未知字段会被删除；需要固定占位符的宿主 formatter 可通过 ``logging.Formatter.defaults``
    提供安全默认值。
    """
    if not _is_http_client_logger(record.name):
        return record

    source_name = record.name
    scrubbed_record = _scrub_http_client_record_fields(record, source_name=source_name)
    # factory/makeRecord 可能先于新 handle/callHandlers wrapper 返回记录；提前登记来源，
    # 让已经进入旧 dispatch 调用栈的记录仍能在 patched Handler.filter 边界被最终清理。
    _remember_http_client_source(scrubbed_record, source_name)
    return scrubbed_record


def _scrub_http_client_record_fields(
    record: logging.LogRecord,
    *,
    source_name: str | None = None,
) -> logging.LogRecord:
    """执行已确认属于 HTTP 客户端记录的字段收窄，不再次读取可变来源字段。"""

    if source_name is not None:
        # filter 可以把 name 改成包含 token 的任意字符串；只写入受控的供应商命名空间，
        # 既保留来源维度，又保证 formatter/序列化器不会看到可变敏感名称。
        record.name = _canonical_http_client_name(source_name)

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


def _scrub_http_client_record_from_logger(
    logger: logging.Logger,
    record: logging.LogRecord,
) -> logging.LogRecord:
    """以调用方 logger.name 作为可信来源，清理 filter 返回或改名后的记录。

    ``Logger.filter`` 可以原地改写 ``LogRecord.name``，也可以返回全新的记录；这两个字段
    都不再代表最初的 HTTPX/HTTPCore 调用方。``Logger.callHandlers`` 收到的 ``logger``
    是真实调用方对象，因此该入口只在其名称属于 HTTP 客户端命名空间时登记可信来源。
    下游 ``Handler.filter`` 另对无 provenance 但自称 HTTP 客户端的手工/反序列化记录执行
    fail-closed，避免部分安装窗口或不可信记录绕过安全边界。
    """
    if not _is_http_client_logger(logger.name):
        return record
    scrubbed_record = _scrub_http_client_record_fields(record, source_name=logger.name)
    _remember_http_client_source(scrubbed_record, logger.name)
    return scrubbed_record


class _HttpClientLogRecordFactory:
    """在保留宿主记录工厂的前提下，固定第三方 HTTP 记录的可观测内容。

    ``logging`` 在创建 ``LogRecord`` 时调用全局 factory；``Logger.makeRecord`` 随后才注入
    ``extra``。Handler.filter、callHandlers、handle、makeRecord 与 factory 五个边界都完成
    收窄后，同一 HTTP 来源记录经过父/子 logger、重配置后的 handler 以及 ``lastResort`` 时
    都共享安全字段。委托原 factory 可保留宿主已经安装的记录类型，不改变应用
    ``ai_employee`` 日志的既有行为。
    """

    def __init__(self, delegate: Callable[..., logging.LogRecord]) -> None:
        """保存宿主 factory，避免覆盖其他框架的记录初始化逻辑。"""
        self._delegate = delegate

    def __call__(self, *args: object, **kwargs: object) -> logging.LogRecord:
        """创建记录并在 extra 注入前清除 HTTP 客户端原文和异常上下文。"""
        record = self._delegate(*args, **kwargs)
        return _scrub_http_client_record(record)


def _wrap_http_client_handle(delegate: Callable[..., None]) -> Callable[..., None]:
    """包装宿主 ``Logger.handle``，以调用方来源覆盖已在旧 makeRecord 中途的记录。"""

    def wrapped(
        logger: logging.Logger,
        record: logging.LogRecord,
        *args: object,
        **kwargs: object,
    ) -> None:
        """在 filter 链前按可信 logger.name 清理记录，再委托宿主实现。"""
        scrubbed_record = _scrub_http_client_record_from_logger(logger, record)
        delegate(logger, scrubbed_record, *args, **kwargs)

    setattr(wrapped, _HTTP_CLIENT_HANDLE_MARKER_ATTR, _HTTP_CLIENT_HANDLE_MARKER)
    return wrapped


def _wrap_http_client_call_handlers(delegate: Callable[..., None]) -> Callable[..., None]:
    """包装宿主 ``Logger.callHandlers``，在 filter 后再次固定记录内容。"""

    def wrapped(
        logger: logging.Logger,
        record: logging.LogRecord,
        *args: object,
        **kwargs: object,
    ) -> None:
        """在 handler dispatch 前清理 logger filter 可能重新写入的字段。"""
        scrubbed_record = _scrub_http_client_record_from_logger(logger, record)
        delegate(logger, scrubbed_record, *args, **kwargs)

    setattr(
        wrapped,
        _HTTP_CLIENT_CALL_HANDLERS_MARKER_ATTR,
        _HTTP_CLIENT_CALL_HANDLERS_MARKER,
    )
    return wrapped


def _wrap_http_client_handler_filter(
    delegate: Callable[..., object],
) -> Callable[..., object]:
    """包装标准 ``Handler.filter``，在全部 handler filter 后、emit 前收窄记录。"""

    def wrapped(
        handler: logging.Handler,
        record: logging.LogRecord,
        *args: object,
        **kwargs: object,
    ) -> object:
        """保留 filter 返回值与后续 lock/emit 流程，同时清理原地或 replacement 记录。"""
        source_name = _resolve_http_client_source(record)
        result = delegate(handler, record, *args, **kwargs)
        if source_name is None and isinstance(result, logging.LogRecord):
            source_name = _resolve_http_client_source(result)

        # 即使宿主 Python 版本尚未把 replacement 传给 emit，也先清理原记录；支持 3.12
        # 的 replacement 语义时，再对 replacement 独立清理并原样返回其对象身份。
        if source_name is None:
            return result
        _scrub_http_client_record_fields(record, source_name=source_name)
        if isinstance(result, logging.LogRecord):
            _remember_http_client_source(result, source_name)
            _scrub_http_client_record_fields(result, source_name=source_name)
        _remember_http_client_source(record, source_name)
        return result

    setattr(
        wrapped,
        _HTTP_CLIENT_HANDLER_FILTER_MARKER_ATTR,
        _HTTP_CLIENT_HANDLER_FILTER_MARKER,
    )
    return wrapped


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

    该 helper 只包装当前 ``Handler.filter``、``Logger.callHandlers``、``Logger.handle``、
    ``Logger.makeRecord`` 与 ``LogRecordFactory``，不删除宿主 handler、不修改 logger level
    或 ``propagate``。Handler.filter 是标准 ``Handler.handle`` 在 emit 前的最后可控边界，
    对已登记来源以及无 provenance 但 name 声称 HTTPX/HTTPCore 的记录都 fail-closed；
    callHandlers 覆盖 logger filter 重新写入的字段以及已在旧 handle 调用栈中的记录；handle
    覆盖已在旧 makeRecord 调用栈中的记录；makeRecord 覆盖标准库在 factory 返回后注入
    ``extra`` 的时序；factory 则保护直接创建记录的路径。名称仍为 ``ai_employee.*`` 的标准
    应用记录完全沿用原行为。
    """
    # 五个全局入口必须在同一把锁下按 Handler.filter → callHandlers → handle → makeRecord →
    # factory 顺序安装：Handler.filter 在全部 handler filter 后、emit 前清理原地突变或
    # replacement，并对尚无 provenance 的 HTTP name 自足 fail-closed；callHandlers 在最终
    # dispatch 前再次清理 logger filter 的突变，并覆盖已进入旧 handle 的 in-flight 调用；
    # handle 再覆盖已进入旧 makeRecord 的调用；makeRecord 覆盖 factory 之后的 extra 注入
    # 步骤；最后由 factory 保护直接创建记录的路径。记录创建本身无需持有该锁，减少 OAuth
    # 并发请求的额外争用。
    with _HTTP_CLIENT_LOGGING_LOCK:
        current_handler_filter = logging.Handler.filter
        if (
            getattr(current_handler_filter, _HTTP_CLIENT_HANDLER_FILTER_MARKER_ATTR, None)
            is not _HTTP_CLIENT_HANDLER_FILTER_MARKER
        ):
            # 通过类属性替换保留 descriptor 绑定语义；标准 Handler.handle 与遵循约定调用
            # self.filter 的自定义 handle 都会进入该边界。自定义 filter 若完全覆盖基类而不
            # 调用 super().filter，或 handle 直接绕过 filter/emit，logging API 无法插入通用
            # 钩子；该宿主自定义 I/O 不在本边界保证内，调用方需自行保证不直接输出原记录。
            type.__setattr__(
                logging.Handler,
                "filter",
                _wrap_http_client_handler_filter(current_handler_filter),
            )

        current_call_handlers = logging.Logger.callHandlers
        if (
            getattr(current_call_handlers, _HTTP_CLIENT_CALL_HANDLERS_MARKER_ATTR, None)
            is not _HTTP_CLIENT_CALL_HANDLERS_MARKER
        ):
            # 通过类属性替换保留 Python descriptor 绑定语义；wrapper 不遍历 loggerDict，
            # 因而未来新建子 logger、dictConfig、lastResort 与 in-flight 记录共享边界。
            type.__setattr__(
                logging.Logger,
                "callHandlers",
                _wrap_http_client_call_handlers(current_call_handlers),
            )

        current_handle = logging.Logger.handle
        if (
            getattr(current_handle, _HTTP_CLIENT_HANDLE_MARKER_ATTR, None)
            is not _HTTP_CLIENT_HANDLE_MARKER
        ):
            # 通过类属性替换保留 Python descriptor 绑定语义；wrapper 不遍历 loggerDict，
            # 因而未来新建子 logger、dictConfig、lastResort 与 in-flight 记录共享边界。
            type.__setattr__(
                logging.Logger,
                "handle",
                _wrap_http_client_handle(current_handle),
            )

        current_make_record = logging.Logger.makeRecord
        if (
            getattr(current_make_record, _HTTP_CLIENT_MAKE_RECORD_MARKER_ATTR, None)
            is not _HTTP_CLIENT_MAKE_RECORD_MARKER
        ):
            # 通过类属性替换保留 Python descriptor 绑定语义；wrapper 本身不遍历 loggerDict。
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
            "event": (
                record.name
                if _stable_diagnostic_value(record.name) is not None
                and record.name.startswith("ai_employee.")
                else "application.log"
            ),
            **{
                field: _stable_diagnostic_value(getattr(record, field, None))
                for field in _DIAGNOSTIC_FIELDS
            },
        }
        # 撤销维护仅公开供应商级整数计数，不能让任意 extra 字符串绕过日志最小披露边界。
        unresolved_count = getattr(record, "unresolved_count", None)
        if (
            record.name == "ai_employee.oauth.revoke_backlog"
            and type(unresolved_count) is int
            and unresolved_count >= 0
        ):
            payload["unresolved_count"] = unresolved_count
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
