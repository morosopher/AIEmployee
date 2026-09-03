"""实现 Gmail ``users.messages.send`` 的受控真实写入与 Sent 只读核对。

本适配器是供应商 HTTP 与领域可信命令之间的唯一边界：它只接收已经冻结的
``MailSendCommand``，把确定性纯文本 MIME 编码为 Gmail 所需的 Base64URL，并将所有
响应收窄为 ``ProviderWriteOutcome``。适配器不持有数据库事务、不申请 OAuth scope、
不访问 Gmail Draft API，也不在 401、超时或 5xx 后自行重放写请求；连接级 token
刷新和任务级重入由上层 coordinator/TrustedAction 用例负责。
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, Mapping
from typing import cast
from urllib.parse import quote

import httpx

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.oauth import MAX_OAUTH_TOKEN_LENGTH
from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
    ExecutionReference,
    ProviderWriteOutcome,
)
from ai_employee.domain.actions import ProviderWriteOutcomeKind
from ai_employee.domain.mail_actions import (
    MailMode,
    MailSendCommand,
    ReplyThreadHeaders,
    normalize_mail_recipients,
    normalize_mailbox_address,
)
from ai_employee.integrations.mail_mime import build_mail_mime, message_id_for

GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
"""Gmail 用户资源根路径；写适配器只使用其 messages 子资源。"""

GMAIL_SEND_URL = f"{GMAIL_API_BASE_URL}/messages/send"
"""Gmail 真实发送端点；仅允许 ``POST``。"""

GMAIL_MESSAGES_URL = f"{GMAIL_API_BASE_URL}/messages"
"""Gmail 消息列表端点，Sent 核对只使用其只读 GET。"""

GMAIL_UI_SENT_BASE_URL = "https://mail.google.com/mail/u/0/?view=sent&message_id="
"""不含账户、地址或主题的 Sent 项目入口前缀。"""

GMAIL_WRITE_TIMEOUT_SECONDS = 15.0
GMAIL_WRITE_CONNECT_TIMEOUT_SECONDS = 3.0
GMAIL_RECONCILE_MAX_CANDIDATES = 10
GMAIL_RECONCILE_MAX_PAGES = 4
GMAIL_RETRY_AFTER_CAP_SECONDS = 300


class GmailWriteAdapter:
    """执行 Gmail 新邮件/回复/全部回复并核对 Sent 结果。

    Args:
        access_token: 已由连接 coordinator 解密并校验的短期 bearer token；本类不记录。
        account_email: 当前 OAuth 连接主账户地址，作为唯一可信 From 来源。
        client: 可选的测试或组合根 HTTP client；不传时每次调用创建短生命周期 client。

    Notes:
        真实写请求没有 adapter-local retry budget。请求开始后的 timeout、连接中断和
        5xx 一律返回 ``unknown``，让 TrustedAction 用例只进入只读核对；401 也只返回
        明确拒绝结果，由外层 OAuthRefreshCoordinator 决定是否完成一次 refresh 后重入。
    """

    provider = "google"

    def __init__(
        self,
        *,
        access_token: str,
        account_email: str,
        client: httpx.AsyncClient | None = None,
        refresh_access_token: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        """保存受控 token、连接身份和可选注入 client。

        ``refresh_access_token`` 仅为与读取适配器的构造兼容而接受，绝不会在本类中
        调用；真实 refresh 必须由连接级 coordinator 先完成 claim/CAS，再通过
        ``update_access_token`` 显式注入新 token。这样即使调用方误传 callback，也不会
        在一个未知写结果后产生隐式第二次 provider call。
        """
        self._access_token = _require_token(access_token)
        # build_mail_mime 会再次规范化并拒绝 Header 注入；这里提前验证能让纯 preflight
        # 和实际 execute 使用同一连接身份，避免 adapter 构造后才暴露配置错误。
        self._account_email = normalize_mailbox_address(account_email)
        self._client = client
        self._refresh_access_token = refresh_access_token

    def update_access_token(self, access_token: str) -> None:
        """在外层 coordinator 已提交一次 refresh 后替换受控内存 token。

        该方法不发起网络请求、不持久化 token，也不重置任何写请求；调用方必须先完成
        connection lease/CAS，再显式决定是否重新调用 ``execute``。
        """
        self._access_token = _require_token(access_token)

    def validate_for_approval(self, command: TrustedCommand) -> ApprovalPreflightResult:
        """纯验证冻结命令可被 Gmail 无损表达，不执行任何 HTTP 调用。

        Args:
            command: 已由应用层严格解析的供应商无关可信命令。

        Returns:
            没有 warning 的固定预检结果。

        Raises:
            TypeError: 命令不是邮件发送领域值。
            ValueError: 模式、来源绑定或 MIME Header 无法表达。
        """
        command = _validated_mail_command(command)
        # MailSendCommand 的构造器已检查 recipient/source limits；重新调用 MIME builder
        # 是最后一道供应商表达能力检查，且仍然只在内存中生成 bytes。
        build_mail_mime(command, from_address=self._account_email)
        return ApprovalPreflightResult()

    async def execute(self, command: TrustedCommand) -> ProviderWriteOutcome:
        """发送一条已批准邮件命令，并返回不含正文的三态结果。

        Args:
            command: 已完成审批、哈希绑定和 request-start 认领的邮件命令。

        Returns:
            Gmail 供应商响应被规范化后的 ``ProviderWriteOutcome``。

        Raises:
            TypeError: 命令不是 ``MailSendCommand``。
            ValueError: MIME 无法安全表达。
        """
        command = _validated_mail_command(command)
        raw = base64.urlsafe_b64encode(
            build_mail_mime(command, from_address=self._account_email)
        ).decode("ascii").rstrip("=")
        body: dict[str, object] = {"raw": raw}
        if command.mode is not MailMode.NEW:
            # threadId 是 Gmail 回复归并所需的冻结供应商事实；新邮件绝不附带猜测值。
            body["threadId"] = command.source_thread_id
        return await self._send_with_classified_outcome(
            "/messages/send",
            json=body,
            correlation_id=message_id_for(command.operation_id),
            expected_thread_id=(
                command.source_thread_id if command.mode is not MailMode.NEW else None
            ),
        )

    async def reconcile(
        self,
        command: TrustedCommand,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """只读核对 Sent 中的稳定 Message-ID，绝不重新调用 send。

        Args:
            command: 原始冻结邮件命令，用于稳定 Message-ID 与回复线程校验。
            execution: 已持久化的 ToolExecution 内容无关引用；其状态不会触发写请求。

        Returns:
            Sent 中唯一且已验证资源的 ``confirmed_applied``，或暂时无法判断的
            ``unknown``；缺少匹配项也不能在单轮读取中证明 ``confirmed_not_applied``。

        Raises:
            TypeError: 命令或执行引用类型不符合可信动作边界。
            ValueError: execution 与 command 的稳定 operation 绑定不一致。
        """
        command = _validated_mail_command(command)
        if type(execution) is not ExecutionReference:
            raise TypeError("Gmail reconciliation requires ExecutionReference")
        if execution.operation_id != command.operation_id:
            raise ValueError("Gmail reconciliation operation binding is invalid")

        correlation_id = message_id_for(command.operation_id)
        query = f"in:sent rfc822msgid:{correlation_id}"
        candidates: list[Mapping[str, object]] = []
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        pages_fetched = 0
        last_provider_request_id: str | None = None

        while True:
            if pages_fetched >= GMAIL_RECONCILE_MAX_PAGES:
                # 分页边界本身就是未完成的供应商事实；不能把已经看到的首个候选当作
                # 唯一发送结果，否则重复投递可能被错误标记为已收敛。
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code="google_sent_reconciliation_page_limit",
                )
            params = {
                "q": query,
                "maxResults": str(GMAIL_RECONCILE_MAX_CANDIDATES),
            }
            if page_token is not None:
                params["pageToken"] = page_token
            response, transport_error = await self._request_read(
                GMAIL_MESSAGES_URL,
                params=params,
            )
            pages_fetched += 1
            if transport_error is not None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code=transport_error,
                )
            if response is None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code="google_sent_reconciliation_unavailable",
                )
            last_provider_request_id = _provider_request_id(response)
            if response.status_code == 401:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code="google_reauthorization_required",
                )
            if response.status_code == 429 or response.status_code >= 500:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code=(
                        "google_rate_limited"
                        if response.status_code == 429
                        else "google_service_unavailable"
                    ),
                )
            if response.status_code >= 400:
                # 查询被明确拒绝且没有写请求发生；这只能说明本轮核对没有可用证据，不能把
                # 供应商拒绝伪装成“邮件未发送”，否则应用会错误地开放自动重发权限。
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code="google_sent_reconciliation_rejected",
                )

            payload = _json_object(response)
            if payload is None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code="google_sent_reconciliation_invalid_response",
                )
            raw_messages = payload.get("messages")
            if not isinstance(raw_messages, list):
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code="google_sent_reconciliation_invalid_response",
                )
            if len(raw_messages) > GMAIL_RECONCILE_MAX_CANDIDATES:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code="google_sent_reconciliation_candidate_limit",
                )
            for raw_candidate in raw_messages:
                if not isinstance(raw_candidate, Mapping):
                    return _unknown_outcome(
                        correlation_id=correlation_id,
                        provider_request_id=last_provider_request_id,
                        error_code="google_sent_reconciliation_invalid_response",
                    )
                candidates.append(cast(Mapping[str, object], raw_candidate))
                if len(candidates) > GMAIL_RECONCILE_MAX_CANDIDATES:
                    return _unknown_outcome(
                        correlation_id=correlation_id,
                        provider_request_id=last_provider_request_id,
                        error_code="google_sent_reconciliation_candidate_limit",
                    )

            next_page_token = payload.get("nextPageToken")
            if next_page_token is None:
                break
            normalized_page_token = _safe_page_token(next_page_token)
            if normalized_page_token is None or normalized_page_token in seen_page_tokens:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code="google_sent_reconciliation_invalid_response",
                )
            seen_page_tokens.add(normalized_page_token)
            page_token = normalized_page_token

        if not candidates:
            # Sent 查询的单次空结果只表示当前读取窗口没有证据，不能证明真实写入未发生；
            # 由上层 bounded reconciliation 决定何时进入 needs_attention。
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=last_provider_request_id,
                error_code="google_sent_message_not_found",
            )

        matching: list[tuple[str, str]] = []
        saw_conflicting_candidate = False
        for raw_candidate in candidates:
            candidate_id = _safe_provider_id(raw_candidate.get("id"))
            candidate_thread = _safe_provider_id(raw_candidate.get("threadId"))
            if candidate_id is None or candidate_thread is None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code="google_sent_reconciliation_invalid_response",
                )
            # Gmail messages.list 已返回 message ID 与 threadId；查询本身绑定了精确
            # ``rfc822msgid``，因此这两个字段就是核对所需的最小事实。避免再 GET 完整
            # message 可减少暴露正文的机会，也让未知结果重入保持严格只读且有界。
            labels = raw_candidate.get("labelIds")
            if labels is not None and (
                not isinstance(labels, list)
                or any(not isinstance(label, str) for label in labels)
            ):
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=last_provider_request_id,
                    error_code="google_sent_reconciliation_invalid_response",
                )
            candidate_matches = (
                (command.mode is MailMode.NEW or candidate_thread == command.source_thread_id)
                and _message_id_matches(raw_candidate, correlation_id)
                and (labels is None or "SENT" in labels)
            )
            if candidate_matches:
                matching.append((candidate_id, candidate_thread))
            else:
                saw_conflicting_candidate = True

        if saw_conflicting_candidate or len(matching) != 1:
            # 有候选但其 thread/Message-ID/label 事实冲突，或出现多个匹配资源时，保守地
            # 保留 unknown；自动重发会有重复风险，只有下一轮只读核对或人工确认可以收敛。
            return _unknown_outcome(
                correlation_id=correlation_id,
                provider_request_id=last_provider_request_id,
                error_code="google_sent_reconciliation_mismatch",
            )
        provider_id, _ = matching[0]
        return _applied_outcome(
            correlation_id=correlation_id,
            provider_resource_id=provider_id,
            provider_request_id=last_provider_request_id,
        )

    async def _send_with_classified_outcome(
        self,
        path: str,
        *,
        json: Mapping[str, object],
        correlation_id: str,
        expected_thread_id: str | None,
    ) -> ProviderWriteOutcome:
        """执行单次真实 POST，并按请求是否可能到达供应商分类结果。

        这里刻意没有循环或 refresh callback：request-start 已由应用层在 PostgreSQL
        独立提交，任何读取超时、连接中断（连接建立阶段除外）和 5xx 都可能已经产生
        外部副作用，必须交给只读 reconcile，而不是盲目再次 POST。
        """
        try:
            response = await self._request("POST", _gmail_url(path), json=json)
        except (httpx.ConnectTimeout, httpx.ConnectError):
            return _not_applied_outcome(
                correlation_id=correlation_id,
                retryable=True,
                error_code="google_connect_failed_before_send",
            )
        except httpx.TimeoutException:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="google_mail_send_timeout",
            )
        except httpx.RequestError:
            return _unknown_outcome(
                correlation_id=correlation_id,
                error_code="google_mail_send_request_failed",
            )

        request_id = _provider_request_id(response)
        if response.status_code == 200:
            payload = _json_object(response)
            if payload is None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=request_id,
                    error_code="google_mail_send_malformed_response",
                )
            provider_id = _safe_provider_id(payload.get("id"))
            thread_id = _safe_provider_id(payload.get("threadId"))
            if provider_id is None or thread_id is None:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=request_id,
                    error_code="google_mail_send_malformed_response",
                )
            if expected_thread_id is not None and thread_id != expected_thread_id:
                return _unknown_outcome(
                    correlation_id=correlation_id,
                    provider_request_id=request_id,
                    error_code="google_mail_send_thread_mismatch",
                )
            return _applied_outcome(
                correlation_id=correlation_id,
                provider_resource_id=provider_id,
                provider_request_id=request_id,
                provider_url=_provider_url(provider_id),
            )

        if 400 <= response.status_code < 500:
            # 401 明确表示当前 access token 无效，但再次 POST 之前必须由连接级
            # OAuthRefreshCoordinator 完成一次有审计的 refresh；不能把它标成普通安全
            # retry，否则通用 Taskiq 重试会用同一过期 token 盲目重放。
            # Gmail 文档只给出 429 的明确限流重试语义；408/425 虽是通用 HTTP 暂态码，
            # 不能在本适配器中推断资源确实未创建，故只作为不可自动重放的拒绝事实。
            retryable = response.status_code == 429
            error_code = _send_error_code(response.status_code)
            return _not_applied_outcome(
                correlation_id=correlation_id,
                provider_request_id=request_id,
                retryable=retryable,
                retry_after_seconds=(
                    _retry_after(response) if retryable and response.status_code == 429 else None
                ),
                error_code=error_code,
            )

        # 3xx and 5xx are semantically ambiguous for a real write. Redirects are not followed,
        # and no response body is retained; both routes therefore stop at unknown.
        return _unknown_outcome(
            correlation_id=correlation_id,
            provider_request_id=request_id,
            error_code=(
                "google_mail_send_service_unavailable"
                if response.status_code >= 500
                else "google_mail_send_ambiguous_response"
            ),
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        """发送单次 HTTP 请求并固定超时、Authorization 与禁止重定向策略。"""
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if self._client is not None:
            return await _client_request(
                self._client,
                method,
                url,
                params=params,
                json=json,
                headers=headers,
            )
        timeout = httpx.Timeout(
            GMAIL_WRITE_TIMEOUT_SECONDS,
            connect=GMAIL_WRITE_CONNECT_TIMEOUT_SECONDS,
        )
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            return await _client_request(
                client,
                method,
                url,
                params=params,
                json=json,
                headers=headers,
            )

    async def _request_read(
        self,
        url: str,
        *,
        params: Mapping[str, str],
    ) -> tuple[httpx.Response | None, str | None]:
        """执行只读 GET；返回 response 或不含敏感内容的稳定传输错误码。"""
        try:
            return await self._request("GET", url, params=params), None
        except httpx.ConnectTimeout:
            return None, "google_connect_failed"
        except httpx.ConnectError:
            return None, "google_connect_failed"
        except httpx.TimeoutException:
            return None, "google_timeout"
        except httpx.RequestError:
            return None, "google_request_failed"


async def _client_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None,
    json: Mapping[str, object] | None,
    headers: Mapping[str, str],
) -> httpx.Response:
    """通过显式 GET/POST 方法发出一次请求，便于审计动作方向并支持注入 client。"""
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
    raise ValueError("Gmail HTTP method is unsupported")


def _validated_mail_command(command: TrustedCommand) -> MailSendCommand:
    """在每个供应商入口重新确认邮件命令的冻结形状与规范化边界。

    应用层通常已经从严格 Schema 构造 ``MailSendCommand``，但 adapter 不能把该假设当作
    唯一防线：checkpoint、测试替身或未来调用方可能传入被篡改的 frozen object。这里不
    修正任何值，只接受已经规范化且可无损表达的命令；所有失败都在 HTTP 之前结束。
    """
    if type(command) is not MailSendCommand:
        raise TypeError("Google Gmail action requires MailSendCommand")
    if type(command.mode) is not MailMode:
        raise TypeError("mail mode must be MailMode")
    if not all(type(addresses) is tuple for addresses in (command.to, command.cc, command.bcc)):
        raise TypeError("mail recipient fields must be tuples")
    normalized_recipients = normalize_mail_recipients(command.to, command.cc, command.bcc)
    if normalized_recipients != (command.to, command.cc, command.bcc):
        raise ValueError("mail recipients must be normalized")
    recipient_count = sum(len(addresses) for addresses in normalized_recipients)
    if not 1 <= recipient_count <= 50:
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
            _safe_source_identifier(command.source_thread_id) is None
            or _safe_source_identifier(command.source_message_id) is None
        ):
            raise ValueError("reply mail source binding is invalid")
        if type(command.thread_headers) is not ReplyThreadHeaders:
            raise ValueError("reply mail source binding is invalid")
    return command


def _safe_source_identifier(value: object) -> str | None:
    """验证 Gmail thread/message opaque 标识，避免空白或控制字符进入请求。"""
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        return None
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    return value


def _require_token(value: object) -> str:
    """验证受控 bearer token 的最小边界，不把其内容写入错误。"""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_OAUTH_TOKEN_LENGTH
    ):
        raise ValueError("Google access token is invalid")
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
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


def _safe_provider_id(value: object) -> str | None:
    """验证 Gmail opaque ID，可用于结果存储和 URL path 编码。"""
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        return None
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    return value


def _safe_page_token(value: object) -> str | None:
    """验证 Gmail 分页 token，只允许有界且不含控制字符的 opaque 文本。"""
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024:
        return None
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    return value


def _provider_request_id(response: httpx.Response) -> str | None:
    """从有限的 Google 请求 Header 中提取可审计 opaque ID，不保存 Authorization。"""
    for name in ("x-request-id", "x-goog-request-id", "x-guploader-uploadid"):
        value = response.headers.get(name)
        normalized = _safe_provider_id(value)
        if normalized is not None and len(normalized) <= 255:
            return normalized
    return None


def _retry_after(response: httpx.Response) -> int | None:
    """解析有界整数 Retry-After，超界值不进入 durable outcome。"""
    try:
        value = int(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    if 0 <= value <= GMAIL_RETRY_AFTER_CAP_SECONDS:
        return value
    return None


def _send_error_code(status_code: int) -> str:
    """把固定 Gmail 4xx 状态映射为稳定、内容无关错误码。"""
    if status_code == 401:
        return "google_reauthorization_required"
    if status_code == 403:
        return "google_mail_permission_required"
    if status_code == 429:
        return "google_rate_limited"
    return "google_mail_send_rejected"


def _message_id_matches(payload: Mapping[str, object], expected: str) -> bool:
    """若列表项含 Message-ID 头则要求其与稳定 operation Message-ID 完全一致。"""
    raw_payload = payload.get("payload")
    if raw_payload is None:
        # metadata 列表响应通常不含 payload；精确 rfc822msgid 查询本身已提供关联事实。
        return True
    if not isinstance(raw_payload, Mapping):
        return False
    raw_headers = raw_payload.get("headers")
    if raw_headers is None:
        return True
    if not isinstance(raw_headers, list):
        return False
    for item in raw_headers:
        if not isinstance(item, Mapping):
            return False
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str):
            return False
        if name.casefold() == "message-id" and (
            not isinstance(value, str) or value != expected
        ):
            return False
    # 能走到这里表示所有存在的 Message-ID 头都与 ``expected`` 相等；头缺失时仍可接受，
    # 因为 Sent 搜索的精确 rfc822msgid 条件本身已经提供稳定关联证明。
    return True


def _provider_url(provider_id: str) -> str:
    """构造不含账户、收件人和主题的 Sent 项目 URL。"""
    return f"{GMAIL_UI_SENT_BASE_URL}{quote(provider_id, safe='')}"


def _gmail_url(path: str) -> str:
    """把适配器内部固定相对路径解析为 Gmail URL，拒绝外部重定向目标。"""
    if path == "/messages/send":
        return GMAIL_SEND_URL
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Gmail API path is invalid")
    return f"{GMAIL_API_BASE_URL}{path}"


def _applied_outcome(
    *,
    correlation_id: str,
    provider_resource_id: str,
    provider_request_id: str | None = None,
    provider_url: str | None = None,
) -> ProviderWriteOutcome:
    """构造成功结果并集中保证 retry 字段为空。"""
    return ProviderWriteOutcome(
        kind=ProviderWriteOutcomeKind.CONFIRMED_APPLIED,
        retryable=False,
        retry_after_seconds=None,
        provider_resource_id=provider_resource_id,
        provider_request_id=provider_request_id,
        correlation_id=correlation_id,
        provider_url=provider_url or _provider_url(provider_resource_id),
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
    """构造明确未应用结果；只有 retryable 时保留 Retry-After。"""
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


__all__ = [
    "GMAIL_API_BASE_URL",
    "GMAIL_MESSAGES_URL",
    "GMAIL_SEND_URL",
    "GmailWriteAdapter",
]
