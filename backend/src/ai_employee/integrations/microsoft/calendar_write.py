"""实现 Microsoft Graph Calendar 可信写入与只读核对边界。

本模块只接受三种冻结日历命令；审批预检保持纯本地，真实写入与未知结果核对由同一固定
adapter 承担。任何供应商响应都必须先规范化，不能把 token、正文或原始错误带出集成层。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast
from urllib.parse import parse_qsl, quote, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.oauth import MAX_OAUTH_TOKEN_LENGTH
from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
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
from ai_employee.domain.errors import PermanentProviderError, StateConflictError
from ai_employee.domain.mail_actions import normalize_mailbox_address
from ai_employee.integrations.microsoft.timezones import (
    to_iana_timezone,
    to_windows_timezone,
)

MICROSOFT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
"""Microsoft Graph v1.0 固定资源根路径。"""

MICROSOFT_CALENDARS_URL = f"{MICROSOFT_GRAPH_BASE_URL}/me/calendars"
"""Graph 当前用户日历集合根路径。"""

MICROSOFT_CALENDAR_WRITE_TIMEOUT_SECONDS = 15.0
"""单次写入或只读核对请求的总超时上限。"""

MICROSOFT_CALENDAR_WRITE_CONNECT_TIMEOUT_SECONDS = 3.0
"""建立 TLS/HTTP 连接的超时上限。"""

MICROSOFT_CALENDAR_RECONCILE_MAX_PAGES = 4
"""单轮创建核对最多读取的 CalendarView 页数。"""

MICROSOFT_CALENDAR_RECONCILE_MAX_CANDIDATES = 10
"""单轮创建核对最多检查的事件数量。"""

MICROSOFT_CALENDAR_RETRY_AFTER_CAP_SECONDS = 300
"""写结果允许持久化的最大 Retry-After 秒数。"""

_FRACTIONAL_SECONDS = re.compile(r"(\.\d{6})\d+(?=Z$|[+-]\d{2}:\d{2}$|$)")
_SAFE_OUTLOOK_HOSTS = frozenset(
    {
        "outlook.example.test",
        "outlook.office.example.test",
        "outlook.com",
        "outlook.live.com",
        "outlook.office.com",
        "outlook.office365.com",
    }
)
_EVENT_RESPONSE_SELECT = (
    "id,transactionId,subject,body,location,start,end,isAllDay,attendees,type,"
    "seriesMasterId,recurrence,isCancelled,isOrganizer,changeKey,webLink"
)
"""严格确认事件状态所需的最小 Graph 属性集合；ETag 由 OData metadata 单独返回。"""

_RefreshAccessToken = Callable[[], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class _GraphVersion:
    """保存可比较的 canonical token 与可直接发送的当前条件 token。

    Attributes:
        canonical: 去掉合法 ETag wrapper 后的精确 opaque token，只用于恒等比较。
        conditional: 当前 Graph 响应提供且经严格校验的原始 ETag/changeKey，用于
            ``If-Match``；永不从冻结 base 重新拼装。
    """

    canonical: str
    conditional: str


def validate_graph_notification_policy(
    *,
    attendees: tuple[str, ...],
    policy: NotificationPolicy,
) -> None:
    """验证冻结通知策略能被 Graph Calendar 无损表达。

    Graph 创建或更新含参会人的事件时没有与 Google ``sendUpdates=none`` 等价的抑制开关。
    因此个人事件的 ``none`` 可以安全表达，而含参会人的 ``none`` 必须在审批记录生成前
    拒绝，不能把用户选择静默提升为 ``all``。

    Args:
        attendees: 已由领域层规范化、去重的参会人 tuple。
        policy: 冻结的供应商中立通知策略。

    Raises:
        TypeError: 参数不是精确领域类型。
        StateConflictError: 含参会人事件请求 ``none``，Graph 无法无损映射。
    """
    if type(attendees) is not tuple:
        raise TypeError("calendar attendees must be a tuple")
    if type(policy) is not NotificationPolicy:
        raise TypeError("calendar notification policy is invalid")
    if attendees and policy is NotificationPolicy.NONE:
        raise StateConflictError(
            error_code="calendar_notification_mapping_unsupported",
            message="Microsoft calendar notification policy cannot be mapped safely",
        )


class MicrosoftCalendarWriteAdapter:
    """执行 Graph 日历创建、条件更新/恢复及只读结果核对。

    Args:
        connection_id: bearer token 所属的精确 Microsoft 连接；所有命令入口都必须与其
            恒等匹配，防止多连接场景把一个账户的凭据用于另一个账户的冻结命令。
        access_token: 已由外层 OAuth coordinator 解密并校验的短期 bearer token。
        client: 可选注入的 HTTPX client，主要供完全合成的契约测试使用。
        refresh_access_token: 统一组合签名保留项；adapter 绝不隐式调用它，以免 401 或
            未知写结果越过持久 refresh fence 后产生第二次外部写入。
    """

    provider = "microsoft"

    def __init__(
        self,
        *,
        connection_id: UUID,
        access_token: str,
        client: httpx.AsyncClient | None = None,
        refresh_access_token: _RefreshAccessToken | None = None,
    ) -> None:
        """保存受控连接事实，不触发网络或 OAuth 刷新。"""
        if type(connection_id) is not UUID:
            raise TypeError("Microsoft Calendar connection_id must be UUID")
        self._connection_id = connection_id
        self._access_token = _require_token(access_token)
        self._client = client
        self._refresh_access_token = refresh_access_token

    def update_access_token(self, access_token: str) -> None:
        """在外层 coordinator 已完成持久 CAS 后替换内存 token。"""
        self._access_token = _require_token(access_token)

    def validate_for_approval(self, command: TrustedCommand) -> ApprovalPreflightResult:
        """纯本地验证时区与通知策略，审批前不访问 Graph。

        Args:
            command: 已由应用层严格解析的冻结日历命令。

        Returns:
            无 warning 的固定预检结果；Graph 不支持的组合直接抛稳定冲突。

        Raises:
            StateConflictError: 含参会人的 ``notification_policy=none`` 无法表达。
            TypeError: 命令不是精确日历命令类型。
            ValueError: 供应商标识或时间字段不能安全表达。
        """
        normalized = self._validated_bound_command(command)
        validate_graph_notification_policy(
            attendees=normalized.attendees,
            policy=normalized.notification_policy,
        )
        # 映射函数同时验证 IANA -> Windows -> IANA 的无损往返；预检只需要建立
        # 表达能力证明，不把供应商名称写回冻结命令。
        to_windows_timezone(normalized.timezone)
        return ApprovalPreflightResult()

    async def execute(self, command: TrustedCommand) -> ProviderWriteOutcome:
        """执行单条已认领命令，任何真实写请求最多发送一次。

        Args:
            command: 已通过哈希绑定、人工审批与持久 request-start 的日历命令。

        Returns:
            内容无关的供应商三态结果。

        Raises:
            StateConflictError: 通知策略无法被 Graph 无损表达。
            TypeError: 命令不是精确日历类型。
            ValueError: 冻结字段不能安全映射到 Graph。
        """
        normalized = self._validated_bound_command(command)
        validate_graph_notification_policy(
            attendees=normalized.attendees,
            policy=normalized.notification_policy,
        )
        if type(normalized) is CalendarCreateCommand:
            return await self._execute_create(normalized)
        if type(normalized) in (CalendarUpdateCommand, CalendarRestoreCommand):
            return await self._execute_conditional_update(
                cast(CalendarUpdateCommand | CalendarRestoreCommand, normalized)
            )
        raise TypeError("Microsoft Calendar command type is unsupported")

    async def reconcile(
        self,
        command: TrustedCommand,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """只读核对精确事件或创建窗口，绝不重放任何日历写请求。"""
        normalized = self._validated_bound_command(command)
        validate_graph_notification_policy(
            attendees=normalized.attendees,
            policy=normalized.notification_policy,
        )
        if type(execution) is not ExecutionReference:
            raise TypeError("Microsoft Calendar reconciliation requires ExecutionReference")
        if (
            execution.operation_id != normalized.operation_id
            or execution.provider != self.provider
            or execution.tool_name != normalized.action
        ):
            raise ValueError("Microsoft Calendar reconciliation binding is invalid")

        if execution.provider_resource_id is not None:
            event_id = _safe_required_identifier(
                execution.provider_resource_id,
                field="provider_resource_id",
                maximum=512,
            )
            if type(normalized) in (CalendarUpdateCommand, CalendarRestoreCommand) and (
                event_id
                != cast(
                    CalendarUpdateCommand | CalendarRestoreCommand, normalized
                ).provider_event_id
            ):
                raise ValueError("Microsoft Calendar reconciliation resource binding is invalid")
            return await self._reconcile_exact_event(normalized, event_id)
        if type(normalized) is CalendarCreateCommand:
            return await self._reconcile_create_window(normalized)
        update = cast(CalendarUpdateCommand | CalendarRestoreCommand, normalized)
        return await self._reconcile_exact_event(update, update.provider_event_id)

    def _validated_bound_command(self, command: TrustedCommand) -> CalendarCommand:
        """验证冻结命令完整且属于当前 bearer token 的精确连接。

        Args:
            command: 由审批或执行用例提供的冻结可信命令。

        Returns:
            已通过供应商字段和连接归属校验的日历命令。

        Raises:
            TypeError: 命令类型或字段类型不满足供应商边界。
            ValueError: 命令连接与当前凭据连接不一致，或命令字段不合法。

        Notes:
            三个公开入口都必须先经过本方法，确保连接不匹配时不会构造或发送任何
            Graph 请求；registry 即使按 provider/action 返回了错误实例也会在这里闭锁。
        """
        normalized = _validated_calendar_command(command)
        if normalized.connection_id != self._connection_id:
            raise ValueError("Microsoft Calendar command connection binding is invalid")
        return normalized

    async def _reconcile_exact_event(
        self,
        command: CalendarCommand,
        event_id: str,
    ) -> ProviderWriteOutcome:
        """读取单个已绑定事件，并要求完整状态与版本证明本次写入。"""
        correlation_id = str(command.operation_id)
        response, transport_error = await self._request_read(
            _event_url(command.calendar_id, event_id),
            params={"$select": _EVENT_RESPONSE_SELECT},
            headers=_event_response_headers(command),
        )
        if transport_error is not None or response is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code=transport_error or "microsoft_calendar_reconciliation_unavailable",
            )
        request_id = _provider_request_id(response)
        if response.status_code != 200:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code=_reconciliation_read_error(response.status_code),
            )
        payload = _json_object(response)
        if payload is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="microsoft_calendar_reconciliation_invalid_response",
            )
        return _reconciled_event_outcome(
            payload,
            command=command,
            expected_id=event_id,
            provider_request_id=request_id,
        )

    async def _reconcile_create_window(
        self,
        command: CalendarCreateCommand,
    ) -> ProviderWriteOutcome:
        """在冻结时间窗内有界分页，并在本地匹配唯一 transactionId。"""
        correlation_id = str(command.operation_id)
        start, end = _calendar_view_window(command)
        current_url = _calendar_view_url(command.calendar_id)
        params: Mapping[str, str] | None = {
            "startDateTime": start,
            "endDateTime": end,
            "$select": _EVENT_RESPONSE_SELECT,
        }
        seen_urls: set[str] = set()
        candidates: list[Mapping[str, object]] = []
        last_request_id: str | None = None
        item_count = 0

        for _ in range(MICROSOFT_CALENDAR_RECONCILE_MAX_PAGES):
            if current_url in seen_urls:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_calendar_reconciliation_pagination_invalid",
                )
            seen_urls.add(current_url)
            response, transport_error = await self._request_read(
                current_url,
                params=params,
                headers=_event_response_headers(command),
            )
            params = None
            if transport_error is not None or response is None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code=transport_error or "microsoft_calendar_reconciliation_unavailable",
                )
            last_request_id = _provider_request_id(response)
            if response.status_code != 200:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code=_reconciliation_read_error(response.status_code),
                )
            payload = _json_object(response)
            if payload is None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_calendar_reconciliation_invalid_response",
                )
            raw_values = payload.get("value")
            if not isinstance(raw_values, list) or payload.get("@odata.deltaLink") is not None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_calendar_reconciliation_invalid_response",
                )
            item_count += len(raw_values)
            if item_count > MICROSOFT_CALENDAR_RECONCILE_MAX_CANDIDATES:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_calendar_reconciliation_candidate_limit",
                )
            for item in raw_values:
                if not isinstance(item, Mapping):
                    return _unknown_outcome(
                        correlation_id=correlation_id,
                        provider_request_id=last_request_id,
                        error_code="microsoft_calendar_reconciliation_invalid_response",
                    )
                candidate = cast(Mapping[str, object], item)
                if candidate.get("transactionId") == correlation_id:
                    candidates.append(candidate)

            next_link = payload.get("@odata.nextLink")
            if next_link is None:
                break
            safe_next = _validate_calendar_view_url(next_link, command.calendar_id)
            if safe_next is None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_calendar_reconciliation_pagination_invalid",
                )
            current_url = safe_next
        else:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=last_request_id,
                error_code="microsoft_calendar_reconciliation_page_limit",
            )

        if len(candidates) != 1:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=last_request_id,
                error_code=(
                    "microsoft_calendar_event_not_found"
                    if not candidates
                    else "microsoft_calendar_reconciliation_ambiguous"
                ),
            )
        provider_id = _safe_identifier(candidates[0].get("id"), maximum=512)
        if provider_id is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=last_request_id,
                error_code="microsoft_calendar_reconciliation_mismatch",
            )
        return _reconciled_event_outcome(
            candidates[0],
            command=command,
            expected_id=provider_id,
            provider_request_id=last_request_id,
        )

    async def _execute_create(
        self,
        command: CalendarCreateCommand,
    ) -> ProviderWriteOutcome:
        """向精确目标日历发送一次带 transactionId 的创建 POST。"""
        correlation_id = str(command.operation_id)
        try:
            response = await self._request(
                "POST",
                _events_url(command.calendar_id),
                json=_event_body(command, include_transaction_id=True),
                headers=_event_response_headers(command),
            )
        except (httpx.ConnectTimeout, httpx.ConnectError):
            # 建连阶段失败能证明请求字节没有到达 Graph，是唯一可直接安全重试的传输失败。
            return _not_applied_outcome(
                correlation_id=correlation_id,
                retryable=True,
                error_code="microsoft_connect_failed_before_send",
            )
        except httpx.TimeoutException:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="microsoft_calendar_write_timeout",
            )
        except httpx.RequestError:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="microsoft_calendar_write_request_failed",
            )

        request_id = _provider_request_id(response)
        if response.status_code == 201:
            return _success_from_response(response, command=command)
        if response.status_code == 409:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="microsoft_calendar_conflict",
            )
        return _classify_write_response(
            response,
            correlation_id=correlation_id,
            provider_request_id=request_id,
            action_prefix="microsoft_calendar_create",
        )

    async def _execute_conditional_update(
        self,
        command: CalendarCommand,
    ) -> ProviderWriteOutcome:
        """读取精确当前事件并只发送一次带版本条件的完整 PATCH。"""
        if not isinstance(command, (CalendarUpdateCommand, CalendarRestoreCommand)):
            raise TypeError("calendar.create cannot use conditional update")
        # 使用公共命令 alias 并保留精确类型约束，避免 helper 绕过领域入口接受子类。
        if type(command) not in (CalendarUpdateCommand, CalendarRestoreCommand):
            raise TypeError("calendar conditional update command type is unsupported")
        correlation_id = str(command.operation_id)
        event_url = _event_url(command.calendar_id, command.provider_event_id)
        current, transport_error = await self._request_read(
            event_url,
            params={"$select": _EVENT_RESPONSE_SELECT},
            headers=_event_response_headers(command),
        )
        if transport_error is not None or current is None:
            # 这里只是 PATCH 前的只读版本检查；无论 GET 是否已经到达 Graph，写请求都
            # 尚未发送，因此可以证明本次外部写未应用并安全地交给持久重试策略。
            return _not_applied_outcome(
                correlation_id=correlation_id,
                retryable=True,
                error_code=transport_error or "microsoft_calendar_current_event_unavailable",
            )
        request_id = _provider_request_id(current)
        if current.status_code == 404:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="microsoft_calendar_event_not_found",
            )
        if current.status_code in {401, 403}:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code=(
                    "microsoft_reauthorization_required"
                    if current.status_code == 401
                    else "microsoft_calendar_permission_required"
                ),
            )
        if current.status_code == 429:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                retryable=True,
                retry_after_seconds=_retry_after(current),
                error_code="microsoft_calendar_rate_limited",
            )
        if current.status_code >= 500:
            # 供应商只拒绝或未完成前置 GET；PATCH 仍为零调用，所以服务恢复后可安全
            # 重试整个动作，但 5xx 的语义不用于推断任何事件事实。
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                retryable=True,
                error_code="microsoft_calendar_current_event_unavailable",
            )
        if current.status_code != 200:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="microsoft_calendar_current_event_unavailable",
            )
        payload = _json_object(current)
        if payload is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="microsoft_calendar_malformed_response",
            )
        precondition_error, current_version = _current_event_precondition(payload, command)
        if precondition_error is not None or current_version is None:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code=(precondition_error or "microsoft_calendar_event_version_invalid"),
            )

        try:
            response = await self._request(
                "PATCH",
                event_url,
                json=_event_body(command, include_transaction_id=False),
                headers={
                    **_event_response_headers(command),
                    "If-Match": current_version.conditional,
                },
            )
        except (httpx.ConnectTimeout, httpx.ConnectError):
            return _not_applied_outcome(
                correlation_id=correlation_id,
                retryable=True,
                error_code="microsoft_connect_failed_before_send",
            )
        except httpx.TimeoutException:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="microsoft_calendar_write_timeout",
            )
        except httpx.RequestError:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="microsoft_calendar_write_request_failed",
            )

        response_request_id = _provider_request_id(response) or request_id
        if response.status_code == 200:
            return _success_from_response(response, command=command)
        if response.status_code in {409, 412}:
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=response_request_id,
                error_code=(
                    "calendar_event_version_conflict"
                    if response.status_code == 412
                    else "microsoft_calendar_conflict"
                ),
            )
        return _classify_write_response(
            response,
            correlation_id=correlation_id,
            provider_request_id=response_request_id,
            action_prefix=f"microsoft_{command.action.replace('.', '_')}",
        )

    async def _request_read(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[httpx.Response | None, str | None]:
        """执行一次只读 GET，并把传输异常压缩为不含响应内容的稳定错误码。"""
        try:
            return await self._request("GET", url, params=params, headers=headers), None
        except (httpx.ConnectTimeout, httpx.ConnectError):
            return None, "microsoft_connect_failed"
        except httpx.TimeoutException:
            return None, "microsoft_timeout"
        except httpx.RequestError:
            return None, "microsoft_request_failed"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """用固定认证、超时和禁止重定向策略发送一条请求。"""
        request_headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
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
            MICROSOFT_CALENDAR_WRITE_TIMEOUT_SECONDS,
            connect=MICROSOFT_CALENDAR_WRITE_CONNECT_TIMEOUT_SECONDS,
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
    """通过显式 HTTP 方法调用注入 client，禁止动作字符串选择任意动词。"""
    normalized_params = dict(params) if params is not None else None
    normalized_json = dict(json) if json is not None else None
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
            json=normalized_json,
            headers=normalized_headers,
            follow_redirects=False,
        )
    if method == "PATCH":
        return await client.patch(
            url,
            params=normalized_params,
            json=normalized_json,
            headers=normalized_headers,
            follow_redirects=False,
        )
    raise ValueError("Microsoft Calendar HTTP method is unsupported")


def _validated_calendar_command(command: TrustedCommand) -> CalendarCommand:
    """在供应商入口重新收窄冻结命令，不修正任何已审批值。

    领域对象通常已经通过构造器校验；这里仍重读所有会进入 URL、Header 或 JSON 的字段，
    防止被低层反射修改的 frozen dataclass 绕过供应商边界。
    """
    if type(command) not in {
        CalendarCreateCommand,
        CalendarUpdateCommand,
        CalendarRestoreCommand,
    }:
        raise TypeError("Microsoft Calendar action requires a CalendarCommand")
    normalized = cast(CalendarCommand, command)
    if type(normalized.operation_id) is not UUID or type(normalized.connection_id) is not UUID:
        raise TypeError("calendar operation and connection IDs must be UUID")
    _safe_required_identifier(normalized.calendar_id, field="calendar_id", maximum=512)
    _validate_text(normalized.title, field="title", maximum=255, allow_empty=False)
    _validate_optional_text(normalized.description, field="description", maximum=100_000)
    _validate_optional_text(normalized.location, field="location", maximum=16_384)
    if type(normalized.all_day) is not bool:
        raise TypeError("calendar all_day is invalid")
    if type(normalized.notification_policy) is not NotificationPolicy:
        raise TypeError("calendar notification policy is invalid")
    if type(normalized.attendees) is not tuple:
        raise TypeError("calendar attendees must be a tuple")
    try:
        canonical_attendees = tuple(
            normalize_mailbox_address(address) for address in normalized.attendees
        )
    except (TypeError, ValueError):
        raise ValueError("calendar attendees are invalid") from None
    if (
        canonical_attendees != normalized.attendees
        or len(canonical_attendees) > 50
        or len(set(canonical_attendees)) != len(canonical_attendees)
    ):
        raise ValueError("calendar attendees must be normalized and unique")
    # 构造两端时间对象同时证明 IANA/Windows 往返和全天/定时表示严格匹配。
    graph_event_time(normalized.starts_at, normalized.timezone, normalized.all_day)
    graph_event_time(normalized.ends_at, normalized.timezone, normalized.all_day)
    if type(normalized) is CalendarCreateCommand:
        if normalized.client_event_id != calendar_client_event_id(normalized.operation_id):
            raise ValueError("calendar client_event_id is not operation-derived")
    else:
        update = cast(CalendarUpdateCommand | CalendarRestoreCommand, normalized)
        _safe_required_identifier(update.provider_event_id, field="provider_event_id", maximum=255)
        _safe_required_identifier(update.base_etag, field="base_etag", maximum=255)
    return normalized


def graph_event_time(
    value: datetime | date,
    timezone: str,
    all_day: bool,
) -> dict[str, str]:
    """把冻结日历时间映射为 Graph ``dateTimeTimeZone``。

    全天事件直接把领域 ``date`` 拼成目标时区的本地午夜，不先转 UTC；定时事件按明确
    IANA 区域转换为本地墙上时间。Graph 的 ``dateTimeTimeZone`` 写入形状不携带 fold，
    因此回拨重叠小时会在移除 offset 前被拒绝，不能让两个冻结瞬间压缩成相同载荷。

    Args:
        value: 全天纯 ``date`` 或带 offset 的 ``datetime``。
        timezone: 冻结 IANA 名称。
        all_day: 是否采用全天表示。

    Returns:
        可直接发送给 Graph 的 ``dateTime``/``timeZone`` 对象。

    Raises:
        PermanentProviderError: IANA/Windows 映射未知、不能无损往返，或本地时刻有多个
            DST fold 而 Graph 无法唯一表达。
        TypeError: 时间值与 ``all_day`` 表示不匹配。
        ValueError: 定时值没有明确瞬间或无法转换。
    """
    if type(all_day) is not bool:
        raise TypeError("calendar all_day must be bool")
    windows_timezone = to_windows_timezone(timezone)
    canonical_iana = to_iana_timezone(windows_timezone)
    if all_day:
        if type(value) is not date:
            raise TypeError("all-day calendar time must be a date")
        return {
            "dateTime": f"{value.isoformat()}T00:00:00",
            "timeZone": windows_timezone,
        }
    if not isinstance(value, datetime):
        raise TypeError("timed calendar time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timed calendar time must be timezone-aware")
    try:
        zone = ZoneInfo(canonical_iana)
        expected_utc = value.astimezone(UTC)
        local = value.astimezone(zone).replace(tzinfo=None)
        candidates = _valid_local_candidates(local, zone)
    except (OverflowError, ValueError, ZoneInfoNotFoundError):
        raise ValueError("timed calendar time cannot be represented") from None
    # Graph 写入只保留墙上时间和 Windows 区域，不保留 offset/fold。只有唯一候选且能
    # 原样恢复冻结 UTC 瞬间时才可批准；回拨小时的两个 fold 均必须 fail closed。
    if len(candidates) != 1 or candidates[0].astimezone(UTC) != expected_utc:
        raise PermanentProviderError(
            error_code="calendar_timezone_mapping_unsupported",
            message="Calendar timezone mapping is unsupported",
        )
    timespec = "microseconds" if local.microsecond else "seconds"
    return {
        "dateTime": local.isoformat(timespec=timespec),
        "timeZone": windows_timezone,
    }


def _event_body(
    command: CalendarCommand,
    *,
    include_transaction_id: bool,
) -> dict[str, object]:
    """仅由冻结命令构造 Graph 完整期望状态，空可选字段也显式用于清除。"""
    body: dict[str, object] = {
        "subject": command.title,
        "body": {"contentType": "text", "content": command.description or ""},
        "location": {"displayName": command.location or ""},
        "start": graph_event_time(command.starts_at, command.timezone, command.all_day),
        "end": graph_event_time(command.ends_at, command.timezone, command.all_day),
        "isAllDay": command.all_day,
        "attendees": [
            {"emailAddress": {"address": address}, "type": "required"}
            for address in command.attendees
        ],
    }
    if include_transaction_id:
        if type(command) is not CalendarCreateCommand:
            raise TypeError("only calendar.create accepts transactionId")
        body["transactionId"] = str(command.operation_id)
    return body


def _event_response_headers(command: CalendarCommand) -> dict[str, str]:
    """要求 Graph 用冻结 Windows 时区与纯文本返回事件字段，供严格核对。"""
    windows_timezone = to_windows_timezone(command.timezone)
    return {"Prefer": (f'outlook.timezone="{windows_timezone}", outlook.body-content-type="text"')}


def _events_url(calendar_id: str) -> str:
    """构造精确目标日历事件 collection URL，opaque ID 只占一个编码路径段。"""
    return f"{MICROSOFT_CALENDARS_URL}/{quote(calendar_id, safe='')}/events"


def _event_url(calendar_id: str, event_id: str) -> str:
    """构造精确 Graph event URL，不让供应商 ID 改变资源层级。"""
    return f"{_events_url(calendar_id)}/{quote(event_id, safe='')}"


def _calendar_view_url(calendar_id: str) -> str:
    """构造仅用于创建结果核对的非 Delta CalendarView URL。"""
    return f"{MICROSOFT_CALENDARS_URL}/{quote(calendar_id, safe='')}/calendarView"


def _calendar_view_window(command: CalendarCreateCommand) -> tuple[str, str]:
    """把冻结事件边界转换为显式 UTC CalendarView 查询参数。"""
    if command.all_day:
        zone = ZoneInfo(to_iana_timezone(to_windows_timezone(command.timezone)))
        starts_at = datetime.combine(cast(date, command.starts_at), datetime.min.time(), zone)
        ends_at = datetime.combine(cast(date, command.ends_at), datetime.min.time(), zone)
    else:
        starts_at = cast(datetime, command.starts_at)
        ends_at = cast(datetime, command.ends_at)
    try:
        return starts_at.astimezone(UTC).isoformat(), ends_at.astimezone(UTC).isoformat()
    except (OverflowError, ValueError):
        raise ValueError("calendar reconciliation window is invalid") from None


def _validate_calendar_view_url(value: object, calendar_id: str) -> str | None:
    """验证 Graph nextLink 仍绑定同一目标日历的非 Delta CalendarView。"""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 8192
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        query = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "graph.microsoft.com"
        or parsed.netloc != "graph.microsoft.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or parsed.path != urlsplit(_calendar_view_url(calendar_id)).path
        or not parsed.query
        or any(name.casefold() == "$filter" for name, _ in query)
    ):
        return None
    return value


def _reconciliation_read_error(status_code: int) -> str:
    """把核对 GET 的非 200 响应收敛为内容无关错误码。"""
    if status_code == 401:
        return "microsoft_reauthorization_required"
    if status_code == 403:
        return "microsoft_calendar_permission_required"
    if status_code == 404:
        return "microsoft_calendar_event_not_found"
    if status_code == 429:
        return "microsoft_calendar_rate_limited"
    if status_code >= 500:
        return "microsoft_calendar_service_unavailable"
    return "microsoft_calendar_reconciliation_rejected"


def _current_event_precondition(
    payload: Mapping[str, object],
    command: CalendarCommand,
) -> tuple[str | None, _GraphVersion | None]:
    """检查条件 PATCH 前的身份、可写事实与 canonical 版本绑定。

    Returns:
        ``(error_code, current_version)``；只有 error 为空且版本存在时调用方才可
        使用 provider 原始条件 token 发送 PATCH。
    """
    if not isinstance(command, (CalendarUpdateCommand, CalendarRestoreCommand)):
        raise TypeError("calendar.create cannot use update precondition")
    # 与执行入口保持同一精确类型约束，同时让类型检查器收窄公开 CalendarCommand alias。
    if type(command) not in (CalendarUpdateCommand, CalendarRestoreCommand):
        raise TypeError("calendar conditional update command type is unsupported")
    if _safe_identifier(payload.get("id"), maximum=512) != command.provider_event_id:
        return "microsoft_calendar_current_event_mismatch", None
    fact_error = _provider_event_fact_error(payload)
    if fact_error is not None:
        return fact_error, None
    current_version, version_error = _provider_version(payload)
    if version_error is not None or current_version is None:
        return version_error or "microsoft_calendar_event_version_invalid", None
    base_version = _normalize_graph_version(command.base_etag, source="base")
    if base_version is None:
        return "microsoft_calendar_event_version_invalid", None
    if current_version.canonical != base_version.canonical:
        return "calendar_event_version_conflict", None
    return None, current_version


def _normalize_graph_version(
    value: object,
    *,
    source: Literal["etag", "change_key", "base"],
) -> _GraphVersion | None:
    """严格解析 Graph ETag wrapper、裸 changeKey 或冻结 base version。

    Args:
        value: 未信任的 provider 字段或已解密冻结 base。
        source: ``etag`` 只接受强/弱 quoted ETag，``change_key`` 只接受裸 token，
            ``base`` 接受这两种已冻结表示。

    Returns:
        可安全比较及用于条件 Header 的版本；空、超长、非 ASCII、控制字符、通配符、
        多值逗号、畸形 wrapper 或嵌套引号均返回 ``None``。
    """
    if (
        type(value) is not str
        or not value
        or len(value) > 255
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        return None
    raw = cast(str, value)
    if source == "change_key":
        if '"' in raw or raw.startswith("W/"):
            return None
        canonical = raw
    elif raw.startswith('W/"'):
        if len(raw) < 5 or not raw.endswith('"'):
            return None
        canonical = raw[3:-1]
    elif raw.startswith('"'):
        if len(raw) < 3 or not raw.endswith('"'):
            return None
        canonical = raw[1:-1]
    else:
        if source == "etag" or raw.startswith("W/"):
            return None
        canonical = raw
    # DQUOTE 会把一个 validator 改写成嵌套/多段语法；逗号和通配符也不能从版本
    # 字段升级为 If-Match 的多值或任意匹配语义。
    if not canonical or canonical == "*" or '"' in canonical or "," in canonical:
        return None
    return _GraphVersion(canonical=canonical, conditional=raw)


def _provider_version(
    payload: Mapping[str, object],
) -> tuple[_GraphVersion | None, str | None]:
    """交叉验证同一 Graph event 的 ETag 与 changeKey，不允许择一掩盖冲突。

    Returns:
        ``(version, error_code)``。两个字段均缺失返回稳定 missing；任一存在字段
        畸形或 canonical 不同返回稳定 invalid；等价时优先保留文档化 ETag wrapper
        作为条件 token，仅有 changeKey 时保留其裸值。
    """
    raw_etag = payload.get("@odata.etag")
    raw_change_key = payload.get("changeKey")
    has_etag = raw_etag is not None
    has_change_key = raw_change_key is not None
    if not has_etag and not has_change_key:
        return None, "microsoft_calendar_event_version_missing"

    etag = _normalize_graph_version(raw_etag, source="etag") if has_etag else None
    change_key = (
        _normalize_graph_version(raw_change_key, source="change_key") if has_change_key else None
    )
    if (has_etag and etag is None) or (has_change_key and change_key is None):
        return None, "microsoft_calendar_event_version_invalid"
    if etag is not None and change_key is not None and etag.canonical != change_key.canonical:
        return None, "microsoft_calendar_event_version_invalid"
    return etag or change_key, None


def _success_from_response(
    response: httpx.Response,
    *,
    command: CalendarCommand,
) -> ProviderWriteOutcome:
    """验证成功响应的资源、关联、完整状态和版本事实后确认 applied。"""
    request_id = _provider_request_id(response)
    payload = _json_object(response)
    if payload is None:
        return _unknown_outcome(
            correlation_id=str(command.operation_id),
            provider_request_id=request_id,
            error_code="microsoft_calendar_malformed_response",
        )
    expected_id = (
        cast(CalendarUpdateCommand | CalendarRestoreCommand, command).provider_event_id
        if type(command) in (CalendarUpdateCommand, CalendarRestoreCommand)
        else None
    )
    return _confirmed_event_outcome(
        payload,
        command=command,
        expected_id=expected_id,
        provider_request_id=request_id,
        require_new_version=False,
        mismatch_error="microsoft_calendar_malformed_response",
    )


def _reconciled_event_outcome(
    payload: Mapping[str, object],
    *,
    command: CalendarCommand,
    expected_id: str,
    provider_request_id: str | None,
) -> ProviderWriteOutcome:
    """以核对语义调用共享事件确认器；修改还必须观察到新版本。"""
    return _confirmed_event_outcome(
        payload,
        command=command,
        expected_id=expected_id,
        provider_request_id=provider_request_id,
        require_new_version=type(command) in (CalendarUpdateCommand, CalendarRestoreCommand),
        mismatch_error="microsoft_calendar_reconciliation_mismatch",
    )


def _confirmed_event_outcome(
    payload: Mapping[str, object],
    *,
    command: CalendarCommand,
    expected_id: str | None,
    provider_request_id: str | None,
    require_new_version: bool,
    mismatch_error: str,
) -> ProviderWriteOutcome:
    """共享成功/核对确认规则，只有完整字段、关联和版本事实才提升为 applied。"""
    correlation_id = str(command.operation_id)
    provider_id = _safe_identifier(payload.get("id"), maximum=512)
    provider_version, provider_version_error = _provider_version(payload)
    error_code: str | None = None
    if provider_id is None or not _event_matches_command(payload, command):
        error_code = mismatch_error
    elif expected_id is not None and provider_id != expected_id:
        error_code = "microsoft_calendar_resource_mismatch"
    elif type(command) is CalendarCreateCommand and payload.get("transactionId") != correlation_id:
        error_code = "microsoft_calendar_correlation_mismatch"
    elif provider_version_error is not None or provider_version is None:
        error_code = "microsoft_calendar_version_unconfirmed"
    elif require_new_version:
        update = cast(CalendarUpdateCommand | CalendarRestoreCommand, command)
        base_version = _normalize_graph_version(update.base_etag, source="base")
        if base_version is None or provider_version.canonical == base_version.canonical:
            error_code = "microsoft_calendar_version_unconfirmed"
    if error_code is not None:
        return _unknown_outcome(
            correlation_id=correlation_id,
            provider_request_id=provider_request_id,
            error_code=error_code,
        )
    return _applied_outcome(
        correlation_id=correlation_id,
        provider_resource_id=cast(str, provider_id),
        provider_request_id=provider_request_id,
        provider_url=_safe_provider_url(payload.get("webLink")),
    )


def _event_matches_command(payload: Mapping[str, object], command: CalendarCommand) -> bool:
    """严格比较 Graph event 与全部冻结可写字段，不使用供应商默认值猜测。"""
    if _provider_event_fact_error(payload) is not None:
        return False
    if _safe_identifier(payload.get("id"), maximum=512) is None:
        return False
    if payload.get("subject") != command.title:
        return False
    body = payload.get("body")
    if (
        not isinstance(body, Mapping)
        or body.get("contentType") not in {"text", "Text"}
        or not _optional_text_matches(body.get("content"), command.description)
    ):
        return False
    location = payload.get("location")
    if not isinstance(location, Mapping) or not _optional_text_matches(
        location.get("displayName"), command.location
    ):
        return False
    if payload.get("isAllDay") is not command.all_day:
        return False
    if not _provider_time_matches(
        payload.get("start"),
        command.starts_at,
        command.timezone,
        command.all_day,
    ):
        return False
    if not _provider_time_matches(
        payload.get("end"),
        command.ends_at,
        command.timezone,
        command.all_day,
    ):
        return False
    return _attendees_match(payload.get("attendees"), command.attendees)


def _provider_event_fact_error(payload: Mapping[str, object]) -> str | None:
    """验证 Graph 删除、重复与权限事实，未知形状一律 fail closed。"""
    if "@removed" in payload and payload.get("@removed") is not None:
        return "microsoft_calendar_event_deleted"
    if type(payload.get("isCancelled")) is not bool:
        return "microsoft_calendar_malformed_response"
    if payload.get("isCancelled") is True:
        return "microsoft_calendar_event_deleted"
    event_type = payload.get("type")
    if type(event_type) is not str:
        return "microsoft_calendar_malformed_response"
    if event_type not in {"singleInstance", "occurrence", "exception", "seriesMaster"}:
        return "microsoft_calendar_malformed_response"
    if (
        event_type != "singleInstance"
        or payload.get("seriesMasterId") is not None
        or payload.get("recurrence") is not None
    ):
        return "calendar_recurring_event_unsupported"
    permission_facts: list[bool] = []
    for field_name in ("canEdit", "isOrganizer"):
        if field_name in payload:
            value = payload[field_name]
            if type(value) is not bool:
                return "microsoft_calendar_malformed_response"
            permission_facts.append(cast(bool, value))
    if not permission_facts:
        return "microsoft_calendar_malformed_response"
    if not any(permission_facts):
        return "microsoft_calendar_event_not_editable"
    return None


def _provider_time_matches(
    value: object,
    expected: datetime | date,
    timezone: str,
    all_day: bool,
) -> bool:
    """按 Graph 墙上时间与声明区域比较，不把全天日期转换成 UTC 日期。"""
    if not isinstance(value, Mapping):
        return False
    raw_datetime = value.get("dateTime")
    raw_timezone = value.get("timeZone")
    if not isinstance(raw_datetime, str) or not isinstance(raw_timezone, str):
        return False
    try:
        expected_windows = to_windows_timezone(timezone)
        canonical_iana = to_iana_timezone(raw_timezone)
        if raw_timezone != expected_windows or canonical_iana != to_iana_timezone(expected_windows):
            return False
        normalized = _FRACTIONAL_SECONDS.sub(r"\1", raw_datetime)
        parsed = datetime.fromisoformat(
            normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
        )
    except (PermanentProviderError, TypeError, ValueError, OverflowError):
        return False
    if all_day:
        return (
            type(expected) is date
            and parsed.tzinfo is None
            and parsed.time() == datetime.min.time()
            and parsed.date() == expected
        )
    if (
        not isinstance(expected, datetime)
        or expected.tzinfo is None
        or expected.utcoffset() is None
    ):
        return False
    try:
        zone = ZoneInfo(canonical_iana)
    except ZoneInfoNotFoundError:
        return False
    local_value = parsed.replace(tzinfo=None)
    candidates: tuple[datetime, ...]
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        # Graph 偶尔返回同时含 offset 与独立 timeZone 的值；两者必须描述同一墙上时间，
        # 不能只因 offset 对应的 UTC 瞬间碰巧相同就忽略矛盾的区域事实。
        zoned = parsed.astimezone(zone)
        if zoned.replace(tzinfo=None) != local_value or zoned.utcoffset() != parsed.utcoffset():
            return False
        candidates = (parsed,)
    else:
        candidates = _valid_local_candidates(local_value, zone)
    try:
        return any(
            candidate.astimezone(UTC) == expected.astimezone(UTC) for candidate in candidates
        )
    except (OverflowError, ValueError):
        return False


def _valid_local_candidates(value: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    """返回能经 UTC 往返同一墙上时间的有限 DST fold 候选。"""
    candidates: list[datetime] = []
    seen_offsets: set[timedelta | None] = set()
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold)
        try:
            round_trip = candidate.astimezone(UTC).astimezone(zone)
        except (OverflowError, ValueError):
            continue
        if round_trip.replace(tzinfo=None) != value:
            continue
        offset = candidate.utcoffset()
        if offset in seen_offsets:
            continue
        seen_offsets.add(offset)
        candidates.append(candidate)
    return tuple(candidates)


def _attendees_match(value: object, expected: tuple[str, ...]) -> bool:
    """比较 Graph required attendee 集合，拒绝畸形、重复和额外地址。"""
    if not isinstance(value, list):
        return False
    actual: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") != "required":
            return False
        email_address = item.get("emailAddress")
        if not isinstance(email_address, Mapping):
            return False
        raw_address = email_address.get("address")
        if not isinstance(raw_address, str):
            return False
        try:
            address = normalize_mailbox_address(raw_address)
        except ValueError:
            return False
        if address != raw_address or address in actual:
            return False
        actual.append(address)
    return len(actual) == len(expected) and set(actual) == set(expected)


def _optional_text_matches(value: object, expected: str | None) -> bool:
    """把冻结 None 与 Graph 空字符串视为同一显式清空状态。"""
    return value == expected if expected is not None else value in {None, ""}


def _json_object(response: httpx.Response) -> Mapping[str, object] | None:
    """把供应商 JSON 顶层收窄为 mapping，不保留原始正文。"""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return cast(Mapping[str, object], payload)


def _provider_request_id(response: httpx.Response) -> str | None:
    """只提取有限 Graph request ID Header，绝不读取认证 Header。"""
    for name in ("request-id", "client-request-id", "x-ms-request-id"):
        value = _safe_identifier(response.headers.get(name), maximum=255)
        if value is not None:
            return value
    return None


def _safe_provider_url(value: object) -> str | None:
    """只保留无 userinfo/port/fragment 的已知 Outlook HTTPS 展示链接。"""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 2048
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _SAFE_OUTLOOK_HOSTS
        or parsed.netloc != parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or not parsed.path
    ):
        return None
    return value


def _retry_after(response: httpx.Response) -> int | None:
    """解析有界整数 Retry-After，畸形值不进入持久结果。"""
    try:
        value = int(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if 0 <= value <= MICROSOFT_CALENDAR_RETRY_AFTER_CAP_SECONDS else None


def _classify_write_response(
    response: httpx.Response,
    *,
    correlation_id: str,
    provider_request_id: str | None,
    action_prefix: str,
) -> ProviderWriteOutcome:
    """按 Graph 写响应把明确拒绝与语义未知结果分离。"""
    status = response.status_code
    if status in {408, 425} or 300 <= status < 400 or status >= 500:
        return _unknown_outcome(
            correlation_id=correlation_id,
            provider_request_id=provider_request_id,
            error_code=(
                f"{action_prefix}_service_unavailable"
                if status >= 500
                else f"{action_prefix}_ambiguous_response"
            ),
        )
    if 400 <= status < 500:
        retryable = status == 429
        if status == 401:
            error_code = "microsoft_reauthorization_required"
        elif status == 403:
            error_code = "microsoft_calendar_permission_required"
        elif status == 429:
            error_code = "microsoft_calendar_rate_limited"
        else:
            error_code = f"{action_prefix}_rejected"
        return _not_applied_outcome(
            correlation_id=correlation_id,
            provider_request_id=provider_request_id,
            retryable=retryable,
            retry_after_seconds=_retry_after(response) if retryable else None,
            error_code=error_code,
        )
    return _unknown_outcome(
        correlation_id=correlation_id,
        provider_request_id=provider_request_id,
        error_code=f"{action_prefix}_unexpected_response",
    )


def _safe_required_identifier(
    value: object,
    *,
    field: str,
    maximum: int,
) -> str:
    """验证 URL/Header opaque 标识，不在异常中回显原值。"""
    result = _safe_identifier(value, maximum=maximum)
    if result is None:
        raise ValueError(f"calendar {field} is invalid")
    return result


def _safe_identifier(value: object, *, maximum: int) -> str | None:
    """拒绝 dot-segment、空白、控制字符和超过边界的 opaque 标识。"""
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or value != value.strip()
        or len(value) > maximum
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        )
    ):
        return None
    return value


def _validate_text(value: object, *, field: str, maximum: int, allow_empty: bool) -> None:
    """验证标题等单行 Graph 文本，不回显正文内容。"""
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"calendar {field} is invalid")


def _validate_optional_text(value: object, *, field: str, maximum: int) -> None:
    """验证可空描述/地点；描述允许换行和水平制表，禁止其它控制字符。"""
    if value is None:
        return
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"calendar {field} is invalid")
    if any(
        (ord(character) < 0x20 and character not in {"\t", "\n", "\r"}) or ord(character) == 0x7F
        for character in value
    ):
        raise ValueError(f"calendar {field} is invalid")


def _applied_outcome(
    *,
    correlation_id: str,
    provider_resource_id: str,
    provider_request_id: str | None,
    provider_url: str | None,
) -> ProviderWriteOutcome:
    """构造已确认成功结果，集中保证 retry 字段为空。"""
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
    provider_request_id: str | None = None,
) -> ProviderWriteOutcome:
    """构造明确未应用结果；Retry-After 只随安全重试许可保留。"""
    return ProviderWriteOutcome(
        kind=ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds if retryable else None,
        provider_resource_id=None,
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
    """构造禁止自动写重试的 unknown 结果。"""
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


def _require_token(value: object) -> str:
    """验证 bearer token 长度与控制字符边界，错误绝不回显 token。"""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_OAUTH_TOKEN_LENGTH
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        )
    ):
        raise ValueError("Microsoft access token is invalid")
    return value


CalendarWriteAdapter = MicrosoftCalendarWriteAdapter

__all__ = [
    "MICROSOFT_CALENDAR_WRITE_CONNECT_TIMEOUT_SECONDS",
    "MICROSOFT_CALENDAR_WRITE_TIMEOUT_SECONDS",
    "MICROSOFT_GRAPH_BASE_URL",
    "CalendarWriteAdapter",
    "MicrosoftCalendarWriteAdapter",
    "validate_graph_notification_policy",
]
