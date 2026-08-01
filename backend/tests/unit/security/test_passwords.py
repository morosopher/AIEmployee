"""验证密码哈希与随机令牌原语不泄露可逆认证材料。"""

import hashlib

from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.infrastructure.security.tokens import hash_token, new_token


def test_password_hash_is_salted_argon2id_and_verifiable() -> None:
    """相同密码应产生不同 Argon2id 哈希，并只接受正确密码。"""
    hasher = PasswordHasher()

    first = hasher.hash("correct horse battery staple")
    second = hasher.hash("correct horse battery staple")

    assert first != second
    assert first.startswith("$argon2id$")
    assert second.startswith("$argon2id$")
    assert hasher.verify(first, "correct horse battery staple")
    assert not hasher.verify(first, "wrong")


def test_fallback_password_hash_is_precomputed_valid_argon2id_work() -> None:
    """Fallback 哈希必须跨实例稳定，并能驱动一次真实 Argon2id 验证。"""
    first = PasswordHasher()
    second = PasswordHasher()

    assert first.fallback_password_hash == second.fallback_password_hash
    assert first.fallback_password_hash.startswith("$argon2id$")
    assert not first.verify(first.fallback_password_hash, "synthetic-candidate-password")


def test_new_token_returns_independent_urlsafe_values() -> None:
    """每次令牌生成都应返回足够长且可安全放入 Cookie 的独立值。"""
    first = new_token()
    second = new_token()

    assert first != second
    assert len(first) >= 43
    assert len(second) >= 43
    assert first.replace("-", "").replace("_", "").isalnum()
    assert second.replace("-", "").replace("_", "").isalnum()


def test_hash_token_uses_fixed_sha256_utf8_digest() -> None:
    """持久化摘要必须是确定的 32 字节 SHA-256，而不是原始令牌。"""
    token = "synthetic-token-value"

    digest = hash_token(token)

    assert digest == hashlib.sha256(token.encode("utf-8")).digest()
    assert len(digest) == 32
    assert token.encode("utf-8") not in digest
