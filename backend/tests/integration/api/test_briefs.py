"""验证简报 REST 资源注册。"""


def test_briefs_router_is_available() -> None:
    """简报路由工厂必须可供应用装配。"""
    from ai_employee.api.routers.briefs import build_briefs_router

    assert build_briefs_router is not None
