"""实现 Microsoft Graph Calendar 目录与 CalendarView Delta 只读适配器。

本模块是 Graph JSON、opaque URL、供应商时区和 HTTP 客户端的唯一边界。所有外部对象先
经过严格类型收窄，再转换为 application.ports.calendar 的不可变值对象；令牌、完整响应、
Delta URL 和供应商正文不会进入错误消息或持久化层。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import httpx

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
MICROSOFT_CALENDAR_MAX_STRING_LENGTH = 16_384
_CALENDAR_SELECT = "id,name,isDefaultCalendar,canEdit,canShare,owner,hexColor"
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
        """读取可见日历目录，并只在最终页返回目录 deltaLink。"""
        self._reset_budget()
        current_url = MICROSOFT_CALENDARS_URL
        params: Mapping[str, str] | None = {"$select": _CALENDAR_SELECT}
        if cursor is not None:
            if cursor == "":
                raise ValueError("calendar directory cursor must not be empty")
            if cursor.startswith("https://"):
                current_url = self._validate_directory_url(cursor)
                params = None
            else:
                params = {"$select": _CALENDAR_SELECT, "$deltatoken": cursor}
        seen: set[str] = set()
        item_count = 0
        for _ in range(MICROSOFT_CALENDAR_MAX_PAGES):
            if current_url in seen:
                raise self._pagination_invalid()
            seen.add(current_url)
            payload = await self._get_json(current_url, params=params, cursor_scope="directory")
            params = None
            values = self._values(payload)
            item_count += len(values)
            if item_count > MICROSOFT_CALENDAR_MAX_ITEMS:
                raise self._pagination_invalid()
            calendars = tuple(self._normalize_calendar(item) for item in values)
            next_link, delta_link = self._links(payload)
            if next_link is not None and delta_link is not None:
                # Graph 分页页只能给出一种后继语义；同时出现会让最终目录游标不确定，
                # 因而必须在发起下一次请求前拒绝，避免把不完整目录标记为成功。
                raise self._pagination_invalid()
            safe_next = self._validate_directory_url(next_link) if next_link is not None else None
            safe_delta = (
                self._validate_directory_url(delta_link) if delta_link is not None else None
            )
            if safe_next is None and safe_delta is None:
                raise self._missing_cursor()
            yield CalendarDirectoryPage(calendars, safe_next, safe_delta)
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
            calendar_id, self._event_delta_url(calendar_id), params
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
        async for page in self._delta_pages(calendar_id, safe_cursor, None):
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
            )
        except _NotFound:
            return None
        return self._normalize_event(payload, calendar_id=calendar_id)

    async def execute_request(
        self,
        parameters: Mapping[str, str] | None = None,
        *,
        url: str = MICROSOFT_CALENDARS_URL,
    ) -> Mapping[str, object]:
        """执行一个受控只读 GET，保留给契约和诊断测试使用。"""
        self._reset_budget()
        return await self._get_json(url, params=parameters, cursor_scope=None)

    async def _delta_pages(
        self,
        calendar_id: str,
        first_url: str,
        params: Mapping[str, str] | None,
    ) -> AsyncIterator[CalendarSyncPage]:
        """执行有限 Delta 分页，只有最终页携带 delta cursor。"""
        current_url = first_url
        current_params = params
        seen: set[str] = set()
        item_count = 0
        for _ in range(MICROSOFT_CALENDAR_MAX_PAGES):
            if current_url in seen:
                raise self._pagination_invalid()
            seen.add(current_url)
            payload = await self._get_json(
                current_url,
                params=current_params,
                cursor_scope=calendar_id if current_params is None else None,
            )
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
    ) -> Mapping[str, object]:
        """流式读取 Graph JSON，分类授权、限流、网络和游标错误。"""
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
                    if response.status_code == 404:
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
        can_edit = item.get("canEdit") is True
        can_share = item.get("canShare") is True
        owner = self._owner(item.get("owner"))
        access_role_value = item.get("accessRole")
        access_role = (
            access_role_value
            if isinstance(access_role_value, str) and access_role_value != ""
            else ("owner" if can_edit else "reader")
        )
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
        event_id = self._required_identifier(item, "id")
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
                change_key=self._optional_string(item, "changeKey"),
                recurrence_metadata=None,
            )
        status = "cancelled" if item.get("isCancelled") is True else "confirmed"
        status_value = item.get("status")
        if isinstance(status_value, str) and status_value != "":
            status = status_value
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
        body = item.get("body")
        description = ""
        if body is not None:
            if not isinstance(body, Mapping):
                raise self._invalid_response()
            content = body.get("content", "")
            if not isinstance(content, str):
                raise self._invalid_response()
            description = content
        location_value = item.get("location")
        location = ""
        if isinstance(location_value, Mapping):
            location = self._text(location_value.get("displayName"), default="")
        elif location_value is not None:
            raise self._invalid_response()
        recurring_event_id = self._recurring_id(item, event_id)
        recurrence_metadata = self._recurrence_metadata(item.get("recurrence"))
        return CalendarEvent(
            event_id=event_id,
            calendar_id=calendar_id,
            title=self._text(item.get("subject"), default=""),
            description=description,
            location=location,
            starts_at=starts_at,
            ends_at=ends_at,
            all_day=all_day,
            transparency=self._text(item.get("showAs"), default="opaque"),
            status=status,
            timezone=timezone,
            recurring_event_id=recurring_event_id,
            etag=self._optional_etag(item),
            provider_url=self._safe_url(item.get("webLink")) or "",
            updated_at=self._datetime_value(item.get("lastModifiedDateTime"), optional=True),
            organizer=self._person(item.get("organizer"), optional=True),
            attendees=self._attendees(item.get("attendees")),
            access_role=self._optional_string(item, "accessRole"),
            can_edit=item.get("canEdit") is True or item.get("isOrganizer") is True,
            change_key=self._optional_string(item, "changeKey"),
            recurrence_metadata=recurrence_metadata,
        )

    def _event_time(self, value: Mapping[str, object]) -> tuple[datetime, str]:
        """解析 Graph dateTimeTimeZone，返回 UTC instant 与 canonical IANA。"""
        raw_datetime = value.get("dateTime")
        raw_timezone = value.get("timeZone")
        if not isinstance(raw_datetime, str) or not isinstance(raw_timezone, str):
            raise self._invalid_response()
        timezone = to_iana_timezone(raw_timezone)
        try:
            normalized = raw_datetime
            if "." in normalized:
                prefix, fraction = normalized.split(".", 1)
                suffix = ""
                if fraction.endswith("Z"):
                    fraction, suffix = fraction[:-1], "Z"
                normalized = f"{prefix}.{fraction[:6]}{suffix}"
            parsed = datetime.fromisoformat(
                normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
            )
        except (TypeError, ValueError):
            raise self._invalid_response() from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
        return parsed.astimezone(UTC), timezone

    @classmethod
    def _recurring_id(cls, item: Mapping[str, object], event_id: str) -> str | None:
        """保留 Graph seriesMasterId 或非空 recurrence sentinel。"""
        series = item.get("seriesMasterId")
        if series is not None:
            if not isinstance(series, str) or series == "":
                raise cls._invalid_response()
            return series
        recurrence = item.get("recurrence")
        if isinstance(recurrence, Mapping) and recurrence:
            return event_id
        if recurrence is not None:
            raise cls._invalid_response()
        return None

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
        normalized: dict[str, str] = {
            "name": name.replace("\r", " ").replace("\n", " ").strip(),
            "email": address,
        }
        status = value.get("status")
        if isinstance(status, Mapping) and isinstance(status.get("response"), str):
            normalized["responseStatus"] = status["response"]
        elif status is not None:
            raise cls._invalid_response()
        person_type = value.get("type")
        if isinstance(person_type, str):
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

    @staticmethod
    def _text(value: object, *, default: str) -> str:
        """读取有限展示文本并清除换行控制字符。"""
        if value is None:
            return default
        if not isinstance(value, str) or len(value) > MICROSOFT_CALENDAR_MAX_STRING_LENGTH:
            raise MicrosoftCalendarAdapter._invalid_response()
        return value.replace("\r", " ").replace("\n", " ").strip()

    @staticmethod
    def _safe_url(value: object) -> str | None:
        """仅保留绝对 HTTPS 展示链接，畸形供应商值按缺失处理。"""
        if not isinstance(value, str) or value == "":
            return None
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
            return None
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
        if not isinstance(value, str) or value == "":
            raise cls._invalid_response()
        return value

    @classmethod
    def _optional_string(cls, item: Mapping[str, object], key: str) -> str | None:
        """读取可选非空字符串，畸形类型统一为安全永久错误。"""
        value = item.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or value == "":
            raise cls._invalid_response()
        return value

    @classmethod
    def _required_identifier(cls, item: Mapping[str, object], key: str) -> str:
        """读取不含空白/控制字符且受持久化长度约束的 provider ID。"""
        value = item.get(key)
        if not isinstance(value, str):
            raise cls._invalid_response()
        cls._validate_identifier(value, key)
        return value

    @classmethod
    def _validate_identifier(cls, value: object, key: str) -> None:
        """验证 opaque ID，不在错误中回显实际值。"""
        if (
            not isinstance(value, str)
            or value == ""
            or value.strip() != value
            or len(value) > MICROSOFT_CALENDAR_MAX_ID_LENGTH
            or any(character.isspace() or ord(character) < 32 for character in value)
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
        if not isinstance(value, str):
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
        """验证目录 next/delta 链仍绑定 /me/calendars path。"""
        safe = cls._validate_absolute_graph_url(value, "microsoft_calendar_invalid_directory_url")
        if urlsplit(safe).path != "/v1.0/me/calendars":
            raise PermanentProviderError(
                error_code="microsoft_calendar_invalid_directory_url",
                message="Microsoft calendar directory URL is invalid",
            )
        return safe

    @classmethod
    def _validate_delta_url(cls, value: str, calendar_id: str) -> str:
        """验证事件 cursor host/path/calendar 精确绑定，并原样保留 query。"""
        safe = cls._validate_absolute_graph_url(value, "microsoft_calendar_invalid_delta_url")
        expected = f"/v1.0/me/calendars/{quote(calendar_id, safe='')}/calendarView/delta"
        if urlsplit(safe).path != expected:
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
