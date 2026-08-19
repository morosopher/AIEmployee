"""验证 OAuth 凭据的记录绑定 AEAD 加密不变量。"""

from typing import cast

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ai_employee.application.ports.encryption import (
    EncryptedValue,
    Encryption,
    EncryptionBoundaryError,
    EncryptionKeyVersionError,
)
from ai_employee.infrastructure.security.encryption import AeadCipher


def _read_key_version(encryption: Encryption) -> int:
    """通过应用端口读取当前单密钥版本，证明调用方无需依赖实现私有字段。"""
    return encryption.key_version


def test_aead_uses_fresh_nonce_and_requires_matching_record_aad() -> None:
    """相同明文必须产生不同密文，且归属变化后不能解密。

    此测试覆盖 OAuth token 落库的最小保密边界：随机 nonce 阻断密文关联，
    AAD 则把凭据严格绑定到用户、所属记录和字段种类。
    """
    cipher = AeadCipher(b"k" * 32, key_version=7)
    plaintext = b"synthetic-token"
    aad = b"user-1:connection-1:access_token"

    first = cipher.encrypt(plaintext, aad)
    second = cipher.encrypt(plaintext, aad)

    assert first.key_version == 7
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert cipher.decrypt(first, aad) == plaintext
    with pytest.raises(InvalidTag):
        cipher.decrypt(first, b"user-2:connection-1:access_token")
    with pytest.raises(InvalidTag):
        cipher.decrypt(first, b"user-1:connection-2:access_token")


def test_aead_and_port_expose_read_only_key_version() -> None:
    """实现和应用端口必须公开同一个只读当前密钥版本。"""
    cipher = AeadCipher(b"v" * 32, key_version=7)

    assert cipher.key_version == 7
    assert _read_key_version(cipher) == 7
    with pytest.raises(AttributeError):
        cipher.key_version = 8
    assert cipher.key_version == 7


def test_persisted_key_version_mismatch_is_rejected_before_aes_gcm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持久化版本不匹配时不得尝试 AES-GCM，更不能盲目使用当前单密钥。"""
    cipher = AeadCipher(b"m" * 32, key_version=7)
    encrypted = cipher.encrypt(b"synthetic-plaintext", b"synthetic-aad")
    mismatched = EncryptedValue(
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        key_version=8,
    )
    decrypt_calls = 0

    def unexpected_decrypt(
        aes_gcm: AESGCM,
        nonce: bytes,
        data: bytes,
        associated_data: bytes | None,
    ) -> bytes:
        """记录任何越过版本门禁的底层解密调用并立即使测试失败。"""
        del aes_gcm, nonce, data, associated_data
        nonlocal decrypt_calls
        decrypt_calls += 1
        raise AssertionError("AES-GCM decrypt must not run for a mismatched key version")

    monkeypatch.setattr(AESGCM, "decrypt", unexpected_decrypt)

    with pytest.raises(EncryptionKeyVersionError):
        cipher.decrypt(mismatched, b"synthetic-aad")
    assert decrypt_calls == 0


def test_malformed_encrypted_value_uses_typed_boundary_error() -> None:
    """无法交给 AES-GCM 的 nonce 形状必须保留为明确的本地解密边界错误。"""
    cipher = AeadCipher(b"b" * 32, key_version=7)
    malformed = EncryptedValue(
        ciphertext=b"synthetic-ciphertext",
        nonce=b"short",
        key_version=7,
    )

    with pytest.raises(EncryptionBoundaryError):
        cipher.decrypt(malformed, b"synthetic-aad")


def test_non_bytes_aad_type_error_is_not_misclassified_as_persisted_damage() -> None:
    """调用方传入非 bytes AAD 时必须保留底层程序错误，不能误报持久化损坏。"""
    cipher = AeadCipher(b"a" * 32, key_version=7)
    encrypted = cipher.encrypt(b"synthetic-plaintext", b"synthetic-aad")

    with pytest.raises(TypeError):
        cipher.decrypt(encrypted, cast(bytes, "not-bytes"))


def test_unexpected_aes_gcm_error_propagates_without_boundary_wrapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知底层程序错误必须原样传播，证明实现没有宽泛异常收敛。"""
    cipher = AeadCipher(b"u" * 32, key_version=7)
    encrypted = cipher.encrypt(b"synthetic-plaintext", b"synthetic-aad")
    sentinel = RuntimeError("synthetic AES-GCM invariant failure")

    def unexpected_decrypt(
        aes_gcm: AESGCM,
        nonce: bytes,
        data: bytes,
        associated_data: bytes | None,
    ) -> bytes:
        """模拟与持久化值形状无关的底层程序不变量失败。"""
        del aes_gcm, nonce, data, associated_data
        raise sentinel

    monkeypatch.setattr(AESGCM, "decrypt", unexpected_decrypt)

    with pytest.raises(RuntimeError) as raised:
        cipher.decrypt(encrypted, b"synthetic-aad")
    assert raised.value is sentinel
