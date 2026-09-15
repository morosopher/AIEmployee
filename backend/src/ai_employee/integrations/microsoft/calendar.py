"""实现 Microsoft Graph Calendar 目录与 CalendarView Delta 只读适配器。

本模块是 Graph JSON、opaque URL、供应商时区和 HTTP 客户端的唯一边界。所有外部对象先
经过严格类型收窄，再转换为 application.ports.calendar 的不可变值对象；令牌、完整响应、
Delta URL 和供应商正文不会进入错误消息或持久化层。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, time, timedelta
from types import MappingProxyType
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from ai_employee.application.ports.calendar import (
    CalendarCursorExpiredError,
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarReader,
    CalendarSyncPage,
    ProviderCalendar,
)
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.domain.mail_actions import normalize_mailbox_address
from ai_employee.integrations.microsoft.timezones import to_iana_timezone

MICROSOFT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MICROSOFT_GRAPH_HOST = "graph.microsoft.com"
MICROSOFT_CALENDARS_URL = f"{MICROSOFT_GRAPH_BASE_URL}/me/calendars"
MICROSOFT_CALENDAR_TIMEOUT_SECONDS = 15.0
MICROSOFT_CALENDAR_CONNECT_TIMEOUT_SECONDS = 3.0
MICROSOFT_CALENDAR_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MICROSOFT_CALENDAR_MAX_CHAIN_BYTES = 32 * 1024 * 1024
MICROSOFT_CALENDAR_MAX_NORMALIZED_BYTES = 32 * 1024 * 1024
MICROSOFT_CALENDAR_MAX_PAGES = 100
MICROSOFT_CALENDAR_MAX_ITEMS = 10_000
MICROSOFT_CALENDAR_MAX_ID_LENGTH = 512
MICROSOFT_CALENDAR_MAX_EVENT_ID_LENGTH = 255
MICROSOFT_CALENDAR_MAX_VERSION_LENGTH = 255
MICROSOFT_CALENDAR_MAX_ENUM_LENGTH = 32
MICROSOFT_CALENDAR_MAX_TIMEZONE_LENGTH = 64
MICROSOFT_CALENDAR_MAX_STRING_LENGTH = 16_384
_CALENDAR_SELECT = "id,name,isDefaultCalendar,canEdit,canShare,owner,hexColor"
_FRACTIONAL_SECONDS = re.compile(r"(\.\d{6})\d+(?=Z$|[+-]\d{2}:\d{2}$|$)")
_GRAPH_EVENT_TYPES = frozenset({"singleInstance", "occurrence", "exception", "seriesMaster"})
_RefreshAccessToken = Callable[[], Awaitable[str]]
_MarkExpired = Callable[[], Awaitable[None]]


class MicrosoftCalendarAdapter(CalendarReader):
    """读取 Microsoft 日历目录、CalendarView Delta 和精确事件版本。"""

    provider = "microsoft"

    def __init__(
        self,
        *,
        access_token: str,
        user_timezone: str,
        now: Callable[[], datetime] | None = None,
        refresh_access_token: _RefreshAccessToken | None = None,
        mark_expired: _MarkExpired | None = None,
    ) -> None:
        """验证 token/用户时区并初始化单次同步链预算。"""
        if not isinstance(access_token, str) or access_token == "":
            raise ValueError("Microsoft access token is invalid")
        try:
            timezone = ZoneInfo(user_timezone)
        except (KeyError, ValueError):
            raise ValueError("user_timezone is invalid") from None
        self._access_token = access_token
        self._timezone = timezone
        self._timezone_name = timezone.key
        self._now = now or (lambda: datetime.now(UTC))
        self._refresh_access_token = refresh_access_token
        self._mark_expired = mark_expired
        self._refresh_attempted = False
        self._chain_wire_bytes = 0
        self._chain_normalized_bytes = 0

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """从固定 collection 读取有界完整目录，不制造 provider cursor。"""
        self._reset_budget()
        if cursor is not None:
            # Graph v1.0 /me/calendars 没有目录 Delta。任何历史非空值都不是可恢复位置，
            # 必须在构造请求和附加 Bearer Header 前 fail closed。
            raise PermanentProviderError(
                error_code="microsoft_calendar_directory_cursor_unsupported",
                message="Microsoft calendar directory cursor is unsupported",
            )
        current_url = MICROSOFT_CALENDARS_URL
        params: Mapping[str, str] | None = {"$select": _CALENDAR_SELECT}
        seen: set[str] = set()
        item_count = 0
        for _ in range(MICROSOFT_CALENDAR_MAX_PAGES):
            if current_url in seen:
                raise self._pagination_invalid()
            seen.add(current_url)
            payload = await self._get_json(current_url, params=params, cursor_scope=None)
            params = None
            values = self._values(payload)
            item_count += len(values)
            if item_count > MICROSOFT_CALENDAR_MAX_ITEMS:
                raise self._pagination_invalid()
            if any(item.get("@removed") is not None for item in values):
                raise self._invalid_response()
            calendars = tuple(self._normalize_calendar(item) for item in values)
            next_link, delta_link = self._links(payload)
            if delta_link is not None:
                # /me/calendars 不是 Delta collection；接受该字段会把供应商异常响应伪装成
                # 可恢复游标，并重新引入目录 cursor 与本地 revision 混淆。
                raise self._pagination_invalid()
            safe_next = self._validate_directory_url(next_link) if next_link is not None else None
            yield CalendarDirectoryPage(calendars, safe_next, None, full_snapshot=True)
            if safe_next is None:
                return
            current_url = safe_next
        raise self._pagination_invalid()

    async def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """读取用户时区当地午夜前一天至未来三十天的初始事件窗口。"""
        self._validate_calendar_id(calendar_id)
        self._reset_budget()
        local_now = self._now()
        if local_now.tzinfo is None or local_now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        local_midnight = local_now.astimezone(self._timezone).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        params = {
            "startDateTime": (local_midnight - timedelta(days=1)).isoformat(),
            "endDateTime": (local_midnight + timedelta(days=30)).isoformat(),
        }
        async for page in self._delta_pages(
            calendar_id,
            self._event_delta_url(calendar_id),
            params,
            persisted_cursor=False,
        ):
            yield page

    async def sync_pages(
        self, calendar_id: str, cursor: str | None = None
    ) -> AsyncIterator[CalendarSyncPage]:
        """跟随某个日历独立的 opaque deltaLink，并绑定 cursor scope。"""
        if cursor is None:
            cursor, calendar_id = calendar_id, "primary"
        self._validate_calendar_id(calendar_id)
        if not isinstance(cursor, str) or cursor == "":
            raise ValueError("calendar sync cursor must not be empty")
        safe_cursor = self._validate_delta_url(cursor, calendar_id)
        self._reset_budget()
        async for page in self._delta_pages(
            calendar_id,
            safe_cursor,
            None,
            persisted_cursor=True,
        ):
            yield page

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """精确读取事件当前版本；供应商 404 只返回 None。"""
        self._validate_calendar_id(calendar_id)
        self._validate_identifier(provider_event_id, "provider_event_id")
        self._reset_budget()
        try:
            payload = await self._get_json(
                self._event_url(calendar_id, provider_event_id),
                params=None,
                cursor_scope=None,
                allow_not_found=True,
            )
        except _NotFound:
            return None
        return self._normalize_event(payload, calendar_id=calendar_id)

    async def _delta_pages(
        self,
        calendar_id: str,
        first_url: str,
        params: Mapping[str, str] | None,
        *,
        persisted_cursor: bool,
    ) -> AsyncIterator[CalendarSyncPage]:
        """执行有限 Delta 分页，只把已持久 cursor 的首请求分类为失效。"""
        current_url = first_url
        current_params = params
        first_request = True
        seen: set[str] = set()
        item_count = 0
        for _ in range(MICROSOFT_CALENDAR_MAX_PAGES):
            if current_url in seen:
                raise self._pagination_invalid()
            seen.add(current_url)
            payload = await self._get_json(
                current_url,
                params=current_params,
                cursor_scope=calendar_id if persisted_cursor and first_request else None,
            )
            first_request = False
            current_params = None
            values = self._values(payload)
            item_count += len(values)
            if item_count > MICROSOFT_CALENDAR_MAX_ITEMS:
                raise self._pagination_invalid()
            events = tuple(self._normalize_event(item, calendar_id=calendar_id) for item in values)
            next_link, delta_link = self._links(payload)
            if next_link is not None and delta_link is not None:
                raise self._pagination_invalid()
            safe_next = (
                self._validate_delta_url(next_link, calendar_id) if next_link is not None else None
            )
            safe_delta = (
                self._validate_delta_url(delta_link, calendar_id)
                if delta_link is not None
                else None
            )
            if safe_next is None and safe_delta is None:
                raise self._missing_cursor()
            yield CalendarSyncPage(events, safe_next, safe_delta)
            if safe_next is None:
                return
            current_url = safe_next
        raise self._pagination_invalid()

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        cursor_scope: str | None,
        allow_not_found: bool = False,
    ) -> Mapping[str, object]:
        """流式读取 Graph JSON，按请求来源分类精确 GET 与持久 cursor 错误。"""
        for attempt in range(2):
            timeout = httpx.Timeout(
                MICROSOFT_CALENDAR_TIMEOUT_SECONDS,
                connect=MICROSOFT_CALENDAR_CONNECT_TIMEOUT_SECONDS,
            )
            try:
                async with (
                    httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client,
                    client.stream(
                        "GET",
                        url,
                        params=params,
                        headers={
                            "Authorization": f"Bearer {self._access_token}",
                            "Accept": "application/json",
                            # 偏好必须随初始、分页、精确 GET 和刷新后的重试一起发送；
                            # Graph 仍可能返回 HTML，事件投影不能依赖 Header 代替类型检查。
                            "Prefer": 'outlook.body-content-type="text"',
                        },
                    ) as response,
                ):
                    if response.status_code == 401:
                        if (
                            attempt == 0
                            and not self._refresh_attempted
                            and self._refresh_access_token is not None
                        ):
                            self._refresh_attempted = True
                            self._access_token = await self._refresh_access_token()
                            continue
                        if self._mark_expired is not None:
                            await self._mark_expired()
                        raise UserActionRequiredError(
                            error_code="microsoft_reauthorization_required",
                            message="Microsoft authorization requires user action",
                        )
                    if response.status_code == 403:
                        raise UserActionRequiredError(
                            error_code="microsoft_calendar_permission_required",
                            message="Microsoft calendar read permission requires user action",
                        )
                    if response.status_code in {404, 410} and cursor_scope is not None:
                        raise CalendarCursorExpiredError("microsoft", cursor_scope)
                    if response.status_code == 404 and allow_not_found:
                        raise _NotFound
                    if response.status_code == 429:
                        raise TransientProviderError(
                            error_code="microsoft_calendar_rate_limited",
                            message="Microsoft calendar is temporarily rate limited",
                            retry_after=self._retry_after(response),
                        )
                    if response.status_code >= 500:
                        raise TransientProviderError(
                            error_code="microsoft_calendar_service_unavailable",
                            message="Microsoft calendar is temporarily unavailable",
                            retry_after=self._retry_after(response),
                        )
                    if not 200 <= response.status_code < 300:
                        if cursor_scope is not None and await self._is_sync_state_not_found(
                            response
                        ):
                            raise CalendarCursorExpiredError("microsoft", cursor_scope)
                        raise PermanentProviderError(
                            error_code="microsoft_calendar_request_rejected",
                            message="Microsoft calendar request was rejected",
                        )
                    content_length = self._content_length(response)
                    if (
                        content_length is not None
                        and content_length > MICROSOFT_CALENDAR_MAX_RESPONSE_BYTES
                    ):
                        raise self._response_too_large()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MICROSOFT_CALENDAR_MAX_RESPONSE_BYTES:
                            raise self._response_too_large()
                        body.extend(chunk)
                        if (
                            self._chain_wire_bytes + response.num_bytes_downloaded
                            > MICROSOFT_CALENDAR_MAX_CHAIN_BYTES
                        ):
                            raise self._budget_exceeded()
                    wire_size = response.num_bytes_downloaded
                    if self._chain_wire_bytes + wire_size > MICROSOFT_CALENDAR_MAX_CHAIN_BYTES:
                        raise self._budget_exceeded()
            except _NotFound:
                raise
            except (
                TransientProviderError,
                UserActionRequiredError,
                CalendarCursorExpiredError,
                PermanentProviderError,
            ):
                raise
            except httpx.TimeoutException:
                raise TransientProviderError(
                    error_code="microsoft_calendar_timeout",
                    message="Microsoft calendar request timed out",
                ) from None
            except httpx.RequestError:
                raise TransientProviderError(
                    error_code="microsoft_calendar_request_failed",
                    message="Microsoft calendar request failed",
                ) from None
            try:
                parsed = json.loads(body)
            except (TypeError, ValueError):
                raise self._invalid_response() from None
            if not isinstance(parsed, Mapping):
                raise self._invalid_response()
            normalized_size = len(
                json.dumps(
                    parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ).encode("utf-8")
            )
            if (
                self._chain_normalized_bytes + normalized_size
                > MICROSOFT_CALENDAR_MAX_NORMALIZED_BYTES
            ):
                raise self._budget_exceeded()
            self._chain_wire_bytes += wire_size
            self._chain_normalized_bytes += normalized_size
            return parsed
        raise AssertionError("Microsoft calendar request retry loop exhausted")

    def _normalize_calendar(self, item: Mapping[str, object]) -> ProviderCalendar:
        """把 Graph calendar 项投影为目录值对象，权限未知时一律只读。"""
        calendar_id = self._required_identifier(item, "id")
        if item.get("@removed") is not None:
            return ProviderCalendar(
                calendar_id,
                calendar_id,
                self._timezone_name,
                False,
                "unknown",
                False,
                None,
                True,
            )
        name = self._text(item.get("name"), default=calendar_id)
        timezone_value = item.get("timeZone")
        timezone = (
            to_iana_timezone(timezone_value)
            if isinstance(timezone_value, str) and timezone_value != ""
            else self._timezone_name
        )
        self._validate_bounded_string(timezone, MICROSOFT_CALENDAR_MAX_TIMEZONE_LENGTH)
        can_edit = item.get("canEdit") is True
        can_share = item.get("canShare") is True
        owner = self._owner(item.get("owner"))
        access_role_value = item.get("accessRole")
        access_role = (
            access_role_value
            if isinstance(access_role_value, str) and access_role_value != ""
            else ("owner" if can_edit else "reader")
        )
        self._validate_bounded_string(access_role, MICROSOFT_CALENDAR_MAX_ENUM_LENGTH)
        provider_url = self._safe_url(item.get("webUrl") or item.get("webLink"))
        hex_color = item.get("hexColor")
        if not isinstance(hex_color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", hex_color):
            hex_color = None
        return ProviderCalendar(
            calendar_id=calendar_id,
            display_name=name,
            timezone=timezone,
            is_primary=item.get("isDefaultCalendar") is True,
            access_role=access_role,
            can_write=can_edit,
            provider_url=provider_url,
            is_deleted=False,
            can_share=can_share,
            hex_color=hex_color,
            owner=owner,
        )

    def _normalize_event(self, item: Mapping[str, object], *, calendar_id: str) -> CalendarEvent:
        """把 Graph event 或 removed tombstone 收窄为 CalendarEvent。"""
        self._validate_identifier(
            calendar_id,
            "calendar_id",
            max_length=MICROSOFT_CALENDAR_MAX_ID_LENGTH,
        )
        event_id = self._required_identifier(
            item,
            "id",
            max_length=MICROSOFT_CALENDAR_MAX_EVENT_ID_LENGTH,
        )
        removed = item.get("@removed")
        if removed is not None:
            if not isinstance(removed, Mapping):
                raise self._invalid_response()
            return CalendarEvent(
                event_id=event_id,
                calendar_id=calendar_id,
                title="",
                description="",
                location="",
                starts_at=None,
                ends_at=None,
                all_day=False,
                transparency="opaque",
                status="cancelled",
                timezone="UTC",
                recurring_event_id=None,
                etag=self._optional_etag(item),
                provider_url="",
                updated_at=None,
                organizer=None,
                attendees=(),
                access_role=None,
                can_edit=False,
                change_key=self._optional_string(
                    item,
                    "changeKey",
                    max_length=MICROSOFT_CALENDAR_MAX_VERSION_LENGTH,
                ),
                recurrence_metadata=None,
            )
        status = "cancelled" if item.get("isCancelled") is True else "confirmed"
        status_value = item.get("status")
        if isinstance(status_value, str) and status_value != "":
            status = status_value
        self._validate_bounded_string(status, MICROSOFT_CALENDAR_MAX_ENUM_LENGTH)
        start_value = item.get("start")
        end_value = item.get("end")
        all_day = item.get("isAllDay") is True
        if not isinstance(start_value, Mapping) or not isinstance(end_value, Mapping):
            raise self._invalid_response()
        starts_at, start_timezone = self._event_time(start_value)
        ends_at, end_timezone = self._event_time(end_value)
        timezone = start_timezone or end_timezone or self._timezone_name
        if start_timezone and end_timezone and start_timezone != end_timezone:
            raise self._invalid_response()
        if ends_at <= starts_at:
            raise self._invalid_response()
        if all_day:
            event_timezone = ZoneInfo(timezone)
            if (
                starts_at.astimezone(event_timezone).timetz().replace(tzinfo=None) != time.min
                or ends_at.astimezone(event_timezone).timetz().replace(tzinfo=None) != time.min
            ):
                raise self._invalid_response()
        body = item.get("body")
        description = ""
        if body is not None:
            if not isinstance(body, Mapping):
                raise self._invalid_response()
            content = body.get("content", "")
            if not isinstance(content, str):
                raise self._invalid_response()
            description = self._body_text(content)
            content_type = body.get("contentType")
            if not isinstance(content_type, str) or content_type.casefold() not in {"text", "html"}:
                raise self._invalid_response()
            if content_type.casefold() == "html":
                description = self._html_body_text(description)
        location_value = item.get("location")
        location = ""
        if isinstance(location_value, Mapping):
            location = self._text(location_value.get("displayName"), default="")
        elif location_value is not None:
            raise self._invalid_response()
        recurring_event_id, recurrence_metadata = self._recurrence_projection(item, event_id)
        return CalendarEvent(
            event_id=event_id,
            calendar_id=calendar_id,
            title=self._text(item.get("subject"), default=""),
            description=description,
            location=location,
            starts_at=starts_at,
            ends_at=ends_at,
            all_day=all_day,
            transparency=self._bounded_text(
                item.get("showAs"),
                default="opaque",
                max_length=MICROSOFT_CALENDAR_MAX_ENUM_LENGTH,
            ),
            status=status,
            timezone=timezone,
            recurring_event_id=recurring_event_id,
            etag=self._optional_etag(item),
            provider_url=self._safe_url(item.get("webLink")) or "",
            updated_at=self._datetime_value(item.get("lastModifiedDateTime"), optional=True),
            organizer=self._person(item.get("organizer"), optional=True),
            attendees=self._attendees(item.get("attendees")),
            access_role=self._optional_string(
                item,
                "accessRole",
                max_length=MICROSOFT_CALENDAR_MAX_ENUM_LENGTH,
            ),
            can_edit=item.get("canEdit") is True or item.get("isOrganizer") is True,
            change_key=self._optional_string(
                item,
                "changeKey",
                max_length=MICROSOFT_CALENDAR_MAX_VERSION_LENGTH,
            ),
            recurrence_metadata=recurrence_metadata,
        )

    def _event_time(self, value: Mapping[str, object]) -> tuple[datetime, str]:
        """解析 Graph dateTimeTimeZone，验证 offset/zone 并返回 UTC instant。

        Graph 常返回七位小数，而 Python 只保留微秒。截断时必须保留尾部 ``Z`` 或数值
        offset；显式 offset 还必须属于声明时区在该本地时刻的有效 offset 集合。对没有
        offset 的本地时间，使用 ZoneInfo 往返校验拒绝 DST 跳时中不存在的墙上时间。
        """
        raw_datetime = value.get("dateTime")
        raw_timezone = value.get("timeZone")
        if not isinstance(raw_datetime, str) or not isinstance(raw_timezone, str):
            raise self._invalid_response()
        timezone = to_iana_timezone(raw_timezone)
        self._validate_bounded_string(timezone, MICROSOFT_CALENDAR_MAX_TIMEZONE_LENGTH)
        try:
            normalized = _FRACTIONAL_SECONDS.sub(r"\1", raw_datetime)
            parsed = datetime.fromisoformat(
                normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
            )
        except (TypeError, ValueError):
            raise self._invalid_response() from None
        zone = ZoneInfo(timezone)
        local_value = parsed.replace(tzinfo=None)
        candidates = self._valid_local_candidates(local_value, zone)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            if not candidates:
                raise self._invalid_response()
            parsed = candidates[0]
        elif parsed.utcoffset() not in {candidate.utcoffset() for candidate in candidates}:
            raise self._invalid_response()
        return parsed.astimezone(UTC), timezone

    @staticmethod
    def _valid_local_candidates(value: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
        """返回能经 UTC 往返恢复同一墙上时间的稳定 ZoneInfo 候选。

        正常时刻两个 fold 会收敛为同一 offset；DST 回拨歧义时保留两个不同 offset；春季
        跳时中不存在的本地时间无法往返，因此返回空集合并由调用方永久拒绝。
        """
        result: list[datetime] = []
        seen_offsets: set[timedelta | None] = set()
        for fold in (0, 1):
            candidate = value.replace(tzinfo=zone, fold=fold)
            round_trip = candidate.astimezone(UTC).astimezone(zone)
            if round_trip.replace(tzinfo=None) != value:
                continue
            offset = candidate.utcoffset()
            if offset in seen_offsets:
                continue
            seen_offsets.add(offset)
            result.append(candidate)
        return tuple(result)

    @classmethod
    def _recurrence_projection(
        cls,
        item: Mapping[str, object],
        event_id: str,
    ) -> tuple[str | None, Mapping[str, str] | None]:
        """按 Graph event type 验证重复关系并返回只读投影。

        ``singleInstance`` 不得携带 series/recurrence；``occurrence`` 与 ``exception`` 必须
        绑定 seriesMasterId 且不能重复声明 recurrence；``seriesMaster`` 必须携带非空
        recurrence 且不能反向绑定另一个 series master。
        """
        event_type = item.get("type")
        if not isinstance(event_type, str) or event_type not in _GRAPH_EVENT_TYPES:
            raise cls._invalid_response()
        series = item.get("seriesMasterId")
        recurrence = item.get("recurrence")
        if event_type == "singleInstance":
            if series is not None or recurrence is not None:
                raise cls._invalid_response()
            return None, None
        if event_type in {"occurrence", "exception"}:
            if recurrence is not None:
                raise cls._invalid_response()
            if not isinstance(series, str):
                raise cls._invalid_response()
            cls._validate_identifier(
                series,
                "seriesMasterId",
                max_length=MICROSOFT_CALENDAR_MAX_EVENT_ID_LENGTH,
            )
            return series, None
        if series is not None or not isinstance(recurrence, Mapping) or not recurrence:
            raise cls._invalid_response()
        return event_id, cls._recurrence_metadata(recurrence)

    @classmethod
    def _recurrence_metadata(cls, value: object) -> Mapping[str, str] | None:
        """抽取有限 provider-neutral recurrence facts，不把 Graph 嵌套对象外泄。"""
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise cls._invalid_response()
        result: dict[str, str] = {}
        pattern = value.get("pattern")
        if isinstance(pattern, Mapping):
            for source, target in (
                ("type", "pattern_type"),
                ("interval", "interval"),
                ("firstDayOfWeek", "first_day_of_week"),
                ("index", "index"),
            ):
                raw = pattern.get(source)
                if isinstance(raw, (str, int)) and not isinstance(raw, bool):
                    result[target] = str(raw)
            days = pattern.get("daysOfWeek")
            if isinstance(days, list) and all(isinstance(day, str) for day in days):
                result["days_of_week"] = ",".join(days)
            elif days is not None:
                raise cls._invalid_response()
        elif pattern is not None:
            raise cls._invalid_response()
        date_range = value.get("range")
        if isinstance(date_range, Mapping):
            for source, target in (
                ("type", "range_type"),
                ("startDate", "start_date"),
                ("endDate", "end_date"),
                ("numberOfOccurrences", "number_of_occurrences"),
            ):
                raw = date_range.get(source)
                if isinstance(raw, (str, int)) and not isinstance(raw, bool):
                    result[target] = str(raw)
        elif date_range is not None:
            raise cls._invalid_response()
        return MappingProxyType(result) if result else MappingProxyType({})

    @classmethod
    def _attendees(cls, value: object) -> tuple[Mapping[str, str], ...]:
        """严格规范参会人，未知结构 fail closed。"""
        if value is None:
            return ()
        if not isinstance(value, list):
            raise cls._invalid_response()
        result: list[Mapping[str, str]] = []
        for item in value:
            person = cls._person(item, optional=False)
            if person is not None:
                result.append(person)
        return tuple(result)

    @classmethod
    def _person(cls, value: object, *, optional: bool) -> Mapping[str, str] | None:
        """规范 Graph organizer/attendee/owner 的公开地址字段。"""
        if value is None and optional:
            return None
        if not isinstance(value, Mapping):
            raise cls._invalid_response()
        address_value = value.get("emailAddress", value)
        if not isinstance(address_value, Mapping):
            raise cls._invalid_response()
        address = address_value.get("address")
        name = address_value.get("name", "")
        if not isinstance(address, str) or address == "" or not isinstance(name, str):
            raise cls._invalid_response()
        # 共享 mailbox normalizer 按邮件语法会消除水平 Tab 等 CFWS；Graph adapter 的契约
        # 更严格，所有原始 C0/DEL/NUL 必须先在供应商边界拒绝，不能规范后伪装成可信地址。
        cls._validate_bounded_string(address, MICROSOFT_CALENDAR_MAX_STRING_LENGTH)
        try:
            normalized_address = normalize_mailbox_address(address)
        except ValueError:
            raise cls._invalid_response() from None
        normalized: dict[str, str] = {
            "name": cls._text(name, default=""),
            "email": normalized_address,
        }
        status = value.get("status")
        if isinstance(status, Mapping) and isinstance(status.get("response"), str):
            response_status = status["response"]
            cls._validate_bounded_string(
                response_status,
                MICROSOFT_CALENDAR_MAX_ENUM_LENGTH,
            )
            normalized["responseStatus"] = response_status
        elif status is not None:
            raise cls._invalid_response()
        person_type = value.get("type")
        if isinstance(person_type, str):
            cls._validate_bounded_string(
                person_type,
                MICROSOFT_CALENDAR_MAX_ENUM_LENGTH,
            )
            normalized["type"] = person_type
        elif person_type is not None:
            raise cls._invalid_response()
        return MappingProxyType(normalized)

    @classmethod
    def _owner(cls, value: object) -> Mapping[str, str] | None:
        """owner 缺失或畸形时只读 fail closed，不回显供应商值。"""
        if value is None:
            return None
        try:
            return cls._person(value, optional=False)
        except PermanentProviderError:
            return None

    @classmethod
    def _text(cls, value: object, *, default: str) -> str:
        """读取有限展示文本并拒绝所有 C0/DEL 控制字符。"""
        return cls._bounded_text(
            value,
            default=default,
            max_length=MICROSOFT_CALENDAR_MAX_STRING_LENGTH,
        )

    @classmethod
    def _bounded_text(cls, value: object, *, default: str, max_length: int) -> str:
        """读取可 trim 的展示标量，并按目标列上限 fail closed。"""
        if value is None:
            return default
        if not isinstance(value, str):
            raise cls._invalid_response()
        cls._validate_bounded_string(value, max_length, allow_empty=True)
        return value.strip()

    @classmethod
    def _body_text(cls, value: str) -> str:
        """保留纯文本换行/制表，同时拒绝 NUL、其他 C0 与 DEL。"""
        if len(value) > MICROSOFT_CALENDAR_MAX_STRING_LENGTH or any(
            (ord(character) < 32 and character not in {"\t", "\n", "\r"}) or ord(character) == 127
            for character in value
        ):
            raise cls._invalid_response()
        return value

    @classmethod
    def _html_body_text(cls, value: str) -> str:
        """把已通过原始长度/控制字符校验的 Graph HTML 描述降为纯文本。

        Args:
            value: 明确声明为 HTML 且受既有字符预算限制的合成或供应商正文。

        Returns:
            删除主动、非正文和显式隐藏节点后的文本，保留行内连接与块起止边界；
            相邻结构边界只分隔一次，正文已有换行和制表不折叠。
            实体只由 HTML parser 解码一次；日程中的签名和引用是正文，不能使用邮件清洗器。

        Raises:
            PermanentProviderError: 实体解码后仍有非法控制字符或结果超出既有字符上限。
        """
        soup = BeautifulSoup(value, "html.parser")
        # 逆序先移除子节点，避免销毁父节点后再访问已失效的子节点属性。
        for node in reversed(
            soup.select(
                "head, script, style, template, noscript, iframe, object, embed, "
                "[hidden], [aria-hidden='true']"
            )
        ):
            node.decompose()
        for node in reversed(soup.select("[style]")):
            style = node.get("style")
            if not isinstance(style, str):
                continue
            style = cls._css_top_level_text(style)
            if re.search(
                r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))"
                r"\s*(?:!\s*important\s*)?(?:;|$)",
                style,
                re.IGNORECASE,
            ):
                node.decompose()
        for node in soup.find_all("br"):
            node.replace_with("\n")
        # 在插入内部边界标记前校验已解码正文，确保供应商不能用原始字符或实体伪造标记。
        cls._body_text(soup.get_text())
        block_separator = "\x00"
        for node in soup.find_all(
            ("div", "p", "li", "tr", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6")
        ):
            node.insert_before(block_separator)
            node.insert_after(block_separator)
        parts: list[str] = []
        for fragment in soup.get_text().split(block_separator):
            if not fragment:
                continue
            # 只合并相邻的结构边界；不能对真实文本全局折叠换行，否则会吞掉连续 br。
            if (
                parts
                and not parts[-1].endswith(("\n", "\r"))
                and not fragment.startswith(("\n", "\r"))
            ):
                parts.append("\n")
            parts.append(fragment)
        # 先验证再 trim，避免边缘 C0 被 Python 当作空白吞掉。get_text 不返回注释/标签；
        # 不能再次 unescape，否则原本可见的实体文本会被改写。
        return cls._body_text("".join(parts)).strip()

    @classmethod
    def _css_top_level_text(cls, value: str) -> str:
        """为既有隐藏声明判定保留顶层字面文本，屏蔽不透明 CSS 值。

        Args:
            value: 已受整个 HTML 字符预算约束的单个内联 style 属性。

        Returns:
            仅用于本地 display/visibility 匹配的文本，不保存或请求 URL。字符串、
            URL token 和括号块以不透明标记代替；普通注释替换为空白，避免拼接标识符。
            含转义名称同样保持不透明，只有顶层未转义分号能成为声明分隔符。
            hash/at-keyword 连同前缀消费，名称后缀不能重新成为 URL token 起点。
            扫描游标只向前移动，括号使用显式栈，恶意嵌套不会消耗 Python 递归深度。
        """
        parts: list[str] = []
        closers: list[str] = []
        index = 0
        while index < len(value):
            if value.startswith("/*", index):
                end = value.find("*/", index + 2)
                index = len(value) if end == -1 else end + 2
                if not closers:
                    parts.append(" ")
                continue
            character = value[index]
            if character in {"'", '"'}:
                quote_character = character
                index += 1
                while index < len(value):
                    if value[index] == quote_character:
                        index += 1
                        break
                    if value[index] in "\r\n\f":
                        break
                    if value[index] == "\\":
                        _, index = cls._css_escape(value, index)
                    else:
                        index += 1
                if not closers:
                    parts.append('""')
                continue
            start = index
            # #url / @url 的前缀属于当前 token；若先单独输出，会误把名称后缀当 URL。
            # 连同名称消费可让后续括号按普通块处理，其中的真实注释仍然屏蔽伪声明。
            prefixed_name = character in {"#", "@"}
            if prefixed_name:
                index += 1
            name: list[str] = []
            name_has_escape = False
            while index < len(value):
                character = value[index]
                if character == "\\":
                    name_has_escape = True
                    decoded, index = cls._css_escape(value, index)
                    name.append(decoded)
                elif character.isalnum() or character in "_-" or ord(character) >= 128:
                    name.append(character)
                    index += 1
                else:
                    break
            if index > start:
                if (
                    not prefixed_name
                    and "".join(name).casefold() == "url"
                    and value[index : index + 1] == "("
                ):
                    content_start = index + 1
                    while content_start < len(value) and value[content_start] in " \t\r\n\f":
                        content_start += 1
                    if value[content_start : content_start + 1] not in {"'", '"'}:
                        # 未加引号 URL 是独立 token；其中的注释形状只是 URL 内容。
                        # 转义的右括号不能提前结束 token，也不能暴露其中的伪声明。
                        index = content_start
                        while index < len(value):
                            if value[index] == ")":
                                index += 1
                                break
                            if value[index] == "\\":
                                _, index = cls._css_escape(value, index)
                            else:
                                index += 1
                        if not closers:
                            parts.append("[]")
                        continue
                if not closers:
                    # URL 函数名已在上面识别；其余转义 token 不能重新暴露原始标点，
                    # 否则被消费的 \; 会再次成为声明分隔符。未转义名称保持字面拼写。
                    parts.append("[]" if name_has_escape else value[start:index])
                continue
            if character in "([{":
                if not closers:
                    parts.append("[]")
                closers.append({"(": ")", "[": "]", "{": "}"}[character])
            elif closers:
                if character == closers[-1]:
                    closers.pop()
            else:
                parts.append(character)
            index += 1
        return "".join(parts)

    @staticmethod
    def _css_escape(value: str, index: int) -> tuple[str, int]:
        """消费一个 CSS 转义，返回函数名识别字符和严格前进的游标。

        index 指向反斜线。十六进制转义最多消费六位及一个可选空白；
        CRLF 作为同一个空白处理。非法或未完成转义返回替代字符，不能伪造 URL 名。
        调用方也用新游标跳过字符串与 URL 内容中的转义，不会解释或加载其值。
        """
        index += 1
        start = index
        while index < len(value) and index - start < 6 and value[index] in "0123456789abcdefABCDEF":
            index += 1
        if index > start:
            codepoint = int(value[start:index], 16)
            decoded = (
                chr(codepoint)
                if 0 < codepoint <= 0x10FFFF and not 0xD800 <= codepoint <= 0xDFFF
                else "\ufffd"
            )
            if value[index : index + 2] == "\r\n":
                index += 2
            elif index < len(value) and value[index] in " \t\r\n\f":
                index += 1
            return decoded, index
        if value[index : index + 2] == "\r\n":
            return "\ufffd", index + 2
        if index < len(value):
            character = value[index]
            return ("\ufffd" if character in "\r\n\f" else character), index + 1
        return "\ufffd", index

    @classmethod
    def _safe_url(cls, value: object) -> str | None:
        """只接受绝对 HTTPS 展示链接；已出现的畸形值不得静默丢弃。"""
        if value is None or value == "":
            return None
        if (
            not isinstance(value, str)
            or value.strip() != value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise cls._invalid_response()
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError:
            raise cls._invalid_response() from None
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.netloc == ""
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment != ""
        ):
            raise cls._invalid_response()
        return value

    @classmethod
    def _datetime_value(cls, value: object, *, optional: bool) -> datetime | None:
        """解析 Graph RFC3339 版本时间并统一为 UTC。"""
        if value is None and optional:
            return None
        if not isinstance(value, str):
            raise cls._invalid_response()
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        except ValueError:
            raise cls._invalid_response() from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise cls._invalid_response()
        return parsed.astimezone(UTC)

    @classmethod
    def _optional_etag(cls, item: Mapping[str, object]) -> str | None:
        """读取 Graph ETag，兼容测试/历史响应的 etag 别名。"""
        value = item.get("@odata.etag", item.get("etag"))
        if value is None:
            return None
        if not isinstance(value, str):
            raise cls._invalid_response()
        cls._validate_bounded_string(value, MICROSOFT_CALENDAR_MAX_VERSION_LENGTH)
        return value

    @classmethod
    def _optional_string(
        cls,
        item: Mapping[str, object],
        key: str,
        *,
        max_length: int = MICROSOFT_CALENDAR_MAX_STRING_LENGTH,
    ) -> str | None:
        """读取可选非空标量，并按调用方目标列长度校验。"""
        value = item.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise cls._invalid_response()
        cls._validate_bounded_string(value, max_length)
        return value

    @classmethod
    def _required_identifier(
        cls,
        item: Mapping[str, object],
        key: str,
        *,
        max_length: int = MICROSOFT_CALENDAR_MAX_ID_LENGTH,
    ) -> str:
        """读取不含空白/控制字符且受持久化长度约束的 provider ID。"""
        value = item.get(key)
        if not isinstance(value, str):
            raise cls._invalid_response()
        cls._validate_identifier(value, key, max_length=max_length)
        return value

    @classmethod
    def _validate_identifier(
        cls,
        value: object,
        key: str,
        *,
        max_length: int = MICROSOFT_CALENDAR_MAX_ID_LENGTH,
    ) -> None:
        """验证 opaque ID，并拒绝会被 HTTP 客户端规范化的精确 dot-segment。"""
        del key
        if (
            not isinstance(value, str)
            or value == ""
            or value in {".", ".."}
            or value.strip() != value
            or len(value) > max_length
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in value
            )
        ):
            raise cls._invalid_response()

    @classmethod
    def _validate_bounded_string(
        cls,
        value: str,
        max_length: int,
        *,
        allow_empty: bool = False,
    ) -> None:
        """验证无 padding/control 且不超过共享列长度的标量。"""
        if (
            (value == "" and not allow_empty)
            or value.strip() != value
            or len(value) > max_length
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise cls._invalid_response()

    @classmethod
    def _values(cls, payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
        """读取 Graph value 数组并拒绝任意非对象项。"""
        values = payload.get("value")
        if not isinstance(values, list):
            raise cls._invalid_response()
        result: list[Mapping[str, object]] = []
        for item in values:
            if not isinstance(item, Mapping):
                raise cls._invalid_response()
            result.append(item)
        return tuple(result)

    @classmethod
    def _links(cls, payload: Mapping[str, object]) -> tuple[str | None, str | None]:
        """读取 next/delta link，确保字段缺失与畸形可分类。"""
        next_link = payload.get("@odata.nextLink")
        delta_link = payload.get("@odata.deltaLink")
        if next_link is not None and (not isinstance(next_link, str) or next_link == ""):
            raise cls._invalid_response()
        if delta_link is not None and (not isinstance(delta_link, str) or delta_link == ""):
            raise cls._invalid_response()
        return next_link, delta_link

    @classmethod
    def _validate_absolute_graph_url(cls, value: object, error_code: str) -> str:
        """只接受无 userinfo/port/fragment 的精确 HTTPS Graph URL。"""
        if (
            not isinstance(value, str)
            or value == ""
            or value.strip() != value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise PermanentProviderError(
                error_code=error_code, message="Microsoft calendar URL is invalid"
            )
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise PermanentProviderError(
                error_code=error_code, message="Microsoft calendar URL is invalid"
            ) from None
        if (
            parsed.scheme != "https"
            or parsed.netloc != MICROSOFT_GRAPH_HOST
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.fragment != ""
            or parsed.path == ""
            or parsed.query == ""
        ):
            raise PermanentProviderError(
                error_code=error_code, message="Microsoft calendar URL is invalid"
            )
        return value

    @classmethod
    def _validate_directory_url(cls, value: str) -> str:
        """验证目录 nextLink 仍绑定固定 /me/calendars collection path。"""
        safe = cls._validate_absolute_graph_url(value, "microsoft_calendar_invalid_directory_url")
        if urlsplit(safe).path != "/v1.0/me/calendars":
            raise PermanentProviderError(
                error_code="microsoft_calendar_invalid_directory_url",
                message="Microsoft calendar directory URL is invalid",
            )
        return safe

    @classmethod
    def _validate_delta_url(cls, value: str, calendar_id: str) -> str:
        """接受同一日历的两种精确 Graph path，并原样保留 opaque query。

        Graph 的 next/deltaLink 可使用 ``calendars('id')`` 字符串键表示。只把正确 OData
        转义后 URL 编码的当前 ID 加入固定路径候选；Base64 填充 ``=`` 也可原样保留。
        不解码整条 path，也不允许改用其他用户、日历或 collection；绝对 URL 的主机等
        安全条件仍由原有校验负责。
        """
        safe = cls._validate_absolute_graph_url(value, "microsoft_calendar_invalid_delta_url")
        expected = f"/v1.0/me/calendars/{quote(calendar_id, safe='')}/calendarView/delta"
        odata_key = quote(calendar_id.replace("'", "''"), safe="")
        keyed_path = f"/v1.0/me/calendars('{odata_key}')/calendarView/delta"
        padded_key = quote(calendar_id.replace("'", "''"), safe="=")
        padded_path = f"/v1.0/me/calendars('{padded_key}')/calendarView/delta"
        if urlsplit(safe).path not in {expected, keyed_path, padded_path}:
            raise PermanentProviderError(
                error_code="microsoft_calendar_invalid_delta_url",
                message="Microsoft calendar delta URL is invalid",
            )
        return safe

    @staticmethod
    def _event_delta_url(calendar_id: str) -> str:
        """构造单个日历的 CalendarView Delta 资源 URL。"""
        return f"{MICROSOFT_CALENDARS_URL}/{quote(calendar_id, safe='')}/calendarView/delta"

    @staticmethod
    def _event_url(calendar_id: str, event_id: str) -> str:
        """构造精确 current-event GET URL，防止 ID 改变资源 path。"""
        return f"{MICROSOFT_CALENDARS_URL}/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}"

    @classmethod
    def _validate_calendar_id(cls, calendar_id: object) -> None:
        """验证 calendar scope，拒绝 directory 保留字和畸形 opaque ID。"""
        if calendar_id == "directory":
            raise ValueError("calendar_id is reserved")
        cls._validate_identifier(calendar_id, "calendar_id")

    @staticmethod
    def _content_length(response: httpx.Response) -> int | None:
        """只信任非负十进制 Content-Length；最终以实际流计数为准。"""
        raw = response.headers.get("Content-Length")
        try:
            value = int(raw) if raw is not None else -1
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        """解析有限非负 Retry-After，畸形值按未知处理。"""
        try:
            value = int(response.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    @classmethod
    async def _is_sync_state_not_found(cls, response: httpx.Response) -> bool:
        """有限读取 Graph 错误码，不回显错误正文或 opaque cursor。

        该分支只用于已经通过 host/path 绑定的持久 CalendarView deltaLink。响应无论是否
        畸形都会在本次请求后终止，因此只执行单页上限，不把错误正文计入后续成功链预算。

        Args:
            response: Graph 的非成功流式响应。

        Returns:
            错误对象明确给出 ``syncStateNotFound`` 时返回 ``True``。
        """
        content_length = cls._content_length(response)
        if content_length is not None and content_length > MICROSOFT_CALENDAR_MAX_RESPONSE_BYTES:
            return False
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > MICROSOFT_CALENDAR_MAX_RESPONSE_BYTES:
                return False
            body.extend(chunk)
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            return False
        if not isinstance(payload, Mapping):
            return False
        error = payload.get("error")
        return isinstance(error, Mapping) and error.get("code") == "syncStateNotFound"

    @staticmethod
    def _invalid_response() -> PermanentProviderError:
        """创建不含字段值、正文、token 或 cursor 的永久解析错误。"""
        return PermanentProviderError(
            error_code="microsoft_calendar_invalid_response",
            message="Microsoft calendar response is invalid",
        )

    @staticmethod
    def _pagination_invalid() -> PermanentProviderError:
        """创建固定分页预算/循环错误。"""
        return PermanentProviderError(
            error_code="microsoft_calendar_pagination_invalid",
            message="Microsoft calendar pagination is invalid",
        )

    @staticmethod
    def _missing_cursor() -> PermanentProviderError:
        """创建最终页缺少 delta cursor 的固定错误。"""
        return PermanentProviderError(
            error_code="microsoft_calendar_delta_missing_cursor",
            message="Microsoft calendar delta response is missing a cursor",
        )

    @staticmethod
    def _response_too_large() -> PermanentProviderError:
        """创建固定单页容量错误。"""
        return PermanentProviderError(
            error_code="microsoft_calendar_response_too_large",
            message="Microsoft calendar response is too large",
        )

    @staticmethod
    def _budget_exceeded() -> PermanentProviderError:
        """创建固定同步链容量错误。"""
        return PermanentProviderError(
            error_code="microsoft_calendar_sync_budget_exceeded",
            message="Microsoft calendar sync budget was exceeded",
        )

    def _reset_budget(self) -> None:
        """重置一次 public read chain 的 refresh 与大小预算。"""
        self._refresh_attempted = False
        self._chain_wire_bytes = 0
        self._chain_normalized_bytes = 0


class _NotFound(Exception):
    """内部标记精确事件 GET 的 404，不跨越 adapter 边界。"""


CalendarAdapter = MicrosoftCalendarAdapter

__all__ = [
    "MICROSOFT_CALENDARS_URL",
    "MICROSOFT_GRAPH_BASE_URL",
    "CalendarAdapter",
    "MicrosoftCalendarAdapter",
]
