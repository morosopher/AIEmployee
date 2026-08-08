"""实现 Microsoft Graph 邮件 folder discovery 与逐 folder Delta 只读适配器。

本模块是唯一允许接触 Graph JSON、绝对 Delta URL 与 HTTP 客户端的边界。所有返回值都会
先规范化为 ``application.ports.mail`` 的 immutable dataclass；原始响应、MIME/HTML、Bearer
token 和 opaque URL 不会进入日志、异常或持久化层。适配器只执行读取，不包含 Mail.Send
或供应商草稿箱写入。
"""

from __future__ import annotations

import html
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from urllib.parse import quote, urlsplit

import httpx
from bs4 import BeautifulSoup

from ai_employee.application.ports.mail import (
    MailCursorExpiredError,
    MailMessage,
    MailReader,
    MailRemoval,
    MailScope,
    MailSyncPage,
)
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.domain.mail_actions import normalize_mailbox_address

MICROSOFT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MICROSOFT_GRAPH_HOST = "graph.microsoft.com"
MICROSOFT_MAIL_TIMEOUT_SECONDS = 15.0
MICROSOFT_MAIL_CONNECT_TIMEOUT_SECONDS = 3.0
MICROSOFT_MAIL_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MICROSOFT_MAIL_MAX_CHAIN_BYTES = 32 * 1024 * 1024
MICROSOFT_MAIL_MAX_NORMALIZED_BYTES = 32 * 1024 * 1024
MICROSOFT_MAIL_MAX_PAGES = 100
MICROSOFT_MAIL_MAX_ITEMS = 10_000
MICROSOFT_MAIL_MAX_STRING_LENGTH = 16_384
MICROSOFT_MAIL_MAX_ID_LENGTH = 512
MICROSOFT_MAIL_MAX_PERSISTED_ID_LENGTH = 255

_MAIL_SELECT = (
    "id,conversationId,internetMessageId,from,toRecipients,ccRecipients,bccRecipients,"
    "subject,body,receivedDateTime,sentDateTime,lastModifiedDateTime,categories,webLink"
)
_MESSAGE_SELECT = _MAIL_SELECT
_PREFER_HEADER = 'IdType="ImmutableId"'
_RefreshAccessToken = Callable[[], Awaitable[str]]


class MicrosoftMailAdapter(MailReader):
    """读取 Microsoft Graph 邮件目录和 Delta，并输出供应商无关页面。

    Args:
        access_token: 已在 Worker 受控内存中解密的短期 bearer token；不会记录。
        refresh_access_token: 只读请求首个 401 时调用一次的 token 刷新端口。刷新结果由
            Worker 原子写回加密凭据后返回新 access token。

    Raises:
        PermanentProviderError: URL、分页、响应字段或 HTTP 永久错误。
        TransientProviderError: 429、5xx、超时或网络暂态错误。
        UserActionRequiredError: 401 重试后仍失败或 Graph 返回 403 权限撤销。
    """

    provider = "microsoft"

    def __init__(
        self,
        *,
        access_token: str,
        refresh_access_token: _RefreshAccessToken | None = None,
    ) -> None:
        """保存受控 token，并为当前 adapter 实例设置一次刷新预算。"""
        if not isinstance(access_token, str) or access_token == "":
            raise ValueError("Microsoft access token is invalid")
        self._access_token = access_token
        self._refresh_access_token = refresh_access_token
        self._refresh_attempted = False
        self._chain_wire_bytes = 0
        self._chain_normalized_bytes = 0

    async def list_sync_scopes(self) -> tuple[MailScope, ...]:
        """发现可访问 mailFolders，保留 Sent 并排除 Deleted/Junk。

        ``includeHiddenFolders=false`` 与固定 ``$select`` 是最小目录读取；目录自身也发送
        ImmutableId，避免 provider object move 后本地 scope key 静默漂移。Graph 目录分页仍
        受同一页数/URL/响应大小边界保护。
        """
        self._reset_refresh_budget()
        values = await self._list_collection(
            f"{MICROSOFT_GRAPH_BASE_URL}/me/mailFolders",
            params={
                "includeHiddenFolders": "false",
                "$select": "id,displayName,wellKnownName",
            },
            expected_path="/v1.0/me/mailFolders",
        )
        try:
            scopes = tuple(self._normalize_scope(item) for item in values)
        except (TypeError, ValueError):
            raise self._invalid_response() from None
        return tuple(
            scope
            for scope in scopes
            if scope.well_known_name not in {"deleteditems", "drafts", "junkemail"}
        )

    def initial_pages(self, scope_key: str, *, since: datetime) -> AsyncIterator[MailSyncPage]:
        """从显式 UTC 下界读取一个 folder 的受限七日 Delta 页面。"""
        self._validate_utc(since)
        if scope_key == "":
            raise ValueError("scope_key must not be empty")
        folder = quote(scope_key, safe="")
        return self._delta_pages(
            f"{MICROSOFT_GRAPH_BASE_URL}/me/mailFolders/{folder}/messages/delta",
            scope_key=scope_key,
            params={
                "$filter": f"receivedDateTime ge {self._utc_text(since)}",
                "$select": _MAIL_SELECT,
            },
        )

    def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """跟随一个 folder 专属 opaque Delta URL，并保留最终 deltaLink。"""
        if scope_key == "":
            raise ValueError("scope_key must not be empty")
        url = self._validate_delta_url(cursor, scope_key=scope_key)
        return self._delta_pages(url, scope_key=scope_key, params=None)

    async def get_message(self, scope_key: str, provider_message_id: str) -> MailMessage:
        """按 folder 与 immutable message ID 精确读取一封源邮件。"""
        self._reset_refresh_budget()
        if scope_key == "" or provider_message_id == "":
            raise ValueError("message scope and ID are required")
        folder = quote(scope_key, safe="")
        message_id = quote(provider_message_id, safe="")
        payload = await self._get_json(
            f"{MICROSOFT_GRAPH_BASE_URL}/me/mailFolders/{folder}/messages/{message_id}",
            params={"$select": _MESSAGE_SELECT},
        )
        try:
            normalized = self._normalize_message(payload, scope_key=scope_key)
        except (TypeError, ValueError):
            raise self._invalid_response() from None
        if not isinstance(normalized, MailMessage):
            raise self._invalid_response()
        return normalized

    async def list_sent_messages(
        self,
        *,
        since: datetime,
        internet_message_id: str | None = None,
    ) -> tuple[MailMessage, ...]:
        """读取 Sent Items 中可用于只读核对的精确邮件集合。

        该方法仍是只读端口；过滤值按 OData 单引号规则转义，响应只返回规范化消息，不保存
        完整 Graph JSON。每页请求都固定 ImmutableId header 并受分页上限保护。
        """
        self._validate_utc(since)
        self._reset_refresh_budget()
        filters = [f"sentDateTime ge {self._utc_text(since)}"]
        if internet_message_id is not None:
            if internet_message_id == "":
                raise ValueError("internet_message_id must not be empty")
            filters.append(f"internetMessageId eq '{self._odata_literal(internet_message_id)}'")
        values = await self._list_collection(
            f"{MICROSOFT_GRAPH_BASE_URL}/me/mailFolders/sentitems/messages",
            params={"$filter": " and ".join(filters), "$select": _MESSAGE_SELECT},
            expected_path="/v1.0/me/mailFolders/sentitems/messages",
        )
        normalized: list[MailMessage] = []
        for item in values:
            try:
                message = self._normalize_message(item, scope_key="sentitems")
            except (TypeError, ValueError):
                raise self._invalid_response() from None
            if not isinstance(message, MailMessage):
                raise self._invalid_response()
            normalized.append(message)
        return tuple(normalized)

    async def _list_collection(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        expected_path: str,
    ) -> tuple[Mapping[str, object], ...]:
        """读取固定 Graph collection，nextLink 只能保留同一精确 path。"""
        current_url = url
        current_params: Mapping[str, str] | None = params
        seen: set[str] = set()
        values: list[Mapping[str, object]] = []
        for _ in range(MICROSOFT_MAIL_MAX_PAGES):
            if current_url in seen:
                raise PermanentProviderError(
                    error_code="microsoft_mail_pagination_invalid",
                    message="Microsoft mail pagination is invalid",
                )
            seen.add(current_url)
            payload = await self._get_json(current_url, params=current_params)
            current_params = None
            page_values = self._values(payload)
            values.extend(page_values)
            if len(values) > MICROSOFT_MAIL_MAX_ITEMS:
                raise PermanentProviderError(
                    error_code="microsoft_mail_pagination_invalid",
                    message="Microsoft mail pagination is invalid",
                )
            next_link = self._optional_string(payload, "@odata.nextLink")
            if next_link is None:
                return tuple(values)
            current_url = self._validate_collection_url(
                next_link,
                expected_path=expected_path,
            )
        raise PermanentProviderError(
            error_code="microsoft_mail_pagination_invalid",
            message="Microsoft mail pagination is invalid",
        )

    async def _delta_pages(
        self,
        first_url: str,
        *,
        scope_key: str,
        params: Mapping[str, str] | None,
    ) -> AsyncIterator[MailSyncPage]:
        """执行有限 Delta 分页，并把每页 Graph 对象立即规范化为 port 值对象。"""
        self._reset_refresh_budget()
        current_url = first_url
        current_params = params
        seen: set[str] = set()
        item_count = 0
        for _ in range(MICROSOFT_MAIL_MAX_PAGES):
            if current_url in seen:
                raise PermanentProviderError(
                    error_code="microsoft_mail_pagination_invalid",
                    message="Microsoft mail pagination is invalid",
                )
            seen.add(current_url)
            payload = await self._get_json(
                current_url,
                params=current_params,
                cursor_scope=scope_key if params is None else None,
            )
            current_params = None
            raw_values = self._values(payload)
            item_count += len(raw_values)
            if item_count > MICROSOFT_MAIL_MAX_ITEMS:
                raise PermanentProviderError(
                    error_code="microsoft_mail_pagination_invalid",
                    message="Microsoft mail pagination is invalid",
                )
            messages: list[MailMessage] = []
            removals: list[MailRemoval] = []
            for item in raw_values:
                try:
                    normalized = self._normalize_message(item, scope_key=scope_key)
                except (TypeError, ValueError):
                    raise self._invalid_response() from None
                if isinstance(normalized, MailRemoval):
                    removals.append(normalized)
                else:
                    messages.append(normalized)

            next_link = self._optional_string(payload, "@odata.nextLink")
            delta_link = self._optional_string(payload, "@odata.deltaLink")
            if next_link is not None and delta_link is not None:
                raise PermanentProviderError(
                    error_code="microsoft_mail_pagination_invalid",
                    message="Microsoft mail pagination is invalid",
                )
            safe_next = (
                self._validate_delta_url(next_link, scope_key=scope_key) if next_link else None
            )
            safe_delta = (
                self._validate_delta_url(delta_link, scope_key=scope_key) if delta_link else None
            )
            if safe_next is None and safe_delta is None:
                raise PermanentProviderError(
                    error_code="microsoft_mail_delta_missing_cursor",
                    message="Microsoft mail delta response is invalid",
                )
            yield MailSyncPage(
                tuple(messages),
                safe_next,
                safe_delta,
                removals=tuple(removals),
            )
            if safe_next is None:
                return
            current_url = safe_next
        raise PermanentProviderError(
            error_code="microsoft_mail_pagination_invalid",
            message="Microsoft mail pagination is invalid",
        )

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        cursor_scope: str | None = None,
    ) -> Mapping[str, object]:
        """流式执行只读 GET，并在解析前实施单响应及整链双重预算。"""
        for attempt in range(2):
            timeout = httpx.Timeout(
                MICROSOFT_MAIL_TIMEOUT_SECONDS,
                connect=MICROSOFT_MAIL_CONNECT_TIMEOUT_SECONDS,
            )
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                ) as client, client.stream(
                    "GET",
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/json",
                        "Prefer": _PREFER_HEADER,
                    },
                ) as response:
                    if response.status_code == 401:
                        if (
                            attempt == 0
                            and not self._refresh_attempted
                            and self._refresh_access_token is not None
                        ):
                            self._refresh_attempted = True
                            self._access_token = await self._refresh_access_token()
                            continue
                        raise UserActionRequiredError(
                            error_code="microsoft_reauthorization_required",
                            message="Microsoft authorization requires user action",
                        )
                    if response.status_code == 403:
                        raise UserActionRequiredError(
                            error_code="microsoft_mail_permission_required",
                            message="Microsoft mail read permission requires user action",
                        )
                    if response.status_code in {404, 410} and cursor_scope is not None:
                        raise MailCursorExpiredError("microsoft", cursor_scope)
                    if response.status_code == 429:
                        raise TransientProviderError(
                            error_code="microsoft_mail_rate_limited",
                            message="Microsoft mail is temporarily rate limited",
                            retry_after=self._retry_after(response),
                        )
                    if response.status_code >= 500:
                        raise TransientProviderError(
                            error_code="microsoft_mail_service_unavailable",
                            message="Microsoft mail is temporarily unavailable",
                            retry_after=self._retry_after(response),
                        )
                    if not 200 <= response.status_code < 300:
                        raise PermanentProviderError(
                            error_code="microsoft_mail_request_rejected",
                            message="Microsoft mail request was rejected",
                        )

                    content_length = self._trusted_content_length(response)
                    if content_length is not None:
                        if content_length > MICROSOFT_MAIL_MAX_RESPONSE_BYTES:
                            raise self._response_too_large()
                        if (
                            self._chain_wire_bytes + content_length
                            > MICROSOFT_MAIL_MAX_CHAIN_BYTES
                        ):
                            raise self._sync_budget_exceeded()

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        decoded_size = len(body) + len(chunk)
                        if decoded_size > MICROSOFT_MAIL_MAX_RESPONSE_BYTES:
                            raise self._response_too_large()
                        # ``aiter_bytes`` 返回经过 content-encoding 解码的内容；HTTPX 独立
                        # 暴露的下载累计值才是传输链 wire 事实，不能用 decoded 长度代替。
                        if (
                            self._chain_wire_bytes + response.num_bytes_downloaded
                            > MICROSOFT_MAIL_MAX_CHAIN_BYTES
                        ):
                            raise self._sync_budget_exceeded()
                        body.extend(chunk)
                    # decoder 可能在某些 raw chunk 上暂不产出 decoded chunk；迭代结束后再
                    # 检查一次，确保任何已下载 wire 都在 JSON 解析前进入链预算。
                    response_wire_bytes = response.num_bytes_downloaded
                    if (
                        self._chain_wire_bytes + response_wire_bytes
                        > MICROSOFT_MAIL_MAX_CHAIN_BYTES
                    ):
                        raise self._sync_budget_exceeded()
            except httpx.TimeoutException:
                raise TransientProviderError(
                    error_code="microsoft_mail_timeout",
                    message="Microsoft mail request timed out",
                ) from None
            except httpx.RequestError:
                raise TransientProviderError(
                    error_code="microsoft_mail_request_failed",
                    message="Microsoft mail request failed",
                ) from None
            try:
                payload = json.loads(body)
            except (TypeError, ValueError):
                raise self._invalid_response() from None
            if not isinstance(payload, Mapping):
                raise self._invalid_response()
            normalized_size = len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            if (
                self._chain_normalized_bytes + normalized_size
                > MICROSOFT_MAIL_MAX_NORMALIZED_BYTES
            ):
                raise self._sync_budget_exceeded()
            self._chain_wire_bytes += response_wire_bytes
            self._chain_normalized_bytes += normalized_size
            return payload
        raise AssertionError("Microsoft mail request retry loop exhausted")

    @classmethod
    def _normalize_scope(cls, item: Mapping[str, object]) -> MailScope:
        """把 Graph folder 条目收窄为安全 scope/display projection。"""
        scope_key = cls._required_identifier(item, "id")
        display_name = cls._required_text(item, "displayName")
        well_known = item.get("wellKnownName")
        if well_known is not None and not isinstance(well_known, str):
            raise cls._invalid_response()
        normalized_well_known = well_known.casefold() if isinstance(well_known, str) else None
        return MailScope(scope_key, display_name, normalized_well_known)

    @classmethod
    def _normalize_message(
        cls,
        item: Mapping[str, object],
        *,
        scope_key: str,
    ) -> MailMessage | MailRemoval:
        """规范化一条 Graph message 或 tombstone，不让供应商字典越过 integrations。"""
        provider_message_id = cls._required_identifier(
            item,
            "id",
            max_length=MICROSOFT_MAIL_MAX_PERSISTED_ID_LENGTH,
        )
        removed = item.get("@removed")
        if removed is not None:
            if not isinstance(removed, Mapping):
                raise ValueError("removed is invalid")
            reason = removed.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise ValueError("removed reason is invalid")
            return MailRemoval(
                provider_message_id=provider_message_id,
                mailbox_scope_key=scope_key,
                reason=reason,
            )

        conversation_id = cls._required_identifier(
            item,
            "conversationId",
            max_length=MICROSOFT_MAIL_MAX_PERSISTED_ID_LENGTH,
        )
        internet_id = item.get("internetMessageId")
        if internet_id is not None and not isinstance(internet_id, str):
            raise ValueError("internetMessageId is invalid")
        sender = cls._address(item.get("from"))
        recipients_by_kind = {
            "to": cls._addresses(item.get("toRecipients")),
            "cc": cls._addresses(item.get("ccRecipients")),
            "bcc": cls._addresses(item.get("bccRecipients")),
        }
        recipients = tuple(
            recipient for key in ("to", "cc", "bcc") for recipient in recipients_by_kind[key]
        )
        subject_value = item.get("subject", "")
        if not isinstance(subject_value, str):
            raise TypeError("subject is invalid")
        body_value = item.get("body")
        if not isinstance(body_value, Mapping):
            raise TypeError("body is invalid")
        content_type = body_value.get("contentType")
        content = body_value.get("content")
        if not isinstance(content_type, str) or not isinstance(content, str):
            raise TypeError("body fields are invalid")
        normalized_content_type = content_type.casefold()
        if normalized_content_type not in {"text", "html"}:
            raise ValueError("body content type is invalid")
        body = (
            cls._clean_html(content)
            if normalized_content_type == "html"
            else cls._clean_plain(content)
        )
        received_at = cls._datetime(item, "receivedDateTime")
        provider_updated_at = cls._datetime(item, "lastModifiedDateTime")
        sent_value = item.get("sentDateTime")
        sent_at = (
            cls._datetime_value(sent_value, "sentDateTime") if sent_value is not None else None
        )
        categories = cls._string_sequence(item.get("categories", ()), "categories")
        provider_url = item.get("webLink", "")
        if not isinstance(provider_url, str):
            raise TypeError("webLink is invalid")
        headers = {
            "from": cls._format_address(sender),
            **{
                key: ", ".join(cls._format_address(address) for address in addresses)
                for key, addresses in recipients_by_kind.items()
                if addresses
            },
        }
        if internet_id:
            headers["message-id"] = internet_id
        return MailMessage(
            provider_message_id=provider_message_id,
            provider_thread_id=conversation_id,
            provider_conversation_id=conversation_id,
            internet_message_id=internet_id,
            mailbox_scope_key=scope_key,
            sender=sender,
            recipients=recipients,
            subject=subject_value,
            sanitized_body=body,
            received_at=received_at,
            sent_at=sent_at,
            provider_updated_at=provider_updated_at,
            labels=categories,
            normalized_reply_headers=headers,
            provider_url=provider_url,
        )

    @classmethod
    def _values(cls, payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
        """读取并限制 Graph collection 的 ``value`` 数组。"""
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
    def _required_identifier(
        cls,
        payload: Mapping[str, object],
        key: str,
        *,
        max_length: int = MICROSOFT_MAIL_MAX_ID_LENGTH,
    ) -> str:
        """读取不含空白/控制字符且不超过调用方持久边界的 provider ID。"""
        value = payload.get(key)
        if (
            not isinstance(value, str)
            or value == ""
            or value.strip() != value
            or len(value) > max_length
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError(f"{key} is invalid")
        return value

    @classmethod
    def _required_text(cls, payload: Mapping[str, object], key: str) -> str:
        """读取可展示文本并移除换行/控制字符造成的日志或 UI 注入。"""
        value = payload.get(key)
        if not isinstance(value, str) or len(value) > MICROSOFT_MAIL_MAX_STRING_LENGTH:
            raise ValueError(f"{key} is invalid")
        normalized = value.replace("\r", " ").replace("\n", " ").strip()
        if normalized == "":
            raise ValueError(f"{key} is invalid")
        return normalized

    @classmethod
    def _address(cls, value: object) -> Mapping[str, str]:
        """规范化 Graph ``from.emailAddress``。"""
        if not isinstance(value, Mapping):
            raise TypeError("from is invalid")
        email_address = value.get("emailAddress")
        if not isinstance(email_address, Mapping):
            raise TypeError("from emailAddress is invalid")
        return cls._address_mapping(email_address)

    @classmethod
    def _addresses(cls, value: object) -> tuple[Mapping[str, str], ...]:
        """规范化收件人数组并保持稳定顺序，畸形条目必须整体失败。"""
        if not isinstance(value, list):
            raise TypeError("recipients are invalid")
        result: list[Mapping[str, str]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise TypeError("recipient is invalid")
            result.append(cls._address_mapping(item.get("emailAddress")))
        return tuple(result)

    @classmethod
    def _address_mapping(cls, value: object) -> Mapping[str, str]:
        """验证并规范化单个 Graph 邮箱地址，拒绝不可回复的供应商文本。"""
        if not isinstance(value, Mapping):
            raise TypeError("emailAddress is invalid")
        address = value.get("address")
        name = value.get("name", "")
        if (
            not isinstance(address, str)
            or address == ""
            or address.strip() != address
            or not isinstance(name, str)
        ):
            raise ValueError("emailAddress is invalid")
        normalized_address = normalize_mailbox_address(address)
        return MappingProxyType(
            {
                "name": name.replace("\r", " ").replace("\n", " "),
                "email": normalized_address,
            }
        )

    @classmethod
    def _string_sequence(cls, value: object, field_name: str) -> tuple[str, ...]:
        """验证类别数组，不把未知 JSON 容器传出。"""
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"{field_name} is invalid")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or len(item) > MICROSOFT_MAIL_MAX_STRING_LENGTH:
                raise ValueError(f"{field_name} is invalid")
            result.append(item)
        return tuple(result)

    @classmethod
    def _datetime(cls, payload: Mapping[str, object], key: str) -> datetime:
        """读取必填 ISO 时间并规范为 UTC。"""
        value = payload.get(key)
        return cls._datetime_value(value, key)

    @staticmethod
    def _datetime_value(value: object, key: str) -> datetime:
        """解析带时区 ISO 时间，拒绝 naive 值。"""
        if not isinstance(value, str):
            raise TypeError(f"{key} is invalid")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{key} is invalid") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{key} is invalid")
        return parsed.astimezone(UTC)

    @staticmethod
    def _clean_plain(value: str) -> str:
        """清洗纯文本签名与引用历史，只保留当前正文语义。"""
        lines: list[str] = []
        for line in value.replace("\r\n", "\n").split("\n"):
            stripped = line.strip()
            if (
                stripped == "--"
                or stripped.startswith(">")
                or stripped.casefold().startswith("on ")
            ):
                break
            if stripped:
                lines.append(stripped)
        return "\n".join(lines)

    @classmethod
    def _clean_html(cls, value: str) -> str:
        """删除主动内容/引用节点后提取纯文本，不持久 HTML。"""
        soup = BeautifulSoup(value, "html.parser")
        for node in soup.select("script, style, blockquote, .gmail_quote, .signature"):
            node.decompose()
        text = html.unescape(soup.get_text("\n", strip=True))
        return cls._clean_plain(text)

    @staticmethod
    def _format_address(address: Mapping[str, str]) -> str:
        """生成仅用于规范 reply header 的安全地址文本。"""
        name = address.get("name", "")
        email_address = address.get("email", "")
        return f"{name} <{email_address}>" if name else email_address

    @staticmethod
    def _optional_string(payload: Mapping[str, object], key: str) -> str | None:
        """读取可选 URL/string；畸形类型视为响应错误而非静默丢弃。"""
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or value == "":
            raise PermanentProviderError(
                error_code="microsoft_mail_invalid_response",
                message="Microsoft mail response is invalid",
            )
        return value

    @staticmethod
    def _validate_utc(value: datetime) -> None:
        """验证调用方传入的时间带明确零时区偏移。"""
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("since must be explicit UTC")

    @staticmethod
    def _utc_text(value: datetime) -> str:
        """把 UTC 时间规范成 Graph 要求的 ``Z`` 表示。"""
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def _validate_delta_url(cls, value: str, *, scope_key: str) -> str:
        """验证 cursor 是当前 folder 的精确 Graph absolute URL。"""
        url = cls._validate_absolute_graph_url(
            value,
            error_code="microsoft_mail_invalid_delta_url",
            message="Microsoft mail delta URL is invalid",
        )
        expected_path = f"/v1.0/me/mailFolders/{quote(scope_key, safe='')}/messages/delta"
        if urlsplit(url).path != expected_path:
            raise PermanentProviderError(
                error_code="microsoft_mail_invalid_delta_url",
                message="Microsoft mail delta URL is invalid",
            )
        return url

    @classmethod
    def _validate_collection_url(cls, value: str, *, expected_path: str) -> str:
        """验证普通 collection nextLink 仍绑定调用方声明的精确 Graph path。"""
        url = cls._validate_absolute_graph_url(
            value,
            error_code="microsoft_mail_invalid_collection_url",
            message="Microsoft mail collection URL is invalid",
        )
        if urlsplit(url).path != expected_path:
            raise PermanentProviderError(
                error_code="microsoft_mail_invalid_collection_url",
                message="Microsoft mail collection URL is invalid",
            )
        return url

    @staticmethod
    def _validate_absolute_graph_url(value: str, *, error_code: str, message: str) -> str:
        """只接受精确 HTTPS Graph 主机，禁止重定向、userinfo、port 与 fragment。"""
        if not isinstance(value, str):
            raise PermanentProviderError(
                error_code=error_code,
                message=message,
            )
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            # 畸形 IPv6/port 解析错误可能包含 opaque URL；切断异常链并仅暴露固定错误。
            raise PermanentProviderError(
                error_code=error_code,
                message=message,
            ) from None
        if (
            parsed.scheme != "https"
            or parsed.netloc != MICROSOFT_GRAPH_HOST
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.fragment != ""
            or parsed.path == ""
        ):
            raise PermanentProviderError(
                error_code=error_code,
                message=message,
            )
        return value

    @staticmethod
    def _trusted_content_length(response: httpx.Response) -> int | None:
        """只信任非负十进制 Content-Length；畸形头交由实际流式计数收窄。"""
        raw = response.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value >= 0 else None

    @staticmethod
    def _odata_literal(value: str) -> str:
        """转义 OData 字符串 literal 中的单引号。"""
        return value.replace("'", "''")

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        """仅接受有限非负整数 Retry-After。"""
        try:
            value = int(response.headers.get("Retry-After", ""))
            if value < 0:
                return None
            float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value

    @staticmethod
    def _invalid_response() -> PermanentProviderError:
        """创建不带字段值/正文的永久解析错误。"""
        return PermanentProviderError(
            error_code="microsoft_mail_invalid_response",
            message="Microsoft mail response is invalid",
        )

    @staticmethod
    def _response_too_large() -> PermanentProviderError:
        """创建固定响应大小错误，不回显供应商长度或 URL。"""
        return PermanentProviderError(
            error_code="microsoft_mail_response_too_large",
            message="Microsoft mail response is too large",
        )

    @staticmethod
    def _sync_budget_exceeded() -> PermanentProviderError:
        """创建不含 URL、长度或正文的整链容量错误。"""
        return PermanentProviderError(
            error_code="microsoft_mail_sync_budget_exceeded",
            message="Microsoft mail sync budget was exceeded",
        )

    def _reset_refresh_budget(self) -> None:
        """重置单次只读链的刷新、wire 与规范化容量预算。"""
        self._refresh_attempted = False
        self._chain_wire_bytes = 0
        self._chain_normalized_bytes = 0


__all__ = [
    "MICROSOFT_GRAPH_BASE_URL",
    "MICROSOFT_MAIL_MAX_CHAIN_BYTES",
    "MICROSOFT_MAIL_MAX_NORMALIZED_BYTES",
    "MICROSOFT_MAIL_MAX_RESPONSE_BYTES",
    "MicrosoftMailAdapter",
]
