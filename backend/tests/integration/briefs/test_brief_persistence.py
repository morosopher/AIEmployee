"""验证每日简报持久化边界。"""


def test_daily_brief_persistence_models_are_available() -> None:
    """简报 ORM 模型必须在持久化实现前可导入。"""
    from ai_employee.infrastructure.db.models.briefs import DailyBriefModel

    assert DailyBriefModel.__tablename__ == "daily_briefs"
