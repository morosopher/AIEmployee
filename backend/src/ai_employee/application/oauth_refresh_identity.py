"""从唯一 APP root 派生固定 v1 refresh-token identity，供受控内存与关闭审计协议使用。

M2 不支持换 root、keyring 或独立 identity Secret。这里的 keyed fingerprint 不能作为
日志、Trace、API 或 SSE 字段；old/new plaintext 同时可得时仍须 constant-time 比较。
"""

import hmac
from unicodedata import category

from ai_employee.application.calendar_aad_digests import canonical_integer, frame
from ai_employee.application.ports.oauth import MAX_OAUTH_TOKEN_LENGTH


def validated_token_text(plaintext: bytes) -> str:
    """在 fingerprint 或 provider 调用前验证非空、UTF-8 和既有 OAuth token 边界。

    Raises:
        ValueError: 输入形状、编码、空白、控制字符或长度不合法；错误不回显内容。
    """
    if type(plaintext) is not bytes:
        raise ValueError("OAuth token bytes are invalid")
    try:
        value = plaintext.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("OAuth token encoding is invalid") from None
    if (
        not value
        or len(value) > MAX_OAUTH_TOKEN_LENGTH
        or value != value.strip()
        or any(char.isspace() or category(char).startswith("C") for char in value)
    ):
        raise ValueError("OAuth token boundary is invalid")
    return value


class OAuthRefreshIdentity:
    """持有固定版本 HKDF 派生密钥，不保留或暴露 root bytes 与 token plaintext。"""

    __slots__ = ("_fingerprint_key", "_version")

    def __init__(self, application_master_key: bytes, *, key_version: int) -> None:
        """从与 AEAD 相同的 32-byte root 和公开 cipher.key_version 构造服务。

        Args:
            application_master_key: 组合根一次读取/解码的 APP_MASTER_KEY_FILE bytes。
            key_version: 同一个 cipher 的公开只读版本；M2 生命周期内固定不变。

        Raises:
            ValueError: root 或版本非法；不选择备用或默认密钥。
        """
        if type(application_master_key) is not bytes or len(application_master_key) != 32:
            raise ValueError("OAuth identity requires the application master key")
        if type(key_version) is not int or key_version < 1:
            raise ValueError("OAuth identity key version must be positive")
        # L=32 等于 SHA-256 输出长度，HKDF-Expand 只需要第一块 T(1)。标签和 framing
        # 均由规格固定；这里不会把 AEAD 密文、随机 nonce 或物理摘要当作 plaintext。
        extracted = hmac.digest(
            b"AIEMPLOYEE/oauth/refresh-token-identity/hkdf-salt/v1\x00",
            application_master_key,
            "sha256",
        )
        info = b"AIEMPLOYEE/oauth/refresh-token-identity/fingerprint-key/v1\x00" + frame(
            canonical_integer(key_version)
        )
        self._fingerprint_key = hmac.digest(extracted, info + b"\x01", "sha256")
        self._version = key_version

    @property
    def key_version(self) -> int:
        """返回固定 identity 版本；不允许运行时改写。"""
        return self._version

    def fingerprint(self, plaintext: bytes) -> str:
        """验证 canonical plaintext 后返回小写 keyed HMAC；调用方不得输出该值。"""
        validated_token_text(plaintext)
        message = b"AIEMPLOYEE/oauth/refresh-token-identity/v1\x00" + frame(plaintext)
        return hmac.digest(self._fingerprint_key, message, "sha256").hex()
