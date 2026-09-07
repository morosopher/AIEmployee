"""用独立标准库 HKDF/HMAC 向量验证固定 APP root 的刷新 token identity。"""

import base64
import hashlib
import hmac

import pytest

from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
from ai_employee.infrastructure.security.encryption import AeadCipher


@pytest.mark.parametrize("version", [7])
def test_fixed_hkdf_hmac_vector_matches_independent_standard_library(version: int) -> None:
    """任何 root、salt、info、framing 或 key-version 漂移都必须使该固定协议失败。"""
    root = bytes(range(32))
    salt = b"AIEMPLOYEE/oauth/refresh-token-identity/hkdf-salt/v1\x00"
    info = b"AIEMPLOYEE/oauth/refresh-token-identity/fingerprint-key/v1\x00\x01\x00\x00\x00\x017"
    extracted = hmac.digest(salt, root, "sha256")
    derived = hmac.digest(extracted, info + b"\x01", "sha256")
    assert derived.hex() == "47119abdcf6b97afcdd6416bf5a8b7771b314a630c53c1553aa1789cc02f95c1"
    message = base64.b64decode(
        "QUlFTVBMT1lFRS9vYXV0aC9yZWZyZXNoLXRva2VuLWlkZW50aXR5L3YxAAEAAAAZc3ludGhldGljLXJlZnJlc2gtdG9rZW4tQQ=="
    )
    assert len(message) == 73
    expected = "9de29ff352c78092c4b7dae5e0e3093fee49815a3473d75c3876e3b316222564"
    assert hmac.new(derived, message, hashlib.sha256).hexdigest() == expected
    identity = OAuthRefreshIdentity(root, key_version=version)
    assert identity.fingerprint(b"synthetic-refresh-token-A") == expected
    assert identity.key_version == version
    assert root.hex() not in repr(identity) and expected not in repr(identity)


def test_same_plaintext_reencryption_preserves_identity() -> None:
    """AEAD nonce 与物理密文改变不能伪造 refresh plaintext 轮换。"""
    cipher = AeadCipher(bytes(range(32)), key_version=7)
    identity = OAuthRefreshIdentity(bytes(range(32)), key_version=cipher.key_version)
    first = cipher.encrypt(b"synthetic-refresh-token-A", b"synthetic-credential-aad")
    second = cipher.encrypt(b"synthetic-refresh-token-A", b"synthetic-credential-aad")
    assert first != second
    assert identity.fingerprint(cipher.decrypt(first, b"synthetic-credential-aad")) == (
        identity.fingerprint(cipher.decrypt(second, b"synthetic-credential-aad"))
    )


@pytest.mark.parametrize("value", [b"", b" padded", b"has space", b"line\n", b"\xff", b"x" * 8193])
def test_identity_rejects_invalid_plaintext_before_fingerprinting(value: bytes) -> None:
    """不得为未通过 token UTF-8、非空和长度边界的字节创建 durable guard。"""
    with pytest.raises(ValueError):
        OAuthRefreshIdentity(bytes(range(32)), key_version=7).fingerprint(value)


@pytest.mark.parametrize(
    "root,version", [(b"", 7), (b"x" * 31, 7), (b"x" * 32, 0), (b"x" * 32, True)]
)
def test_missing_root_or_invalid_version_fails_closed(root: bytes, version: int) -> None:
    """固定 identity service 不为缺失 root 或版本提供隐式默认/备用密钥。"""
    with pytest.raises(ValueError):
        OAuthRefreshIdentity(root, key_version=version)
