"""验证设置 REST 资源注册。"""


def test_settings_router_is_available() -> None:
    """设置路由工厂必须可供应用装配。"""
    from ai_employee.api.routers.settings import build_settings_router

    assert build_settings_router is not None
