"""验证云模型请求前的本地敏感信息脱敏。"""

from ai_employee.domain.model_redaction import redact_for_model


def test_builtin_secrets_are_masked_with_auditable_reason_codes() -> None:
    """Bearer、API 密钥与六位验证码必须在离开本地前替换且不回显原值。"""
    text = "Bearer synthetic-token-value api_key=sk-syntheticValue123 code 123456"

    result = redact_for_model(text)

    assert "synthetic-token-value" not in result.text
    assert "sk-syntheticValue123" not in result.text
    assert "123456" not in result.text
    assert result.reason_codes == ("bearer_token", "api_key", "one_time_code")


def test_configured_patterns_are_masked_without_changing_normal_content() -> None:
    """用户配置正则应追加本地掩码，普通姓名与会议时间保持原样。"""
    text = "Alice meets Bob at 10:30; internal id EMP-1234."

    result = redact_for_model(text, configured_patterns=(r"EMP-\d+",))

    assert "EMP-1234" not in result.text
    assert "Alice" in result.text
    assert "10:30" in result.text
    assert result.reason_codes == ("configured_pattern_1",)
