"""验证用户设置应用边界不会把动态非法类型强转成可持久值。"""

import pytest

from ai_employee.application.use_cases.settings import UpdateUserSettings


@pytest.mark.parametrize("field_name", ("timezone", "locale"))
def test_settings_validation_rejects_null_strings_without_coercion(field_name: str) -> None:
    """绕过 API 直接调用时，timezone/locale 的 null 也必须由防御性类型检查拒绝。"""
    with pytest.raises(TypeError, match=rf"{field_name} must be a string"):
        UpdateUserSettings.validate({field_name: None})
