"""用户设置 API 的公开 JSON Schema 契约测试。"""

from collections.abc import Mapping

from ai_employee.main import create_app

_NON_NULLABLE_PATCH_FIELDS = frozenset(
    {
        "timezone",
        "locale",
        "brief_time",
        "email_body_retention_days",
        "source_metadata_retention_days",
        "workspace_history_retention_days",
        "working_hours",
        "meeting_buffer_minutes",
    }
)
_NULLABLE_PATCH_FIELDS = frozenset(
    {
        "default_mail_connection_id",
        "default_calendar_connection_id",
        "default_calendar_id",
    }
)


def _declares_null(schema: Mapping[str, object]) -> bool:
    """识别 OpenAPI 3.0/3.1 常见的 nullable 与 null 联合表达。"""
    if schema.get("nullable") is True:
        return True
    schema_type = schema.get("type")
    if schema_type == "null":
        return True
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    for union_keyword in ("anyOf", "oneOf"):
        branches = schema.get(union_keyword)
        if isinstance(branches, list) and any(
            isinstance(branch, Mapping) and _declares_null(branch) for branch in branches
        ):
            return True
    return False


def test_settings_patch_openapi_distinguishes_omission_from_explicit_null() -> None:
    """PATCH 字段均可省略，但只有三个默认身份字段允许客户端显式传 null。"""
    schema = create_app().openapi()["components"]["schemas"]["SettingsPatch"]
    properties = schema["properties"]
    all_patch_fields = _NON_NULLABLE_PATCH_FIELDS | _NULLABLE_PATCH_FIELDS

    assert set(properties) == all_patch_fields
    assert not schema.get("required")
    assert {
        field_name
        for field_name, field_schema in properties.items()
        if _declares_null(field_schema)
    } == _NULLABLE_PATCH_FIELDS
