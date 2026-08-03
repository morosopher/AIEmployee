"""任务 SSE 接口的集成验收测试。"""

import pytest


@pytest.mark.asyncio
async def test_task_event_route_is_registered() -> None:
    """任务事件流必须使用规定的可重放 SSE 地址。"""
    from ai_employee.main import create_app

    paths = {
        route.path
        for included in create_app().routes
        for route in getattr(getattr(included, "original_router", included), "routes", (included,))
        if hasattr(route, "path")
    }

    assert "/api/v1/tasks/{task_id}/events" in paths
