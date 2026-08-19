"""定义应用用例可依赖的最小字段加密边界。"""

from dataclasses import dataclass
from typing import Protocol


class EncryptionKeyVersionError(Exception):
    """表示持久化密钥版本与当前单密钥 AEAD 版本不匹配。

    调用方必须据此失败关闭或进入明确恢复流程，不能用当前密钥盲目重试；异常内容不得
    包含密钥、密文或记录标识符。
    """


class EncryptionBoundaryError(Exception):
    """表示持久化加密值的确定性形状无法安全交给底层 AEAD 原语处理。

    该类型只描述 nonce、ciphertext 等持久化边界损坏，不用于包装调用方类型错误、认证失败
    或未知密码学实现异常。
    """


@dataclass(frozen=True, slots=True)
class EncryptedValue:
    """表示可持久化的认证加密结果，不暴露任何具体算法或密钥实现。"""

    ciphertext: bytes
    nonce: bytes
    key_version: int


class Encryption(Protocol):
    """定义以调用方提供的 AAD 保护敏感字段的最小应用端口。"""

    @property
    def key_version(self) -> int:
        """返回当前单密钥版本，且不允许调用方修改。

        组合根通过该公共只读属性让持久化版本与其他密钥派生边界保持一致，不读取实现私有字段。
        """
        ...

    def encrypt(self, plaintext: bytes, aad: bytes) -> EncryptedValue:
        """返回与 AAD 绑定的密文、随机 nonce 和密钥版本。"""
        ...

    def decrypt(self, value: EncryptedValue, aad: bytes) -> bytes:
        """仅在持久化值、密钥版本与 AAD 全部通过校验时返回原始字节。

        Args:
            value: 从持久化层读取的认证密文、nonce 与密钥版本。
            aad: 调用方按业务契约构造的不透明记录绑定字节。

        Returns:
            通过实现认证后的原始字节。

        Raises:
            EncryptionKeyVersionError: 持久化版本与当前单密钥版本不匹配。
            EncryptionBoundaryError: 持久化密文或 nonce 的确定性形状不合法。

        Notes:
            认证失败由具体实现的类型化异常表达；当前 ``AeadCipher`` 保留 ``InvalidTag``，
            但应用端口不导入或绑定具体密码学库类型。
        """
        ...
