"""暴露非重复日历提案、确定性候选、提交与历史恢复 REST 边界。"""

import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from ai_employee.api.deps import (
    ApiProblem,
    CsrfProtectedSession,
    CurrentSession,
    get_auth_clock,
    get_calendar_availability_use_case,
    get_calendar_proposal_use_case,
    get_calendar_restore_enqueue_use_case,
    get_submit_calendar_proposal_use_case,
)
from ai_employee.application.use_cases.auth import Clock
from ai_employee.application.use_cases.calendar_proposals import (
    CalendarProposalNotFoundError,
    CalendarProposalUseCase,
    CalendarProposalView,
    CalendarRestoreEnqueueUseCase,
)
from ai_employee.application.use_cases.trusted_actions import (
    CalendarProposalSubmissionNotFoundError,
    SubmitCalendarProposalUseCase,
)
from ai_employee.domain.actions import CalendarProposalStatus
from ai_employee.domain.calendar_actions import NotificationPolicy
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import normalize_mailbox_address

IdempotencyKeyHeader = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^[^\s\r\n](?:[^\r\n]*[^\s\r\n])?$",
    ),
]

CalendarEditableField = Literal[
    "title",
    "description",
    "location",
    "starts_at",
    "ends_at",
    "timezone",
    "all_day",
    "attendees",
]


class _StrictCalendarModel(BaseModel):
    """为全部公开日历模型固定 ``extra=forbid``。"""

    model_config = ConfigDict(extra="forbid")


class _StrictCalendarTemporalModel(_StrictCalendarModel):
    """为所有请求时间字段执行 JSON 字符串级严格解析。"""

    @field_validator("starts_at", "ends_at", mode="before", check_fields=False)
    @classmethod
    def strict_temporal_value(cls, value: object) -> date | datetime | None:
        """只接受 ISO date 或携带 offset 的 ISO datetime 字符串。"""
        return _parse_calendar_temporal(value)


class _CalendarEventFields(_StrictCalendarTemporalModel):
    """创建请求共享的完整、供应商中立非重复事件字段。"""

    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=100_000)
    location: str | None = Field(default=None, max_length=16_384)
    starts_at: date | datetime
    ends_at: date | datetime
    timezone: str = Field(min_length=1, max_length=64)
    all_day: bool = False
    attendees: list[str] = Field(default_factory=list, max_length=50)
    notification_policy: NotificationPolicy | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """要求显式 IANA 时区，禁止宿主机本地时区推断。"""
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("timezone must be a valid IANA name") from error
        return value

    @field_validator("attendees")
    @classmethod
    def valid_attendees(cls, values: list[str]) -> list[str]:
        """在开启事务前拒绝无效邮箱与 CR/LF 注入。"""
        for value in values:
            normalize_mailbox_address(value)
        return values

    @model_validator(mode="after")
    def valid_interval(self) -> "_CalendarEventFields":
        """强制全天纯日期和定时 aware datetime 两种表示严格互斥。"""
        if self.all_day:
            if type(self.starts_at) is not date or type(self.ends_at) is not date:
                raise ValueError("all-day intervals must use ISO dates")
            if self.ends_at <= self.starts_at:
                raise ValueError("calendar ends_at must be after starts_at")
            return self
        if not isinstance(self.starts_at, datetime) or not isinstance(self.ends_at, datetime):
            # Pydantic v2 不再把 model validator 抛出的 TypeError 包装成 ValidationError；
            # 使用其原生自定义校验错误才能稳定映射为 API 422。
            raise PydanticCustomError(
                "calendar_interval_type",
                "timed intervals must use date-time values",
            )
        if (
            self.starts_at.tzinfo is None
            or self.starts_at.utcoffset() is None
            or self.ends_at.tzinfo is None
            or self.ends_at.utcoffset() is None
        ):
            raise ValueError("timed intervals must be timezone-aware")
        if self.ends_at.astimezone(UTC) <= self.starts_at.astimezone(UTC):
            raise ValueError("calendar ends_at must be after starts_at")
        return self


class CreateEventProposalRequest(_CalendarEventFields):
    """创建一个明确连接、日历和完整期望状态的日程提案。"""

    operation_kind: Literal["create"]
    connection_id: UUID
    calendar_id: str = Field(min_length=1, max_length=512)


class CreateUpdateProposalRequest(_StrictCalendarTemporalModel):
    """从用户拥有的本地非重复事件创建修改提案。"""

    operation_kind: Literal["update"]
    event_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=100_000)
    location: str | None = Field(default=None, max_length=16_384)
    starts_at: date | datetime | None = None
    ends_at: date | datetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    all_day: bool | None = None
    attendees: list[str] | None = Field(default=None, max_length=50)
    notification_policy: NotificationPolicy | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        """修改请求出现时区时立即验证 IANA 名称。"""
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("timezone must be a valid IANA name") from error
        return value

    @field_validator("attendees")
    @classmethod
    def valid_attendees(cls, values: list[str] | None) -> list[str] | None:
        """修改请求出现参会人时采用与创建相同的地址边界。"""
        if values is not None:
            for value in values:
                normalize_mailbox_address(value)
        return values

    @model_validator(mode="after")
    def has_changes(self) -> "CreateUpdateProposalRequest":
        """要求 update 至少有一个变更，并校验已提供时间字段的组合。"""
        if not set(self.model_fields_set).difference({"operation_kind", "event_id"}):
            raise ValueError("update proposal requires at least one change")
        _validate_partial_calendar_interval(
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            all_day=self.all_day,
            fields_set=self.model_fields_set,
        )
        return self


type CreateProposalRequest = Annotated[
    CreateEventProposalRequest | CreateUpdateProposalRequest,
    Field(discriminator="operation_kind"),
]


class UpdateProposalRequest(_StrictCalendarTemporalModel):
    """以客户端观察到的当前版本 CAS 保存下一不可变提案版本。"""

    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=100_000)
    location: str | None = Field(default=None, max_length=16_384)
    starts_at: date | datetime | None = None
    ends_at: date | datetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    all_day: bool | None = None
    attendees: list[str] | None = Field(default=None, max_length=50)
    notification_policy: NotificationPolicy | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        """PATCH 显式时区必须是有效 IANA 名称。"""
        return CreateUpdateProposalRequest.valid_timezone(value)

    @field_validator("attendees")
    @classmethod
    def valid_attendees(cls, values: list[str] | None) -> list[str] | None:
        """PATCH 显式参会人必须全部为规范邮箱地址。"""
        return CreateUpdateProposalRequest.valid_attendees(values)

    @model_validator(mode="after")
    def has_changes(self) -> "UpdateProposalRequest":
        """拒绝空 PATCH，并校验已提供时间字段的表示与顺序。"""
        if not set(self.model_fields_set).difference({"version"}):
            raise ValueError("calendar proposal patch requires at least one change")
        _validate_partial_calendar_interval(
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            all_day=self.all_day,
            fields_set=self.model_fields_set,
        )
        return self


class SubmitProposalRequest(_StrictCalendarModel):
    """冻结精确当前提案版本所需的最小输入。"""

    version: int = Field(ge=1)


class SuggestTimesRequest(_StrictCalendarModel):
    """可选固定版本与搜索下界；空 body 使用当前版本和显式 API 时钟。"""

    version: int | None = Field(default=None, ge=1)
    search_start: datetime | None = None

    @field_validator("search_start")
    @classmethod
    def aware_search_start(cls, value: datetime | None) -> datetime | None:
        """搜索下界出现时必须携带时区。"""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("search_start must be timezone-aware")
        return value


class RestoreProposalRequest(_StrictCalendarModel):
    """选择一个精确历史 before snapshot 作为恢复来源。"""

    snapshot_id: UUID


class CalendarFieldDiffResponse(_StrictCalendarModel):
    """返回一个受控日程字段发生变化的类型化事实。"""

    field: CalendarEditableField
    changed: Literal[True] = True


class CandidateTimeResponse(_StrictCalendarModel):
    """表示候选算法返回的精确 UTC 起止瞬间。"""

    starts_at: datetime
    ends_at: datetime


class SuggestTimesResponse(_StrictCalendarModel):
    """返回最多三个候选及本人日历同步完整性，固定未查询参会人。"""

    proposal_id: UUID
    version: int = Field(ge=1)
    candidates: list[CandidateTimeResponse] = Field(max_length=3)
    completeness: Literal["complete", "partial"]
    missing_connections: list[UUID]
    attendee_availability_checked: Literal[False] = False


class CalendarProposalResponse(_StrictCalendarModel):
    """返回当前用户可解密的提案当前版本与受控差异/候选事实。"""

    id: UUID
    connection_id: UUID
    calendar_id: str
    operation_kind: Literal["create", "update", "restore"]
    target_event_id: str | None
    base_etag: str | None
    before_snapshot_id: UUID | None
    version: int = Field(ge=1)
    status: CalendarProposalStatus
    title: str | None
    description: str | None
    location: str | None
    starts_at: str | None
    ends_at: str | None
    timezone: str | None
    all_day: bool | None
    attendees: list[str]
    notification_policy: NotificationPolicy | None
    changed_fields: list[CalendarEditableField]
    field_diffs: list[CalendarFieldDiffResponse]
    required_confirmations: list[str]
    retain_until: datetime
    availability: SuggestTimesResponse | None


class CalendarProposalListResponse(_StrictCalendarModel):
    """返回带显式 limit/offset 的当前用户提案页。"""

    items: list[CalendarProposalResponse]
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class AcceptedTaskResponse(_StrictCalendarModel):
    """返回已持久排队的长任务 ID，不暗示执行或审批已经完成。"""

    task_id: UUID
    status: Literal["queued"]


_ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_calendar_temporal(value: object) -> date | datetime | None:
    """严格解析公开请求中的日期或带时区日期时间。

    Args:
        value: Pydantic 进行任何宽松转换前观察到的原始字段值。

    Returns:
        ``None``，或由 ISO 字符串解析出的纯 ``date`` / aware ``datetime``。

    Raises:
        ValueError: 输入不是字符串、不是受支持的 ISO 表示或 datetime 缺少时区。
    """
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("calendar temporal values must be ISO strings")
    if _ISO_DATE_PATTERN.fullmatch(value) is not None:
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("calendar date must be a valid ISO date") from error
    if "T" not in value:
        raise ValueError("calendar datetime must be an ISO date-time string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("calendar datetime must be a valid ISO date-time") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("calendar datetime must be timezone-aware")
    return parsed


def _validate_partial_calendar_interval(
    *,
    starts_at: date | datetime | None,
    ends_at: date | datetime | None,
    all_day: bool | None,
    fields_set: set[str],
) -> None:
    """验证 update/PATCH 中已提供时间字段，不要求缺失的另一端。

    Args:
        starts_at: 已严格解析的可选开始日期或瞬间。
        ends_at: 已严格解析的可选结束日期或瞬间。
        all_day: 请求显式提供的全天模式，或未提供时的 ``None``。
        fields_set: Pydantic 记录的显式字段集合，用于区分省略与默认 ``None``。

    Raises:
        ValueError: 已提供字段与显式全天模式不匹配、两端混用表示或结束不晚于开始。
    """
    provided_start = "starts_at" in fields_set
    provided_end = "ends_at" in fields_set
    provided = tuple(
        value
        for is_present, value in ((provided_start, starts_at), (provided_end, ends_at))
        if is_present
    )
    if any(value is None for value in provided):
        raise ValueError("calendar temporal values cannot be null")
    if all_day is True and any(type(value) is not date for value in provided):
        raise ValueError("all-day intervals must use ISO dates")
    if all_day is False and any(not isinstance(value, datetime) for value in provided):
        raise ValueError("timed intervals must use date-time values")
    if not (provided_start and provided_end):
        return
    if type(starts_at) is date and type(ends_at) is date:
        if ends_at <= starts_at:
            raise ValueError("calendar ends_at must be after starts_at")
        return
    if isinstance(starts_at, datetime) and isinstance(ends_at, datetime):
        if ends_at.astimezone(UTC) <= starts_at.astimezone(UTC):
            raise ValueError("calendar ends_at must be after starts_at")
        return
    raise ValueError("calendar interval endpoints must use the same representation")


def _missing_proposal() -> ApiProblem:
    """隐藏不存在与跨用户提案，防止资源归属探测。"""
    return ApiProblem(
        404,
        "calendar_proposal_not_found",
        "Calendar proposal not found",
        "The requested calendar proposal was not found.",
    )


def _invalid_calendar_request() -> ApiProblem:
    """把应用层组合校验失败收敛为不回显日程内容的 422。"""
    return ApiProblem(
        422,
        "request_validation_failed",
        "Request validation failed",
        "The request did not match the required calendar schema.",
    )


def _actionable_calendar_conflict(error: StateConflictError) -> ApiProblem | None:
    """为两个客户端可自行恢复的日历冲突提供明确且不含内部值的操作提示。"""
    if error.error_code == "idempotency_key_payload_mismatch":
        return ApiProblem(
            409,
            error.error_code,
            "Idempotency key content mismatch",
            "Use a new Idempotency-Key when calendar request content changes.",
        )
    if error.error_code == "external_writes_disabled":
        return ApiProblem(
            409,
            error.error_code,
            "External calendar writes disabled",
            "Enable external calendar writes for this provider and account, then retry.",
        )
    return None


def _set_no_store(response: Response) -> None:
    """禁止浏览器、中间缓存或离线层保存日程敏感内容。"""
    response.headers["Cache-Control"] = "no-store"


def _proposal_response(value: CalendarProposalView) -> CalendarProposalResponse:
    """显式白名单投影应用视图，并把缓存候选转换为公共 Schema。"""
    availability = value.content.availability
    availability_response = None
    if availability is not None:
        availability_response = SuggestTimesResponse(
            proposal_id=value.proposal_id,
            version=value.current_version,
            candidates=[
                CandidateTimeResponse(
                    starts_at=candidate.starts_at,
                    ends_at=candidate.ends_at,
                )
                for candidate in availability.candidates
            ],
            completeness=availability.completeness,
            missing_connections=list(availability.missing_connection_ids),
            attendee_availability_checked=False,
        )
    # 应用层内容模型已把 changed_fields 限定为同一白名单；这里收窄到 API Literal，
    # 避免为静态类型重新实现一套可能漂移的运行时验证。
    changed_fields = [cast(CalendarEditableField, field) for field in value.content.changed_fields]
    return CalendarProposalResponse(
        id=value.proposal_id,
        connection_id=value.connection_id,
        calendar_id=value.calendar_id,
        operation_kind=value.operation_kind,
        target_event_id=value.target_event_id,
        base_etag=value.base_etag,
        before_snapshot_id=value.before_snapshot_id,
        version=value.current_version,
        status=value.status,
        title=value.content.title,
        description=value.content.description,
        location=value.content.location,
        starts_at=value.content.starts_at,
        ends_at=value.content.ends_at,
        timezone=value.content.timezone,
        all_day=value.content.all_day,
        attendees=list(value.content.attendees),
        notification_policy=value.content.notification_policy,
        changed_fields=changed_fields,
        field_diffs=[CalendarFieldDiffResponse(field=field) for field in changed_fields],
        required_confirmations=list(value.content.required_confirmations),
        retain_until=value.retain_until,
        availability=availability_response,
    )


def _changes(payload: BaseModel, *, excluded: set[str]) -> dict[str, object]:
    """提取显式出现的编辑字段，保留 ``None`` 以支持清空可选文本。"""
    values = payload.model_dump(exclude_unset=True)
    for name in excluded:
        values.pop(name, None)
    return values


def build_calendar_router() -> APIRouter:
    """构建八条日历提案、建议、提交与恢复路由。"""
    router = APIRouter(prefix="/api/v1/calendar", tags=["calendar"])

    @router.get("/proposals", response_model=CalendarProposalListResponse)
    async def list_calendar_proposals(
        authenticated: CurrentSession,
        response: Response,
        use_case: Annotated[CalendarProposalUseCase, Depends(get_calendar_proposal_use_case)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> CalendarProposalListResponse:
        """分页列出当前用户提案并禁止缓存其敏感内容。"""
        _set_no_store(response)
        values = await use_case.list(
            user_id=authenticated.user.id,
            limit=limit,
            offset=offset,
        )
        return CalendarProposalListResponse(
            items=[_proposal_response(value) for value in values],
            limit=limit,
            offset=offset,
        )

    @router.post(
        "/proposals",
        status_code=status.HTTP_201_CREATED,
        response_model=CalendarProposalResponse,
    )
    async def create_calendar_proposal(
        payload: CreateProposalRequest,
        authenticated: CsrfProtectedSession,
        response: Response,
        use_case: Annotated[CalendarProposalUseCase, Depends(get_calendar_proposal_use_case)],
        idempotency_key: IdempotencyKeyHeader,
    ) -> CalendarProposalResponse:
        """幂等创建本地 create/update 提案，绝不写供应商日历。"""
        _set_no_store(response)
        try:
            if isinstance(payload, CreateEventProposalRequest):
                values = _changes(
                    payload,
                    excluded={"operation_kind", "connection_id", "calendar_id"},
                )
                value = await use_case.create_event(
                    user_id=authenticated.user.id,
                    connection_id=payload.connection_id,
                    calendar_id=payload.calendar_id,
                    idempotency_key=idempotency_key,
                    values=values,
                )
            else:
                value = await use_case.create_update(
                    user_id=authenticated.user.id,
                    event_id=payload.event_id,
                    idempotency_key=idempotency_key,
                    changes=_changes(
                        payload,
                        excluded={"operation_kind", "event_id"},
                    ),
                )
        except CalendarProposalNotFoundError:
            raise _missing_proposal() from None
        except StateConflictError as error:
            actionable = _actionable_calendar_conflict(error)
            if actionable is not None:
                raise actionable from None
            raise
        except (TypeError, ValueError):
            raise _invalid_calendar_request() from None
        return _proposal_response(value)

    @router.get("/proposals/{proposal_id}", response_model=CalendarProposalResponse)
    async def get_calendar_proposal(
        proposal_id: UUID,
        authenticated: CurrentSession,
        response: Response,
        use_case: Annotated[CalendarProposalUseCase, Depends(get_calendar_proposal_use_case)],
    ) -> CalendarProposalResponse:
        """读取当前用户拥有的当前提案版本。"""
        _set_no_store(response)
        try:
            value = await use_case.get(
                user_id=authenticated.user.id,
                proposal_id=proposal_id,
            )
        except CalendarProposalNotFoundError:
            raise _missing_proposal() from None
        return _proposal_response(value)

    @router.patch("/proposals/{proposal_id}", response_model=CalendarProposalResponse)
    async def update_calendar_proposal(
        proposal_id: UUID,
        payload: UpdateProposalRequest,
        authenticated: CsrfProtectedSession,
        response: Response,
        use_case: Annotated[CalendarProposalUseCase, Depends(get_calendar_proposal_use_case)],
    ) -> CalendarProposalResponse:
        """以版本 CAS 保存下一不可变 desired snapshot。"""
        _set_no_store(response)
        try:
            value = await use_case.edit(
                user_id=authenticated.user.id,
                proposal_id=proposal_id,
                expected_version=payload.version,
                changes=_changes(payload, excluded={"version"}),
            )
        except CalendarProposalNotFoundError:
            raise _missing_proposal() from None
        except (TypeError, ValueError):
            raise _invalid_calendar_request() from None
        return _proposal_response(value)

    @router.delete("/proposals/{proposal_id}", response_model=CalendarProposalResponse)
    async def cancel_calendar_proposal(
        proposal_id: UUID,
        authenticated: CsrfProtectedSession,
        response: Response,
        use_case: Annotated[CalendarProposalUseCase, Depends(get_calendar_proposal_use_case)],
    ) -> CalendarProposalResponse:
        """取消纯本地未执行提案，不创建审批、任务或外部副作用。"""
        _set_no_store(response)
        try:
            value = await use_case.cancel(
                user_id=authenticated.user.id,
                proposal_id=proposal_id,
            )
        except CalendarProposalNotFoundError:
            raise _missing_proposal() from None
        return _proposal_response(value)

    @router.post(
        "/proposals/{proposal_id}/suggest-times",
        response_model=SuggestTimesResponse,
    )
    async def suggest_calendar_times(
        proposal_id: UUID,
        authenticated: CsrfProtectedSession,
        response: Response,
        use_case: Annotated[
            CalendarProposalUseCase,
            Depends(get_calendar_availability_use_case),
        ],
        clock: Annotated[Clock, Depends(get_auth_clock)],
        payload: Annotated[SuggestTimesRequest | None, Body()] = None,
    ) -> SuggestTimesResponse:
        """在两个短事务之间同步执行纯确定性候选计算，不创建 TaskRun。"""
        _set_no_store(response)
        request = payload or SuggestTimesRequest()
        try:
            value = await use_case.suggest_times(
                user_id=authenticated.user.id,
                proposal_id=proposal_id,
                expected_version=request.version,
                search_start=request.search_start or clock.now(),
            )
        except CalendarProposalNotFoundError:
            raise _missing_proposal() from None
        availability = value.content.availability
        if availability is None:
            raise RuntimeError("calendar availability result was not persisted")
        return SuggestTimesResponse(
            proposal_id=value.proposal_id,
            version=value.current_version,
            candidates=[
                CandidateTimeResponse(
                    starts_at=candidate.starts_at,
                    ends_at=candidate.ends_at,
                )
                for candidate in availability.candidates
            ],
            completeness=availability.completeness,
            missing_connections=list(availability.missing_connection_ids),
            attendee_availability_checked=False,
        )

    @router.post(
        "/proposals/{proposal_id}/submit",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AcceptedTaskResponse,
    )
    async def submit_calendar_proposal(
        proposal_id: UUID,
        payload: SubmitProposalRequest,
        authenticated: CsrfProtectedSession,
        response: Response,
        submission: Annotated[
            SubmitCalendarProposalUseCase,
            Depends(get_submit_calendar_proposal_use_case),
        ],
        clock: Annotated[Clock, Depends(get_auth_clock)],
        idempotency_key: IdempotencyKeyHeader,
    ) -> AcceptedTaskResponse:
        """冻结精确当前版本为单操作加密审批并返回持久任务 ID。"""
        _set_no_store(response)
        try:
            result = await submission.execute(
                user_id=authenticated.user.id,
                proposal_id=proposal_id,
                expected_version=payload.version,
                idempotency_key=idempotency_key,
                now=clock.now(),
            )
        except CalendarProposalSubmissionNotFoundError:
            raise _missing_proposal() from None
        except StateConflictError as error:
            actionable = _actionable_calendar_conflict(error)
            if actionable is not None:
                raise actionable from None
            raise
        return AcceptedTaskResponse(task_id=result.task_id, status="queued")

    @router.post(
        "/events/{event_id}/restore-proposal",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AcceptedTaskResponse,
    )
    async def enqueue_restore_proposal(
        event_id: UUID,
        payload: RestoreProposalRequest,
        authenticated: CsrfProtectedSession,
        response: Response,
        use_case: Annotated[
            CalendarRestoreEnqueueUseCase,
            Depends(get_calendar_restore_enqueue_use_case),
        ],
        idempotency_key: IdempotencyKeyHeader,
    ) -> AcceptedTaskResponse:
        """验证历史 before source 后只排队恢复准备任务，不提前创建恢复提案。"""
        _set_no_store(response)
        try:
            result = await use_case.execute(
                user_id=authenticated.user.id,
                event_id=event_id,
                source_snapshot_id=payload.snapshot_id,
                creation_idempotency_key=idempotency_key,
            )
        except CalendarProposalNotFoundError:
            raise ApiProblem(
                404,
                "calendar_restore_source_not_found",
                "Calendar restore source not found",
                "The requested restore source was not found.",
            ) from None
        except StateConflictError as error:
            actionable = _actionable_calendar_conflict(error)
            if actionable is not None:
                raise actionable from None
            raise
        return AcceptedTaskResponse(task_id=result.task_id, status="queued")

    return router


__all__ = [
    "AcceptedTaskResponse",
    "CalendarProposalListResponse",
    "CalendarProposalResponse",
    "CandidateTimeResponse",
    "CreateEventProposalRequest",
    "CreateProposalRequest",
    "CreateUpdateProposalRequest",
    "RestoreProposalRequest",
    "SuggestTimesResponse",
    "UpdateProposalRequest",
    "build_calendar_router",
]
