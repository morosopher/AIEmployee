"""日历提案与恢复 API 的公开 Schema 契约测试。"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import TypeAdapter, ValidationError

from ai_employee.api.deps import (
    ApiProblem,
    get_authenticated_session,
    get_calendar_editor_use_case,
    get_calendar_proposal_use_case,
    handle_api_problem,
    handle_request_validation_error,
)
from ai_employee.api.routers.calendar import (
    AcceptedTaskResponse,
    CalendarProposalListResponse,
    CreateEventProposalRequest,
    CreateProposalRequest,
    CreateUpdateProposalRequest,
    RestoreProposalRequest,
    SuggestTimesRequest,
    SuggestTimesResponse,
    UpdateProposalRequest,
    build_calendar_router,
)
from ai_employee.main import create_app

_NON_STRING_TEMPORAL_VALUES = (
    pytest.param(123, id="number"),
    pytest.param(True, id="boolean"),
    pytest.param(date(2030, 1, 1), id="python-date"),
    pytest.param(
        datetime.fromisoformat("2030-01-01T09:00:00"),
        id="python-naive-datetime",
    ),
    pytest.param(
        datetime(2030, 1, 1, 9, tzinfo=UTC),
        id="python-aware-datetime",
    ),
)
_NON_RFC3339_DATETIME_VALUES = (
    pytest.param("2030-01-01T09:00Z", id="missing-seconds"),
    pytest.param("2030-01-01T090000Z", id="compact-time"),
    pytest.param("2030-W01-1T09:00:00Z", id="week-date"),
    pytest.param("2030-01-01T09:00:00,1Z", id="comma-fraction"),
    pytest.param("2030-01-01T09:00:00.1234567Z", id="seven-digit-fraction"),
    pytest.param("2030-01-01T09:00:00+0000", id="compact-offset"),
    pytest.param("2030-01-01T09:00:00-00:00", id="unknown-offset"),
    pytest.param("2030-01-01T09:00:60Z", id="leap-second"),
    pytest.param("2030-01-01T09:00:00+24:00", id="offset-out-of-range"),
)


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
            "starts_at": "2030-01-01",
            "ends_at": "2030-01-02",
            "timezone": "Asia/Shanghai",
            "all_day": True,
        }
    )
    assert request.starts_at == date(2030, 1, 1)
    assert request.ends_at == date(2030, 1, 2)


@pytest.mark.parametrize(
    "invalid_time",
    _NON_STRING_TEMPORAL_VALUES,
)
@pytest.mark.parametrize("field_name", ("starts_at", "ends_at"))
def test_create_request_rejects_non_string_temporal_values(
    invalid_time: object,
    field_name: str,
) -> None:
    """公开 JSON 边界只接受 ISO 字符串，禁止 Pydantic 数字时间与 Python 对象直通。"""
    payload: dict[str, object] = {
        "operation_kind": "create",
        "connection_id": uuid4(),
        "calendar_id": "calendar",
        "title": "Synthetic event",
        "starts_at": "2030-01-01T09:00:00+00:00",
        "ends_at": "2030-01-01T10:00:00+00:00",
        "timezone": "UTC",
        "all_day": False,
    }
    payload[field_name] = invalid_time
    with pytest.raises(ValidationError):
        TypeAdapter(CreateProposalRequest).validate_python(payload)


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


@pytest.mark.parametrize(
    "invalid_time",
    _NON_RFC3339_DATETIME_VALUES,
)
@pytest.mark.parametrize("field_name", ("starts_at", "ends_at"))
def test_create_timed_request_rejects_non_rfc3339_values(
    invalid_time: str,
    field_name: str,
) -> None:
    """定时创建只接受与可信命令相同的严格 RFC3339 线格式。"""
    payload: dict[str, object] = {
        "operation_kind": "create",
        "connection_id": uuid4(),
        "calendar_id": "calendar",
        "title": "Synthetic event",
        "starts_at": "2030-01-01T08:00:00Z",
        "ends_at": "2030-01-01T10:00:00Z",
        "timezone": "UTC",
        "all_day": False,
    }
    payload[field_name] = invalid_time
    with pytest.raises(ValidationError):
        TypeAdapter(CreateProposalRequest).validate_python(payload)


@pytest.mark.parametrize(
    ("schema", "identity"),
    (
        (CreateUpdateProposalRequest, {"operation_kind": "update", "event_id": uuid4()}),
        (UpdateProposalRequest, {"version": 1}),
    ),
    ids=("create-update", "patch"),
)
def test_update_requests_reject_mixed_or_naive_intervals(
    schema: type[object],
    identity: dict[str, object],
) -> None:
    """update 与 PATCH 不能让混合 date/datetime 或无 offset 时间进入应用用例。"""
    with pytest.raises(ValidationError):
        TypeAdapter(schema).validate_python(
            {
                **identity,
                "starts_at": "2030-01-01",
                "ends_at": "2030-01-01T10:00:00+00:00",
                "all_day": False,
            }
        )
    with pytest.raises(ValidationError):
        TypeAdapter(schema).validate_python(
            {
                **identity,
                "starts_at": "2030-01-01T09:00:00",
            }
        )


@pytest.mark.parametrize(
    ("schema", "identity"),
    (
        (CreateUpdateProposalRequest, {"operation_kind": "update", "event_id": uuid4()}),
        (UpdateProposalRequest, {"version": 1}),
    ),
    ids=("create-update", "patch"),
)
def test_partial_update_allows_one_strict_temporal_value(
    schema: type[object],
    identity: dict[str, object],
) -> None:
    """部分更新可只给一端，但该值自身仍必须是严格 ISO aware datetime。"""
    request = TypeAdapter(schema).validate_python(
        {
            **identity,
            "starts_at": "2030-01-01T09:00:00Z",
            "all_day": False,
        }
    )
    assert request.starts_at == datetime(2030, 1, 1, 9, tzinfo=UTC)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("schema", "identity"),
    (
        (CreateUpdateProposalRequest, {"operation_kind": "update", "event_id": uuid4()}),
        (UpdateProposalRequest, {"version": 1}),
    ),
    ids=("create-update", "patch"),
)
@pytest.mark.parametrize(
    "invalid_value",
    (*_NON_STRING_TEMPORAL_VALUES, *_NON_RFC3339_DATETIME_VALUES),
)
@pytest.mark.parametrize("field_name", ("starts_at", "ends_at"))
def test_update_timed_requests_reject_non_rfc3339_values(
    schema: type[object],
    identity: dict[str, object],
    invalid_value: object,
    field_name: str,
) -> None:
    """创建修改与 PATCH 的单端时间也必须共享严格 RFC3339 边界。"""
    with pytest.raises(ValidationError):
        TypeAdapter(schema).validate_python(
            {
                **identity,
                field_name: invalid_value,
                "all_day": False,
            }
        )


@pytest.mark.parametrize(
    "invalid_value",
    (*_NON_STRING_TEMPORAL_VALUES, *_NON_RFC3339_DATETIME_VALUES),
)
def test_suggest_search_start_rejects_non_rfc3339_values(invalid_value: object) -> None:
    """候选搜索下界必须在 Pydantic 转换前执行同一字符串级严格规则。"""
    with pytest.raises(ValidationError):
        SuggestTimesRequest.model_validate({"search_start": invalid_value})


@pytest.mark.parametrize(
    "value",
    (
        "2030-01-01T09:00:00Z",
        "2030-01-01T09:00:00.1Z",
        "2030-01-01T09:00:00.123456+08:00",
    ),
)
def test_calendar_timed_inputs_accept_strict_rfc3339_values(value: str) -> None:
    """完整秒、1–6 位小数与冒号 offset 在创建和建议入口均应被接受。"""
    request = TypeAdapter(CreateProposalRequest).validate_python(
        {
            "operation_kind": "create",
            "connection_id": uuid4(),
            "calendar_id": "calendar",
            "title": "Synthetic event",
            "starts_at": value,
            "ends_at": "2030-01-01T11:00:00Z",
            "timezone": "Asia/Shanghai",
            "all_day": False,
        }
    )
    suggestion = SuggestTimesRequest.model_validate({"search_start": value})

    assert isinstance(request.starts_at, datetime)
    assert request.starts_at.utcoffset() is not None
    assert suggestion.search_start is not None
    assert suggestion.search_start.utcoffset() is not None


@pytest.mark.parametrize(
    ("schema", "identity"),
    (
        (
            CreateEventProposalRequest,
            {
                "operation_kind": "create",
                "connection_id": uuid4(),
                "calendar_id": "calendar",
                "title": "Synthetic holiday",
                "timezone": "UTC",
            },
        ),
        (CreateUpdateProposalRequest, {"operation_kind": "update", "event_id": uuid4()}),
        (UpdateProposalRequest, {"version": 1}),
    ),
    ids=("create", "create-update", "patch"),
)
def test_all_day_intervals_require_exclusive_end(
    schema: type[object],
    identity: dict[str, object],
) -> None:
    """全天结束日期是独占边界，必须严格晚于开始日期。"""
    with pytest.raises(ValidationError):
        TypeAdapter(schema).validate_python(
            {
                **identity,
                "starts_at": "2030-01-01",
                "ends_at": "2030-01-01",
                "all_day": True,
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


@pytest.mark.asyncio
async def test_calendar_sensitive_http_responses_are_no_store() -> None:
    """真实成功响应与 Problem Details 错误都必须携带 no-store Header。"""

    class _ListProposals:
        """为成功路径提供不访问数据库的最小日历提案用例。"""

        async def list(self, **_kwargs: object) -> list[object]:
            """返回空页以聚焦验证响应 Header。"""
            return []

    app = FastAPI()
    app.add_exception_handler(ApiProblem, handle_api_problem)
    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    app.include_router(build_calendar_router())
    authenticated = SimpleNamespace(user=SimpleNamespace(id=uuid4()))
    app.dependency_overrides[get_authenticated_session] = lambda: authenticated
    app.dependency_overrides[get_calendar_proposal_use_case] = lambda: _ListProposals()
    # 无效 UUID 的测试不应启动真实组合根；GET 新增的编辑器依赖也须在此最小应用中替换。
    app.dependency_overrides[get_calendar_editor_use_case] = lambda: object()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        success = await client.get("/api/v1/calendar/proposals")
        problem = await client.get("/api/v1/calendar/proposals/not-a-uuid")

    assert success.status_code == 200
    assert success.headers["Cache-Control"] == "no-store"
    assert problem.status_code == 422
    assert problem.headers["Cache-Control"] == "no-store"
    assert problem.headers["content-type"].startswith("application/problem+json")
