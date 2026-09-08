"""提供 0019 rollout 与 OAuth fence 共用的不可变 v1 物理快照编码。

摘要只绑定物理数据库行；随机 nonce、updated_at 或摘要改变都不能证明 refresh
plaintext 被替换。字段顺序、域标签和 framing 属于持久协议，改变时必须新增版本。
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

CALENDAR_AAD_SOURCE_REVISION = "20260809_0018"
CALENDAR_AAD_TARGET_REVISION = "20260809_0019"
CALENDAR_AAD_ROLLOUT_SAFETY_MARGIN_SECONDS = 900


@dataclass(frozen=True, slots=True)
class CredentialSnapshot:
    """冻结一行完整 credential CAS 身份，不包含明文或授权 generation。

    字段必须来自同一 user/connection 的真实持久行。密文与 nonce 不进入 repr、日志
    或审计；generation 由独立 claim 字段绑定，不能混入 v1 digest。
    """

    id: UUID
    user_id: UUID
    connection_id: UUID
    credential_kind: Literal["access_token", "refresh_token"]
    ciphertext: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    key_version: int
    token_expires_at: datetime | None
    updated_at: datetime


def frame(value: bytes | None) -> bytes:
    """编码一个 nullable bytes 字段，保留 NULL 与非 NULL 空串的区别。

    Raises:
        TypeError: 调用方不是精确 bytes/None。
        ValueError: 字段长度不能由无符号 32 位网络字节序表示。
    """
    if value is None:
        return b"\x00"
    if type(value) is not bytes:
        raise TypeError("canonical field must be bytes or null")
    if len(value) > 0xFFFFFFFF:
        raise ValueError("canonical field exceeds framing boundary")
    return b"\x01" + len(value).to_bytes(4, "big") + value


def canonical_uuid(value: UUID) -> bytes:
    """仅接受类型化 UUID，输出小写 canonical ASCII；不猜测修正字符串输入。"""
    if not isinstance(value, UUID):
        raise TypeError("canonical UUID value is required")
    return str(value).encode("ascii")


def canonical_integer(value: int) -> bytes:
    """输出无符号、无前导零的十进制 ASCII，并拒绝 bool 或字符串。"""
    if type(value) is not int or value < 0:
        raise ValueError("canonical unsigned integer is required")
    return str(value).encode("ascii")


def canonical_utc(value: datetime) -> str:
    """输出固定六位 microseconds 的 UTC RFC3339，不接受隐式或非 UTC 时区。"""
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("explicit UTC datetime is required")
    return value.isoformat(timespec="microseconds").removesuffix("+00:00") + "Z"


def _row_bytes(row: CredentialSnapshot, kind: str) -> bytes:
    """按冻结顺序编码九列；不把 ciphertext 当作 UTF-8 或篡改 NULL expiry。"""
    if row.credential_kind != kind:
        raise ValueError("credential row order is invalid")
    if row.key_version < 1:
        raise ValueError("credential key version must be positive")
    values = (
        kind.encode("ascii"),
        canonical_uuid(row.id),
        canonical_uuid(row.user_id),
        canonical_uuid(row.connection_id),
        row.ciphertext,
        row.nonce,
        canonical_integer(row.key_version),
        canonical_utc(row.token_expires_at).encode("ascii")
        if row.token_expires_at is not None
        else None,
        canonical_utc(row.updated_at).encode("ascii"),
    )
    return b"".join(frame(value) for value in values)


def canonical_credential_snapshot_v1(
    access: CredentialSnapshot, refresh: CredentialSnapshot
) -> bytes:
    """返回 access→refresh 的完整 v1 规范字节，跨用户或跨连接行一律拒绝。"""
    if access.user_id != refresh.user_id or access.connection_id != refresh.connection_id:
        raise ValueError("credential snapshot ownership is inconsistent")
    return (
        b"AIEMPLOYEE/calendar-aad/credential-snapshot/v1\x00"
        + _row_bytes(access, "access_token")
        + _row_bytes(refresh, "refresh_token")
    )


def canonical_refresh_credential_snapshot_v1(refresh: CredentialSnapshot) -> bytes:
    """返回仅包含 refresh row 的 v1 规范字节。"""
    return b"AIEMPLOYEE/calendar-aad/refresh-credential-snapshot/v1\x00" + _row_bytes(
        refresh, "refresh_token"
    )


def canonical_rollout_v1(backup_artifact_basename: str, immutable_image_id: str) -> bytes:
    """绑定固定 revision、合法 basename、不可变镜像内容 ID 与 900 秒安全余量。

    Args:
        backup_artifact_basename: 不含路径、空白或点目录的单一安全文件基名。
        immutable_image_id: 宿主实际解析的精确小写 sha256 内容标识。

    Raises:
        ValueError: 任一发布身份不是规范输入；不进行 trim 或大小写修复。
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", backup_artifact_basename):
        raise ValueError("rollout basename is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", immutable_image_id):
        raise ValueError("immutable image ID is invalid")
    values = (
        b"calendar_aad_0019_preflight.v1",
        CALENDAR_AAD_SOURCE_REVISION.encode("ascii"),
        CALENDAR_AAD_TARGET_REVISION.encode("ascii"),
        backup_artifact_basename.encode("ascii"),
        immutable_image_id.encode("ascii"),
        b"900",
    )
    return b"AIEMPLOYEE/calendar-aad/rollout/v1\x00" + b"".join(frame(value) for value in values)


def credential_snapshot_digest_v1(access: CredentialSnapshot, refresh: CredentialSnapshot) -> str:
    """返回两行完整物理快照的固定小写 SHA-256，不证明 plaintext 轮换。"""
    return hashlib.sha256(canonical_credential_snapshot_v1(access, refresh)).hexdigest()


def refresh_credential_snapshot_digest_v1(refresh: CredentialSnapshot) -> str:
    """返回 refresh row 物理快照摘要，不替代 AEAD 或 keyed identity。"""
    return hashlib.sha256(canonical_refresh_credential_snapshot_v1(refresh)).hexdigest()


def rollout_digest_v1(backup_artifact_basename: str, immutable_image_id: str) -> str:
    """返回不含 token、expiry 或任何运行后事实的发布身份摘要。"""
    return hashlib.sha256(
        canonical_rollout_v1(backup_artifact_basename, immutable_image_id)
    ).hexdigest()


def connection_digest_v1(connection_id: UUID) -> str:
    """用固定目标 revision 域隔离连接诊断摘要，不输出原 connection UUID。"""
    return hashlib.sha256(
        CALENDAR_AAD_TARGET_REVISION.encode("ascii") + b"\x00" + canonical_uuid(connection_id)
    ).hexdigest()


def calendar_pair_digest_v1(connection_id: UUID, calendar_id: str) -> str:
    """固定 revision/连接/opaque calendar 的 NUL 分隔摘要，供所有 rollout 边界共用。

    Args:
        connection_id: 已验证的 canonical 连接 UUID。
        calendar_id: 原样保留的非目录事件 scope，禁止空白补齐、NUL 或超长输入。

    Raises:
        ValueError: scope 不能由 M2 现有 512 字符事件游标安全表达。
    """
    if (
        type(calendar_id) is not str
        or not calendar_id
        or calendar_id.strip() != calendar_id
        or calendar_id == "directory"
        or "\0" in calendar_id
        or len(calendar_id) > 512
    ):
        raise ValueError("calendar event scope is invalid")
    return hashlib.sha256(
        CALENDAR_AAD_TARGET_REVISION.encode("ascii")
        + b"\0"
        + canonical_uuid(connection_id)
        + b"\0"
        + calendar_id.encode("utf-8")
    ).hexdigest()
