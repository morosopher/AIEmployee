"""提供只保存 Argon2id 哈希的密码处理适配器。"""

from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2.exceptions import VerifyMismatchError
from argon2.low_level import Type

# 此 Argon2id 哈希由一次性随机输入预生成且原文已丢弃，只用于让未知、停用或无哈希身份
# 执行真实密码验证工作。它不是用户凭据，也不得写入日志或 API 响应。
_FALLBACK_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$whX9PnBcbFaROV5yRWw4Sw$IqWRQPLFm87vR7WPoUB3+pJ4QZQrVcTFj6JfiiA3sFA"


class PasswordHasher:
    """以 Argon2id 对管理员密码进行不可逆哈希与恒定接口验证。

    第三方库负责盐值生成和参数编码；应用只保存返回的编码哈希，绝不保存或
    记录明文密码。显式固定 ``Type.ID``，避免依赖库默认值变化后静默切换算法。
    """

    def __init__(self) -> None:
        """创建使用 Argon2id 的密码哈希器。"""
        self._hasher = Argon2PasswordHasher(type=Type.ID)

    @property
    def fallback_password_hash(self) -> str:
        """返回跨请求稳定且可由 Argon2id 验证器处理的 fallback 哈希。

        Returns:
            预先生成的有效 Argon2id 编码哈希；其原始随机输入不保存也不使用。
        """
        return _FALLBACK_PASSWORD_HASH

    def hash(self, password: str) -> str:
        """为明文密码生成带独立随机盐的 Argon2id 编码哈希。

        Args:
            password: 仅在当前调用内使用的管理员明文密码。

        Returns:
            包含算法参数和盐值的 Argon2id 编码哈希。
        """
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        """验证密码是否匹配已保存的 Argon2id 哈希。

        Args:
            password_hash: 数据库中保存的 Argon2id 编码哈希。
            password: 登录请求提供的候选明文密码。

        Returns:
            密码匹配时返回 ``True``；普通凭据不匹配时返回 ``False``。

        Raises:
            VerificationError: 哈希损坏或算法参数无效时保留库异常，避免把持久化
                数据损坏伪装成普通认证失败。
        """
        try:
            return self._hasher.verify(password_hash, password)
        except VerifyMismatchError:
            return False
