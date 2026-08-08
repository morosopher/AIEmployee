"""验证结构化日志入口对异常记录元数据保持类型安全。"""

import pytest

import ai_employee.infrastructure.observability.logging as observability_logging


@pytest.mark.parametrize("name", (None, 42, object()))
def test_is_http_client_logger_rejects_non_string_name(name: object) -> None:
    """非字符串 logger 名称只能被视为未知来源，不能触发字符串操作或清洗。"""
    assert observability_logging._is_http_client_logger(name) is False
