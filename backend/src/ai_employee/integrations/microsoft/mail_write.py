"""实现 Microsoft Graph 直接 MIME 发信与 Sent Items 只读核对。

本模块是 Microsoft ``Mail.Send`` 与供应商无关可信命令之间的唯一真实写入边界。Graph
的 MIME 接口要求标准 Base64 的 ``text/plain`` 请求体，而不是 JSON ``message`` 包装；
每次执行最多发出一次 POST，响应不明确时只允许走本模块的 GET 核对路径。适配器不持有
数据库事务、不申请或刷新 OAuth scope、不创建 Graph 草稿，也不把供应商原始响应带出
integrations 边界。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import cast
from urllib.parse import quote, unquote_to_bytes, urlsplit
from uuid import UUID

import httpx

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.oauth import MAX_OAUTH_TOKEN_LENGTH
from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
    ExecutionReference,
    ProviderWriteOutcome,
)
from ai_employee.domain.actions import ProviderWriteOutcomeKind
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import (
    MailMode,
    MailSendCommand,
    ReplyThreadHeaders,
    normalize_mail_recipients,
    normalize_mailbox_address,
)
from ai_employee.integrations.mail_mime import build_mail_mime_base64, message_id_for

MICROSOFT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
"""Graph v1.0 的固定资源根路径。"""

MICROSOFT_SEND_MAIL_URL = f"{MICROSOFT_GRAPH_BASE_URL}/me/sendMail"
"""新邮件直接 MIME 发送端点。"""

MICROSOFT_SENT_MESSAGES_URL = f"{MICROSOFT_GRAPH_BASE_URL}/me/mailFolders/sentitems/messages"
"""Sent Items 只读核对端点。"""

MICROSOFT_MAIL_WRITE_TIMEOUT_SECONDS = 15.0
"""单次写入或核对请求的总超时上限。"""

MICROSOFT_MAIL_WRITE_CONNECT_TIMEOUT_SECONDS = 3.0
"""建立 TLS/HTTP 连接的超时上限。"""

MICROSOFT_MAIL_RECONCILE_MAX_PAGES = 4
"""单轮只读核对允许跟随的最大分页数。"""

MICROSOFT_MAIL_RECONCILE_MAX_CANDIDATES = 10
"""单轮核对允许检查的最大候选条目数。"""

MICROSOFT_MAIL_RETRY_AFTER_CAP_SECONDS = 300
"""持久化 Retry-After 允许的最大秒数。"""

_PREFER_HEADER = 'IdType="ImmutableId"'
_SENT_SELECT = "id,conversationId,internetMessageId,sentDateTime,webLink"
_EXPECTED_SENT_PATH = "/v1.0/me/mailFolders/sentitems/messages"
_SAFE_LINK_HOSTS = frozenset(
    {
        # 测试环境只允许这两个明确的合成主机；不能用 ``endswith`` 放宽到任意子域。
        "outlook.office.example.test",
        "outlook.example.test",
        # Graph 生产 webLink 的已知 Outlook 入口。主机必须逐字匹配这些条目，
        # 因而 ``attacker.office.com``、尾随点和伪造后缀都会被拒绝。
        "outlook.office.com",
        "outlook.office365.com",
        "outlook.live.com",
        "outlook.com",
    }
)
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ADDRESS_IN_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,63}(?![A-Za-z0-9.-])"
)
_SECRET_QUERY_KEY = re.compile(
    r"(?i)(?:^|[?&;])(?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"authorization|bearer|token|password|passwd|client[_-]?secret|api[_-]?key|"
    r"secret|credential|oauth[_-]?code|oauth|auth|code|signature|sig|assertion|jwt|"
    r"session|cookie|redirect(?:[_-]?uri)?|return(?:[_-]?url)?|continue|target|"
    r"destination|dest|next|url|link)"
    r"(?:$|[=&#;])"
)
_DANGEROUS_PATH_ESCAPE = re.compile(r"(?i)%(?:2e|2f|3f|23|3a|40|5c)")
_RFC3339_SENT_DATETIME = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,7})?(?P<zone>Z|[+-]\d{2}:\d{2})$"
)
_RefreshAccessToken = Callable[[], Awaitable[str]]
type RecipientTriple = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
type ReplyBindingKey = tuple[UUID, str, str, MailMode]


@dataclass(frozen=True, slots=True)
class MicrosoftReplyRecipientFact:
    """冻结一封 Graph 来源邮件可直接回复的精确收件人事实。

    该供应商边界值同时绑定连接、来源会话、来源消息与回复模式；To/CC/BCC 只作为这
    一个来源动作的结果事实使用，不能被同模式的另一封邮件复用。构造后不可变，适配器
    还会复制到只读映射中，避免审批与执行之间由调用方替换事实。

    Attributes:
        connection_id: 持有来源邮件并提供发送 token 的精确连接。
        source_thread_id: Graph 来源 ``conversationId``。
        source_message_id: Graph 来源不可变消息 ID。
        mode: 仅允许 ``reply`` 或 ``reply_all``。
        to: 供应商直接动作会采用的规范 To 地址。
        cc: 供应商直接动作会采用的规范 CC 地址。
        bcc: 供应商直接动作会采用的规范 BCC 地址。

    Raises:
        TypeError: 标识、模式或收件人容器不是精确领域类型。
        ValueError: 来源标识、模式或收件人集合不满足冻结边界。
    """

    connection_id: UUID
    source_thread_id: str
    source_message_id: str
    mode: MailMode
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]

    def __post_init__(self) -> None:
        """验证事实完整、规范且属于单个可直接回复动作。"""
        if type(self.connection_id) is not UUID:
            raise TypeError("reply recipient fact connection_id must be UUID")
        if type(self.mode) is not MailMode:
            raise TypeError("reply recipient fact mode must be MailMode")
        if self.mode is MailMode.NEW:
            raise ValueError("new mail does not accept reply recipient facts")
        if (
            _safe_identifier(self.source_thread_id) is None
            or _safe_identifier(self.source_message_id) is None
        ):
            raise ValueError("reply recipient fact source binding is invalid")
        if not all(type(field) is tuple for field in (self.to, self.cc, self.bcc)):
            raise TypeError("reply recipient fact recipients must be tuples")
        try:
            normalized = normalize_mail_recipients(self.to, self.cc, self.bcc)
        except (TypeError, ValueError):
            raise ValueError("reply recipient fact recipients are invalid") from None
        if normalized != (self.to, self.cc, self.bcc):
            raise ValueError("reply recipient fact recipients must be normalized")
        if not 1 <= sum(len(field) for field in normalized) <= 50:
            raise ValueError("reply recipient fact recipient limit is invalid")


class MicrosoftMailWriteAdapter:
    """执行 Microsoft 新邮件/回复/全部回复，并以 Sent Items 进行只读核对。

    Args:
        access_token: 由连接 coordinator 解密并校验的短期 bearer token；不会记录。
        account_email: 当前连接的主账户地址，作为唯一可信 From 来源。
        client: 可选注入的 HTTPX client，主要供测试和组合根使用。
        refresh_access_token: 仅为统一构造签名保留；本适配器永不调用该 callback。
        reply_recipient_facts: 已由同步源事实计算的精确不可变回复事实；每项同时绑定
            connection、source thread/message、mode 与 ``(To, Cc, Bcc)``。Graph 的直接
            MIME 回复会自行采用源邮件收件人，因此任一绑定缺失或不匹配时必须在审批前
            拒绝，不能借用另一封邮件的事实或通过隐藏草稿绕过冻结载荷。

    Notes:
        Graph 的直接 MIME ``/reply`` 与 ``/replyAll`` 返回 202 且通常没有响应体；这
        只代表请求被接受，不能证明 Sent Items 已落地。OAuth 401、请求后 timeout、
        连接中断和 5xx 都交给上层 coordinator/reconciliation，适配器不会盲目重放。
    """

    provider = "microsoft"

    def __init__(
        self,
        *,
        access_token: str,
        account_email: str,
        client: httpx.AsyncClient | None = None,
        refresh_access_token: _RefreshAccessToken | None = None,
        reply_recipient_facts: tuple[MicrosoftReplyRecipientFact, ...] = (),
    ) -> None:
        """保存受控连接事实，不触发网络或 token 刷新。"""
        self._access_token = _require_token(access_token)
        self._account_email = normalize_mailbox_address(account_email)
        self._client = client
        # 保留 callback 只是为了让组合根能共享读/写 adapter 工厂；调用路径刻意不引用，
        # 防止 401 或未知结果在 adapter 内产生第二次 provider call。
        self._refresh_access_token = refresh_access_token
        self._reply_recipient_facts = _index_reply_recipient_facts(reply_recipient_facts)

    def update_access_token(self, access_token: str) -> None:
        """在外层 coordinator 已完成一次 CAS refresh 后替换内存 token。"""
        self._access_token = _require_token(access_token)

    def validate_for_approval(self, command: TrustedCommand) -> ApprovalPreflightResult:
        """纯验证冻结命令可被 Graph 直接 MIME 无损表达。

        回复动作必须有同步得到的确定性收件人集合；Graph 直接回复会重新采用源邮件的
        ``replyTo``/收件人，而不会可靠地执行用户调整后的 To/CC/BCC。该不确定性在
        审批前以 ``mail_thread_binding_conflict`` fail closed，且本方法绝不发 HTTP。

        Args:
            command: 已由应用层严格解析的供应商无关可信命令。

        Returns:
            无 warning 的固定预检结果。

        Raises:
            StateConflictError: 回复收件人集合缺失或与源事实不一致。
            TypeError: 命令类型不正确。
            ValueError: MIME、地址或命令字段无法安全表达。
        """
        # 来源线程/消息是回复命令的持久绑定事实；它们一旦缺失或落入 dot-segment，
        # 不是普通的 MIME 格式错误，而是当前线程状态不能安全表达，必须使用精确的
        # StateConflictError 让审批用例原子失败，且不留下可批准记录。
        if (
            type(command) is MailSendCommand
            and command.mode is not MailMode.NEW
            and (
                _safe_identifier(command.source_thread_id) is None
                or _safe_identifier(command.source_message_id) is None
                or type(command.thread_headers) is not ReplyThreadHeaders
            )
        ):
            raise _mail_thread_binding_conflict()
        normalized = _validated_mail_command(command)
        # 在审批前生成同一份 MIME，确保 From、纯文本和回复引用在实际 execute 时不会
        # 因 adapter 配置错误才暴露；这一步只在本地构建 bytes。
        build_mail_mime_base64(normalized, from_address=self._account_email)
        self._ensure_reply_recipient_compatibility(normalized)
        return ApprovalPreflightResult()

    async def execute(self, command: TrustedCommand) -> ProviderWriteOutcome:
        """执行一条已批准邮件命令，最多向 Graph 发出一次直接 MIME POST。

        Args:
            command: 已完成精确审批、哈希绑定和 request-start 认领的邮件命令。

        Returns:
            规范化的 Microsoft 三态写入结果。

        Raises:
            StateConflictError: 回复收件人事实无法证明无损表达。
            TypeError: 命令不是 ``MailSendCommand``。
            ValueError: MIME 或供应商路径边界无效。
        """
        normalized = _validated_mail_command(command)
        # execute 再做一次同样的本地检查，避免未来绕过 approval preflight 的调用方把
        # 无法表达的冻结回复直接写入 Graph。
        self._ensure_reply_recipient_compatibility(normalized)
        encoded_mime = build_mail_mime_base64(normalized, from_address=self._account_email)
        if normalized.mode is MailMode.NEW:
            url = MICROSOFT_SEND_MAIL_URL
        else:
            source_id = _safe_identifier(normalized.source_message_id)
            if source_id is None:
                raise ValueError("Microsoft reply source message ID is invalid")
            action = "reply" if normalized.mode is MailMode.REPLY else "replyAll"
            url = f"{MICROSOFT_GRAPH_BASE_URL}/me/messages/{quote(source_id, safe='')}/{action}"
        return await self._send_once(
            url,
            content=encoded_mime,
            correlation_id=message_id_for(normalized.operation_id),
        )

    async def reconcile(
        self,
        command: TrustedCommand,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """只读查询 Sent Items 中的稳定 Message-ID，绝不再次调用写端点。

        Args:
            command: 原始冻结命令，用于稳定关联 ID 与操作绑定校验。
            execution: 已持久化的 ToolExecution 内容无关引用。

        Returns:
            Sent Items 中唯一且字段事实完整匹配时为 ``confirmed_applied``；无匹配、
            重复、冲突、分页异常或读取错误均保留 ``unknown``。

        Raises:
            TypeError: 命令或执行引用类型不正确。
            ValueError: 执行引用与命令的 operation/provider/action 不一致。
        """
        normalized = _validated_mail_command(command)
        if type(execution) is not ExecutionReference:
            raise TypeError("Microsoft mail reconciliation requires ExecutionReference")
        if (
            execution.operation_id != normalized.operation_id
            or execution.provider != self.provider
            or execution.tool_name != "mail.send"
        ):
            raise ValueError("Microsoft mail reconciliation binding is invalid")

        correlation_id = message_id_for(normalized.operation_id)
        current_url = MICROSOFT_SENT_MESSAGES_URL
        params: Mapping[str, str] | None = {
            "$filter": f"internetMessageId eq '{_odata_literal(correlation_id)}'",
            "$select": _SENT_SELECT,
        }
        seen_urls: set[str] = set()
        candidates: list[Mapping[str, object]] = []
        last_request_id: str | None = None

        for _ in range(MICROSOFT_MAIL_RECONCILE_MAX_PAGES):
            if current_url in seen_urls:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_sent_reconciliation_pagination_invalid",
                )
            seen_urls.add(current_url)
            response, transport_error = await self._request_read(
                current_url,
                params=params,
            )
            params = None
            if transport_error is not None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code=transport_error,
                )
            if response is None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_sent_reconciliation_unavailable",
                )
            last_request_id = _provider_request_id(response)
            if response.status_code == 401:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_reauthorization_required",
                )
            if response.status_code == 403:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_mail_permission_required",
                )
            if response.status_code == 429 or response.status_code >= 500:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code=(
                        "microsoft_mail_rate_limited"
                        if response.status_code == 429
                        else "microsoft_mail_service_unavailable"
                    ),
                )
            if not 200 <= response.status_code < 300:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_sent_reconciliation_rejected",
                )

            payload = _json_object(response)
            if payload is None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_sent_reconciliation_invalid_response",
                )
            raw_values = payload.get("value")
            if not isinstance(raw_values, list):
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_sent_reconciliation_invalid_response",
                )
            if len(raw_values) > MICROSOFT_MAIL_RECONCILE_MAX_CANDIDATES:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_sent_reconciliation_candidate_limit",
                )
            for raw in raw_values:
                if not isinstance(raw, Mapping):
                    return _unknown_outcome(
                        correlation_id=correlation_id,
                        provider_request_id=last_request_id,
                        error_code="microsoft_sent_reconciliation_invalid_response",
                    )
                candidates.append(cast(Mapping[str, object], raw))
                if len(candidates) > MICROSOFT_MAIL_RECONCILE_MAX_CANDIDATES:
                    return _unknown_outcome(
                        correlation_id=correlation_id,
                        provider_request_id=last_request_id,
                        error_code="microsoft_sent_reconciliation_candidate_limit",
                    )

            next_link = payload.get("@odata.nextLink")
            if next_link is None:
                break
            if not isinstance(next_link, str):
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_sent_reconciliation_pagination_invalid",
                )
            safe_next = _validate_sent_collection_url(next_link)
            if safe_next is None:
                # 绝不把 opaque nextLink 交给 HTTPX；这样即便供应商响应被篡改，也不会把
                # Authorization header 发到外部主机。
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_request_id,
                    error_code="microsoft_sent_reconciliation_pagination_invalid",
                )
            current_url = safe_next
        else:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=last_request_id,
                error_code="microsoft_sent_reconciliation_page_limit",
            )

        if len(candidates) != 1:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=last_request_id,
                error_code=(
                    "microsoft_sent_message_not_found"
                    if not candidates
                    else "microsoft_sent_reconciliation_ambiguous"
                ),
            )
        candidate = candidates[0]
        expected_conversation_id = (
            normalized.source_thread_id if normalized.mode is not MailMode.NEW else None
        )
        candidate_values = _candidate_values(
            candidate,
            correlation_id,
            expected_conversation_id=expected_conversation_id,
        )
        if candidate_values is None:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=last_request_id,
                error_code="microsoft_sent_reconciliation_mismatch",
            )
        provider_id, provider_url = candidate_values
        return _applied_outcome(
            correlation_id=correlation_id,
            provider_resource_id=provider_id,
            provider_request_id=last_request_id,
            provider_url=provider_url,
        )

    def _ensure_reply_recipient_compatibility(self, command: MailSendCommand) -> None:
        """按完整来源键读取事实，再与冻结 To/CC/BCC 做无序集合比较。"""
        if command.mode is MailMode.NEW:
            return
        source_thread_id = _safe_identifier(command.source_thread_id)
        source_message_id = _safe_identifier(command.source_message_id)
        if (
            type(command.connection_id) is not UUID
            or source_thread_id is None
            or source_message_id is None
        ):
            raise _mail_thread_binding_conflict()
        # mode-wide 的 recipient cache 会让邮件 A 的事实授权邮件 B。精确四元键确保只有
        # 同一连接、会话、消息和动作的供应商事实，才能证明这次直接回复无损。
        expected = self._reply_recipient_facts.get(
            (
                command.connection_id,
                source_thread_id,
                source_message_id,
                command.mode,
            )
        )
        actual: RecipientTriple = (command.to, command.cc, command.bcc)
        if expected is None or _recipient_sets_key(expected) != _recipient_sets_key(actual):
            raise _mail_thread_binding_conflict()

    async def _send_once(
        self,
        url: str,
        *,
        content: bytes,
        correlation_id: str,
    ) -> ProviderWriteOutcome:
        """发出一次标准 Base64 MIME POST，并按请求阶段分类结果。"""
        try:
            response = await self._request("POST", url, content=content)
        except (httpx.ConnectTimeout, httpx.ConnectError):
            return _not_applied_outcome(
                correlation_id=correlation_id,
                retryable=True,
                error_code="microsoft_connect_failed_before_send",
            )
        except httpx.TimeoutException:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="microsoft_mail_send_timeout",
            )
        except httpx.RequestError:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="microsoft_mail_send_request_failed",
            )

        request_id = _provider_request_id(response)
        status = response.status_code
        if status == 202:
            # Graph 202 没有消息 ID，也不保证 Sent Items 已立即可见；绝不能把接受当成
            # 已应用，必须让上层进入有界只读核对。
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code="microsoft_mail_send_accepted_unconfirmed",
            )
        if status in {408, 425} or status >= 500 or 300 <= status < 400:
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                error_code=(
                    "microsoft_mail_send_service_unavailable"
                    if status >= 500
                    else "microsoft_mail_send_ambiguous_response"
                ),
            )
        if 400 <= status < 500:
            retryable = status == 429
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                retryable=retryable,
                retry_after_seconds=_retry_after(response) if retryable else None,
                error_code=_send_error_code(status),
            )
        # Graph direct mail contract only documents 202. Any other successful status lacks a
        # verifiable Sent resource and therefore remains unknown rather than being trusted.
        return _unknown_outcome(
            correlation_id=correlation_id,
            provider_request_id=request_id,
            error_code="microsoft_mail_send_unexpected_response",
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """通过固定方法、超时和 headers 发出单次 HTTP 请求。"""
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Prefer": _PREFER_HEADER,
        }
        if method == "POST":
            headers["Content-Type"] = "text/plain"
        if self._client is not None:
            return await _client_request(
                self._client,
                method,
                url,
                params=params,
                content=content,
                headers=headers,
            )
        timeout = httpx.Timeout(
            MICROSOFT_MAIL_WRITE_TIMEOUT_SECONDS,
            connect=MICROSOFT_MAIL_WRITE_CONNECT_TIMEOUT_SECONDS,
        )
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            return await _client_request(
                client,
                method,
                url,
                params=params,
                content=content,
                headers=headers,
            )

    async def _request_read(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
    ) -> tuple[httpx.Response | None, str | None]:
        """执行只读 GET，并将传输异常收窄为不含敏感内容的固定错误码。"""
        try:
            return await self._request("GET", url, params=params), None
        except httpx.ConnectTimeout:
            return None, "microsoft_connect_failed"
        except httpx.ConnectError:
            return None, "microsoft_connect_failed"
        except httpx.TimeoutException:
            return None, "microsoft_mail_timeout"
        except httpx.RequestError:
            return None, "microsoft_mail_request_failed"


async def _client_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None,
    content: bytes | None,
    headers: Mapping[str, str],
) -> httpx.Response:
    """使用显式 GET/POST 方法调用 HTTPX，避免误用 JSON body 或重定向。"""
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
            content=content,
            headers=normalized_headers,
            follow_redirects=False,
        )
    raise ValueError("Microsoft mail HTTP method is unsupported")


def _index_reply_recipient_facts(
    values: tuple[MicrosoftReplyRecipientFact, ...],
) -> Mapping[ReplyBindingKey, RecipientTriple]:
    """复制精确回复事实到只读索引，并拒绝重复来源动作。

    Args:
        values: 调用方提供的不可变事实 tuple。

    Returns:
        以 connection/thread/message/mode 四元组索引的只读收件人映射。

    Raises:
        TypeError: 容器或任一成员不是精确不可变事实类型。
        ValueError: 同一来源动作出现重复事实，无法证明哪一项应被审批。
    """
    if type(values) is not tuple:
        raise TypeError("reply_recipient_facts must be a tuple")
    indexed: dict[ReplyBindingKey, RecipientTriple] = {}
    for fact in values:
        if type(fact) is not MicrosoftReplyRecipientFact:
            raise TypeError("reply_recipient_facts contains an invalid fact")
        key: ReplyBindingKey = (
            fact.connection_id,
            fact.source_thread_id,
            fact.source_message_id,
            fact.mode,
        )
        if key in indexed:
            raise ValueError("reply_recipient_facts contains a duplicate source binding")
        indexed[key] = (fact.to, fact.cc, fact.bcc)
    return MappingProxyType(indexed)


def _recipient_sets_key(value: RecipientTriple) -> tuple[frozenset[str], ...]:
    """按 To/CC/BCC 字段生成无序比较键，保留字段语义并消除展示顺序差异。"""
    return tuple(frozenset(field) for field in value)


def _validated_mail_command(command: TrustedCommand) -> MailSendCommand:
    """在供应商入口重新收窄冻结邮件命令，不能修正任何已审批值。"""
    if type(command) is not MailSendCommand:
        raise TypeError("Microsoft mail action requires MailSendCommand")
    if type(command.mode) is not MailMode:
        raise TypeError("mail mode must be MailMode")
    if not all(type(field) is tuple for field in (command.to, command.cc, command.bcc)):
        raise TypeError("mail recipient fields must be tuples")
    try:
        normalized_recipients = normalize_mail_recipients(
            command.to,
            command.cc,
            command.bcc,
        )
    except (TypeError, ValueError):
        raise ValueError("mail recipients are invalid") from None
    if normalized_recipients != (command.to, command.cc, command.bcc):
        raise ValueError("mail recipients must be normalized")
    if not 1 <= sum(len(field) for field in normalized_recipients) <= 50:
        raise ValueError("mail command recipient limit is invalid")
    if type(command.subject) is not str or len(command.subject) > 255:
        raise ValueError("mail subject is invalid")
    if "\r" in command.subject or "\n" in command.subject:
        raise ValueError("mail subject must not contain CR or LF")
    if type(command.body_text) is not str or len(command.body_text) > 100_000:
        raise ValueError("mail body is invalid")
    if command.mode is MailMode.NEW:
        if any(
            value is not None
            for value in (
                command.source_thread_id,
                command.source_message_id,
                command.thread_headers,
            )
        ):
            raise ValueError("new mail source binding is invalid")
    else:
        if (
            _safe_identifier(command.source_thread_id) is None
            or _safe_identifier(command.source_message_id) is None
            or type(command.thread_headers) is not ReplyThreadHeaders
        ):
            raise ValueError("reply mail source binding is invalid")
    return command


def _require_token(value: object) -> str:
    """验证 bearer token 的长度/控制字符边界，错误不回显 token。"""
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


def _safe_identifier(value: object) -> str | None:
    """收窄 Graph opaque ID，防止 dot-segment、控制字符、空白或超长值进入 URL/结果。"""
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or value != value.strip()
        or len(value) > 512
    ):
        return None
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        return None
    return value


def _json_object(response: httpx.Response) -> Mapping[str, object] | None:
    """把供应商响应收窄为顶层 JSON mapping，不保留原始正文。"""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return cast(Mapping[str, object], payload)


def _provider_request_id(response: httpx.Response) -> str | None:
    """提取有限的 Graph 请求关联 Header，不读取或记录 Authorization。"""
    for name in ("request-id", "client-request-id", "x-ms-request-id"):
        value = response.headers.get(name)
        if _safe_identifier(value) is not None and len(cast(str, value)) <= 255:
            return cast(str, value)
    return None


def _retry_after(response: httpx.Response) -> int | None:
    """解析有界整数 Retry-After，畸形值不进入可重试结果。"""
    try:
        value = int(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if 0 <= value <= MICROSOFT_MAIL_RETRY_AFTER_CAP_SECONDS else None


def _send_error_code(status_code: int) -> str:
    """把固定 Graph 4xx 映射为稳定、内容无关错误码。"""
    if status_code == 401:
        return "microsoft_reauthorization_required"
    if status_code == 403:
        return "microsoft_mail_permission_required"
    if status_code == 429:
        return "microsoft_mail_rate_limited"
    return "microsoft_mail_send_rejected"


def _odata_literal(value: str) -> str:
    """按 OData 单引号规则转义 equality filter 的 literal。"""
    return value.replace("'", "''")


def _validate_sent_collection_url(value: str) -> str | None:
    """验证 nextLink 仍是精确 Sent collection URL，拒绝 SSRF 与解析器折叠。"""
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if _unsafe_url_text(value):
        return None
    variants = _decoded_url_variants(value)
    if variants is None:
        return None
    if any(_unsafe_url_text(variant) for variant in variants):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "graph.microsoft.com"
        or parsed.netloc != "graph.microsoft.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment != ""
        or parsed.path != _EXPECTED_SENT_PATH
    ):
        return None
    # urlsplit/httpx 会对某些 percent 编码和控制字符做不同程度的规范化；路径必须
    # 在有限解码视图中仍逐字等于预期 collection，不能借此切换到另一个资源。
    for variant in variants[1:]:
        try:
            decoded_parts = urlsplit(variant)
        except ValueError:
            return None
        if decoded_parts.path != _EXPECTED_SENT_PATH or decoded_parts.fragment != "":
            return None
    return value


def _safe_provider_link(value: object) -> str | None:
    """验证 Outlook webLink 的安全 HTTPS 形状，拒绝伪造主机与敏感 query。"""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if value != value.strip() or _unsafe_url_text(value):
        return None
    variants = _decoded_url_variants(value)
    if variants is None:
        return None
    if any(
        _unsafe_url_text(variant) or _ADDRESS_IN_URL.search(variant) is not None
        for variant in variants
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host is None
        or host not in _SAFE_LINK_HOSTS
        or parsed.netloc != host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment != ""
        or not parsed.path
        or "%" in parsed.netloc
        or _DANGEROUS_PATH_ESCAPE.search(parsed.path) is not None
    ):
        return None
    # webLink 可能携带 opaque 的非敏感查询，但不能把 token、授权码或控制字符带到
    # 用户界面；最多解码三层即可覆盖常见代理双编码，同时保持固定 CPU 上限。
    if any(_SECRET_QUERY_KEY.search(variant) is not None for variant in variants):
        return None
    for variant in variants:
        try:
            decoded_parts = urlsplit(variant)
        except ValueError:
            return None
        if decoded_parts.fragment:
            return None
        decoded_path = decoded_parts.path
        segments = decoded_path.split("/")
        if any(segment in {".", ".."} for segment in segments):
            return None
        # 编码后的路径分隔符/查询分隔符会在不同 HTTP 客户端中被重新解释，不能持久化。
        if any(character in decoded_path for character in ("\\", "?", "#")):
            return None
        # 查询值同样可能被代理解码成路径跳转；只要出现明确的 dot-segment 片段，
        # 就拒绝整个展示链接，避免不同浏览器产生不同导航结果。
        if any(marker in variant for marker in ("../", "./", "\\..", "\\.")):
            return None
    return value


def _candidate_matches(
    candidate: Mapping[str, object],
    expected_message_id: str,
    *,
    expected_conversation_id: str | None = None,
) -> bool:
    """严格验证 Sent 候选的最小事实，不让畸形值确认成功。"""
    return (
        _candidate_values(
            candidate,
            expected_message_id,
            expected_conversation_id=expected_conversation_id,
        )
        is not None
    )


def _candidate_values(
    candidate: Mapping[str, object],
    expected_message_id: str,
    *,
    expected_conversation_id: str | None,
) -> tuple[str, str | None] | None:
    """读取已验证的 provider ID，并把不可用或不可信 webLink 收窄为空。"""
    provider_id = _safe_identifier(candidate.get("id"))
    conversation_id = _safe_identifier(candidate.get("conversationId"))
    internet_id = candidate.get("internetMessageId")
    sent_value = candidate.get("sentDateTime")
    # webLink 只是便捷展示链接，不是发送事实。供应商缺失、空值或返回任何不可信 URL
    # 时一律丢弃原值；不能因此推翻由 message/conversation/time 共同证明的唯一 Sent 项。
    provider_url = _safe_provider_link(candidate.get("webLink"))
    if (
        provider_id is None
        or conversation_id is None
        or (expected_conversation_id is not None and conversation_id != expected_conversation_id)
        or not isinstance(internet_id, str)
        or internet_id != expected_message_id
        or not isinstance(sent_value, str)
        or not _aware_datetime(sent_value)
    ):
        return None
    return provider_id, provider_url


def _aware_datetime(value: str) -> datetime | None:
    """解析严格 RFC3339 Sent 时间，避免标准库宽松语法造成哈希/事实歧义。"""
    if not isinstance(value, str):
        return None
    matched = _RFC3339_SENT_DATETIME.fullmatch(value)
    if matched is None:
        return None
    zone = matched.group("zone")
    if zone != "Z":
        offset_hour = int(zone[1:3])
        offset_minute = int(zone[4:6])
        # RFC3339 的 -00:00 表示未知 offset，不足以证明一个确定发送时刻。
        if (
            offset_hour > 23
            or offset_minute > 59
            or (zone.startswith("-") and offset_hour == 0 and offset_minute == 0)
        ):
            return None
    normalized = value.removesuffix("Z") + "+00:00" if zone == "Z" else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _unsafe_url_text(value: str) -> bool:
    """拒绝 URL 中会被解析器折叠或注入 Header 的空白、控制和反斜杠。"""
    return any(
        character == "\\" or character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    )


def _decoded_url_variants(value: str) -> tuple[str, ...] | None:
    """生成最多三层 percent 解码视图，捕获有限的双重编码安全风险。"""
    variants: list[str] = [value]
    current = value
    for _ in range(3):
        if "%" not in current:
            break
        if _INVALID_PERCENT_ESCAPE.search(current) is not None:
            return None
        try:
            decoded = unquote_to_bytes(current).decode("utf-8")
        except UnicodeDecodeError:
            return None
        variants.append(decoded)
        if decoded == current:
            break
        current = decoded
    return tuple(variants)


def _mail_thread_binding_conflict() -> StateConflictError:
    """构造不回显来源 ID 的精确回复线程冲突。"""
    return StateConflictError(
        error_code="mail_thread_binding_conflict",
        message="Microsoft reply source binding is unavailable or inconsistent",
    )


def _applied_outcome(
    *,
    correlation_id: str,
    provider_resource_id: str,
    provider_request_id: str | None,
    provider_url: str | None,
) -> ProviderWriteOutcome:
    """构造确认成功结果，集中保证 retry 字段为空。"""
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
    retryable: bool,
    retry_after_seconds: int | None = None,
    provider_request_id: str | None = None,
) -> ProviderWriteOutcome:
    """构造明确未应用结果；仅 retryable 路径保留 Retry-After。"""
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


# 兼容组合根可能采用的简短命名；两个名称指向同一个严格实现，不建立第二套 adapter。
MicrosoftMailAdapter = MicrosoftMailWriteAdapter


__all__ = [
    "MICROSOFT_GRAPH_BASE_URL",
    "MICROSOFT_SEND_MAIL_URL",
    "MICROSOFT_SENT_MESSAGES_URL",
    "MicrosoftMailAdapter",
    "MicrosoftMailWriteAdapter",
    "MicrosoftReplyRecipientFact",
]
