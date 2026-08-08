"""验证连接 scope 持久化列表的跨供应商稳定排序。"""

from ai_employee.infrastructure.db.repositories.connections import _scope_sort_key


def test_user_read_is_sorted_with_identity_scopes() -> None:
    """Microsoft User.Read 应与 OIDC 基础身份一起排在资源 scope 前。"""
    scopes = frozenset(
        {
            "openid",
            "profile",
            "email",
            "User.Read",
            "offline_access",
            "Mail.Read",
            "Calendars.Read",
        }
    )

    assert sorted(scopes, key=_scope_sort_key) == [
        "openid",
        "profile",
        "email",
        "User.Read",
        "offline_access",
        "Mail.Read",
        "Calendars.Read",
    ]
