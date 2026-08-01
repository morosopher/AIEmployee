"""定义真实 PostgreSQL 集成测试的 fail-closed URL 校验边界。

集成测试会执行 Alembic 迁移并清空应用表，因此 URL 校验必须发生在任何
``Config``、引擎或连接对象构造之前。该模块只允许明确的本地 asyncpg
端点，并通过不可混淆的类型把已校验值交给测试基础设施。
"""

import re
from dataclasses import dataclass, field
from ipaddress import ip_address

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


class TestDatabaseUrl(str):
    """已经通过本地测试数据库安全校验且默认不泄露内容的字符串类型。"""

    def __repr__(self) -> str:
        """隐藏密码、主机和数据库名，避免 pytest 或调试器意外回显 DSN。"""
        return "<validated test database URL>"


_DATABASE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class InvalidTestDatabaseUrl(ValueError):
    """表示测试数据库 URL 不满足安全边界。

    异常消息只包含静态拒绝原因，禁止把含密码的原始 URL 或解析结果写入测试输出。
    """


@dataclass(frozen=True, slots=True)
class ValidatedTestDatabaseUrl:
    """保存已校验 URL 及其结构化解析结果。

    ``value`` 使用 ``TestDatabaseUrl`` 类型，调用方必须先经过
    :func:`validate_test_database_url` 才能得到它。``parsed`` 只用于安全地替换
    数据库名，不通过字符串拼接构造维护连接目标。
    """

    value: TestDatabaseUrl = field(repr=False)
    parsed: URL

    def __repr__(self) -> str:
        """避免 dataclass 调试表示包含完整连接目标。"""
        return "<validated test database target>"

    @property
    def database_name(self) -> str:
        """返回已验证的应用测试数据库名。"""
        database = self.parsed.database
        if database is None:  # pragma: no cover - 构造器已保证该不变量
            raise RuntimeError("validated URL unexpectedly has no database")
        return database

    def maintenance_url(self) -> URL:
        """返回同一本地端点连接固定 ``postgres`` 维护数据库的 URL。

        维护数据库名是代码常量，不来自环境变量；调用方只能用它执行创建或删除临时
        数据库所需的管理语句，不能误把共享应用库当作维护目标。
        """
        return self.parsed.set(database="postgres")

    def for_database(self, database_name: str) -> URL:
        """返回同一已验证端点连接指定内部生成数据库名的 URL。

        Args:
            database_name: 由测试代码生成并经标识符校验的临时数据库名。

        Raises:
            InvalidTestDatabaseUrl: 数据库名不是安全的 PostgreSQL 简单标识符。
        """
        _validate_database_identifier(database_name)
        if not database_name.endswith("_test"):
            raise InvalidTestDatabaseUrl("temporary database name must end with _test")
        return self.parsed.set(database=database_name)


def validate_test_database_url(value: str) -> ValidatedTestDatabaseUrl:
    """严格校验只供 PostgreSQL 集成测试使用的本地 URL。

    Args:
        value: 环境变量提供的原始 SQLAlchemy URL；函数不会记录或回显它。

    Returns:
        带有结构化 URL 的已校验值，后续迁移和引擎构造只能使用其中的 ``value``。

    Raises:
        InvalidTestDatabaseUrl: URL 缺少必要 authority、指向非本地目标、携带覆盖参数，
            或数据库名不是 ``_test`` 结尾。
    """
    if not isinstance(value, str) or value == "":
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL is required")
    if value != value.strip():
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL must not contain surrounding whitespace")
    if any(character in value for character in "\t\n\r"):
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL must not contain TAB, LF, or CR characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL contains an invalid control character")
    if not value.startswith("postgresql+asyncpg://"):
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL must use postgresql+asyncpg")
    # asyncpg 接受 query 中的 host/database/user 等连接参数；空 query 也拒绝，避免解析器
    # 在不同版本中对目标覆盖产生差异。Fragment 同样不能成为连接目标的一部分。
    if "?" in value:
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL must not contain a query")
    if "#" in value:
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL must not contain a fragment")

    try:
        parsed = make_url(value)
        port = parsed.port
    except (ArgumentError, TypeError, ValueError) as error:
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL is malformed") from error

    if parsed.drivername != "postgresql+asyncpg":
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL must use postgresql+asyncpg")
    if parsed.query:
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL must not contain a query")
    if parsed.username is None or parsed.username == "":
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL username is required")
    if _is_control_character(parsed.username) or (
        parsed.password is not None and _is_control_character(parsed.password)
    ):
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL credentials contain an invalid character")
    if parsed.host is None or not _is_loopback_host(parsed.host):
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL host must be loopback")
    if port is None or not 1 <= port <= 65535:
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL must include an explicit valid port")
    if parsed.database is None or parsed.database == "":
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL database is required")
    _validate_database_identifier(parsed.database)
    if not parsed.database.endswith("_test"):
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL database name must end with _test")

    return ValidatedTestDatabaseUrl(value=TestDatabaseUrl(value), parsed=parsed)


def _is_loopback_host(host: str) -> bool:
    """判断主机是否为明确的 loopback 地址，不执行 DNS 解析。"""
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_database_identifier(database_name: str) -> None:
    """验证数据库名可安全作为 PostgreSQL 简单标识符使用。"""
    if _DATABASE_IDENTIFIER.fullmatch(database_name) is None:
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL database name is malformed")
    if len(database_name.encode("utf-8")) > 63:
        raise InvalidTestDatabaseUrl("TEST_DATABASE_URL database name is too long")


def _is_control_character(value: str) -> bool:
    """判断解析后凭据是否包含不可安全传递的 ASCII 控制字符。"""
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
