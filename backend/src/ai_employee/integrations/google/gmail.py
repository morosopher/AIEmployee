"""实现 Gmail REST 只读访问、内容规范化与刷新感知错误映射。"""

import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from email.header import decode_header
from email.message import EmailMessage
from email.utils import getaddresses
from html import unescape
from typing import cast

import httpx
from bs4 import BeautifulSoup

from ai_employee.application.ports.gmail import (
    GmailMessage,
    GmailSyncPage,
    HistoryCursorExpiredError,
    TransientProviderError,
    UserActionRequiredError,
)

GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
_RefreshAccessToken = Callable[[], Awaitable[str]]
_MarkExpired = Callable[[], Awaitable[None]]


class GmailAdapter:
    """以 httpx 调用 Gmail REST 并只输出可持久化的规范化事实。

    ``refresh_access_token`` 由组装层注入，负责把刷新后的密文和过期时间原子保存；本适配器
    因而只暂存短生命周期明文。附件端点从不调用，所有响应中的原始 MIME 结构也不会离开本类。
    """

    def __init__(
        self,
        *,
        access_token: str,
        refresh_access_token: _RefreshAccessToken | None = None,
        mark_expired: _MarkExpired | None = None,
    ) -> None:
        """保存已解密 access token 与可选的一次刷新回调。

        Args:
            access_token: 受控内存中的短期 bearer token，绝不记录。
            refresh_access_token: 首次 401 时获取并原子保存新 token 的能力。
            mark_expired: 第二次 401 后把连接标为 ``expired`` 的持久化能力。
        """
        self._access_token = access_token
        self._refresh_access_token = refresh_access_token
        self._mark_expired = mark_expired

    async def initial_pages(self) -> AsyncIterator[GmailSyncPage]:
        """分页读取 ``newer_than:7d`` 初次候选并逐条按 full 格式获取消息。

        Yields:
            已获取完整消息、但从未获取附件的规范化同步页。

        Raises:
            TransientProviderError: Google 限流、5xx 或网络暂态失败。
            UserActionRequiredError: token 经过一次刷新重试仍为 401。
        """
        page_token: str | None = None
        while True:
            parameters: dict[str, str] = {"q": "newer_than:7d"}
            if page_token is not None:
                parameters["pageToken"] = page_token
            payload = await self.execute_request("/messages", parameters)
            message_ids = self._message_ids(cast(dict[str, object], payload).get("messages"))
            messages = await self._load_messages(message_ids)
            page_token = self._optional_string(cast(dict[str, object], payload).get("nextPageToken"))
            latest = self._latest_history_id(cast(dict[str, object], payload), messages)
            yield GmailSyncPage(tuple(messages), page_token, latest)
            if page_token is None:
                return

    async def history_pages(self, cursor: str) -> AsyncIterator[GmailSyncPage]:
        """分页读取 Gmail history，并把新增和标签变更统一规范为消息重取。

        Args:
            cursor: 上一次全部成功提交后的 Gmail history ID。

        Yields:
            包含新消息或标签变化消息的同步页。

        Raises:
            HistoryCursorExpiredError: Gmail 返回 404，调用方必须改为七日重同步。
        """
        page_token: str | None = None
        while True:
            parameters = {"startHistoryId": cursor}
            if page_token is not None:
                parameters["pageToken"] = page_token
            try:
                payload = await self.execute_request("/history", parameters)
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 404:
                    raise HistoryCursorExpiredError from error
                raise
            record = cast(dict[str, object], payload)
            messages = await self._load_messages(self._history_message_ids(record.get("history")))
            page_token = self._optional_string(record.get("nextPageToken"))
            yield GmailSyncPage(tuple(messages), page_token, self._latest_history_id(record, messages))
            if page_token is None:
                return

    async def execute_request(self, path: str, parameters: dict[str, str]) -> object:
        """执行一次只读请求，并在首个 401 后严格刷新重试一次。

        该方法是端口公开分页调用的唯一 HTTP 出口，保证不会出现调用方各自实现不同次数的
        401 重试。第二个 401 先写 ``expired`` 再抛用户可操作错误，防止后台任务无限重试。
        """
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
                    response = await client.get(
                        f"{GMAIL_API_BASE_URL}{path}",
                        params=parameters,
                        headers={"Authorization": f"Bearer {self._access_token}"},
                    )
            except httpx.TimeoutException as error:
                raise TransientProviderError(
                    error_code="google_timeout",
                    message="Google Gmail request timed out",
                ) from error
            except httpx.RequestError as error:
                raise TransientProviderError(
                    error_code="google_request_failed",
                    message="Google Gmail request failed",
                ) from error
            if response.status_code == 401:
                if attempt == 0 and self._refresh_access_token is not None:
                    self._access_token = await self._refresh_access_token()
                    continue
                if self._mark_expired is not None:
                    await self._mark_expired()
                raise UserActionRequiredError(
                    error_code="google_reauthorization_required",
                    message="Google Gmail authorization requires user action",
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise TransientProviderError(
                    error_code=(
                        "google_rate_limited" if response.status_code == 429 else "google_service_unavailable"
                    ),
                    message="Google Gmail is temporarily unavailable",
                    retry_after=self._retry_after(response),
                )
            response.raise_for_status()
            return response.json()
        raise AssertionError("Gmail request retry loop exhausted")

    async def _load_messages(self, message_ids: tuple[str, ...]) -> list[GmailMessage]:
        """按 message ID 获取 full 格式；从不访问 Gmail attachment API。"""
        messages: list[GmailMessage] = []
        for message_id in message_ids:
            payload = await self.execute_request(f"/messages/{message_id}", {"format": "full"})
            messages.append(self._normalize_message(cast(dict[str, object], payload)))
        return messages

    @staticmethod
    def _message_ids(value: object) -> tuple[str, ...]:
        """从 list 响应严格提取有限的消息 ID。"""
        if not isinstance(value, list):
            return ()
        return tuple(
            item["id"] for item in value if isinstance(item, dict) and isinstance(item.get("id"), str)
        )

    @classmethod
    def _history_message_ids(cls, value: object) -> tuple[str, ...]:
        """合并 history 中新增、标签加入及标签移除涉及的消息，保持稳定去重顺序。"""
        if not isinstance(value, list):
            return ()
        ids: dict[str, None] = {}
        for history in value:
            if not isinstance(history, dict):
                continue
            for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                changes = history.get(key)
                if not isinstance(changes, list):
                    continue
                for change in changes:
                    if isinstance(change, dict) and isinstance(change.get("message"), dict):
                        message = cast(dict[str, object], change["message"])
                        message_id = message.get("id")
                        if isinstance(message_id, str):
                            ids[message_id] = None
        return tuple(ids)

    @staticmethod
    def _optional_string(value: object) -> str | None:
        """把供应商可选字符串字段收窄，忽略畸形值。"""
        return value if isinstance(value, str) else None

    @classmethod
    def _latest_history_id(cls, payload: dict[str, object], messages: list[GmailMessage]) -> str:
        """优先响应顶层游标，缺失时使用已读取消息的最大数值 history ID。"""
        history_id = cls._optional_string(payload.get("historyId"))
        if history_id is not None:
            return history_id
        return max((message.history_id for message in messages), key=lambda item: int(item), default="")

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        """解析正整数 Retry-After 秒数，日期格式或畸形值留给耐久退避策略处理。"""
        try:
            value = int(response.headers.get("Retry-After", ""))
        except ValueError:
            return None
        return value if value >= 0 else None

    @classmethod
    def _normalize_message(cls, payload: dict[str, object]) -> GmailMessage:
        """验证 full 消息必要字段，解码头部并提取优先纯文本的安全正文。"""
        message_id = cls._required_string(payload, "id")
        thread_id = cls._required_string(payload, "threadId")
        history_id = cls._required_string(payload, "historyId")
        received_at = datetime.fromtimestamp(int(cls._required_string(payload, "internalDate")) / 1000, UTC)
        raw_payload = payload.get("payload")
        if not isinstance(raw_payload, dict):
            raise TypeError("Gmail message payload is invalid")
        headers = cls._headers(raw_payload.get("headers"))
        sender = cls._addresses(headers.get("from", ""))[0] if headers.get("from") else {"name": "", "email": ""}
        recipients = cls._addresses(
            ", ".join(headers[name] for name in ("to", "cc", "bcc") if headers.get(name))
        )
        subject = headers.get("subject", "")
        text, html = cls._body_parts(raw_payload)
        body = cls._clean_plain(text) if text else cls._clean_html(html)
        labels = payload.get("labelIds")
        return GmailMessage(
            message_id=message_id,
            thread_id=thread_id,
            history_id=history_id,
            received_at=received_at,
            sender=sender,
            recipients=recipients,
            subject=subject,
            snippet=cls._optional_string(payload.get("snippet")) or "",
            normalized_body=body,
            labels=tuple(label for label in labels if isinstance(label, str)) if isinstance(labels, list) else (),
            headers=headers,
            provider_url=f"https://mail.google.com/mail/u/0/#all/{thread_id}",
        )

    @staticmethod
    def _required_string(payload: dict[str, object], key: str) -> str:
        """读取 Gmail 必填字符串字段，在边界立即拒绝不完整响应。"""
        value = payload.get(key)
        if not isinstance(value, str):
            raise TypeError(f"Gmail response missing {key}")
        return value

    @classmethod
    def _headers(cls, value: object) -> dict[str, str]:
        """使用标准库 RFC 2047 解码器规范化需要持久化的邮件头。"""
        if not isinstance(value, list):
            return {}
        result: dict[str, str] = {}
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value"), str):
                result[item["name"].lower()] = cls._decode_header(item["value"])
        return result

    @staticmethod
    def _decode_header(value: str) -> str:
        """按标准库 email 规则解码折叠或编码的 header 文本。"""
        pieces: list[str] = []
        for part, charset in decode_header(value):
            pieces.append(part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part)
        return "".join(pieces).replace("\r", " ").replace("\n", " ").strip()

    @staticmethod
    def _addresses(value: str) -> tuple[dict[str, str], ...]:
        """把 RFC 地址列表规范为姓名和邮箱的有限字典。"""
        return tuple(
            {"name": name, "email": address} for name, address in getaddresses([value]) if address
        )

    @classmethod
    def _body_parts(cls, payload: dict[str, object]) -> tuple[str | None, str | None]:
        """使用标准库 ``email`` 遍历 MIME，优先首个内联纯文本后回退 HTML。

        Gmail REST 返回的 part tree 先转换为 ``EmailMessage``，使 charset、filename 和
        Content-Disposition 均由标准库按 MIME 语义解析。仅使用响应内已经携带的数据，绝不
        请求 attachment API；具有文件名或 ``attachment`` disposition 的文本部件一律跳过。
        """
        plain: str | None = None
        html: str | None = None
        message = cls._as_email_message(payload)
        for part in message.walk():
            if part.is_multipart() or part.get_filename() or part.get_content_disposition() == "attachment":
                continue
            mime_type = part.get_content_type()
            if mime_type not in {"text/plain", "text/html"}:
                continue
            decoded = cls._decode_mime_text(part)
            if mime_type == "text/plain" and plain is None:
                plain = decoded
            elif mime_type == "text/html" and html is None:
                html = decoded
        return plain, html

    @classmethod
    def _as_email_message(cls, payload: dict[str, object]) -> EmailMessage:
        """将 Gmail JSON part tree 还原为标准库 MIME 结构，供统一处理 disposition 和 charset。"""
        message = EmailMessage()
        headers = payload.get("headers")
        if isinstance(headers, list):
            for header in headers:
                if isinstance(header, dict) and isinstance(header.get("name"), str) and isinstance(
                    header.get("value"), str
                ):
                    message[header["name"]] = header["value"]
        children = payload.get("parts")
        mime_type = payload.get("mimeType")
        if not isinstance(mime_type, str):
            mime_type = "multipart/mixed" if isinstance(children, list) else "text/plain"
        if message.get_content_type() == "text/plain" and "Content-Type" not in message:
            message.set_type(mime_type)
        filename = payload.get("filename")
        if isinstance(filename, str) and filename and message.get_filename() is None:
            message.set_param("name", filename, header="Content-Type")
        if isinstance(children, list):
            message.set_payload(
                [cls._as_email_message(cast(dict[str, object], child)) for child in children if isinstance(child, dict)]
            )
            return message
        body = payload.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, str):
            message.set_payload(cls._decode_body_bytes(data))
        return message

    @staticmethod
    def _decode_body_bytes(value: str) -> bytes:
        """解码 Gmail URL-safe Base64 为原始 MIME bytes，不在此处猜测文本编码。"""
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded)

    @staticmethod
    def _decode_mime_text(part: EmailMessage) -> str:
        """按标准库解析出的 charset 解码内联文本；未知 charset 安全回退 UTF-8 替换。"""
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")

    @staticmethod
    def _clean_plain(value: str | None) -> str:
        """移除空白、常见签名和引用历史，只保留当前文本语义。"""
        if not value:
            return ""
        lines: list[str] = []
        for line in value.replace("\r\n", "\n").split("\n"):
            stripped = line.strip()
            if stripped == "--" or stripped.startswith(">") or stripped.lower().startswith("on "):
                break
            if stripped:
                lines.append(stripped)
        return "\n".join(lines)

    @classmethod
    def _clean_html(cls, value: str | None) -> str:
        """删除主动内容、追踪像素、引用/签名节点和空块后提取纯文本。"""
        if not value:
            return ""
        soup = BeautifulSoup(value, "html.parser")
        for node in soup.select("script, style, blockquote, .gmail_quote, .signature"):
            node.decompose()
        for image in soup.find_all("img"):
            width = str(image.get("width", ""))
            height = str(image.get("height", ""))
            source = str(image.get("src", "")).lower()
            if (
                width in {"1", "1px"}
                or height in {"1", "1px"}
                or any(marker in source for marker in ("track", "pixel", "open?", "beacon"))
            ):
                image.decompose()
        for node in list(soup.find_all()):
            if not node.get_text(" ", strip=True) and node.name not in {"br"}:
                node.decompose()
        text = unescape(soup.get_text("\n", strip=True))
        return cls._clean_plain(text)
