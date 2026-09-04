"""实现 Google Calendar 非重复事件的受控创建、条件更新和只读核对。

本模块是 Google Calendar HTTP 与供应商无关可信命令之间的唯一写入边界。它不持有
数据库事务、不申请或刷新 OAuth scope，也不从供应商事件复制 recurrence、conference
或其它未冻结字段。所有真实写请求都只执行一次；请求结果不明确时仅允许调用
``reconcile`` 进行 GET 核对，避免未知结果导致重复日程。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime
from typing import cast
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.oauth import MAX_OAUTH_TOKEN_LENGTH
from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
    ApprovalWarningCode,
    ExecutionReference,
    ProviderWriteOutcome,
)
from ai_employee.domain.actions import ProviderWriteOutcomeKind
from ai_employee.domain.calendar_actions import (
    CalendarCommand,
    CalendarCreateCommand,
    CalendarRestoreCommand,
    CalendarUpdateCommand,
    NotificationPolicy,
    calendar_client_event_id,
)

GOOGLE_CALENDAR_API_BASE_URL = "https://www.googleapis.com/calendar/v3"
"""Google Calendar REST API 固定根路径。"""

GOOGLE_CALENDAR_EVENTS_BASE_URL = f"{GOOGLE_CALENDAR_API_BASE_URL}/calendars"
"""Google Calendar 事件资源集合根路径。"""

GOOGLE_CALENDAR_WRITE_TIMEOUT_SECONDS = 15.0
"""写入与核对请求的总超时上限。"""

GOOGLE_CALENDAR_WRITE_CONNECT_TIMEOUT_SECONDS = 3.0
"""建立 TLS/HTTP 连接的超时上限。"""

GOOGLE_CALENDAR_RETRY_AFTER_CAP_SECONDS = 300
"""持久化 Retry-After 允许的最大秒数。"""

_GOOGLE_EVENT_STATUSES = frozenset({"confirmed", "tentative", "cancelled"})
"""Google Event ``status`` 文档允许的完整枚举。"""

_RefreshAccessToken = Callable[[], Awaitable[str]]


class GoogleCalendarWriteAdapter:
    """执行 Google Calendar 创建、条件更新/恢复以及只读结果核对。

    Args:
        access_token: 已由连接 coordinator 解密并校验的短期 bearer token；本类不记录。
        client: 可选注入的 HTTP client，生产环境不传入时由每次调用创建短生命周期 client。
        refresh_access_token: 为组合根兼容而保留的 callback；本适配器永不隐式调用，401
            必须由外层 coordinator 完成一次有审计 refresh 后重新进入执行流程。

    Notes:
        创建使用领域命令中预先冻结的 ``client_event_id``。更新和恢复先 GET 当前事件并
        比较 ETag，只有相等时才 PUT 完整期望状态；任何 timeout、连接中断（连接建立后）
        或 5xx 都返回 ``unknown``，不在 adapter 内重放。
    """

    provider = "google"

    def __init__(
        self,
        *,
        access_token: str,
        client: httpx.AsyncClient | None = None,
        refresh_access_token: _RefreshAccessToken | None = None,
    ) -> None:
        """保存受控 token 和可选测试 client，不触发网络或 token 刷新。"""
        self._access_token = _require_token(access_token)
        self._client = client
        # 仅保留引用以兼容读取适配器的组合签名；任何隐式 refresh 都会破坏未知写结果
        # 的一次性边界，因此故意不调用该 callback。
        self._refresh_access_token = refresh_access_token

    def update_access_token(self, access_token: str) -> None:
        """在外层 coordinator 已完成 CAS refresh 后替换内存 token。"""
        self._access_token = _require_token(access_token)

    def validate_for_approval(self, command: TrustedCommand) -> ApprovalPreflightResult:
        """纯验证冻结日历命令可被 Google 无损表达，不执行任何 HTTP 请求。

        Args:
            command: 已由应用层严格解析的三种日历领域命令之一。

        Returns:
            ``notification_policy=none`` 时包含固定外部同步 warning，否则为空 warning。

        Raises:
            TypeError: 命令不是精确日历命令类型。
            ValueError: 命令字段无法安全映射到 Google Calendar。
        """
        normalized = _validated_calendar_command(command)
        warnings: tuple[ApprovalWarningCode, ...] = ()
        if normalized.notification_policy is NotificationPolicy.NONE:
            warnings = (ApprovalWarningCode.GOOGLE_SEND_UPDATES_NONE_EXTERNAL_SYNC,)
        return ApprovalPreflightResult(warnings=warnings)

    async def execute(self, command: TrustedCommand) -> ProviderWriteOutcome:
        """执行一条已审批日历命令，返回内容无关的三态结果。

        创建调用 ``POST``；修改和恢复先读取当前资源、校验删除/重复/权限/ETag，再调用
        ``PUT``。本方法不会自动重试任何已经开始的写请求。

        Args:
            command: 已完成审批、哈希绑定和 request-start 认领的日历命令。

        Returns:
            规范化的 ``ProviderWriteOutcome``。

        Raises:
            TypeError: 命令类型或字段类型不符合冻结领域边界。
            ValueError: URL、时间或标识无法安全表达。
        """
        normalized = _validated_calendar_command(command)
        if type(normalized) is CalendarCreateCommand:
            return await self._execute_create(normalized)
        if type(normalized) is CalendarUpdateCommand:
            return await self._execute_conditional_update(normalized)
        if type(normalized) is CalendarRestoreCommand:
            return await self._execute_conditional_update(normalized)
        raise TypeError("Google Calendar command type is unsupported")

    async def reconcile(
        self,
        command: TrustedCommand,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """只读 GET 核对精确事件，绝不再次调用 POST 或 PUT。

        Args:
            command: 原始冻结日历命令，用于目标、operation 和完整期望状态校验。
            execution: 已持久化的 ToolExecution 引用；其状态只用于操作绑定校验。

        Returns:
            事件与 operation/期望状态及版本均匹配时为 ``confirmed_applied``；明确的
            404 或其它缺少证据、冲突情况均为 ``unknown``。写请求开始后的“不存在”
            不能排除资源曾被写入后又删除，因此绝不据此证明本次操作未应用。

        Raises:
            TypeError: command 或 execution 不是可信领域类型。
            ValueError: execution 与 command 的 operation 绑定不一致。
        """
        normalized = _validated_calendar_command(command)
        if type(execution) is not ExecutionReference:
            raise TypeError("Google Calendar reconciliation requires ExecutionReference")
        if execution.operation_id != normalized.operation_id:
            raise ValueError("Google Calendar reconciliation operation binding is invalid")
        event_id = _target_event_id(normalized)
        response, transport_error = await self._request_read(
            _event_url(normalized.calendar_id, event_id)
        )
        correlation_id = _correlation_id(normalized)
        if transport_error is not None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code=transport_error,
            )
        if response is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="google_calendar_reconciliation_unavailable",
            )
        request_id = _provider_request_id(response)
        if response.status_code == 404:
            # reconcile 只会发生在写请求已经可能离开进程之后。当前资源不存在无法证明
            # 历史写入从未生效（事件可能随后被删除），所以只能等待后续核对或人工结论。
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_calendar_event_not_found",
            )
        if response.status_code == 401:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_reauthorization_required",
            )
        if response.status_code == 429 or response.status_code >= 500:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code=(
                    "google_rate_limited"
                    if response.status_code == 429
                    else "google_calendar_service_unavailable"
                ),
            )
        if response.status_code >= 400:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_calendar_reconciliation_rejected",
            )
        payload = _json_object(response)
        if payload is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_calendar_malformed_response",
            )
        if not _event_matches_command(payload, normalized):
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_calendar_reconciliation_mismatch",
            )
        if not _version_is_confirmed(payload, normalized):
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_calendar_version_unconfirmed",
            )
        provider_id = _safe_identifier(payload.get("id"))
        if provider_id is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_calendar_malformed_response",
            )
        return _applied_outcome(
            correlation_id=correlation_id,
            provider_resource_id=provider_id,
            provider_request_id=request_id,
            provider_url=_safe_provider_url(payload.get("htmlLink")),
        )

    async def _execute_create(self, command: CalendarCreateCommand) -> ProviderWriteOutcome:
        """发送创建 POST，并在 409 稳定 ID 冲突时只做一次精确 GET 核对。"""
        correlation_id = _correlation_id(command)
        url = _events_url(command.calendar_id)
        try:
            response = await self._request(
                "POST",
                url,
                params={"sendUpdates": command.notification_policy.value},
                json=_event_body(command, include_id=True),
            )
        except (httpx.ConnectTimeout, httpx.ConnectError):
            return _not_applied_outcome(
                correlation_id=correlation_id,
                retryable=True,
                error_code="google_connect_failed_before_send",
            )
        except httpx.TimeoutException:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="google_calendar_write_timeout",
            )
        except httpx.RequestError:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="google_calendar_write_request_failed",
            )

        request_id = _provider_request_id(response)
        if response.status_code in {200, 201}:
            return _success_from_response(
                response,
                expected_id=command.client_event_id,
                correlation_id=correlation_id,
            )
        if response.status_code == 409:
            # Google stable event IDs make a duplicate conflict potentially equivalent to an
            # earlier successful POST.  We must prove that fact with a read, never replay POST.
            return await self._reconcile_duplicate_create(command, request_id=request_id)
        return _classify_write_response(
            response,
            correlation_id=correlation_id,
            request_id=request_id,
            action_prefix="google_calendar_create",
        )

    async def _execute_conditional_update(
        self,
        command: CalendarCommand,
    ) -> ProviderWriteOutcome:
        """读取当前事件并以冻结 ETag 执行完整 PUT。"""
        if not isinstance(command, (CalendarUpdateCommand, CalendarRestoreCommand)):
            raise TypeError("calendar.create cannot use conditional update")
        # 领域入口已经拒绝子类；这里再次保留精确类型边界，避免私有 helper 被伪造对象绕过。
        if type(command) not in (CalendarUpdateCommand, CalendarRestoreCommand):
            raise TypeError("calendar conditional update command type is unsupported")
        correlation_id = _correlation_id(command)
        event_url = _event_url(command.calendar_id, command.provider_event_id)
        current, transport_error = await self._request_read(event_url)
        if transport_error is not None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code=transport_error,
            )
        if current is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="google_calendar_current_event_unavailable",
            )
        request_id = _provider_request_id(current)
        if current.status_code == 404:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_calendar_event_not_found",
            )
        if current.status_code == 401:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_reauthorization_required",
            )
        if current.status_code == 403:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_calendar_permission_required",
            )
        if current.status_code == 429 or current.status_code >= 500:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code=(
                    "google_rate_limited"
                    if current.status_code == 429
                    else "google_calendar_service_unavailable"
                ),
            )
        if current.status_code >= 400:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_calendar_current_event_rejected",
            )
        payload = _json_object(current)
        if payload is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="google_calendar_malformed_response",
            )
        precondition_error = _current_event_precondition_error(payload, command)
        if precondition_error is not None:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code=precondition_error,
            )

        try:
            response = await self._request(
                "PUT",
                event_url,
                params={"sendUpdates": command.notification_policy.value},
                json=_event_body(command, include_id=False),
                headers={"If-Match": command.base_etag},
            )
        except (httpx.ConnectTimeout, httpx.ConnectError):
            # A connection failure before the PUT bytes leave this process proves no update was
            # accepted; it is the only transport failure that receives safe retry permission.
            return _not_applied_outcome(
                correlation_id=correlation_id,
                retryable=True,
                error_code="google_connect_failed_before_send",
            )
        except httpx.TimeoutException:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="google_calendar_write_timeout",
            )
        except httpx.RequestError:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="google_calendar_write_request_failed",
            )
        response_request_id = _provider_request_id(response) or request_id
        if response.status_code in {200, 201}:
            return _success_from_response(
                response,
                expected_id=command.provider_event_id,
                correlation_id=correlation_id,
            )
        if response.status_code == 412:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=response_request_id,
                error_code="calendar_event_version_conflict",
            )
        if response.status_code == 409:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=response_request_id,
                error_code="google_calendar_conflict",
            )
        return _classify_write_response(
            response,
            correlation_id=correlation_id,
            request_id=response_request_id,
            action_prefix="google_calendar_update",
        )

    async def _reconcile_duplicate_create(
        self,
        command: CalendarCreateCommand,
        *,
        request_id: str | None,
    ) -> ProviderWriteOutcome:
        """核对 create 409 的稳定事件 ID，限制为一次只读 GET。"""
        correlation_id = _correlation_id(command)
        response, transport_error = await self._request_read(
            _event_url(command.calendar_id, command.client_event_id)
        )
        if transport_error is not None or response is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code=transport_error or "google_calendar_reconciliation_unavailable",
            )
        read_request_id = _provider_request_id(response) or request_id
        if response.status_code != 200:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=read_request_id,
                error_code=(
                    "google_calendar_duplicate_missing"
                    if response.status_code == 404
                    else "google_calendar_reconciliation_rejected"
                ),
            )
        payload = _json_object(response)
        if payload is None or not _event_matches_command(payload, command):
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=read_request_id,
                error_code="google_calendar_reconciliation_mismatch",
            )
        if not _version_is_confirmed(payload, command):
            # 409 只说明稳定 ID 已存在；缺少安全 ETag 时无法证明读到的是一个可审计的
            # 供应商版本，不能仅凭字段相似就把未知外部副作用提升为成功。
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=read_request_id,
                error_code="google_calendar_version_unconfirmed",
            )
        provider_id = _safe_identifier(payload.get("id"))
        if provider_id != command.client_event_id:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=read_request_id,
                error_code="google_calendar_reconciliation_mismatch",
            )
        return _applied_outcome(
            correlation_id=correlation_id,
            provider_resource_id=provider_id,
            provider_request_id=read_request_id,
            provider_url=_safe_provider_url(payload.get("htmlLink")),
        )

    async def _request_read(self, url: str) -> tuple[httpx.Response | None, str | None]:
        """执行单次只读 GET，返回脱敏稳定 transport 错误码。"""
        try:
            return await self._request("GET", url), None
        except httpx.ConnectTimeout:
            return None, "google_connect_failed"
        except httpx.ConnectError:
            return None, "google_connect_failed"
        except httpx.TimeoutException:
            return None, "google_timeout"
        except httpx.RequestError:
            return None, "google_request_failed"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """发送一条固定方法请求，禁止重定向并统一认证/超时策略。"""
        request_headers = {"Authorization": f"Bearer {self._access_token}"}
        if headers is not None:
            request_headers.update(headers)
        if self._client is not None:
            return await _client_request(
                self._client,
                method,
                url,
                params=params,
                json=json,
                headers=request_headers,
            )
        timeout = httpx.Timeout(
            GOOGLE_CALENDAR_WRITE_TIMEOUT_SECONDS,
            connect=GOOGLE_CALENDAR_WRITE_CONNECT_TIMEOUT_SECONDS,
        )
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            return await _client_request(
                client,
                method,
                url,
                params=params,
                json=json,
                headers=request_headers,
            )


async def _client_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None,
    json: Mapping[str, object] | None,
    headers: Mapping[str, str],
) -> httpx.Response:
    """通过显式 HTTP 方法调用注入 client，避免动作方向被动态字符串绕过。"""
    normalized_params = dict(params) if params is not None else None
    normalized_headers = dict(headers)
    if method == "GET":
        return await client.get(
            url,
            params=normalized_params,
            headers=normalized_headers,
            follow_redirects=False,
        )
    if method == "POST":
        return await client.post(
            url,
            params=normalized_params,
            json=dict(json) if json is not None else None,
            headers=normalized_headers,
            follow_redirects=False,
        )
    if method == "PUT":
        return await client.put(
            url,
            params=normalized_params,
            json=dict(json) if json is not None else None,
            headers=normalized_headers,
            follow_redirects=False,
        )
    raise ValueError("Google Calendar HTTP method is unsupported")


def google_event_time(
    value: datetime | date,
    timezone: str,
    all_day: bool,
) -> dict[str, str]:
    """把领域时间转换为 Google Event 的严格时间对象。

    全天事件只发送纯 ``date``，结束日期保持领域提供的排他边界；定时事件保留明确
    offset 并附 IANA ``timeZone``。该函数不使用宿主机时区，也不改变冻结命令值。

    Args:
        value: 定时 aware datetime 或全天纯 date。
        timezone: 已由领域命令校验的 IANA 时区名称。
        all_day: 是否采用 Google 全天日期表示。

    Returns:
        可直接放入 Google Event JSON 的 ``start``/``end`` 对象。

    Raises:
        TypeError: value 与 all_day 表示不匹配。
        ValueError: 定时 datetime 没有明确 offset 或 timezone 为空。
    """
    if type(all_day) is not bool:
        raise TypeError("calendar all_day must be bool")
    if not isinstance(timezone, str) or not timezone or "\r" in timezone or "\n" in timezone:
        raise ValueError("calendar timezone is invalid")
    if all_day:
        if type(value) is not date:
            raise TypeError("all-day calendar time must be a date")
        return {"date": value.isoformat()}
    if not isinstance(value, datetime):
        raise TypeError("timed calendar time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timed calendar time must be timezone-aware")
    return {"dateTime": value.isoformat(), "timeZone": timezone}


def _event_body(
    command: CalendarCommand,
    *,
    include_id: bool,
) -> dict[str, object]:
    """仅从冻结命令映射完整 Google Event 状态，显式省略空可选字段。"""
    body: dict[str, object] = {
        "summary": command.title,
        "start": google_event_time(command.starts_at, command.timezone, command.all_day),
        "end": google_event_time(command.ends_at, command.timezone, command.all_day),
        "extendedProperties": {"private": {"ai_employee_operation_id": str(command.operation_id)}},
    }
    if include_id:
        if type(command) is not CalendarCreateCommand:
            raise TypeError("only calendar.create may include an event id")
        if command.client_event_id != calendar_client_event_id(command.operation_id):
            raise ValueError("client_event_id is not operation-derived")
        body["id"] = command.client_event_id
    if command.description:
        body["description"] = command.description
    if command.location:
        body["location"] = command.location
    if command.attendees:
        body["attendees"] = [{"email": address} for address in command.attendees]
    return body


def _validated_calendar_command(command: TrustedCommand) -> CalendarCommand:
    """重新验证供应商入口收到的冻结命令，不修正任何被篡改值。"""
    if type(command) not in {
        CalendarCreateCommand,
        CalendarUpdateCommand,
        CalendarRestoreCommand,
    }:
        raise TypeError("Google Calendar action requires a CalendarCommand")
    normalized = cast(CalendarCommand, command)
    if not isinstance(normalized.operation_id, UUID) or not isinstance(
        normalized.connection_id, UUID
    ):
        raise TypeError("calendar operation and connection IDs must be UUID")
    _safe_required_identifier(normalized.calendar_id, field="calendar_id")
    if type(normalized.title) is not str or not normalized.title:
        raise ValueError("calendar title is invalid")
    for field_name in ("description", "location"):
        value = getattr(normalized, field_name)
        if value is not None and type(value) is not str:
            raise TypeError(f"calendar {field_name} is invalid")
        if isinstance(value, str) and "\x00" in value:
            raise ValueError(f"calendar {field_name} contains an invalid control")
    if type(normalized.timezone) is not str or not normalized.timezone:
        raise ValueError("calendar timezone is invalid")
    if type(normalized.all_day) is not bool:
        raise TypeError("calendar all_day is invalid")
    if type(normalized.notification_policy) is not NotificationPolicy:
        raise TypeError("calendar notification policy is invalid")
    if not isinstance(normalized.attendees, tuple):
        raise TypeError("calendar attendees must be a tuple")
    if type(normalized) is CalendarCreateCommand:
        if normalized.client_event_id != calendar_client_event_id(normalized.operation_id):
            raise ValueError("client_event_id is not operation-derived")
    else:
        update_or_restore = cast(CalendarUpdateCommand | CalendarRestoreCommand, normalized)
        _safe_required_identifier(update_or_restore.provider_event_id, field="provider_event_id")
        _safe_required_identifier(update_or_restore.base_etag, field="base_etag")
    return normalized


def _current_event_precondition_error(
    payload: Mapping[str, object],
    command: CalendarCommand,
) -> str | None:
    """严格检查更新前供应商事件的删除、重复、权限和 ETag 事实。"""
    if not isinstance(command, (CalendarUpdateCommand, CalendarRestoreCommand)):
        raise TypeError("calendar.create cannot use update precondition")
    # 与执行入口保持同一精确类型约束，同时让类型检查器收窄公开 CalendarCommand alias。
    if type(command) not in (CalendarUpdateCommand, CalendarRestoreCommand):
        raise TypeError("calendar conditional update command type is unsupported")
    event_id = _safe_identifier(payload.get("id"))
    if event_id != command.provider_event_id:
        return "google_calendar_current_event_mismatch"
    fact_error = _provider_event_fact_error(payload)
    if fact_error is not None:
        return fact_error
    current_etag = _safe_identifier(payload.get("etag"))
    if current_etag is None:
        return "google_calendar_event_version_missing"
    if current_etag != command.base_etag:
        return "calendar_event_version_conflict"
    return None


def _event_matches_command(payload: Mapping[str, object], command: CalendarCommand) -> bool:
    """验证核对事件的严格供应商事实、operation 关联与全部冻结可写字段。"""
    if _provider_event_fact_error(payload) is not None:
        # 核对阶段没有再次写入的机会；任何畸形、矛盾或 M2 不支持的事件事实都必须
        # 保持 unknown，不能让 Python 的真值/equality 规则把 ``0`` 等值当作布尔事实。
        return False
    expected_id = _target_event_id(command)
    if _safe_identifier(payload.get("id")) != expected_id:
        return False
    extension = payload.get("extendedProperties")
    if not isinstance(extension, Mapping):
        return False
    private = extension.get("private")
    if not isinstance(private, Mapping) or private.get("ai_employee_operation_id") != str(
        command.operation_id
    ):
        return False
    if payload.get("summary") != command.title:
        return False
    if not _optional_text_matches(payload.get("description"), command.description):
        return False
    if not _optional_text_matches(payload.get("location"), command.location):
        return False
    if not _time_matches(
        payload.get("start"), command.starts_at, command.timezone, command.all_day
    ):
        return False
    if not _time_matches(payload.get("end"), command.ends_at, command.timezone, command.all_day):
        return False
    return _attendees_match(payload.get("attendees"), command.attendees)


def _provider_event_fact_error(payload: Mapping[str, object]) -> str | None:
    """收窄 Google Event 可写性事实并返回稳定的 fail-closed 错误码。

    Google 的这些字段均可在普通非重复事件上省略；一旦出现，就必须严格符合 JSON
    schema。先验证全部形状再解释语义，可避免 ``0 == False``、字符串 ``"false"`` 或
    畸形 recurrence 被当成安全缺省。有效但已删除、重复或不可编辑的事件继续使用既有
    领域错误；无法解释的第三方响应统一返回不含原值的固定错误。

    Args:
        payload: 已确认顶层为 JSON object 的未信任供应商事件。

    Returns:
        ``None`` 表示这些事实可安全解释为普通可编辑事件；否则返回稳定错误码。
    """
    for field_name in ("deleted", "locked", "canEdit"):
        if field_name in payload and type(payload[field_name]) is not bool:
            return "google_calendar_malformed_response"

    status: str | None = None
    if "status" in payload:
        raw_status = payload["status"]
        if type(raw_status) is not str or raw_status not in _GOOGLE_EVENT_STATUSES:
            return "google_calendar_malformed_response"
        status = raw_status

    recurrence: list[object] | None = None
    if "recurrence" in payload:
        raw_recurrence = payload["recurrence"]
        if type(raw_recurrence) is not list:
            return "google_calendar_malformed_response"
        recurrence = cast(list[object], raw_recurrence)
        if any(
            type(rule) is not str
            or not rule
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in rule)
            for rule in recurrence
        ):
            return "google_calendar_malformed_response"

    recurring_id: str | None = None
    if "recurringEventId" in payload:
        raw_recurring_id = payload["recurringEventId"]
        if raw_recurring_id is None:
            recurring_id = None
        elif type(raw_recurring_id) is not str:
            return "google_calendar_malformed_response"
        elif raw_recurring_id == "":
            recurring_id = ""
        else:
            recurring_id = _safe_identifier(raw_recurring_id)
            if recurring_id is None:
                return "google_calendar_malformed_response"

    if payload.get("deleted") is True or status == "cancelled":
        return "google_calendar_event_deleted"
    if recurrence or recurring_id:
        return "google_calendar_recurring_event_unsupported"
    if payload.get("locked") is True or payload.get("canEdit") is False:
        return "google_calendar_event_not_editable"
    return None


def _version_is_confirmed(payload: Mapping[str, object], command: CalendarCommand) -> bool:
    """确保更新/恢复核对取得了新的 ETag 版本事实。"""
    if type(command) is CalendarCreateCommand:
        return _safe_identifier(payload.get("etag")) is not None
    update_or_restore = cast(CalendarUpdateCommand | CalendarRestoreCommand, command)
    etag = _safe_identifier(payload.get("etag"))
    return etag is not None and etag != update_or_restore.base_etag


def _time_matches(
    value: object,
    expected: datetime | date,
    timezone: str,
    all_day: bool,
) -> bool:
    """比较供应商时间与冻结时间的语义值，容忍 Z 与 +00:00 等等价写法。"""
    if not isinstance(value, Mapping):
        return False
    if all_day:
        return type(expected) is date and value.get("date") == expected.isoformat()
    raw = value.get("dateTime")
    if not isinstance(raw, str) or not isinstance(expected, datetime):
        return False
    normalized = raw.removesuffix("Z") + "+00:00" if raw.endswith("Z") else raw
    try:
        actual = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    if actual.tzinfo is None or actual.utcoffset() is None or expected.tzinfo is None:
        return False
    # 比较 UTC 瞬间，避免供应商把同一时刻格式化为不同 offset 后误报未知。
    if actual.astimezone(UTC) != expected.astimezone(UTC):
        return False
    provider_zone = value.get("timeZone")
    return provider_zone is None or provider_zone == timezone


def _attendees_match(value: object, expected: tuple[str, ...]) -> bool:
    """比较参会人邮箱集合，拒绝畸形、重复或额外地址。"""
    if not expected:
        return value is None or value == []
    if not isinstance(value, list):
        return False
    emails: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("email"), str):
            return False
        email = item["email"]
        if email in emails:
            return False
        emails.append(email)
    return set(emails) == set(expected) and len(emails) == len(expected)


def _optional_text_matches(value: object, expected: str | None) -> bool:
    """把命令的 None/空字符串与供应商省略字段视为同一空状态。"""
    if expected:
        return value == expected
    return value is None or value == ""


def _target_event_id(command: CalendarCommand) -> str:
    """返回创建稳定 ID 或更新/恢复目标 ID。"""
    if type(command) is CalendarCreateCommand:
        return command.client_event_id
    update_or_restore = cast(CalendarUpdateCommand | CalendarRestoreCommand, command)
    return update_or_restore.provider_event_id


def _events_url(calendar_id: str) -> str:
    """构造精确日历事件集合 URL，calendar_id 只作为编码后的路径段。"""
    return f"{GOOGLE_CALENDAR_EVENTS_BASE_URL}/{quote(calendar_id, safe='')}/events"


def _event_url(calendar_id: str, event_id: str) -> str:
    """构造精确事件资源 URL，拒绝把供应商 ID 当作 URL 模板。"""
    return f"{_events_url(calendar_id)}/{quote(event_id, safe='')}"


def _correlation_id(command: CalendarCommand) -> str:
    """使用 operation UUID 作为内容无关的核对关联标识。"""
    return str(command.operation_id)


def _safe_required_identifier(value: object, *, field: str) -> str:
    """验证路径/ETag 标识不含空白、控制字符或换行。"""
    result = _safe_identifier(value)
    if result is None:
        raise ValueError(f"calendar {field} is invalid")
    return result


def _safe_identifier(value: object) -> str | None:
    """收窄 Google opaque 标识，避免路径、Header 和日志边界注入。"""
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024:
        return None
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        return None
    return value


def _require_token(value: object) -> str:
    """验证 bearer token 长度和控制字符边界，不回显 token 内容。"""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_OAUTH_TOKEN_LENGTH
    ):
        raise ValueError("Google access token is invalid")
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        raise ValueError("Google access token is invalid")
    return value


def _json_object(response: httpx.Response) -> Mapping[str, object] | None:
    """把供应商 JSON 顶层收窄为 mapping，隐藏解码异常和原始正文。"""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return cast(Mapping[str, object], payload)


def _provider_request_id(response: httpx.Response) -> str | None:
    """从有限 Google response headers 提取可审计 opaque request ID。"""
    for name in ("x-request-id", "x-goog-request-id", "x-guploader-uploadid"):
        value = _safe_identifier(response.headers.get(name))
        if value is not None and len(value) <= 255:
            return value
    return None


def _safe_provider_url(value: object) -> str | None:
    """只接受绝对 HTTPS provider URL，不保存 userinfo、fragment 或控制字符。"""
    if not isinstance(value, str) or not value or any(ord(char) < 0x20 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.fragment:
        return None
    return value


def _retry_after(response: httpx.Response) -> int | None:
    """解析有界整数 Retry-After，畸形或超界值不进入 durable outcome。"""
    try:
        value = int(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if 0 <= value <= GOOGLE_CALENDAR_RETRY_AFTER_CAP_SECONDS else None


def _success_from_response(
    response: httpx.Response,
    *,
    expected_id: str,
    correlation_id: str,
) -> ProviderWriteOutcome:
    """解析 2xx 响应并要求资源 ID 与冻结目标精确一致。"""
    request_id = _provider_request_id(response)
    payload = _json_object(response)
    if payload is None:
        return _unknown_outcome(
            correlation_id=correlation_id,
            provider_request_id=request_id,
            error_code="google_calendar_malformed_response",
        )
    provider_id = _safe_identifier(payload.get("id"))
    if provider_id != expected_id:
        return _unknown_outcome(
            correlation_id=correlation_id,
            provider_request_id=request_id,
            error_code="google_calendar_resource_mismatch",
        )
    return _applied_outcome(
        correlation_id=correlation_id,
        provider_resource_id=provider_id,
        provider_request_id=request_id,
        provider_url=_safe_provider_url(payload.get("htmlLink")),
    )


def _classify_write_response(
    response: httpx.Response,
    *,
    correlation_id: str,
    request_id: str | None,
    action_prefix: str,
) -> ProviderWriteOutcome:
    """按 Google HTTP 状态映射安全三态结果。"""
    if 400 <= response.status_code < 500:
        retryable = response.status_code == 429
        return _not_applied_outcome(
            correlation_id=correlation_id,
            provider_request_id=request_id,
            retryable=retryable,
            retry_after_seconds=_retry_after(response) if retryable else None,
            error_code=(
                "google_rate_limited"
                if response.status_code == 429
                else (
                    "google_reauthorization_required"
                    if response.status_code == 401
                    else (
                        "google_calendar_permission_required"
                        if response.status_code == 403
                        else f"{action_prefix}_rejected"
                    )
                )
            ),
        )
    return _unknown_outcome(
        correlation_id=correlation_id,
        provider_request_id=request_id,
        error_code=(
            f"{action_prefix}_service_unavailable"
            if response.status_code >= 500
            else f"{action_prefix}_ambiguous_response"
        ),
    )


def _applied_outcome(
    *,
    correlation_id: str,
    provider_resource_id: str,
    provider_request_id: str | None,
    provider_url: str | None,
) -> ProviderWriteOutcome:
    """构造成功结果并集中保证 retry 字段为空。"""
    return ProviderWriteOutcome(
        kind=ProviderWriteOutcomeKind.CONFIRMED_APPLIED,
        retryable=False,
        retry_after_seconds=None,
        provider_resource_id=provider_resource_id,
        provider_request_id=provider_request_id,
        correlation_id=correlation_id,
        provider_url=provider_url,
        error_code=None,
    )


def _not_applied_outcome(
    *,
    correlation_id: str,
    error_code: str,
    retryable: bool = False,
    retry_after_seconds: int | None = None,
    provider_resource_id: str | None = None,
    provider_request_id: str | None = None,
) -> ProviderWriteOutcome:
    """构造明确未应用结果；Retry-After 仅与安全重试一起保留。"""
    return ProviderWriteOutcome(
        kind=ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds if retryable else None,
        provider_resource_id=provider_resource_id,
        provider_request_id=provider_request_id,
        correlation_id=correlation_id,
        provider_url=None,
        error_code=error_code,
    )


def _unknown_outcome(
    *,
    correlation_id: str,
    error_code: str,
    provider_request_id: str | None = None,
) -> ProviderWriteOutcome:
    """构造禁止自动重试的 unknown 结果。"""
    return ProviderWriteOutcome(
        kind=ProviderWriteOutcomeKind.UNKNOWN,
        retryable=False,
        retry_after_seconds=None,
        provider_resource_id=None,
        provider_request_id=provider_request_id,
        correlation_id=correlation_id,
        provider_url=None,
        error_code=error_code,
    )


# 统一命名兼容旧组合根；新代码优先使用显式 GoogleCalendarWriteAdapter。
CalendarWriteAdapter = GoogleCalendarWriteAdapter

__all__ = [
    "GOOGLE_CALENDAR_API_BASE_URL",
    "GOOGLE_CALENDAR_EVENTS_BASE_URL",
    "GOOGLE_CALENDAR_WRITE_CONNECT_TIMEOUT_SECONDS",
    "GOOGLE_CALENDAR_WRITE_TIMEOUT_SECONDS",
    "CalendarWriteAdapter",
    "GoogleCalendarWriteAdapter",
    "google_event_time",
]
