"""使用规范 JSON 与记录绑定 AAD 保护 M2 敏感操作内容。"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

from ai_employee.application.ports.encryption import EncryptedValue, Encryption
from ai_employee.infrastructure.security.encryption import AeadCipher


class ActionPayloadFormatError(ValueError):
    """表示敏感操作内容不是可接受的标准 JSON object。

    异常消息固定且不接收原始异常，避免序列化器或解析器把邮件正文、日程内容、
    解密字节及其内部属性带到日志边界。调用方只能把它解释为不可重试的本地格式
    错误；AEAD 认证失败仍由底层 ``InvalidTag`` 独立表达。
    """

    def __init__(self) -> None:
        """构造不包含敏感内容、解析器对象或异常链的固定错误。"""
        super().__init__("action payload format is invalid")


class _ActionPayloadFormatViolation(Exception):
    """仅在模块内部标记非标准 JSON，永不携带原始值或解析器状态。"""


_PREPARED_ACTION_PAYLOAD_PROOF = object()


@dataclass(frozen=True, slots=True)
class PreparedActionPayload:
    """保存严格 helper 一次性冻结的规范 JSON 字节及模块内构造证明。

    ``canonical_bytes`` 是不可变 bytes，可同时用于哈希与 AEAD。``_proof`` 不参与
    repr 或比较，且加密入口会验证对象类型和证明身份，避免任意 bytes 伪装成已经通过
    严格 JSON 校验的载荷。
    """

    canonical_bytes: bytes
    _proof: object = field(repr=False, compare=False)


def canonical_action_payload_json(payload: Mapping[str, object]) -> PreparedActionPayload:
    """递归验证并冻结敏感操作 object 的唯一规范 JSON UTF-8 字节。

    顶层和嵌套容器只接受精确内建 ``dict/list``，因此拒绝自定义 Mapping/list 后
    不会调用其 iterator、``items`` 或其他用户代码。标量只接受精确
    ``bool/int/float/str`` 与 ``None``；浮点数必须有限，对象键必须是精确字符串，
    所有字符串都必须只含 Unicode scalar value。成功路径严格保持 M2 计划要求的
    排序键、未转义 Unicode、禁用非有限数和紧凑分隔符。

    Args:
        payload: 顶层 JSON object 候选；返回值不会与其共享 list 或 object 容器。

    Returns:
        带模块内构造证明、可同时用于 AEAD 明文与 SHA-256 的冻结载荷。

    Raises:
        ActionPayloadFormatError: 任一值不属于严格标准 JSON；错误不含原始内容或异常链。
    """
    try:
        if type(payload) is not dict:
            raise _ActionPayloadFormatViolation
        standard_json = _copy_standard_json(payload)
        if type(standard_json) is not dict:
            raise _ActionPayloadFormatViolation
        canonical_bytes = json.dumps(
            standard_json,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (
        _ActionPayloadFormatViolation,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
    ):
        # 离开 except 后再抛出，防止编码器异常通过 context 保留完整敏感 object。
        pass
    else:
        return PreparedActionPayload(
            canonical_bytes=canonical_bytes,
            _proof=_PREPARED_ACTION_PAYLOAD_PROOF,
        )
    raise ActionPayloadFormatError


def action_payload_aad(
    *,
    user_id: UUID,
    record_id: UUID,
    content_kind: str,
    action: str,
    schema_version: str,
) -> bytes:
    """绑定用户、记录、内容类别、动作和 Schema，阻止跨记录或跨动作替换。"""
    # fmt: off
    return (
        f"{user_id}:{record_id}:{content_kind}:{action}:{schema_version}"
    ).encode("ascii")
    # fmt: on


class ActionPayloadCipher:
    """把敏感 JSON object 规范化后委托现有 AEAD 原语进行认证加密。

    本类不复制 AES-GCM、nonce 或密钥版本逻辑，只负责构造跨领域稳定的 AAD，
    并保证同一 JSON object 始终映射为相同 UTF-8 明文字节。认证失败时保留底层
    ``InvalidTag``，让调用方把篡改视为内部安全失败而不是供应商可重试错误。
    """

    def __init__(self, encryption: Encryption) -> None:
        """绑定实现应用加密端口的 AEAD 适配器。

        Args:
            encryption: 支持调用方 AAD 的认证加密实现。
        """
        self._encryption = encryption

    @classmethod
    def from_key(cls, key: bytes, key_version: int = 1) -> "ActionPayloadCipher":
        """使用现有 ``AeadCipher`` 构造测试或组合根可注入的包装器。

        Args:
            key: 由调用方安全取得的 32 字节 AES-256 密钥。
            key_version: 与该密钥对应的正整数持久化版本。

        Returns:
            委托现有 AEAD 实现的操作内容加密器。

        Raises:
            ValueError: 密钥长度或版本不符合 ``AeadCipher`` 契约。
        """
        return cls(AeadCipher(key, key_version=key_version))

    def encrypt_json(
        self,
        payload: Mapping[str, object],
        *,
        user_id: UUID,
        record_id: UUID,
        content_kind: str,
        action: str,
        schema_version: str,
    ) -> EncryptedValue:
        """规范序列化 JSON object 并与完整记录上下文绑定后加密。

        Args:
            payload: 只含 JSON 可序列化值的敏感 object；调用方负责领域 Schema 校验。
            user_id: 内容所属用户。
            record_id: 实际承载 AEAD 三元组的行 ID。
            content_kind: 字段或记录内容类别，例如 ``approval_command``。
            action: 精确可信动作；内部草稿和快照使用固定内部动作。
            schema_version: 明文内容的显式版本。

        Returns:
            可直接持久化到目标行的密文、nonce 与密钥版本。

        Raises:
            ActionPayloadFormatError: payload 无法编码为规范 JSON UTF-8；错误不含内容。
            UnicodeEncodeError: AAD 维度含非 ASCII 字符。
        """
        prepared = canonical_action_payload_json(payload)
        return self.encrypt_prepared_json(
            prepared,
            user_id=user_id,
            record_id=record_id,
            content_kind=content_kind,
            action=action,
            schema_version=schema_version,
        )

    def encrypt_prepared_json(
        self,
        payload: PreparedActionPayload,
        *,
        user_id: UUID,
        record_id: UUID,
        content_kind: str,
        action: str,
        schema_version: str,
    ) -> EncryptedValue:
        """加密同一严格 helper 已冻结的规范字节，不再遍历原始输入。

        Args:
            payload: ``canonical_action_payload_json`` 返回的冻结载荷。
            user_id: 内容所属用户。
            record_id: 实际承载 AEAD 三元组的行 ID。
            content_kind: 字段或记录内容类别。
            action: 精确可信动作或固定内部动作。
            schema_version: 明文内容的显式版本。

        Returns:
            与完整五维 AAD 绑定的密文、nonce 与密钥版本。

        Raises:
            ActionPayloadFormatError: payload 不是本模块严格 helper 构造的精确类型。
        """
        if (
            type(payload) is not PreparedActionPayload
            or payload._proof is not _PREPARED_ACTION_PAYLOAD_PROOF
        ):
            raise ActionPayloadFormatError
        return self._encryption.encrypt(
            payload.canonical_bytes,
            action_payload_aad(
                user_id=user_id,
                record_id=record_id,
                content_kind=content_kind,
                action=action,
                schema_version=schema_version,
            ),
        )

    def decrypt_json(
        self,
        value: EncryptedValue,
        *,
        user_id: UUID,
        record_id: UUID,
        content_kind: str,
        action: str,
        schema_version: str,
    ) -> dict[str, object]:
        """认证记录上下文、解码 UTF-8，并只接受 JSON object 根节点。

        Args:
            value: 从目标记录读取的完整 AEAD 三元组。
            user_id: 预期内容所属用户。
            record_id: 预期承载密文的记录 ID。
            content_kind: 预期字段或记录内容类别。
            action: 预期可信动作或固定内部动作。
            schema_version: 预期内容版本。

        Returns:
            与持久化输入解除容器共享的标准 JSON object。

        Raises:
            cryptography.exceptions.InvalidTag: 任一密文或 AAD 维度不匹配。
            ActionPayloadFormatError: 认证明文不是 UTF-8 JSON object；错误不含内容。
        """
        plaintext = self._encryption.decrypt(
            value,
            action_payload_aad(
                user_id=user_id,
                record_id=record_id,
                content_kind=content_kind,
                action=action,
                schema_version=schema_version,
            ),
        )
        try:
            decoded: object = json.loads(
                plaintext.decode("utf-8"),
                parse_constant=_reject_non_standard_json_constant,
            )
            standard_json = _copy_standard_json(decoded)
        except (
            _ActionPayloadFormatViolation,
            ValueError,
            RecursionError,
        ):
            # 认证后的明文仍属敏感数据，解析器异常不得成为隐式异常链的一部分。
            pass
        else:
            if type(standard_json) is dict:
                return cast(dict[str, object], standard_json)
        raise ActionPayloadFormatError


def _copy_standard_json(
    value: object,
    *,
    active_container_ids: set[int] | None = None,
) -> object:
    """递归复制严格标准 JSON，并以无内容内部哨兵拒绝歧义值。

    容器 ID 只在当前递归路径内保留，因此拒绝直接或间接循环，同时允许同一个
    非循环 list/object 被多个字段安全共享。所有返回容器都是新对象，解密调用方
    不会取得解析器内部容器的可变引用。

    Args:
        value: 当前待验证的 Python 值。
        active_container_ids: 当前递归路径内的 object/list ID；顶层调用省略。

    Returns:
        只由严格标准 JSON 类型组成的独立副本。

    Raises:
        _ActionPayloadFormatViolation: 类型、数值、键、Unicode 或容器图不符合约束。
    """
    active_ids = set() if active_container_ids is None else active_container_ids
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is str:
        _validate_unicode_scalar_string(value)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _ActionPayloadFormatViolation
        return value
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        container_id = id(value)
        if container_id in active_ids:
            raise _ActionPayloadFormatViolation
        active_ids.add(container_id)
        try:
            copied: dict[str, object] = {}
            for key, child in mapping.items():
                if type(key) is not str:
                    raise _ActionPayloadFormatViolation
                _validate_unicode_scalar_string(key)
                copied[key] = _copy_standard_json(
                    child,
                    active_container_ids=active_ids,
                )
            return copied
        finally:
            active_ids.remove(container_id)
    if type(value) is list:
        children = cast(list[object], value)
        container_id = id(value)
        if container_id in active_ids:
            raise _ActionPayloadFormatViolation
        active_ids.add(container_id)
        try:
            return [
                _copy_standard_json(child, active_container_ids=active_ids) for child in children
            ]
        finally:
            active_ids.remove(container_id)
    raise _ActionPayloadFormatViolation


def _validate_unicode_scalar_string(value: str) -> None:
    """拒绝任一无法作为 UTF-8 Unicode scalar value 编码的代理码位。"""
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise _ActionPayloadFormatViolation


def _reject_non_standard_json_constant(_value: str) -> object:
    """让 ``json.loads`` 在产生 NaN 或正负 Infinity 前立即 fail closed。"""
    raise _ActionPayloadFormatViolation
