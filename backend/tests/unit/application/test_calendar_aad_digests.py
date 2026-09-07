"""用规格中独立固定字节向量冻结三种 v1 物理摘要，避免自证的 expected helper。"""

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from ai_employee.application.calendar_aad_digests import (
    CredentialSnapshot,
    canonical_credential_snapshot_v1,
    canonical_refresh_credential_snapshot_v1,
    canonical_rollout_v1,
    credential_snapshot_digest_v1,
    frame,
    refresh_credential_snapshot_digest_v1,
    rollout_digest_v1,
)

FULL_BYTES_B64 = "QUlFTVBMT1lFRS9jYWxlbmRhci1hYWQvY3JlZGVudGlhbC1zbmFwc2hvdC92MQABAAAADGFjY2Vzc190b2tlbgEAAAAkMTExMTExMTEtMTExMS00MTExLTgxMTEtMTExMTExMTExMTExAQAAACRhYWFhYWFhYS1hYWFhLTRhYWEtOGFhYS1hYWFhYWFhYWFhYWEBAAAAJGJiYmJiYmJiLWJiYmItNGJiYi04YmJiLWJiYmJiYmJiYmJiYgEAAAAIAAECA/Dx8vMBAAAADBAREhMUFRYXGBkaGwEAAAABMwEAAAAbMjAzMC0wMS0wMlQwMzowNDowNS4xMjM0NTZaAQAAABsyMDMwLTAxLTAyVDAyOjAwOjAwLjAwMDAwMVoBAAAADXJlZnJlc2hfdG9rZW4BAAAAJDIyMjIyMjIyLTIyMjItNDIyMi04MjIyLTIyMjIyMjIyMjIyMgEAAAAkYWFhYWFhYWEtYWFhYS00YWFhLThhYWEtYWFhYWFhYWFhYWFhAQAAACRiYmJiYmJiYi1iYmJiLTRiYmItOGJiYi1iYmJiYmJiYmJiYmIBAAAACN6tvu8Af4D/AQAAAAwgISIjJCUmJygpKisBAAAAATQAAQAAABsyMDMwLTAxLTAxVDAwOjAwOjAwLjk5OTk5OVo="
REFRESH_BYTES_B64 = "QUlFTVBMT1lFRS9jYWxlbmRhci1hYWQvcmVmcmVzaC1jcmVkZW50aWFsLXNuYXBzaG90L3YxAAEAAAANcmVmcmVzaF90b2tlbgEAAAAkMjIyMjIyMjItMjIyMi00MjIyLTgyMjItMjIyMjIyMjIyMjIyAQAAACRhYWFhYWFhYS1hYWFhLTRhYWEtOGFhYS1hYWFhYWFhYWFhYWEBAAAAJGJiYmJiYmJiLWJiYmItNGJiYi04YmJiLWJiYmJiYmJiYmJiYgEAAAAI3q2+7wB/gP8BAAAADCAhIiMkJSYnKCkqKwEAAAABNAABAAAAGzIwMzAtMDEtMDFUMDA6MDA6MDAuOTk5OTk5Wg=="
ROLLOUT_BYTES_B64 = "QUlFTVBMT1lFRS9jYWxlbmRhci1hYWQvcm9sbG91dC92MQABAAAAHmNhbGVuZGFyX2FhZF8wMDE5X3ByZWZsaWdodC52MQEAAAANMjAyNjA4MDlfMDAxOAEAAAANMjAyNjA4MDlfMDAxOQEAAAAbY2FsZW5kYXItYWFkLTAwMTktc3ludGhldGljAQAAAEdzaGEyNTY6MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWYwMTIzNDU2Nzg5YWJjZGVmMDEyMzQ1Njc4OWFiY2RlZgEAAAADOTAw"
IMAGE_ID = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def vector_rows() -> tuple[CredentialSnapshot, CredentialSnapshot]:
    """逐字段抄录规范合成输入；这里只构造输入，不推导任何 expected 输出。"""
    common = {
        "user_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "connection_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    }
    access = CredentialSnapshot(
        **common,
        id=UUID("11111111-1111-4111-8111-111111111111"),
        credential_kind="access_token",
        ciphertext=base64.b64decode("AAECA/Dx8vM="),
        nonce=base64.b64decode("EBESExQVFhcYGRob"),
        key_version=3,
        token_expires_at=datetime(2030, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
        updated_at=datetime(2030, 1, 2, 2, 0, 0, 1, tzinfo=UTC),
    )
    refresh = CredentialSnapshot(
        **common,
        id=UUID("22222222-2222-4222-8222-222222222222"),
        credential_kind="refresh_token",
        ciphertext=base64.b64decode("3q2+7wB/gP8="),
        nonce=base64.b64decode("ICEiIyQlJicoKSor"),
        key_version=4,
        token_expires_at=None,
        updated_at=datetime(2030, 1, 1, 0, 0, 0, 999999, tzinfo=UTC),
    )
    return access, refresh


def test_three_independent_canonical_vectors_and_sha256_values() -> None:
    """字段顺序、域标签、时间精度或 framing 漂移都会破坏外部固定向量。"""
    access, refresh = vector_rows()
    full = base64.b64decode(FULL_BYTES_B64)
    refresh_only = base64.b64decode(REFRESH_BYTES_B64)
    rollout = base64.b64decode(ROLLOUT_BYTES_B64)
    assert len(full) == 497 and len(refresh_only) == 265 and len(rollout) == 222
    assert canonical_credential_snapshot_v1(access, refresh) == full
    assert canonical_refresh_credential_snapshot_v1(refresh) == refresh_only
    assert canonical_rollout_v1("calendar-aad-0019-synthetic", IMAGE_ID) == rollout
    assert credential_snapshot_digest_v1(access, refresh) == (
        "607c4c12cb3e4ff7736cb56f06b9dd2f63d2681c8d7a36c7801160eaafcaa84a"
    )
    assert refresh_credential_snapshot_digest_v1(refresh) == (
        "3a4b1dbd0e77c915067c3af352772580949957815573c953baa41131827582f0"
    )
    assert rollout_digest_v1("calendar-aad-0019-synthetic", IMAGE_ID) == (
        "0746bb13c476b6009e8b73e5647daa6bbe8bc1a75f0f717a7632b36915c509e5"
    )


def test_null_empty_and_row_order_are_distinct() -> None:
    """NULL 不能与 empty bytes 混淆，access/refresh 顺序不能由调用方静默交换。"""
    assert frame(None) == b"\x00"
    assert frame(b"") == b"\x01\x00\x00\x00\x00"
    access, refresh = vector_rows()
    with pytest.raises(ValueError):
        canonical_credential_snapshot_v1(refresh, access)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ("key_version", "03"),
        ("key_version", True),
        ("key_version", -1),
        ("updated_at", datetime(2030, 1, 1)),  # noqa: DTZ001 - 负向向量证明 naive 时间被拒绝。
        ("updated_at", datetime(2030, 1, 1, tzinfo=timezone(timedelta(hours=8)))),
    ],
)
def test_snapshot_rejects_noncanonical_types(field: str, value: object) -> None:
    """不能把非规范 UUID、整数或时区输入猜测转换为同一 v1 身份。"""
    access, refresh = vector_rows()
    with pytest.raises((TypeError, ValueError)):
        canonical_credential_snapshot_v1(replace(access, **{field: value}), refresh)


@pytest.mark.parametrize(
    "basename,image",
    [
        ("../escape", IMAGE_ID),
        ("padded ", IMAGE_ID),
        ("synthetic", IMAGE_ID.upper()),
        ("synthetic", "image:latest"),
    ],
)
def test_rollout_rejects_unsafe_identity(basename: str, image: str) -> None:
    """发布摘要不能替操作员修正危险 basename 或可变镜像标签。"""
    with pytest.raises(ValueError):
        canonical_rollout_v1(basename, image)


def test_uuid_value_subclasses_have_the_same_canonical_bytes() -> None:
    """数据库驱动可返回 UUID 子类；它仍是规范 UUID 值，不得误报 credential 冲突。"""

    class DriverUUID(UUID):
        """模拟 asyncpg 的 UUID 值子类，不把驱动类型引入应用依赖。"""

    access, refresh = vector_rows()
    assert canonical_credential_snapshot_v1(
        replace(access, id=DriverUUID(str(access.id))), refresh
    ) == base64.b64decode(FULL_BYTES_B64)
