"""验证 Calendar 应用同步的字段级 AAD 边界。"""

from uuid import UUID

from ai_employee.application.use_cases.sync_calendar import SyncCalendarUseCase


def test_calendar_aad_binds_event_and_field_kind() -> None:
    """地点与描述必须使用不同 AAD，阻止同一事件内的密文替换。"""
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    connection_id = UUID("00000000-0000-0000-0000-000000000002")
    assert SyncCalendarUseCase._aad(user_id, connection_id, "event-1", "description") != SyncCalendarUseCase._aad(user_id, connection_id, "event-1", "location")
