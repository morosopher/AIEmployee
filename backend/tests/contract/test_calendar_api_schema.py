"""日历提案与恢复 API 的公开 Schema 契约 RED 测试。"""

from datetime import date
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_employee.api.routers.calendar import (
    AcceptedTaskResponse,
    CalendarProposalListResponse,
    CreateEventProposalRequest,
    CreateProposalRequest,
    CreateUpdateProposalRequest,
    RestoreProposalRequest,
    SuggestTimesResponse,
    UpdateProposalRequest,
)
from ai_employee.main import create_app


def test_calendar_api_exposes_all_eight_routes() -> None:
    """OpenAPI 必须公开提案列表、编辑、建议、提交和恢复八条稳定路径。"""
    paths = create_app().openapi()["paths"]
    assert "/api/v1/calendar/proposals" in paths
    assert set(paths["/api/v1/calendar/proposals"]) >= {"get", "post"}
    assert set(paths["/api/v1/calendar/proposals/{proposal_id}"]) >= {
        "get",
        "patch",
        "delete",
    }
    assert "/api/v1/calendar/proposals/{proposal_id}/suggest-times" in paths
    assert "/api/v1/calendar/proposals/{proposal_id}/submit" in paths
    assert "/api/v1/calendar/events/{event_id}/restore-proposal" in paths


@pytest.mark.parametrize(
    "schema",
    (
        CreateEventProposalRequest,
        CreateUpdateProposalRequest,
        UpdateProposalRequest,
        RestoreProposalRequest,
        AcceptedTaskResponse,
        CalendarProposalListResponse,
        SuggestTimesResponse,
    ),
)
def test_calendar_api_models_forbid_unknown_fields(schema: type[object]) -> None:
    """公开 Schema 必须拒绝 provider recurrence/conference 等未批准扩展。"""
    assert schema.model_config.get("extra") == "forbid"  # type: ignore[attr-defined]


def test_create_proposal_rejects_provider_extensions_and_bad_timezone() -> None:
    """请求边界拒绝重复日程、会议链接和非法 IANA 时区。"""
    with pytest.raises(ValidationError):
        TypeAdapter(CreateProposalRequest).validate_python(
            {
                "operation_kind": "create",
                "connection_id": uuid4(),
                "calendar_id": "calendar",
                "title": "Synthetic event",
                "starts_at": "2030-01-01T09:00:00+00:00",
                "ends_at": "2030-01-01T10:00:00+00:00",
                "timezone": "not/a-zone",
                "recurrence": {"rrule": "FREQ=DAILY"},
            }
        )
    with pytest.raises(ValidationError):
        TypeAdapter(CreateProposalRequest).validate_python(
            {
                "operation_kind": "create",
                "connection_id": uuid4(),
                "calendar_id": "calendar",
                "title": "Synthetic event",
                "starts_at": "2030-01-01T09:00:00+00:00",
                "ends_at": "2030-01-01T10:00:00+00:00",
                "timezone": "UTC",
                "conference": {"provider": "meet"},
            }
        )


def test_all_day_request_uses_iso_dates() -> None:
    """全天事件只接受本地日期，不把日期误解释为带时区瞬间。"""
    request = TypeAdapter(CreateProposalRequest).validate_python(
        {
            "operation_kind": "create",
            "connection_id": uuid4(),
            "calendar_id": "calendar",
            "title": "Synthetic holiday",
            "starts_at": date(2030, 1, 1),
            "ends_at": date(2030, 1, 2),
            "timezone": "Asia/Shanghai",
            "all_day": True,
        }
    )
    assert request.starts_at == date(2030, 1, 1)
    assert request.ends_at == date(2030, 1, 2)


def test_timed_request_rejects_date_values_and_naive_datetimes() -> None:
    """定时提案不能把纯日期或 offset-naive 时间偷偷解释为 UTC。"""
    base = {
        "operation_kind": "create",
        "connection_id": uuid4(),
        "calendar_id": "calendar",
        "title": "Synthetic event",
        "timezone": "UTC",
        "all_day": False,
    }
    with pytest.raises(ValidationError):
        TypeAdapter(CreateProposalRequest).validate_python(
            {**base, "starts_at": "2030-01-01", "ends_at": "2030-01-02"}
        )
    with pytest.raises(ValidationError):
        TypeAdapter(CreateProposalRequest).validate_python(
            {
                **base,
                "starts_at": "2030-01-01T09:00:00",
                "ends_at": "2030-01-01T10:00:00",
            }
        )


def test_update_and_restore_requests_reject_provider_only_fields() -> None:
    """修改与恢复输入拒绝 ETag、recurrence、conference 等供应商扩展。"""
    with pytest.raises(ValidationError):
        UpdateProposalRequest.model_validate(
            {
                "version": 1,
                "title": "Changed",
                "etag": 'W/"provider-etag"',
            }
        )
    with pytest.raises(ValidationError):
        RestoreProposalRequest.model_validate(
            {
                "snapshot_id": str(uuid4()),
                "recurrence": {"rrule": "FREQ=DAILY"},
            }
        )


def test_suggest_response_is_explicitly_not_attendee_free_busy() -> None:
    """候选响应必须固定声明没有查询参会人 Free/Busy。"""
    response = SuggestTimesResponse.model_validate(
        {
            "proposal_id": str(uuid4()),
            "version": 1,
            "candidates": [],
            "completeness": "partial",
            "missing_connections": [],
        }
    )
    assert response.attendee_availability_checked is False


def test_calendar_mutating_routes_require_idempotency_key_header() -> None:
    """创建、提交和恢复的 OpenAPI 参数必须声明必填 Idempotency-Key。"""
    paths = create_app().openapi()["paths"]
    operations = (
        paths["/api/v1/calendar/proposals"]["post"],
        paths["/api/v1/calendar/proposals/{proposal_id}/submit"]["post"],
        paths["/api/v1/calendar/events/{event_id}/restore-proposal"]["post"],
    )
    for operation in operations:
        header = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["name"] == "Idempotency-Key"
        )
        assert header["in"] == "header"
        assert header["required"] is True


def test_calendar_sensitive_responses_declare_no_store_policy() -> None:
    """契约保留 no-store 响应要求，避免日程内容落入浏览器缓存。"""
    paths = create_app().openapi()["paths"]
    for path, methods in paths.items():
        if path.startswith("/api/v1/calendar/"):
            assert methods
