"""Google 渐进授权适配器的脱敏供应商契约测试。"""

import logging
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from ai_employee.application.ports.oauth import OAuthAuthorizationRequest, OAuthTokenSet
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.infrastructure.observability.logging import configure_http_client_logging
from ai_employee.integrations.google.oauth import (
    GOOGLE_BASE_SCOPES,
    GOOGLE_REVOKE_URL,
    GOOGLE_TOKEN_INFO_URL,
    GoogleOAuthAdapter,
)


def _adapter() -> GoogleOAuthAdapter:
    """构造只使用合成配置的 Google adapter，测试绝不连接真实账户。"""
    return GoogleOAuthAdapter(
        client_id="synthetic-client",
        client_secret="synthetic-secret",
        redirect_uri="https://app.example.test/callback",
    )


def test_google_scope_union_is_minimal_and_deterministic() -> None:
    """能力到 scope 的映射必须最小且不能跨数据源隐式扩大。"""
    adapter = _adapter()

    assert adapter.scopes_for(frozenset({ConnectionCapability.MAIL_READ})) == frozenset(
        {
            *GOOGLE_BASE_SCOPES,
            "https://www.googleapis.com/auth/gmail.readonly",
        }
    )
    mail_scopes = adapter.scopes_for(
        frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND})
    )
    assert "https://www.googleapis.com/auth/gmail.send" in mail_scopes
    assert "https://www.googleapis.com/auth/calendar.events" not in mail_scopes
    assert type(mail_scopes) is frozenset


def test_google_authorization_url_uses_requested_scopes_and_oidc_nonce() -> None:
    """授权 URL 必须解析出精确请求 scope、离线参数、PKCE 与 nonce。"""
    request = OAuthAuthorizationRequest(
        state="synthetic-state",
        code_challenge="synthetic-challenge",
        requested_scopes=frozenset(
            {
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            }
        ),
        oidc_nonce="synthetic-nonce",
    )

    query = parse_qs(urlparse(_adapter().build_authorization_url(request)).query)
    assert set(query["scope"][0].split()) == request.requested_scopes
    assert query["include_granted_scopes"] == ["true"]
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [request.state]
    assert query["nonce"] == [request.oidc_nonce]
    assert "contacts" not in query["scope"][0].lower()
    assert "draft" not in query["scope"][0].lower()


@pytest.mark.parametrize(
    "invalid_scope",
    (
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/drive.readonly",
    ),
)
def test_google_authorization_request_rejects_response_alias_and_unknown_scope(
    invalid_scope: str,
) -> None:
    """响应兼容别名与 M2 外 scope 都不得进入授权请求扩大权限。"""
    request = OAuthAuthorizationRequest(
        state="synthetic-state",
        code_challenge="synthetic-challenge",
        requested_scopes=frozenset({"openid", "email", invalid_scope}),
        oidc_nonce="synthetic-nonce",
    )

    with pytest.raises(ValueError, match="requested scopes are not allowed"):
        _adapter().build_authorization_url(request)


@pytest.mark.asyncio
@respx.mock
async def test_token_scope_is_normalized_and_id_token_is_retained() -> None:
    """token endpoint 的 scope 字符串应成为 frozenset，id_token 不能被丢弃。"""
    route = respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_in": 3600,
                "scope": "email openid https://www.googleapis.com/auth/gmail.readonly",
                "id_token": "synthetic-id-token",
            },
        )
    )

    token = await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert route.called
    assert token.granted_scopes == frozenset(
        {
            "email",
            "openid",
            "https://www.googleapis.com/auth/gmail.readonly",
        }
    )
    assert token.id_token == "synthetic-id-token"


@pytest.mark.asyncio
@respx.mock
async def test_google_response_userinfo_email_alias_is_canonicalized() -> None:
    """Google 响应可能返回 userinfo.email 别名，但本地事实必须归一为 email。"""
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "expires_in": 3600,
                "scope": (
                    "openid email https://www.googleapis.com/auth/userinfo.email "
                    "https://www.googleapis.com/auth/gmail.readonly"
                ),
            },
        )
    )

    token = await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert token.granted_scopes == frozenset(
        {
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.readonly",
        }
    )


@pytest.mark.asyncio
@respx.mock
async def test_missing_token_scope_uses_token_info_boundary() -> None:
    """scope 缺失时必须通过 token-info 核验，而不是假设请求被授予。"""
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "synthetic-access", "expires_in": 3600},
        )
    )
    info = respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json={"scope": "openid email https://www.googleapis.com/auth/calendar.readonly"},
        )
    )

    token = await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert info.called
    assert token.granted_scopes == frozenset(
        {
            "openid",
            "email",
            "https://www.googleapis.com/auth/calendar.readonly",
        }
    )


@pytest.mark.asyncio
@respx.mock
async def test_google_oauth_request_logs_never_contain_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """直接导入适配器时，HTTPX/HTTPCore 请求记录也不得输出任何 OAuth 凭据。"""
    access_token = "synthetic-log-access-token"
    id_token = "synthetic-log-id-token"
    authorization_value = "Bearer synthetic-log-authorization"
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": access_token,
                "expires_in": 3600,
                "id_token": id_token,
            },
        )
    )
    respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"scope": "openid email https://www.googleapis.com/auth/calendar.readonly"},
            ),
            httpx.Response(200, json={"nonce": "synthetic-log-nonce"}),
        ]
    )
    respx.get("https://openidconnect.googleapis.com/v1/userinfo").mock(
        return_value=httpx.Response(
            200,
            json={"sub": "synthetic-log-subject", "email": "owner@example.test"},
        )
    )

    caplog.set_level(logging.INFO)
    caplog.clear()
    adapter = _adapter()
    token = await adapter.exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    await adapter.fetch_account(
        token,
        expected_nonce_hash=sha256(b"synthetic-log-nonce").digest(),
    )
    # 模拟第三方 logger 未来在异常或调试模式下记录 Authorization/header；安全默认必须
    # 统一阻断，而不能只针对当前 HTTPX INFO 模板做字符串替换。
    logging.getLogger("httpx").warning(
        "HTTP Request: GET %s Authorization: %s id_token=%s",
        f"{GOOGLE_TOKEN_INFO_URL}?id_token={id_token}",
        authorization_value,
        id_token,
    )
    logging.getLogger("httpcore.http11").warning(
        "send_request_headers Authorization: %s", authorization_value
    )
    logging.getLogger("ai_employee.test").info("safe-structured-event")
    rendered = "\n".join(record.getMessage() for record in caplog.records)

    assert access_token not in rendered
    assert id_token not in rendered
    assert authorization_value not in rendered
    assert "access_token=" not in rendered
    assert "id_token=" not in rendered
    assert any(record.name == "ai_employee.test" for record in caplog.records)


@pytest.mark.parametrize(
    ("namespace", "child_suffix"),
    (("httpx", "_client"), ("httpcore", "http11")),
)
def test_http_client_logging_scrubs_records_for_preexisting_parent_and_child_handlers(
    namespace: str,
    child_suffix: str,
) -> None:
    """预先注册的第三方 handler 仍可接收记录，但只能看到固定安全事件。"""

    class RecordingHandler(logging.Handler):
        """记录 handler 实际接收的文本，验证每个第三方 sink 都得到安全事件。"""

        def __init__(self) -> None:
            """初始化空的合成记录列表。"""
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            """保存格式化前消息，避免测试依赖 stdout/stderr。"""
            self.messages.append(record.getMessage())

    parent_logger = logging.getLogger(namespace)
    child_name = f"{namespace}.{child_suffix}"
    manager = logging.Logger.manager
    child_entry = manager.loggerDict.get(child_name)
    child_logger = logging.getLogger(child_name)
    logger_states = {
        logger: (
            logger.level,
            logger.propagate,
            logger.disabled,
            logger.handlers[:],
            logger.filters[:],
        )
        for logger in (parent_logger, child_logger)
    }
    original_factory = logging.getLogRecordFactory()
    original_make_record = logging.Logger.makeRecord
    parent_sink = RecordingHandler()
    child_sink = RecordingHandler()
    parent_logger.addHandler(parent_sink)
    child_logger.addHandler(child_sink)
    try:
        configure_http_client_logging()
        child_logger.setLevel(logging.INFO)
        child_logger.warning(
            "HTTP Request: GET https://oauth2.googleapis.com/tokeninfo?access_token=synthetic-handler-token"
        )
        assert parent_sink.messages == ["http_client_event"]
        assert child_sink.messages == ["http_client_event"]
    finally:
        logging.setLogRecordFactory(original_factory)
        logging.Logger.makeRecord = original_make_record
        for logger, state in logger_states.items():
            for handler in tuple(logger.handlers):
                logger.removeHandler(handler)
            for handler in state[3]:
                logger.addHandler(handler)
            logger.setLevel(state[0])
            logger.propagate = state[1]
            logger.disabled = state[2]
            logger.filters[:] = state[4]
        if child_entry is None:
            manager.loggerDict.pop(child_name, None)
        else:
            manager.loggerDict[child_name] = child_entry


def test_http_client_logging_installation_window_scrubs_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """安装窗口内创建的 HTTP 记录也必须阻断 extra，避免两步替换之间发生泄漏。"""

    class RecordingHandler(logging.Handler):
        """捕获消息和完整记录快照，验证 handler 看不到 race canary。"""

        def __init__(self) -> None:
            """初始化线程安全事件与快照容器。"""
            super().__init__()
            self.emitted = Event()
            self.messages: list[str] = []
            self.snapshots: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            """保存 handler 收到的记录并唤醒测试线程。"""
            self.messages.append(record.getMessage())
            self.snapshots.append(repr(record.__dict__))
            self.emitted.set()

    def baseline_make_record(
        _logger: logging.Logger,
        *args: object,
        **kwargs: object,
    ) -> logging.LogRecord:
        """复现未安装 wrapper 的标准 makeRecord，保留 factory 后置 extra 时序。"""
        extra = kwargs.get("extra")
        factory_args = args
        if len(args) >= 9:
            extra = args[8]
            factory_args = (*args[:8], *args[9:])
        elif "extra" in kwargs:
            kwargs = {key: value for key, value in kwargs.items() if key != "extra"}
        record = logging.getLogRecordFactory()(*factory_args, **kwargs)
        if extra is not None:
            extra_values = extra  # type: ignore[assignment]
            for key in extra_values:  # type: ignore[union-attr]
                if (key in {"message", "asctime"}) or (key in record.__dict__):
                    raise KeyError(f"Attempt to overwrite {key!r} in LogRecord")
                record.__dict__[key] = extra_values[key]  # type: ignore[index]
        return record

    logger_name = "httpx.installation_window_race"
    app_name = "ai_employee.installation_window_race_app"
    manager = logging.Logger.manager
    logger = logging.getLogger(logger_name)
    app_logger = logging.getLogger(app_name)
    logger_state = (
        logger.level,
        logger.propagate,
        logger.disabled,
        logger.handlers[:],
        logger.filters[:],
    )
    app_state = (
        app_logger.level,
        app_logger.propagate,
        app_logger.disabled,
        app_logger.handlers[:],
        app_logger.filters[:],
    )
    logger_entry = manager.loggerDict.get(logger_name)
    app_entry = manager.loggerDict.get(app_name)
    original_factory = logging.getLogRecordFactory()
    original_make_record = logging.Logger.makeRecord
    real_set_factory = logging.setLogRecordFactory
    factory_installed = Event()
    release_installation = Event()
    installation_timeout = Event()
    handler = RecordingHandler()
    app_handler = RecordingHandler()
    installer: Thread | None = None
    emitter: Thread | None = None

    def host_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        """模拟宿主 factory，确认包装后仍保留应用自定义字段。"""
        record = logging.LogRecord(*args, **kwargs)  # type: ignore[arg-type]
        record.host_factory_marker = "synthetic-host-factory-marker"
        return record

    def gated_set_factory(factory: object) -> None:
        """在 factory 已替换、makeRecord 尚未替换时暂停安装线程。"""
        real_set_factory(factory)  # type: ignore[arg-type]
        factory_installed.set()
        if not release_installation.wait(timeout=5):
            installation_timeout.set()

    try:
        # 先恢复到可控的“未安装 makeRecord wrapper”状态，再只门控 helper 的 setter。
        real_set_factory(host_factory)
        type.__setattr__(logging.Logger, "makeRecord", baseline_make_record)
        monkeypatch.setattr(logging, "setLogRecordFactory", gated_set_factory)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)
        app_logger.propagate = False
        app_logger.addHandler(app_handler)

        installer = Thread(target=configure_http_client_logging)
        installer.start()
        assert factory_installed.wait(timeout=5)

        def emit_during_installation() -> None:
            """在 setter barrier 内发送携带敏感 extra 的 HTTP 记录。"""
            logger.warning(
                "HTTP Request: GET https://oauth2.googleapis.com/tokeninfo?access_token=synthetic-race-token",
                extra={
                    "authorization": "Bearer synthetic-race-extra-secret",
                    "opaque": "synthetic-race-extra-token",
                },
            )

        emitter = Thread(target=emit_during_installation)
        emitter.start()
        assert handler.emitted.wait(timeout=5)
        emitter.join(timeout=5)
        release_installation.set()
        installer.join(timeout=5)

        assert not installation_timeout.is_set()
        assert handler.messages == ["http_client_event"]
        assert all(
            secret not in handler.snapshots[0]
            for secret in (
                "synthetic-race-token",
                "synthetic-race-extra-secret",
                "synthetic-race-extra-token",
            )
        )
        app_logger.info("safe-race-application-event")
        assert app_handler.messages == ["safe-race-application-event"]
        assert "synthetic-host-factory-marker" in app_handler.snapshots[0]
    finally:
        release_installation.set()
        if emitter is not None:
            emitter.join(timeout=5)
        if installer is not None:
            installer.join(timeout=5)
        logging.setLogRecordFactory(original_factory)
        type.__setattr__(logging.Logger, "makeRecord", original_make_record)
        for target, state in ((logger, logger_state), (app_logger, app_state)):
            for existing_handler in tuple(target.handlers):
                target.removeHandler(existing_handler)
            for existing_handler in state[3]:
                target.addHandler(existing_handler)
            target.setLevel(state[0])
            target.propagate = state[1]
            target.disabled = state[2]
            target.filters[:] = state[4]
        if logger_entry is None:
            manager.loggerDict.pop(logger_name, None)
        else:
            manager.loggerDict[logger_name] = logger_entry
        if app_entry is None:
            manager.loggerDict.pop(app_name, None)
        else:
            manager.loggerDict[app_name] = app_entry


def test_http_client_log_record_scrub_survives_future_children_and_concurrent_initialization() -> None:
    """记录级边界覆盖新子 logger、extra、异常和并发重复初始化。"""

    class RecordingHandler(logging.Handler):
        """保留 sink 接收的消息，确保断言覆盖每个 handler。"""

        def __init__(self) -> None:
            """初始化空消息列表。"""
            super().__init__()
            self.messages: list[str] = []
            self.snapshots: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            """保存消息而不调用未知对象的格式化逻辑。"""
            self.messages.append(record.getMessage())
            self.snapshots.append(repr(record.__dict__))

    namespace = "httpx"
    child_name = "httpx.future_sensitive_child"
    app_name = "ai_employee.logging_boundary_test"
    manager = logging.Logger.manager
    original_factory = logging.getLogRecordFactory()
    original_make_record = logging.Logger.makeRecord
    original_entries = {
        name: entry
        for name, entry in tuple(manager.loggerDict.items())
        if name == namespace or name.startswith(f"{namespace}.")
    }
    original_states: dict[
        str,
        tuple[int, bool, bool, list[logging.Handler], list[logging.Filter]],
    ] = {}
    for name, entry in original_entries.items():
        if isinstance(entry, logging.Logger):
            original_states[name] = (
                entry.level,
                entry.propagate,
                entry.disabled,
                entry.handlers[:],
                entry.filters[:],
            )
    parent_logger = logging.getLogger(namespace)
    app_entry = manager.loggerDict.get(app_name)
    app_logger = logging.getLogger(app_name)
    app_state = (
        app_logger.level,
        app_logger.propagate,
        app_logger.disabled,
        app_logger.handlers[:],
        app_logger.filters[:],
    )
    parent_sink = RecordingHandler()
    child_sink = RecordingHandler()
    reattached_sink = RecordingHandler()
    app_sink = RecordingHandler()
    parent_logger.addHandler(parent_sink)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    app_logger.addHandler(app_sink)
    try:
        configure_http_client_logging()
        # 该 child 在 helper 完成后才创建，模拟供应商未来新增 logger 并自行挂载 handler。
        child_logger = logging.getLogger(child_name)
        child_logger.setLevel(logging.INFO)
        child_logger.propagate = True
        child_logger.addHandler(child_sink)
        try:
            raise RuntimeError("Authorization: Bearer synthetic-record-exception")
        except RuntimeError:
            child_logger.warning(
                "HTTP Request: GET %s Authorization: %s",
                "https://oauth2.googleapis.com/tokeninfo?access_token=synthetic-record-token",
                "Bearer synthetic-record-header",
                extra={
                    "authorization": "Bearer synthetic-extra-secret",
                    "opaque": "synthetic-extra-token",
                },
                exc_info=True,
                stack_info=True,
            )

        # helper 完成后再挂第二个 handler，模拟第三方运行时自行扩展 sink；所有 handler
        # 共享同一条已清理的记录，且不会把 extra 中的 token/header 原样序列化。
        child_logger.addHandler(reattached_sink)
        try:
            raise RuntimeError("Cookie: synthetic-reconfigured-cookie")
        except RuntimeError:
            child_logger.warning(
                "HTTP Request: GET %s Authorization: %s",
                "https://oauth2.googleapis.com/tokeninfo?id_token=synthetic-record-id",
                "Bearer synthetic-record-header",
                extra={
                    "authorization": "Bearer synthetic-reconfigured-extra-secret",
                    "opaque": "synthetic-reconfigured-extra-token",
                },
                exc_info=True,
            )

        def configure_repeatedly(_index: int) -> None:
            """并发重复配置只验证不抛错，不改变业务状态。"""
            for _ in range(20):
                configure_http_client_logging()

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(configure_repeatedly, range(4)))

        all_http_messages = (
            parent_sink.messages
            + child_sink.messages
            + reattached_sink.messages
        )
        assert all_http_messages
        assert set(all_http_messages) == {"http_client_event"}
        all_http_snapshots = parent_sink.snapshots + child_sink.snapshots + reattached_sink.snapshots
        assert all(
            secret not in snapshot
            for snapshot in all_http_snapshots
            for secret in (
                "synthetic-record-token",
                "synthetic-record-header",
                "synthetic-extra-secret",
                "synthetic-extra-token",
                "synthetic-reconfigured-extra-secret",
                "synthetic-reconfigured-extra-token",
                "synthetic-record-exception",
                "synthetic-reconfigured-cookie",
            )
        )
        app_logger.info(
            "safe-application-event",
            extra={"application_marker": "synthetic-app-extra"},
        )
        assert app_sink.messages == ["safe-application-event"]
        assert any("synthetic-app-extra" in snapshot for snapshot in app_sink.snapshots)
    finally:
        logging.setLogRecordFactory(original_factory)
        logging.Logger.makeRecord = original_make_record
        for logger in (app_logger,):
            for handler in tuple(logger.handlers):
                logger.removeHandler(handler)
            for handler in app_state[3]:
                logger.addHandler(handler)
            logger.setLevel(app_state[0])
            logger.propagate = app_state[1]
            logger.disabled = app_state[2]
            logger.filters[:] = app_state[4]
        for name, _entry in tuple(manager.loggerDict.items()):
            if (
                name == namespace or name.startswith(f"{namespace}.")
            ) and name not in original_entries:
                manager.loggerDict.pop(name, None)
        for name, entry in original_entries.items():
            manager.loggerDict[name] = entry
            state = original_states.get(name)
            if state is not None and isinstance(entry, logging.Logger):
                for handler in tuple(entry.handlers):
                    entry.removeHandler(handler)
                for handler in state[3]:
                    entry.addHandler(handler)
                entry.setLevel(state[0])
                entry.propagate = state[1]
                entry.disabled = state[2]
                entry.filters[:] = state[4]
        if app_entry is None:
            manager.loggerDict.pop(app_name, None)
        else:
            manager.loggerDict[app_name] = app_entry


def test_http_client_log_record_scrub_survives_dict_config_and_last_resort_in_subprocess() -> None:
    """在隔离进程验证 dictConfig/lastResort，避免污染 pytest 进程的 logging 全局状态。"""
    script = textwrap.dedent(
        """
        import io
        import logging
        import logging.config

        from ai_employee.infrastructure.observability.logging import configure_http_client_logging

        configure_http_client_logging()
        child_name = "httpx.subprocess_sensitive_child"
        configured_stream = io.StringIO()
        logging.config.dictConfig(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {
                    "plain": {
                        "format": "%(message)s %(authorization)s %(opaque)s",
                        "defaults": {"authorization": None, "opaque": None},
                    }
                },
                "handlers": {
                    "configured_sink": {
                        "class": "logging.StreamHandler",
                        "stream": configured_stream,
                        "formatter": "plain",
                    }
                },
                "loggers": {
                    child_name: {
                        "handlers": ["configured_sink"],
                        "level": "INFO",
                        "propagate": False,
                    }
                },
                "root": {"handlers": [], "level": "WARNING"},
            }
        )
        child_logger = logging.getLogger(child_name)
        try:
            raise RuntimeError("Authorization: synthetic-subprocess-exception")
        except RuntimeError:
            child_logger.warning(
                "HTTP Request: GET https://oauth2.googleapis.com/tokeninfo?access_token=synthetic-subprocess-token",
                extra={
                    "authorization": "Bearer synthetic-subprocess-extra-secret",
                    "opaque": "synthetic-subprocess-extra-token",
                },
                exc_info=True,
                stack_info=True,
            )

        for handler in tuple(child_logger.handlers):
            child_logger.removeHandler(handler)
        last_resort_stream = io.StringIO()
        logging.lastResort = logging.StreamHandler(last_resort_stream)
        logging.lastResort.setLevel(logging.INFO)
        logging.lastResort.setFormatter(
            logging.Formatter(
                "%(message)s %(authorization)s %(opaque)s",
                defaults={"authorization": None, "opaque": None},
            )
        )
        try:
            raise RuntimeError("Cookie: synthetic-last-resort-exception")
        except RuntimeError:
            child_logger.warning(
                "HTTP Request: GET https://oauth2.googleapis.com/tokeninfo?id_token=synthetic-last-resort-token",
                extra={
                    "authorization": "Bearer synthetic-last-resort-extra-secret",
                    "opaque": "synthetic-last-resort-extra-token",
                },
                exc_info=True,
                stack_info=True,
            )

        print("configured=" + configured_stream.getvalue().replace("\\n", "|"))
        print("last_resort=" + last_resort_stream.getvalue().replace("\\n", "|"))
        """
    )
    source_root = Path(__file__).resolve().parents[3] / "src"
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(source_root), existing_pythonpath) if path
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    rendered = completed.stdout + completed.stderr
    assert "http_client_event" in completed.stdout
    assert all(
        secret not in rendered
        for secret in (
            "synthetic-subprocess-token",
            "synthetic-subprocess-extra-secret",
            "synthetic-subprocess-extra-token",
            "synthetic-subprocess-exception",
            "synthetic-last-resort-token",
            "synthetic-last-resort-extra-secret",
            "synthetic-last-resort-extra-token",
            "synthetic-last-resort-exception",
        )
    )


@pytest.mark.asyncio
@respx.mock
async def test_refresh_without_rotated_refresh_token_returns_none() -> None:
    """Google 刷新响应未轮换 refresh token 时，适配器必须保留 ``None`` 语义。"""
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed-access",
                "expires_in": 3600,
                "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
            },
        )
    )

    token = await _adapter().refresh("existing-refresh")

    assert token.access_token == "refreshed-access"
    assert token.refresh_token is None


@pytest.mark.asyncio
@respx.mock
async def test_revoke_reports_revoked_only_after_google_success() -> None:
    """Google 撤销端点明确返回 2xx 后才可报告 ``REVOKED``。"""
    route = respx.post(GOOGLE_REVOKE_URL).mock(return_value=httpx.Response(204))

    result = await _adapter().revoke("synthetic-refresh")

    assert route.called
    assert result.status.value == "revoked"


@pytest.mark.asyncio
@respx.mock
async def test_token_info_http_failure_does_not_retain_access_token() -> None:
    """token-info 非成功响应的异常、请求和上下文都不得携带 access token。"""
    sensitive_access_token = "synthetic-sensitive-access-token"
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": sensitive_access_token, "expires_in": 3600},
        )
    )
    respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(400, json={"error": "synthetic-invalid-token"})
    )

    with pytest.raises(UserActionRequiredError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert sensitive_access_token not in str(raised.value)
    assert raised.value.error_code == "google_reauthorization_required"
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_token_info_transport_failure_does_not_retain_access_token() -> None:
    """token-info 传输异常保持原分类，但异常 request 不得保留 access token。"""
    sensitive_access_token = "synthetic-sensitive-access-token"
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": sensitive_access_token, "expires_in": 3600},
        )
    )

    def fail_token_info(request: httpx.Request) -> httpx.Response:
        """构造携带原始请求的连接异常，复现 httpx 默认敏感 URL 传播。"""
        raise httpx.ConnectError("synthetic token-info transport failure", request=request)

    respx.get(GOOGLE_TOKEN_INFO_URL).mock(side_effect=fail_token_info)

    with pytest.raises(TransientProviderError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert sensitive_access_token not in str(raised.value)
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_google_oauth_http_and_payload_failures_are_classified_without_provider_text() -> None:
    """token 端点的 503 与 malformed JSON 必须变成稳定脱敏领域错误。"""
    sensitive_token = "synthetic-sensitive-access-token"
    token_route = respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(503, text=f"provider body {sensitive_token}")
    )
    with pytest.raises(TransientProviderError) as transient:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert token_route.called
    assert transient.value.error_code == "google_oauth_unavailable"
    assert sensitive_token not in str(transient.value)
    assert transient.value.__context__ is None

    respx.reset()
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "only-access"})
    )
    with pytest.raises(PermanentProviderError) as malformed:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert malformed.value.error_code == "google_oauth_invalid_response"
    assert malformed.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_http_status_error_from_transport_is_converted_without_request_details() -> None:
    """httpx 直接抛出的 HTTPStatusError 也必须在 adapter 边界脱敏分类。"""
    sensitive_token = "synthetic-sensitive-token"

    def raise_status(request: httpx.Request) -> httpx.Response:
        """模拟底层 transport 抛出携带请求/供应商正文的 HTTPStatusError。"""
        response = httpx.Response(
            503,
            request=request,
            text=f"provider response {sensitive_token}",
            headers={"Retry-After": "7"},
        )
        raise httpx.HTTPStatusError(
            f"provider status {sensitive_token} at {request.url}",
            request=request,
            response=response,
        )

    respx.post("https://oauth2.googleapis.com/token").mock(side_effect=raise_status)

    with pytest.raises(TransientProviderError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    error = raised.value
    assert error.error_code == "google_oauth_unavailable"
    assert error.retry_after == 7
    assert sensitive_token not in str(error)
    assert "oauth2.googleapis.com/token" not in str(error)
    assert error.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_malformed_retry_after_does_not_escape_domain_error_boundary() -> None:
    """异常大的 Retry-After 不能让错误构造溢出为裸 ValueError。"""
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            503,
            headers={"Retry-After": "1" + ("0" * 1000)},
            text="synthetic provider body",
        )
    )

    with pytest.raises(TransientProviderError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert raised.value.error_code == "google_oauth_unavailable"
    assert raised.value.retry_after is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_google_oauth_unauthorized_and_nonce_mismatch_require_user_action() -> None:
    """401 与 OIDC nonce 不匹配都必须要求用户重新授权且不回显 token。"""
    sensitive_token = "synthetic-sensitive-access-token"
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(401, text=f"provider body {sensitive_token}")
    )
    with pytest.raises(UserActionRequiredError) as unauthorized:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert unauthorized.value.error_code == "google_reauthorization_required"
    assert sensitive_token not in str(unauthorized.value)

    nonce = "synthetic-nonce"
    respx.reset()
    respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(200, json={"nonce": "different-nonce"})
    )
    respx.get("https://openidconnect.googleapis.com/v1/userinfo").mock(
        return_value=httpx.Response(
            200, json={"sub": "synthetic-subject", "email": "owner@example.test"}
        )
    )
    token = OAuthTokenSet(
        access_token=sensitive_token,
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({"openid", "email"}),
        id_token="synthetic-id-token",
    )
    with pytest.raises(UserActionRequiredError) as nonce_error:
        await _adapter().fetch_account(
            token,
            expected_nonce_hash=sha256(nonce.encode("utf-8")).digest(),
        )
    assert nonce_error.value.error_code == "google_oidc_nonce_mismatch"
    assert sensitive_token not in str(nonce_error.value)
    assert nonce_error.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_nonce_requires_verified_token_info_claim() -> None:
    """adapter 不能通过无签名 JWT 解码伪造 OIDC nonce 绑定。"""
    nonce = "synthetic-nonce"
    respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "nonce": nonce,
                "sub": "synthetic-subject",
                "email": "owner@example.test",
            },
        )
    )
    respx.get("https://openidconnect.googleapis.com/v1/userinfo").mock(
        return_value=httpx.Response(
            200,
            json={"sub": "synthetic-subject", "email": "owner@example.test"},
        )
    )
    token = OAuthTokenSet(
        access_token="synthetic-access",
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({"openid", "email"}),
        id_token="synthetic-id-token",
    )

    account = await _adapter().fetch_account(
        token,
        expected_nonce_hash=sha256(nonce.encode("utf-8")).digest(),
    )

    assert account.provider_account_id == "synthetic-subject"


@pytest.mark.asyncio
@respx.mock
async def test_malformed_expected_nonce_hash_requires_user_action() -> None:
    """本地 nonce 摘要损坏时也必须 fail closed，而不能让 compare_digest 抛裸 TypeError。"""
    respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(200, json={"nonce": "synthetic-nonce"})
    )
    token = OAuthTokenSet(
        access_token="synthetic-access",
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({"openid", "email"}),
        id_token="synthetic-id-token",
    )

    with pytest.raises(UserActionRequiredError) as raised:
        await _adapter().fetch_account(
            token,
            expected_nonce_hash="synthetic-malformed-hash",  # type: ignore[arg-type]
        )

    assert raised.value.error_code == "google_oidc_nonce_mismatch"
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_malformed_scope_is_rejected_without_token_info_fallback() -> None:
    """非字符串、空白、控制字符或超长 scope 必须 fail closed。"""
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "expires_in": 3600,
                "scope": "openid\nemail",
            },
        )
    )
    info = respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(200, json={"scope": "openid"})
    )

    with pytest.raises(PermanentProviderError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert raised.value.error_code == "google_oauth_invalid_response"
    assert raised.value.__context__ is None
    assert not info.called
