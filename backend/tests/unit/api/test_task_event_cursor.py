"""验证任务 SSE 路由的游标选择与输入拒绝规则。"""

import pytest

from ai_employee.api.deps import ApiProblem
from ai_employee.api.routers.tasks import _event_cursor


def test_event_cursor_prefers_standard_header_over_query_cursor() -> None:
    """自动重连 Header 存在时不得被主动订阅使用的查询游标回退。"""
    assert _event_cursor(header_value="24", query_value="3") == 24


@pytest.mark.parametrize(
    ("header_value", "query_value"),
    [
        ("not-a-cursor", None),
        ("-1", None),
        ("+1", None),
        (" 1", None),
        ("1 ", None),
        ("01", None),
        ("00", None),
        ("", None),
        (None, "+1"),
        (None, " 1"),
        (None, "01"),
        (None, ""),
    ],
)
def test_event_cursor_rejects_noncanonical_header_or_query_value(
    header_value: str | None, query_value: str | None
) -> None:
    """Header 与查询参数的游标都必须保持唯一的规范十进制文本表示。"""
    with pytest.raises(ApiProblem) as captured:
        _event_cursor(header_value=header_value, query_value=query_value)

    assert captured.value.status_code == 422
    assert captured.value.error_code == "invalid_last_event_id"


@pytest.mark.parametrize(
    ("header_value", "query_value"),
    [("9223372036854775808", None), (None, "9223372036854775808")],
)
def test_event_cursor_rejects_value_larger_than_postgresql_bigint(
    header_value: str | None, query_value: str | None
) -> None:
    """Header 与查询游标不得超过持久审计事件 ID 的 BIGINT 上限。"""
    with pytest.raises(ApiProblem) as captured:
        _event_cursor(header_value=header_value, query_value=query_value)

    assert captured.value.status_code == 422
    assert captured.value.error_code == "invalid_last_event_id"
