"""生成高熵认证令牌并转换为数据库可保存的不可逆摘要。"""

import hashlib
import secrets


def new_token() -> str:
    """生成供会话 Cookie 或 CSRF Cookie 使用的 URL-safe 随机令牌。

    Returns:
        由 32 字节密码学安全随机数据编码而成的 URL-safe 字符串。
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    """把原始认证令牌转换为固定 32 字节 SHA-256 摘要。

    Args:
        token: 仅存在于请求或响应边界的原始会话/CSRF 令牌。

    Returns:
        可持久化和安全比较的 32 字节摘要；数据库不得保存原始令牌。
    """
    return hashlib.sha256(token.encode("utf-8")).digest()
