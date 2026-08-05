"""验证测试故障注入入口只在显式测试模式中存在。"""

import pytest


def test_test_support_router_requires_both_test_switches() -> None:
    """生产及普通开发应用绝不能暴露可改变 fake adapter 场景的接口。"""
    from ai_employee.api.routers.test_support import should_register_test_support
    from ai_employee.config import Settings

    assert not should_register_test_support(Settings(app_env="development", app_test_mode=False))
    assert not should_register_test_support(Settings(app_env="production", app_test_mode=False))
    assert should_register_test_support(Settings(app_env="test", app_test_mode=True))


def test_test_mode_uses_http_cookie_only_for_local_e2e_harness() -> None:
    """测试双开关需要在本机 HTTP harness 中携带 Cookie，生产策略仍必须 Secure。"""
    from ai_employee.api.routers.auth import _secure_cookies
    from ai_employee.config import Settings

    assert not _secure_cookies(Settings(app_env="test", app_test_mode=True))
    assert _secure_cookies(Settings(app_env="production", app_test_mode=False))


@pytest.mark.asyncio
async def test_scenario_store_uses_user_scoped_key_and_six_hundred_second_ttl() -> None:
    """测试场景必须按用户隔离、原子消费，并在十分钟后自动失效。"""
    from ai_employee.api.routers.test_support import TestScenarioStore

    class Redis:
        """记录 Redis 命令，不连接任何真实服务。"""

        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        async def set(self, *args: object, **kwargs: object) -> None:
            """记录带 EX 的设置请求。"""
            self.calls.append(("set", *args, kwargs))

        async def getdel(self, key: str) -> bytes | None:
            """记录一次性读取并模拟已消费。"""
            self.calls.append(("getdel", key))
            return b"oauth_revoked"

    redis = Redis()
    store = TestScenarioStore(redis)  # type: ignore[arg-type]
    await store.set(user_id="user-a", scenario="oauth_revoked")
    assert redis.calls[0][0:3] == ("set", "ai_employee:test-scenario:user-a", "oauth_revoked")
    assert redis.calls[0][3] == {"ex": 600}
    assert await store.consume(user_id="user-a") == "oauth_revoked"
    assert redis.calls[1] == ("getdel", "ai_employee:test-scenario:user-a")


def test_router_registers_a_csrf_protected_endpoint() -> None:
    """测试场景及同步执行均属于写操作，必须使用认证和 CSRF 依赖。"""
    from ai_employee.api.routers.test_support import build_test_support_router

    router = build_test_support_router()
    route = next(route for route in router.routes if getattr(route, "path", "") == "/api/v1/test-support/scenario")
    assert "POST" in route.methods
    assert route.dependant.dependencies
    execute_route = next(
        route
        for route in router.routes
        if getattr(route, "path", "") == "/api/v1/test-support/execute-task"
    )
    assert "POST" in execute_route.methods
    assert execute_route.dependant.dependencies


def test_seed_google_source_returns_the_new_connection_id() -> None:
    """E2E 必须绑定本次创建的连接，不能从历史列表任取一项产生假阳性。"""
    from ai_employee.api.routers.test_support import (
        SeedGoogleSourceResponse,
        build_test_support_router,
    )

    router = build_test_support_router()
    seed_route = next(
        route
        for route in router.routes
        if getattr(route, "path", "") == "/api/v1/test-support/seed-google-source"
    )
    assert seed_route.status_code == 200
    assert seed_route.response_model is SeedGoogleSourceResponse


def test_main_registers_router_only_for_explicit_test_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组合根必须使生产与开发 ASGI 表中完全没有 test-support 路径。"""
    from ai_employee import main
    from ai_employee.config import Settings

    monkeypatch.setattr(main, "get_settings", lambda: Settings(app_env="development"))
    development = main.create_app()
    assert not any("test-support" in getattr(route, "path", "") for route in development.routes)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(app_env="test", app_test_mode=True),
    )
    testing = main.create_app()
    included = testing.routes[-1]
    assert any("test-support" in getattr(route, "path", "") for route in included.original_router.routes)
