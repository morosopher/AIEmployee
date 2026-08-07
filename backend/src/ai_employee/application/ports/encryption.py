"""定义应用用例可依赖的最小字段加密边界。"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EncryptedValue:
    """表示可持久化的认证加密结果，不暴露任何具体算法或密钥实现。"""

    ciphertext: bytes
    nonce: bytes
    key_version: int


class Encryption(Protocol):
    """定义以调用方提供的 AAD 保护敏感字段的最小应用端口。"""

    def encrypt(self, plaintext: bytes, aad: bytes) -> EncryptedValue:
        """返回与 AAD 绑定的密文、随机 nonce 和密钥版本。"""
        ...

    def decrypt(self, value: EncryptedValue, aad: bytes) -> bytes:
        """仅在密文、nonce 与 AAD 全部通过认证时返回原始字节。"""
        ...
