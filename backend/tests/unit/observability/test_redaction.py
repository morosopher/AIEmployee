"""验证可观测性边界绝不保留或输出敏感业务内容。"""

from ai_employee.infrastructure.observability.redaction import redact_value


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
