"""验证会话 REST 资源注册。"""


def test_conversations_router_is_available() -> None:
    """会话路由工厂必须可供应用装配。"""
    from ai_employee.api.routers.conversations import build_conversations_router

    assert build_conversations_router is not None
