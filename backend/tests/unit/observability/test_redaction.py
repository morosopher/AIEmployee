"""验证可观测性边界绝不保留或输出敏感业务内容。"""

import json
import logging

import pytest

from ai_employee.infrastructure.observability.logging import _JsonFormatter
from ai_employee.infrastructure.observability.redaction import redact_value


def test_redaction_removes_m2_content_fields_at_any_depth() -> None:
    """地址、主题、日程字段和完整命令不能通过嵌套或大小写变体进入日志。"""
    sensitive = {
        "Account_Address": "synthetic address",
        "subject": "synthetic subject",
        "event_title": "synthetic title",
        "description": "synthetic description",
        "Location": "synthetic room",
        "attendees": ["synthetic attendee"],
        "frozen_command": {"safe_looking_key": "synthetic payload"},
        "body_text": "synthetic body",
        "email": "person@example.test",
        "to": ["person@example.test"],
        "cc": [],
        "bcc": [],
    }
    result = redact_value({"provider": "google", "nested": sensitive})
    assert result == {"provider": "google", "nested": {}}
    assert redact_value("subject=synthetic private subject") == "[redacted]"
    assert redact_value("upstream: person@example.test") == "[redacted]"


def test_redact_value_removes_known_secret_and_content_fields() -> None:
    """递归脱敏认证、令牌、邮件正文与配置命中值，保留安全诊断字段。"""
    payload = {
        "event": "provider.failed",
        "Authorization": "Bearer synthetic-secret",
        "cookie": "session=synthetic-cookie",
        "oauth_access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "model_api_key": "synthetic-model-key",
        "body_ciphertext": "synthetic-email-body",
        "safe_code": "timeout",
        "nested": {"custom_secret": "matches-pattern"},
    }

    result = redact_value(payload, secret_patterns=(r"custom_secret",))

    assert result == {
        "event": "provider.failed",
        "safe_code": "timeout",
        "nested": {},
    }


def test_redact_value_never_preserves_sensitive_scalar_or_exception_message() -> None:
    """裸字符串与异常文本带有认证、正文或 Prompt 标签时也必须被替换。

    日志 formatter 可能接收插值后的字符串或 ``exc_info``，因此不能只依赖结构化字段名。
    """
    payload = {
        "safe": "timeout",
        "raw_authorization": "Authorization: Bearer synthetic-secret",
        "nested": ["Cookie=session=synthetic-cookie", "prompt: synthetic prompt"],
        "configured": "private-value-123",
    }

    result = redact_value(payload, secret_patterns=(r"private-value-\d+",))

    assert result == {
        "safe": "timeout",
        "nested": ["[redacted]", "[redacted]"],
        "configured": "[redacted]",
    }


@pytest.mark.parametrize("field", ["trace_id", "task_id", "step_id", "provider", "error_code"])
@pytest.mark.parametrize("nested", [False, True])
def test_json_log_schema_rejects_raw_request_targets_even_in_allowed_fields(
    field: str,
    nested: bool,
) -> None:
    """固定诊断字段也必须是稳定标量，不能夹带 URL/query 对象或原始 request target。"""
    canary = "synthetic-log-query-canary"
    request_fields = {
        "url": f"https://app.example.test/callback?code={canary}",
        "raw_url": f"/callback?state={canary}",
        "query": f"code={canary}",
        "query_string": f"state={canary}",
        "request_target": f"/callback?code={canary}",
    }
    record = logging.getLogger("ai_employee.oauth.callback").makeRecord(
        "ai_employee.oauth.callback",
        logging.INFO,
        __file__,
        1,
        "unused",
        (),
        None,
        extra={**request_fields, field: request_fields if nested else request_fields["url"]},
    )
    serialized = _JsonFormatter(()).format(record)
    payload = json.loads(serialized)
    safe = canary not in serialized and not (request_fields.keys() & payload.keys())
    assert safe, "structured logs must exclude raw request targets"
    assert payload[field] is None


def test_json_log_schema_keeps_stable_oauth_diagnostics() -> None:
    """移除请求原文后，固定供应商、错误码与 trace 关联仍然可供诊断。"""
    record = logging.getLogger("ai_employee.oauth.authorization_failed").makeRecord(
        "ai_employee.oauth.authorization_failed",
        logging.INFO,
        __file__,
        1,
        "unused",
        (),
        None,
        extra={
            "provider": "microsoft",
            "error_code": "oauth_authorization_failed",
            "trace_id": "synthetic-trace-1",
        },
    )
    payload = json.loads(_JsonFormatter(()).format(record))
    assert payload["provider"] == "microsoft"
    assert payload["error_code"] == "oauth_authorization_failed"
    assert payload["trace_id"] == "synthetic-trace-1"


def test_json_log_event_name_cannot_smuggle_oauth_request_target() -> None:
    """动态 logger 名称也必须遵守稳定字段边界，不能成为遗漏的 query 输出入口。"""
    canary = "synthetic-event-name-canary"
    record = logging.LogRecord(
        f"ai_employee.oauth/callback?code={canary}", logging.INFO, __file__, 1,
        "unused", (), None,
    )
    serialized = _JsonFormatter(()).format(record)
    safe = canary not in serialized
    assert safe, "event names must not contain request targets"
    assert json.loads(serialized)["event"] == "application.log"
