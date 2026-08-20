"""验证可信命令公开严格 RFC3339 解析边界。"""

from datetime import UTC, datetime

import pytest

import ai_employee.application.commands as command_module


def test_strict_rfc3339_parser_is_a_public_application_boundary() -> None:
    """API 等调用方必须复用公开 parser，不能复制可信命令的时间语法。"""
    parser = command_module.parse_strict_rfc3339_datetime

    assert "parse_strict_rfc3339_datetime" in command_module.__all__
    assert parser("2030-01-01T00:00:00.123456Z") == datetime(
        2030,
        1,
        1,
        0,
        0,
        0,
        123456,
        tzinfo=UTC,
    )
    with pytest.raises(ValueError, match="strict RFC 3339"):
        parser(datetime.fromisoformat("2030-01-01T00:00:00"))
