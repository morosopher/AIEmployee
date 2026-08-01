"""从显式密码文件创建 M1 唯一管理员，拒绝覆盖现有身份。"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from argon2.exceptions import HashingError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.application.use_cases.auth import (
    AdminAlreadyExistsError,
    CreateAdminUseCase,
    EmptyAdminPasswordError,
    InvalidAdminEmailError,
)
from ai_employee.config import get_settings
from ai_employee.infrastructure.db.repositories.identity import (
    SqlAlchemyIdentityRepositoryFactory,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher


@dataclass(frozen=True, slots=True)
class CreateAdminArguments:
    """保存 argparse 已解析且仍不含密码内容的 CLI 参数。"""

    email: str
    password_file: Path
    if_absent: bool


def _parse_arguments(arguments: Sequence[str] | None) -> CreateAdminArguments:
    """解析管理员邮箱、显式密码文件与受限幂等开关。"""
    parser = argparse.ArgumentParser(description="Create the single AI Employee administrator.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password-file", required=True, type=Path)
    parser.add_argument("--if-absent", action="store_true")
    namespace = parser.parse_args(arguments)
    return CreateAdminArguments(
        email=str(namespace.email),
        password_file=Path(namespace.password_file),
        if_absent=bool(namespace.if_absent),
    )


def _read_password_file(path: Path) -> str:
    """按 UTF-8 读取密码文件并只移除常见行结束符。

    Args:
        path: 调用方明确传入的密码文件路径。

    Returns:
        保留有意义边界空格、但去除文件末尾换行的密码。

    Raises:
        OSError: 文件不存在、权限不足或读取失败。
        UnicodeDecodeError: 文件不是有效 UTF-8。
        EmptyAdminPasswordError: 文件去除行结束符后为空或全为空白。
    """
    password = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not password or password.isspace():
        raise EmptyAdminPasswordError
    return password


async def _create_admin(arguments: CreateAdminArguments, password: str) -> bool:
    """组合配置、事务和 Argon2id 适配器并执行管理员创建用例。"""
    settings = get_settings()
    session_factory = build_session_factory(settings.database_url)
    try:
        result = await CreateAdminUseCase(
            SqlAlchemyIdentityRepositoryFactory(session_factory),
            PasswordHasher(),
        ).execute(
            email=arguments.email,
            password=password,
            timezone=settings.default_timezone,
            brief_time=settings.default_brief_time,
            if_absent=arguments.if_absent,
        )
        return result.created
    finally:
        await session_factory.dispose()


def main(arguments: Sequence[str] | None = None) -> int:
    """运行管理员创建命令并只输出不含身份或凭据的稳定结果。

    Args:
        arguments: 可选参数序列；未提供时由 argparse 读取进程参数。

    Returns:
        创建或同邮箱幂等 no-op 返回 0；安全拒绝或基础设施失败返回 1。
    """
    parsed = _parse_arguments(arguments)
    try:
        password = _read_password_file(parsed.password_file)
        created = asyncio.run(_create_admin(parsed, password))
    except EmptyAdminPasswordError:
        print("create-admin refused: password file is empty", file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError):
        print("create-admin refused: password file could not be read", file=sys.stderr)
        return 1
    except InvalidAdminEmailError:
        print("create-admin refused: email is invalid", file=sys.stderr)
        return 1
    except AdminAlreadyExistsError:
        print("create-admin refused: an administrator already exists", file=sys.stderr)
        return 1
    except (ValidationError, SQLAlchemyError, HashingError, ValueError):
        # 配置、数据库和哈希异常可能携带内部值；CLI 边界只返回静态消息，避免泄露 DSN 或 Hash。
        print("create-admin failed: configuration or database operation failed", file=sys.stderr)
        return 1

    if created:
        print("Administrator created.")
    else:
        print("Administrator already exists; no changes made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
