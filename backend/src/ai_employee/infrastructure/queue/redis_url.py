"""定义 Redis 集成测试连接目标的不可混淆值类型。"""

from dataclasses import dataclass, field
from ipaddress import ip_address
from urllib.parse import unquote, urlsplit


class RedisTestUrl(str):
    """已经通过本地测试 Redis 安全校验且默认不泄露内容的字符串类型。"""

    def __repr__(self) -> str:
        """隐藏连接目标，避免 pytest 或调试器意外回显 URL。"""
        return "<validated test redis URL>"


class InvalidTestRedisUrl(ValueError):
    """表示测试 Redis URL 不满足安全边界；消息不得包含原始连接字符串。"""


@dataclass(frozen=True, slots=True)
class ValidatedTestRedisUrl:
    """保存已校验 URL 与明确选择的 Redis 测试数据库编号。"""

    value: RedisTestUrl = field(repr=False)
    database_number: int

    def __repr__(self) -> str:
        """避免 dataclass 调试表示包含完整连接目标。"""
        return "<validated test redis target>"


def validate_test_redis_url(value: str) -> ValidatedTestRedisUrl:
    """严格校验只供 Redis Streams 集成测试使用的本地 URL。

    Args:
        value: ``TEST_REDIS_URL`` 提供的原始 URL；函数不会记录或回显它。

    Returns:
        已绑定保留测试数据库 15 的安全值。

    Raises:
        InvalidTestRedisUrl: URL 缺少必要 authority、指向非 loopback、未选择保留 DB 15，
            或包含 query、fragment、控制字符及其他歧义路径。
    """
    if not isinstance(value, str) or value == "":
        raise InvalidTestRedisUrl("TEST_REDIS_URL is required")
    if value != value.strip():
        raise InvalidTestRedisUrl("TEST_REDIS_URL must not contain surrounding whitespace")
    if any(character in value for character in "\t\n\r"):
        raise InvalidTestRedisUrl("TEST_REDIS_URL must not contain TAB, LF, or CR characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidTestRedisUrl("TEST_REDIS_URL contains an invalid control character")
    if not value.startswith("redis://"):
        raise InvalidTestRedisUrl("TEST_REDIS_URL must use redis scheme")
    # Redis 客户端允许 query 覆盖数据库等连接参数；测试边界一律拒绝覆盖入口。
    if "?" in value:
        raise InvalidTestRedisUrl("TEST_REDIS_URL must not contain a query")
    if "#" in value:
        raise InvalidTestRedisUrl("TEST_REDIS_URL must not contain a fragment")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise InvalidTestRedisUrl("TEST_REDIS_URL is malformed") from error

    if parsed.scheme != "redis":
        raise InvalidTestRedisUrl("TEST_REDIS_URL must use redis scheme")
    if parsed.query:
        raise InvalidTestRedisUrl("TEST_REDIS_URL must not contain a query")
    if parsed.fragment:
        raise InvalidTestRedisUrl("TEST_REDIS_URL must not contain a fragment")
    if parsed.hostname is None or not _is_loopback_host(parsed.hostname):
        raise InvalidTestRedisUrl("TEST_REDIS_URL host must be loopback")
    if port is None or not 1 <= port <= 65535:
        raise InvalidTestRedisUrl("TEST_REDIS_URL must include an explicit valid port")

    username = unquote(parsed.username) if parsed.username is not None else None
    password = unquote(parsed.password) if parsed.password is not None else None
    if (username is not None and _has_control_character(username)) or (
        password is not None and _has_control_character(password)
    ):
        raise InvalidTestRedisUrl("TEST_REDIS_URL credentials contain an invalid character")

    database_text = parsed.path.removeprefix("/")
    if (
        not parsed.path.startswith("/")
        or not database_text.isascii()
        or not database_text.isdigit()
    ):
        raise InvalidTestRedisUrl("TEST_REDIS_URL must select one numeric database")
    database_number = int(database_text)
    if parsed.path != f"/{database_number}" or database_number != 15:
        raise InvalidTestRedisUrl("TEST_REDIS_URL database must be reserved database 15")

    return ValidatedTestRedisUrl(
        value=RedisTestUrl(value),
        database_number=database_number,
    )


def _is_loopback_host(host: str) -> bool:
    """判断主机是否为明确 loopback 地址，不执行 DNS 解析。"""
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _has_control_character(value: str) -> bool:
    """判断解码后的凭据是否包含不可安全传递的控制字符。"""
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
