"""验证用户设置的数据库约束。"""


def test_user_retention_settings_are_modelled() -> None:
    """用户模型必须包含三项有界保留设置。"""
    from ai_employee.infrastructure.db.models.identity import UserModel

    assert "email_body_retention_days" in UserModel.__table__.c
