"""提供用于凭据和敏感源数据的带版本 AEAD 加密原语。"""

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ai_employee.application.ports.encryption import (
    EncryptedValue,
    EncryptionBoundaryError,
    EncryptionKeyVersionError,
)


class AeadCipher:
    """使用 AES-256-GCM 加密并认证记录绑定的敏感字节串。"""

    def __init__(self, key: bytes, key_version: int = 1) -> None:
        """验证 32 字节主密钥并构建 AES-GCM 实例。

        Args:
            key: 从受控 Secret 文件读取的 32 字节 AES-256 密钥。
            key_version: 当前密钥版本，必须为正整数以支持显式轮换。

        Raises:
            ValueError: 密钥长度或版本不符合安全契约时抛出。
        """
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        if key_version < 1:
            raise ValueError("key_version must be positive")
        self._aead = AESGCM(key)
        self._key_version = key_version

    @property
    def key_version(self) -> int:
        """返回当前单密钥版本，供持久化与读取边界进行一致性校验。

        该属性没有 setter；轮换只能通过重新构建受组合根管理的 cipher，避免运行时静默改写版本。
        """
        return self._key_version

    @classmethod
    def from_file(cls, path: Path, key_version: int = 1) -> "AeadCipher":
        """从 Base64URL Secret 文件安全构建加密器。

        Args:
            path: 仅由部署环境挂载、内容不进入日志的密钥文件。
            key_version: 与该 Secret 对应的持久化密钥版本。

        Returns:
            经过长度校验的 AES-256-GCM 加密器。

        Raises:
            OSError: 读取 Secret 文件失败时保留原始系统异常。
            ValueError: 文件不是有效 Base64URL 或不是 32 字节密钥时抛出。
        """
        try:
            encoded = path.read_text(encoding="utf-8").strip().encode("ascii")
            key = base64.urlsafe_b64decode(encoded)
        except (UnicodeEncodeError, ValueError) as error:
            raise ValueError("master key file must contain Base64URL data") from error
        return cls(key, key_version)

    def encrypt(self, plaintext: bytes, aad: bytes) -> EncryptedValue:
        """以新鲜 12 字节 nonce 加密数据并认证归属上下文。

        Args:
            plaintext: 待保护的 Token 或已清洗的敏感正文。
            aad: 由调用方规范 helper 生成的不透明记录绑定 bytes；cipher 不解释其内部格式。

        Returns:
            包含随机 nonce、认证密文和当前版本的持久化值。
        """
        nonce = os.urandom(12)
        return EncryptedValue(self._aead.encrypt(nonce, plaintext, aad), nonce, self._key_version)

    def decrypt(self, value: EncryptedValue, aad: bytes) -> bytes:
        """在 AAD 与认证标签均匹配时返回原始字节。

        Args:
            value: 从受控数据库行读取的密文三元组。
            aad: 与加密时完全相同的记录绑定上下文。

        Returns:
            通过认证后的原始字节。

        Raises:
            EncryptionKeyVersionError: 持久化版本与当前单密钥版本不匹配。
            EncryptionBoundaryError: 持久化密文或 nonce 形状不满足冻结边界。
            cryptography.exceptions.InvalidTag: 密文、nonce 或归属上下文被替换时抛出。
            TypeError: 调用方传入的 AAD 不是底层 AEAD 接受的 bytes 时原样传播。

        Notes:
            只有无需调用 AES-GCM 即可判定的持久化字段形状错误会转换为
            ``EncryptionBoundaryError``。通过该边界后的调用方错误或未知底层异常均原样传播，
            避免把程序错误误报为数据库密文损坏。
        """
        # 版本不匹配意味着当前进程没有正确密钥，必须在任何 AES-GCM 调用前失败，
        # 不能把认证失败误当成可用当前密钥重试或引入隐式 keyring。
        if value.key_version != self._key_version:
            raise EncryptionKeyVersionError("encrypted value key version does not match")

        # 本实现只生成 12-byte nonce，且 AES-GCM 密文至少包含 16-byte tag；这些形状
        # 可以在调用密码学库前确定性验证，而 AAD 与底层程序错误不能归类为持久化损坏。
        if (
            type(value.nonce) is not bytes
            or len(value.nonce) != 12
            or type(value.ciphertext) is not bytes
            or len(value.ciphertext) < 16
        ):
            raise EncryptionBoundaryError("encrypted value is malformed")

        return self._aead.decrypt(value.nonce, value.ciphertext, aad)
