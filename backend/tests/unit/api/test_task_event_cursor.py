"""验证任务 SSE 路由的游标选择与输入拒绝规则。"""

import pytest

from ai_employee.api.deps import ApiProblem
from ai_employee.api.routers.tasks import _event_cursor


def test_event_cursor_prefers_standard_header_over_query_cursor() -> None:
    """自动重连 Header 存在时不得被主动订阅使用的查询游标回退。"""
    assert _event_cursor(header_value="24", query_value="3") == 24


@pytest.mark.parametrize("header_value", ["not-a-cursor", "-1"])
def test_event_cursor_rejects_invalid_standard_header(header_value: str) -> None:
    """非法 Last-Event-ID Header 必须保留稳定的 422 问题错误码。"""
    with pytest.raises(ApiProblem) as captured:
        _event_cursor(header_value=header_value, query_value=None)

    assert captured.value.status_code == 422
    assert captured.value.error_code == "invalid_last_event_id"
