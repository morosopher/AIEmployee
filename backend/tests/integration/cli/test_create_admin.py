"""验证管理员创建 CLI 的单管理员、密码文件和幂等安全边界。"""

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher


@dataclass(frozen=True, slots=True)
class CliResult:
    """保存 CLI 退出码及经 UTF-8 解码的标准输出，便于检查泄漏。"""

    return_code: int
    stdout: str
    stderr: str


async def _run_create_admin(database_url: str, *arguments: str) -> CliResult:
    """以参数数组而非 Shell 字符串运行管理员 CLI。"""
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": database_url,
            "DEFAULT_TIMEZONE": "UTC",
            "DEFAULT_BRIEF_TIME": "09:15",
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ai_employee.cli.create_admin",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    stdout, stderr = await process.communicate()
    return CliResult(
        return_code=process.returncode or 0,
        stdout=stdout.decode("utf-8"),
        stderr=stderr.decode("utf-8"),
    )


def _assert_no_sensitive_output(
    result: CliResult,
    *,
    database_url: str,
    password: str,
    password_hash: str | None = None,
) -> None:
    """确认 CLI 不回显密码、哈希或完整数据库 URL。"""
    combined = result.stdout + result.stderr
    assert database_url not in combined
    if password:
        assert password not in combined
    if password_hash is not None:
        assert password_hash not in combined


@pytest.mark.asyncio
async def test_create_admin_is_single_account_and_if_absent_is_same_email_noop(
    database_url: str,
    tmp_path: Path,
) -> None:
    """首次创建成功；覆盖、不同邮箱幂等均拒绝；同规范化邮箱保持原哈希。"""
    first_password = "synthetic-first-password"
    replacement_password = "synthetic-replacement-password"
    first_password_file = tmp_path / "first password.txt"
    replacement_password_file = tmp_path / "replacement password.txt"
    first_password_file.write_text(f"{first_password}\n", encoding="utf-8")
    replacement_password_file.write_text(replacement_password, encoding="utf-8")

    first = await _run_create_admin(
        database_url,
        "--email",
        "  OWNER@EXAMPLE.COM  ",
        "--password-file",
        str(first_password_file),
    )
    assert first.return_code == 0

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(UserModel)) == 1
            saved = await session.scalar(select(UserModel))
            assert saved is not None
            original_hash = saved.password_hash
            assert saved.email == "owner@example.com"
            assert saved.is_active is True
            assert saved.locale == "zh-CN"
            assert saved.timezone == "UTC"
            assert saved.brief_time.isoformat() == "09:15:00"
            assert original_hash is not None
            assert original_hash.startswith("$argon2id$")
            assert PasswordHasher().verify(original_hash, first_password)
            assert not PasswordHasher().verify(original_hash, replacement_password)

        _assert_no_sensitive_output(
            first,
            database_url=database_url,
            password=first_password,
            password_hash=original_hash,
        )

        overwrite = await _run_create_admin(
            database_url,
            "--email",
            "owner@example.com",
            "--password-file",
            str(replacement_password_file),
        )
        assert overwrite.return_code != 0
        _assert_no_sensitive_output(
            overwrite,
            database_url=database_url,
            password=replacement_password,
            password_hash=original_hash,
        )

        same_email_noop = await _run_create_admin(
            database_url,
            "--email",
            " Owner@Example.Com ",
            "--password-file",
            str(replacement_password_file),
            "--if-absent",
        )
        assert same_email_noop.return_code == 0
        _assert_no_sensitive_output(
            same_email_noop,
            database_url=database_url,
            password=replacement_password,
            password_hash=original_hash,
        )

        different_email = await _run_create_admin(
            database_url,
            "--email",
            "other@example.com",
            "--password-file",
            str(replacement_password_file),
            "--if-absent",
        )
        assert different_email.return_code != 0
        _assert_no_sensitive_output(
            different_email,
            database_url=database_url,
            password=replacement_password,
            password_hash=original_hash,
        )

        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(UserModel)) == 1
            unchanged = await session.scalar(select(UserModel))
            assert unchanged is not None
            assert unchanged.password_hash == original_hash
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_create_admin_rejects_empty_or_missing_password_file_without_user(
    database_url: str,
    tmp_path: Path,
) -> None:
    """空密码与缺少显式文件参数都必须在创建数据库记录前失败。"""
    empty_password_file = tmp_path / "empty-password.txt"
    empty_password_file.write_text(" \n\t", encoding="utf-8")

    empty = await _run_create_admin(
        database_url,
        "--email",
        "owner@example.com",
        "--password-file",
        str(empty_password_file),
    )
    assert empty.return_code != 0
    assert "create-admin refused: password file is empty" in empty.stderr
    _assert_no_sensitive_output(empty, database_url=database_url, password="")

    missing_argument = await _run_create_admin(
        database_url,
        "--email",
        "owner@example.com",
    )
    assert missing_argument.return_code != 0
    assert "--password-file" in missing_argument.stderr
    _assert_no_sensitive_output(missing_argument, database_url=database_url, password="")

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(UserModel)) == 0
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_concurrent_create_admin_calls_cannot_create_two_different_emails(
    database_url: str,
    tmp_path: Path,
) -> None:
    """两个并发首次调用也必须由数据库事务串行化为恰好一个管理员。"""
    first_password = "synthetic-concurrent-password-one"
    second_password = "synthetic-concurrent-password-two"
    first_password_file = tmp_path / "concurrent-first.txt"
    second_password_file = tmp_path / "concurrent-second.txt"
    first_password_file.write_text(first_password, encoding="utf-8")
    second_password_file.write_text(second_password, encoding="utf-8")

    session_factory = build_session_factory(database_url)
    try:
        # 测试触发器让首个 INSERT 暂停，确保无串行化锁时两个进程都能先观察到空表。
        async with session_factory.begin() as session:
            await session.execute(
                text(
                    """
                    CREATE FUNCTION task4_delay_admin_insert() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                    BEGIN
                      PERFORM pg_sleep(0.5);
                      RETURN NEW;
                    END;
                    $$
                    """
                )
            )
            await session.execute(
                text(
                    """
                    CREATE TRIGGER task4_delay_admin_insert
                    BEFORE INSERT ON users
                    FOR EACH ROW EXECUTE FUNCTION task4_delay_admin_insert()
                    """
                )
            )

        try:
            first, second = await asyncio.gather(
                _run_create_admin(
                    database_url,
                    "--email",
                    "first@example.com",
                    "--password-file",
                    str(first_password_file),
                ),
                _run_create_admin(
                    database_url,
                    "--email",
                    "second@example.com",
                    "--password-file",
                    str(second_password_file),
                ),
            )
        finally:
            async with session_factory.begin() as session:
                await session.execute(
                    text("DROP TRIGGER IF EXISTS task4_delay_admin_insert ON users")
                )
                await session.execute(text("DROP FUNCTION IF EXISTS task4_delay_admin_insert()"))

        assert sorted((first.return_code, second.return_code)) == [0, 1]
        _assert_no_sensitive_output(
            first,
            database_url=database_url,
            password=first_password,
        )
        _assert_no_sensitive_output(
            second,
            database_url=database_url,
            password=second_password,
        )
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(UserModel)) == 1
    finally:
        await session_factory.dispose()
