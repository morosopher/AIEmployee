"""导出并注册基础设施层的 ORM 模型。"""

from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel

__all__ = ["UserModel", "UserSessionModel"]
