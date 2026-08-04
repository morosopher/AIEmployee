"""验证 OAuth 凭据的记录绑定 AEAD 加密不变量。"""

import pytest
from cryptography.exceptions import InvalidTag

from ai_employee.infrastructure.security.encryption import AeadCipher


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
