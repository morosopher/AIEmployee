"""任务历史专用签名游标；只承载分页元数据，不作为认证或执行授权。"""

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ai_employee.application.task_history import (
    HistoryCursorState,
    HistoryKey,
    TaskHistoryCursorError,
    TaskHistoryFilters,
    history_filters_hash,
)
from ai_employee.application.task_history_dates import (
    canonical_utc_timestamp,
    parse_history_timestamp,
)

_MAX_LENGTH = 2048
_FIELDS = frozenset({"v", "user_id", "filters_hash", "direction", "upper", "anchor", "issued_at"})
_KEY_FIELDS = frozenset({"created_at", "task_id"})
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_HASH = re.compile(r"[0-9a-f]{64}")
_KEY_ERROR = "task_history_cursor_key_invalid"


def _b64encode(value: bytes) -> str:
    """产生唯一无填充 Base64URL 表示，避免同字节存在多个游标文本。"""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    """严格拒绝填充、非 URL 字符和非零尾部位，再返回有界字节。"""
    if not _BASE64URL.fullmatch(value):
        raise TaskHistoryCursorError()
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _b64encode(decoded) != value:
        raise TaskHistoryCursorError()
    return decoded


def _canonical(value: object) -> bytes:
    """固定 UTF8、字段排序和紧凑分隔符；非有限数字永不参与签名。"""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """在 JSON 解析边界拒绝所有层级的重复键，避免最后值覆盖语义。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TaskHistoryCursorError()
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    """JSON 的 NaN/Infinity 扩展不是合法分页元数据。"""
    raise TaskHistoryCursorError()


def _object(value: object, fields: frozenset[str]) -> dict[str, object]:
    """把第三方 JSON 边界立即收窄为字段精确的对象；后续逐字段验证。"""
    if not isinstance(value, dict) or value.keys() != fields:
        raise TaskHistoryCursorError()
    return cast(dict[str, object], value)


def _string(value: object) -> str:
    """禁止整数、布尔、空值及容器隐式转为文本。"""
    if not isinstance(value, str):
        raise TaskHistoryCursorError()
    return value


def _uuid(value: object) -> UUID:
    """只接受小写连字符规范 UUID，保证排序键表示唯一。"""
    text = _string(value)
    result = UUID(text)
    if str(result) != text:
        raise TaskHistoryCursorError()
    return result


def _key(value: object) -> HistoryKey:
    """验证固定键字段及六位微秒 UTC 时间，不依赖边界行存在。"""
    obj = _object(value, _KEY_FIELDS)
    return HistoryKey(parse_history_timestamp(_string(obj["created_at"])), _uuid(obj["task_id"]))


def _state(value: object) -> HistoryCursorState:
    """将已认证 JSON 收窄为不可变状态，并校验版本、枚举及上界。

    Returns:
        仅含已规范化元数据的状态；anchor 可以等于 upper。

    Raises:
        ValueError: schema、时间、UUID 或排序界无效；调用边界统一去除载荷。
    """
    obj = _object(value, _FIELDS)
    if type(obj["v"]) is not int or obj["v"] != 1:
        raise TaskHistoryCursorError()
    direction = obj["direction"]
    if direction != "older" and direction != "newer":
        raise TaskHistoryCursorError()
    filters_hash = _string(obj["filters_hash"])
    if not _HASH.fullmatch(filters_hash):
        raise TaskHistoryCursorError()
    upper, anchor = _key(obj["upper"]), _key(obj["anchor"])
    if anchor > upper:
        raise TaskHistoryCursorError()
    return HistoryCursorState(
        _uuid(obj["user_id"]),
        filters_hash,
        direction,
        upper,
        anchor,
        parse_history_timestamp(_string(obj["issued_at"])),
    )


def _key_json(key: HistoryKey) -> dict[str, str]:
    """把内部排序键转换成唯一 UTC 表示，拒绝 naive 时间。"""
    return {"created_at": canonical_utc_timestamp(key.created_at), "task_id": str(key.task_id)}


class TaskHistoryCursorCodec:
    """使用 HKDF 隔离的 HMAC-SHA256 编解码器，不保留或复用 AEAD 私有密钥。

    主密钥轮换后旧游标自然失效；调用方仍必须独立认证当前用户。
    时间由调用方注入，不读取宿主时钟，不维护可变分页状态。
    """

    def __init__(self, master_key: bytes) -> None:
        """验证 32 字节主密钥并仅保存用途隔离后的签名材料。

        Args:
            master_key: 已由组合根读取的应用主密钥，禁止日志输出。

        Raises:
            ValueError: 类型或长度错误，固定配置错误不含密钥内容。
        """
        if type(master_key) is not bytes or len(master_key) != 32:
            raise ValueError(_KEY_ERROR)
        self._signing_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"ai-employee/task-history-cursor/v1",
        ).derive(master_key)

    @classmethod
    def from_file(cls, path: Path) -> "TaskHistoryCursorCodec":
        """显式调用时读取现有 Base64URL 主密钥，不在导入或应用创建时访问 Secret。

        Args:
            path: 组合根提供的现有 Secret 路径；异步调用方应在线程中读取。

        Returns:
            已派生专用签名材料的编码器，兼容文件首尾空白及标准填充。

        Raises:
            OSError: 原样传播文件系统故障，不能伪装成客户端游标错误。
            ValueError: 格式或长度不合法；隐藏原始解码异常，防止泄露秘密。
        """
        try:
            encoded = path.read_text(encoding="utf-8").strip()
            unpadded = encoded.rstrip("=")
            if encoded not in (unpadded, unpadded + "=" * (-len(unpadded) % 4)):
                raise ValueError(_KEY_ERROR)
            key = _b64decode(unpadded)
            return cls(key)
        except (ValueError, UnicodeError):
            raise ValueError(_KEY_ERROR) from None

    def encode(self, state: HistoryCursorState) -> str:
        """签发固定 v1 元数据，编码前验证内部状态，不签发无效分页边界。

        Args:
            state: 只读查询构造的用户、过滤哈希、方向、上界、锚点与签发时间。

        Returns:
            最长 2048 字符的无填充 Base64URL 签名游标。

        Raises:
            TaskHistoryCursorError: 状态或时间非法，固定错误无载荷。
        """
        try:
            obj = {
                "v": 1,
                "user_id": str(state.user_id),
                "filters_hash": state.filters_hash,
                "direction": state.direction,
                "upper": _key_json(state.upper),
                "anchor": _key_json(state.anchor),
                "issued_at": canonical_utc_timestamp(state.issued_at),
            }
            _state(obj)
            payload = _canonical(obj)
            signature = hmac.digest(self._signing_key, payload, hashlib.sha256)
            token = f"v1.{_b64encode(payload)}.{_b64encode(signature)}"
            if len(token) > _MAX_LENGTH:
                raise TaskHistoryCursorError()
            return token
        except (ValueError, OverflowError):
            raise TaskHistoryCursorError() from None

    def decode(
        self, token: str, *, filters: TaskHistoryFilters, now: datetime
    ) -> HistoryCursorState:
        """先限长和认证，再严格解析并验证当前用户、完整筛选和精确时效。

        Args:
            token: 客户端不透明游标，不能代替认证。
            filters: 当前认证用户及规范化后的筛选，包括时区和页大小。
            now: 从 Clock 注入的带时区当前时间。

        Returns:
            已通过签名、归属、过滤及边界检查的不可变分页状态。

        Raises:
            TaskHistoryCursorError: 任一格式、签名、绑定或时效失败，统一无载荷错误。
            其他系统异常原样传播，不隐瞒未知故障。
        """
        if not isinstance(token, str) or len(token) > _MAX_LENGTH:
            raise TaskHistoryCursorError()
        try:
            version, encoded, encoded_mac = token.split(".")
            if version != "v1":
                raise TaskHistoryCursorError()
            payload, mac = _b64decode(encoded), _b64decode(encoded_mac)
            expected = hmac.digest(self._signing_key, payload, hashlib.sha256)
            if len(mac) != 32 or not hmac.compare_digest(mac, expected):
                raise TaskHistoryCursorError()
            # 认证前不解析 JSON；认证后仍不能信任签发方曾执行 schema 验证。
            obj: object = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            state = _state(obj)
            if _canonical(obj) != payload:
                raise TaskHistoryCursorError()
            current = parse_history_timestamp(canonical_utc_timestamp(now))
            age = current - state.issued_at
            if (
                state.user_id != filters.user_id
                or not hmac.compare_digest(state.filters_hash, history_filters_hash(filters))
                or age >= timedelta(hours=24)
                or age < -timedelta(seconds=60)
            ):
                raise TaskHistoryCursorError()
            return state
        except (ValueError, UnicodeError, OverflowError, RecursionError):
            # JSON/UUID 原始异常可能持有载荷；对外只呈现固定错误及被抑制的异常上下文。
            raise TaskHistoryCursorError() from None
