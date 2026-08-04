"""定义用户设置的可测试应用层校验入口。"""

from ai_employee.domain.settings import (
    validate_brief_time,
    validate_locale,
    validate_retention,
    validate_timezone,
)


class UpdateUserSettings:
    """在 API 适配器之前复用严格设置值校验。"""
    @staticmethod
    def validate(values: dict[str, object]) -> dict[str, object]:
        """验证并规范化 PATCH 提供字段；未知字段必须在 API Schema 拒绝。"""
        validated = dict(values)
        if "timezone" in validated: validated["timezone"] = validate_timezone(str(validated["timezone"]))
        if "locale" in validated: validated["locale"] = validate_locale(str(validated["locale"]))
        if "brief_time" in validated: validated["brief_time"] = validate_brief_time(str(validated["brief_time"]))
        for name in (
            "email_body_retention_days",
            "source_metadata_retention_days",
            "workspace_history_retention_days",
        ):
            if name in validated:
                raw_value = validated[name]
                if not isinstance(raw_value, int):
                    raise ValueError(f"{name} must be an integer")
                validated[name] = validate_retention(raw_value)
        return validated
