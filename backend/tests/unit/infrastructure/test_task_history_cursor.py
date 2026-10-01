"""使用合成密钥和固定时钟验证游标绑定、规范编码及恶意输入拒绝。"""

import base64
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from ai_employee.application.task_history import (
    HistoryCursorState,
    HistoryKey,
    TaskHistoryCursorError,
    TaskHistoryQuery,
    history_filters_hash,
    normalize_history_filters,
)
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.security.task_history_cursor import TaskHistoryCursorCodec

NOW = datetime(2026, 10, 1, 0, 0, 0, 123456, tzinfo=UTC)
UID = UUID("aaaaaaaa-0000-0000-0000-000000000001")
MASTER = bytes(range(32))
FILTERS = normalize_history_filters(user_id=UID, timezone="UTC", query=TaskHistoryQuery())
KEY = HistoryKey(NOW, UID)
STATE = HistoryCursorState(UID, history_filters_hash(FILTERS), "older", KEY, KEY, NOW)


def _b64(value: bytes) -> str:
    """仅供合成向量的独立 Base64URL 编码。"""
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _signed(payload: bytes) -> str:
    """直接按 RFC 5869 extract/expand 计算期望签名，不调用生产 HKDF 或 codec。"""
    prk = hmac.digest(bytes(32), MASTER, "sha256")
    signing = hmac.digest(prk, b"ai-employee/task-history-cursor/v1\x01", "sha256")
    return f"v1.{_b64(payload)}.{_b64(hmac.digest(signing, payload, 'sha256'))}"


def _payload() -> dict[str, object]:
    """固定元数据向量；字段和值独立于生产编码器。"""
    return {
        "v": 1,
        "user_id": str(UID),
        "filters_hash": hashlib.sha256(
            b'{"created_before":null,"created_from":null,"kind":null,"limit":20,'
            b'"scope":"business","status":null,"timezone":"UTC",'
            b'"user_id":"aaaaaaaa-0000-0000-0000-000000000001"}'
        ).hexdigest(),
        "direction": "older",
        "upper": {"created_at": "2026-10-01T00:00:00.123456Z", "task_id": str(UID)},
        "anchor": {"created_at": "2026-10-01T00:00:00.123456Z", "task_id": str(UID)},
        "issued_at": "2026-10-01T00:00:00.123456Z",
    }


def _json(value: object) -> bytes:
    """生成测试攻击载荷，故意允许非标准浮点值。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _reject(token: str) -> None:
    """所有攻击必须返回相同无载荷异常，避免测试日志泄露原始内容。"""
    with pytest.raises(TaskHistoryCursorError) as caught:
        TaskHistoryCursorCodec(MASTER).decode(token, filters=FILTERS, now=NOW)
    assert str(caught.value) == "task_history_cursor_invalid"


def test_independent_vector_and_round_trip() -> None:
    """固定元数据及独立 RFC 5869 实现锁定字节格式和用途隔离。"""
    expected = _signed(_json(_payload()))
    codec = TaskHistoryCursorCodec(MASTER)
    assert (
        hashlib.sha256(codec.encode(STATE).encode()).digest()
        == hashlib.sha256(expected.encode()).digest()
    )
    assert codec.decode(expected, filters=FILTERS, now=NOW) == STATE
    newer = replace(STATE, direction="newer", anchor=replace(KEY, task_id=UUID(int=1)))
    assert codec.decode(codec.encode(newer), filters=FILTERS, now=NOW) == newer


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", UUID(int=2)),
        ("scope", "all"),
        ("kind", "daily_brief"),
        ("status", TaskStatus.FAILED),
        ("timezone", "Asia/Shanghai"),
        ("limit", 21),
        ("created_from", NOW),
        ("created_before", NOW),
    ],
)
def test_binding(field: str, value: object) -> None:
    """任何归属或规范过滤字段变化必须使旧游标失效。"""
    codec = TaskHistoryCursorCodec(MASTER)
    with pytest.raises(TaskHistoryCursorError):
        codec.decode(codec.encode(STATE), filters=replace(FILTERS, **{field: value}), now=NOW)


@pytest.mark.parametrize(
    "offset,valid",
    [
        (timedelta(hours=24) - timedelta(microseconds=1), True),
        (timedelta(hours=24), False),
        (timedelta(seconds=-60), True),
        (timedelta(seconds=-60, microseconds=-1), False),
    ],
)
def test_exact_time_boundaries(offset: timedelta, valid: bool) -> None:
    """24 小时采用排他边界，未来容差最多 60 秒，保留微秒。"""
    codec = TaskHistoryCursorCodec(MASTER)
    if valid:
        assert codec.decode(codec.encode(STATE), filters=FILTERS, now=NOW + offset) == STATE
    else:
        with pytest.raises(TaskHistoryCursorError):
            codec.decode(codec.encode(STATE), filters=FILTERS, now=NOW + offset)


@pytest.mark.parametrize(
    "field,value",
    [
        ("v", True),
        ("v", 1.0),
        ("v", 2),
        ("v", "1"),
        ("user_id", str(UID).upper()),
        ("user_id", UID.hex),
        ("user_id", 1),
        ("filters_hash", "A" * 64),
        ("filters_hash", False),
        ("direction", "sideways"),
        ("direction", []),
        ("issued_at", "2026-10-01T00:00:00Z"),
        ("issued_at", "2026-10-01T00:00:00.123456"),
        ("issued_at", "2026-02-30T00:00:00.123456Z"),
        ("issued_at", None),
        ("upper", []),
        ("anchor", {"created_at": "2026-10-01T00:00:00.123457Z", "task_id": str(UID)}),
        (
            "anchor",
            {"created_at": "2026-10-01T00:00:00.123456Z", "task_id": str(UUID(int=2**128 - 1))},
        ),
        ("anchor", {"created_at": "2026-10-01T00:00:00.123456Z", "task_id": str(UID), "extra": 0}),
        ("anchor", {"created_at": True, "task_id": str(UID)}),
        ("anchor", {"created_at": "2026-10-01T00:00:00.123456Z", "task_id": UID.hex}),
        ("extra", 0),
    ],
)
def test_signed_wrong_schema(field: str, value: object) -> None:
    """正确签名不能绕过精确 schema、UUID、时间或分页顺序检查。"""
    payload = _payload()
    payload[field] = value
    _reject(_signed(_json(payload)))


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b"null",
        b"\xff",
        b"{",
        b'{"v":1,"v":1}',
        b'{"v":NaN}',
        b'{"v":Infinity}',
        b'{"v":-Infinity}',
        b"[" * 1500,
    ],
)
def test_invalid_json(payload: bytes) -> None:
    """签名先通过后仍须安全拒绝重复键、非 UTF8 及非有限数字。"""
    _reject(_signed(payload))


def test_noncanonical_json_and_missing_keys() -> None:
    """空白、键顺序及缺失字段不构成第二种合法编码。"""
    _reject(_signed(json.dumps(_payload()).encode()))
    canonical = _json(_payload())
    _reject(_signed(canonical.replace(b'"v":1', b'"v":1,"v":1')))
    _reject(_signed(canonical.replace(b'"created_at":', b'"task_id":"ignored","created_at":')))
    _reject(_signed(canonical.replace(b'"v":1', b'"v":NaN,"v":1')))
    _reject(_signed(canonical.replace(b'"v":1', b'"v":Infinity,"v":1')))
    for field in _payload():
        payload = _payload()
        del payload[field]
        _reject(_signed(_json(payload)))


def test_token_envelope_tampering_rotation_and_bounds() -> None:
    """限制必须先于解析；任何段的字节变化和密钥轮换均失败。"""
    token = _signed(_json(_payload()))
    version, payload, mac = token.split(".")
    for invalid in [
        "",
        "x" * 2049,
        token + ".",
        token.replace("v1.", "v2."),
        f"{version}.{payload}=.{mac}",
        f"{version}.+.{mac}",
        f"{version}.{payload}.{mac}=",
        f"{version}.{payload}.AA",
        f"{version}.{payload}.{_b64(bytes(32))}",
        f"{version}.A{payload[1:]}.{mac}",
        f"{version}.{payload}.{mac[:-1]}!",
        "v1.é.AA",
    ]:
        _reject(invalid)
    with pytest.raises(TaskHistoryCursorError):
        TaskHistoryCursorCodec(bytes(reversed(MASTER))).decode(token, filters=FILTERS, now=NOW)
    with pytest.raises(TaskHistoryCursorError):
        TaskHistoryCursorCodec(MASTER).decode(token, filters=FILTERS, now=NOW.replace(tzinfo=None))


def test_encode_rejects_invalid_state() -> None:
    """内部调用也不能签发 naive 时间或越界锚点。"""
    for state in [
        replace(STATE, issued_at=NOW.replace(tzinfo=None)),
        replace(STATE, anchor=replace(KEY, created_at=NOW + timedelta(microseconds=1))),
        replace(STATE, upper=replace(KEY, created_at=NOW.replace(tzinfo=None))),
        replace(STATE, filters_hash="bad"),
    ]:
        with pytest.raises(TaskHistoryCursorError):
            TaskHistoryCursorCodec(MASTER).encode(state)


@pytest.mark.parametrize("padding", [True, False])
def test_from_file_uses_existing_secret_format(tmp_path: Path, padding: bool) -> None:
    """仅显式工厂调用读取合成 Secret，兼容换行及 Base64URL 填充。"""
    path = tmp_path / "synthetic-key"
    encoded = base64.urlsafe_b64encode(MASTER).decode()
    path.write_text((encoded if padding else encoded.rstrip("=")) + "\n")
    codec = TaskHistoryCursorCodec.from_file(path)
    assert codec.decode(_signed(_json(_payload())), filters=FILTERS, now=NOW) == STATE


def test_invalid_keys_and_file_errors(tmp_path: Path) -> None:
    """配置错误保持固定消息；系统读取错误不伪装成无效游标。"""
    for key in [b"", bytes(31), bytes(33)]:
        with pytest.raises(ValueError, match="task_history_cursor_key_invalid"):
            TaskHistoryCursorCodec(key)
    path = tmp_path / "synthetic-key"
    for content in [b"not base64!", b"\xff", b"AA=="]:
        path.write_bytes(content)
        with pytest.raises(ValueError, match="task_history_cursor_key_invalid"):
            TaskHistoryCursorCodec.from_file(path)
    with pytest.raises(FileNotFoundError):
        TaskHistoryCursorCodec.from_file(tmp_path / "missing")
