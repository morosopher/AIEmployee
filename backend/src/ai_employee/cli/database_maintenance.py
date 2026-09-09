"""提供唯一 role-bootstrap、migrate 与 protected db-reset 生命周期 CLI。

公开参数只包含 typed 子命令、Secret 文件路径与 reset 的非敏感确认字段。数据库
目标继续复用仓库现有 ``DATABASE_URL`` authority，但该 URL 必须不含密码、query 或
fragment；owner/app/retention Secret 只从普通文件读取，并在进程内以遮蔽类型保存。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import stat
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import NoReturn
from urllib.parse import unquote, urlsplit

from alembic.config import Config
from alembic.util.exc import CommandError
from pydantic import SecretStr
from sqlalchemy import URL, Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadFacts,
    CalendarAadRevision,
    CalendarAadRolloutError,
)
from ai_employee.infrastructure.calendar_aad_resources import await_calendar_aad_resource
from ai_employee.infrastructure.db.alembic import (
    load_published_alembic_authority,
    run_alembic_upgrade_on_connection,
)
from ai_employee.infrastructure.db.database_access import BootstrapCaller
from ai_employee.infrastructure.db.database_maintenance import (
    DatabaseMaintenanceInvariantError,
    ProtectedResetPolicy,
    ReadOnlyMaintenanceHolder,
    RestoreClaim,
    RestoreCompletionAckUnknown,
    RestoreEvidenceStore,
    RestoreFingerprint,
    SqlAlchemyDatabaseMaintenanceContext,
    migrate_database,
    reset_then_migrate,
    role_bootstrap_database,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_SIMPLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MALFORMED_PERCENT_TRIPLET = re.compile(r"%(?![0-9A-Fa-f]{2})")
_MAX_SECRET_BYTES = 65536
_ENDPOINT_ERROR_MESSAGE = "database endpoint is invalid"
_SECRET_ERROR_MESSAGE = "database Secret file is invalid"
_ALLOWED_URL_DRIVERS = {
    "postgresql",
    "postgresql+asyncpg",
    "postgresql+psycopg",
}


class DatabaseEndpointError(ValueError):
    """表示 passwordless ``DATABASE_URL`` 不满足 lifecycle endpoint 闭集。"""


class SecretFileError(ValueError):
    """表示 Secret 路径、权限、编码或内容不满足固定闭集。"""


def _endpoint_fail() -> NoReturn:
    """抛出不包含 URL、主机、用户名或数据库名的稳定 endpoint 错误。"""
    raise DatabaseEndpointError(_ENDPOINT_ERROR_MESSAGE)


def _strict_unquote_url_component(value: str) -> str:
    """校验 percent triplet 后只执行一次严格 UTF-8 URL component 解码。

    ``urllib.parse.unquote`` 的 ``errors='strict'`` 只约束 percent bytes 解码后的
    UTF-8，仍会原样保留 incomplete/non-hex ``%``。这里先要求每个字面 ``%`` 后
    恰有两个 ASCII hex digits，再单次解码；不得 normalization 或二次 unquote。

    Args:
        value: 尚未解码的单个 URL component。

    Returns:
        单次解码且保持原始 Unicode code points 的文本。

    Raises:
        DatabaseEndpointError: percent triplet 或 UTF-8 不合法。
    """
    if _MALFORMED_PERCENT_TRIPLET.search(value) is not None:
        _endpoint_fail()
    try:
        return unquote(value, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError):
        _endpoint_fail()


def _secret_fail() -> NoReturn:
    """抛出不包含路径、权限位或 Secret 内容的稳定文件错误。"""
    raise SecretFileError(_SECRET_ERROR_MESSAGE)


@dataclass(frozen=True, slots=True)
class RoleBootstrapArguments:
    """保存 standalone role-bootstrap 的三个 Secret 文件路径。"""

    owner_password_file: Path
    app_password_file: Path
    retention_password_file: Path


@dataclass(frozen=True, slots=True)
class MigrateArguments:
    """保存 ordinary typed migration 唯一需要的 owner Secret 文件路径。"""

    owner_password_file: Path


@dataclass(frozen=True, slots=True)
class DbResetArguments:
    """保存 protected reset 的 Secret 路径与 exact 非生产确认事实。"""

    owner_password_file: Path
    app_password_file: Path
    retention_password_file: Path
    app_env: str
    confirmed_database_name: str

    @property
    def reset_policy(self) -> ProtectedResetPolicy:
        """每次访问都重新构造不可绕过的 exact reset policy。"""
        return ProtectedResetPolicy(
            app_env=self.app_env,
            confirmed_database_name=self.confirmed_database_name,
        )


LifecycleArguments = RoleBootstrapArguments | MigrateArguments | DbResetArguments


@dataclass(frozen=True, slots=True, repr=False)
class DatabaseEndpoint:
    """保存从唯一 passwordless ``DATABASE_URL`` 解析出的非敏感 endpoint。"""

    host: str
    port: int
    owner_role: str
    database_name: str

    def __post_init__(self) -> None:
        """分别验证 endpoint、owner role 与逐字保留的 PostgreSQL 数据库名。"""
        if (
            type(self.host) is not str
            or self.host == ""
            or any(ord(character) < 32 or ord(character) == 127 for character in self.host)
        ):
            _endpoint_fail()
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            _endpoint_fail()
        if (
            type(self.owner_role) is not str
            or _SIMPLE_IDENTIFIER.fullmatch(self.owner_role) is None
            or len(self.owner_role.encode("utf-8")) > 63
        ):
            _endpoint_fail()

        # PostgreSQL quoted database identifier 允许任意严格 UTF-8（NUL 除外）；这里不得
        # normalization、case-fold 或截断，否则后续锁 identity 与 destructive target 会漂移。
        if (
            type(self.database_name) is not str
            or self.database_name == ""
            or "\0" in self.database_name
        ):
            _endpoint_fail()
        try:
            database_name_utf8 = self.database_name.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            _endpoint_fail()
        if not 1 <= len(database_name_utf8) <= 63:
            _endpoint_fail()

    def owner_url(self, password: SecretStr, *, database_name: str) -> URL:
        """以 SQLAlchemy 结构化 URL 注入进程内 owner Secret，不做字符串渲染。"""
        if database_name not in {"postgres", self.database_name}:
            _endpoint_fail()
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.owner_role,
            password=password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=database_name,
        )


def _add_owner_password_file(parser: argparse.ArgumentParser) -> None:
    """向一个 subparser 添加唯一 owner Secret 文件参数。"""
    parser.add_argument("--owner-password-file", required=True, type=Path)


def _add_runtime_password_files(parser: argparse.ArgumentParser) -> None:
    """向需要角色 mutation 的 subparser 添加两个 runtime Secret 文件参数。"""
    parser.add_argument("--app-password-file", required=True, type=Path)
    parser.add_argument("--retention-password-file", required=True, type=Path)


def _parse_arguments(arguments: Sequence[str] | None) -> LifecycleArguments:
    """解析唯一三个 lifecycle 子命令，未知 flag/命令由 argparse fail closed。"""
    parser = argparse.ArgumentParser(description="Run a typed database lifecycle operation.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    role_parser = subparsers.add_parser("role-bootstrap")
    _add_owner_password_file(role_parser)
    _add_runtime_password_files(role_parser)

    migrate_parser = subparsers.add_parser("migrate")
    _add_owner_password_file(migrate_parser)

    reset_parser = subparsers.add_parser("db-reset")
    _add_owner_password_file(reset_parser)
    _add_runtime_password_files(reset_parser)
    reset_parser.add_argument("--app-env", required=True, choices=("development", "test"))
    reset_parser.add_argument("--confirmed-database-name", required=True)

    namespace = parser.parse_args(arguments)
    if namespace.command == "role-bootstrap":
        return RoleBootstrapArguments(
            owner_password_file=Path(namespace.owner_password_file),
            app_password_file=Path(namespace.app_password_file),
            retention_password_file=Path(namespace.retention_password_file),
        )
    if namespace.command == "migrate":
        return MigrateArguments(owner_password_file=Path(namespace.owner_password_file))
    if namespace.command == "db-reset":
        return DbResetArguments(
            owner_password_file=Path(namespace.owner_password_file),
            app_password_file=Path(namespace.app_password_file),
            retention_password_file=Path(namespace.retention_password_file),
            app_env=str(namespace.app_env),
            confirmed_database_name=str(namespace.confirmed_database_name),
        )
    _endpoint_fail()


def _read_secret_file(path: Path) -> SecretStr:
    """以 no-follow、regular-file 与固定权限闭集读取一个 UTF-8 Secret。

    Docker Secret 的只读 ``0444`` 形状被允许；任何执行位、group/other 写位、无读取位、
    symlink、目录、设备、FIFO、超长文件、空白内容或 NUL 都拒绝。末尾 CR/LF 仅作为
    文件边界移除，其他空格保持原样并只在全空白时拒绝。

    Args:
        path: argparse 提供的显式 Secret 文件路径。

    Returns:
        默认 ``repr`` 遮蔽内容的 ``SecretStr``。

    Raises:
        SecretFileError: 路径、权限、编码、大小或内容不满足闭集。
    """
    try:
        path_stat = path.lstat()
        unsafe_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_IWGRP | stat.S_IWOTH
        readable_bits = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_mode & unsafe_bits
            or not path_stat.st_mode & readable_bits
        ):
            _secret_fail()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_dev != path_stat.st_dev
                or opened_stat.st_ino != path_stat.st_ino
            ):
                _secret_fail()
            raw = os.read(descriptor, _MAX_SECRET_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > _MAX_SECRET_BYTES:
            _secret_fail()
        value = raw.decode("utf-8", errors="strict").rstrip("\r\n")
    except SecretFileError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        _secret_fail()
    if value == "" or value.isspace() or "\0" in value:
        _secret_fail()
    return SecretStr(value)


def _load_database_endpoint(
    environment: Mapping[str, str] | None = None,
) -> DatabaseEndpoint:
    """从现有 ``DATABASE_URL`` 读取唯一 passwordless lifecycle endpoint。

    URL 中的密码会与 Secret-file authority 形成第二来源，因此明确拒绝；query 与 fragment
    可能覆盖 host/user/database，也在交给 SQLAlchemy parser 前拒绝。函数不回显原始 URL。
    """
    source = os.environ if environment is None else environment
    value = source.get("DATABASE_URL")
    if type(value) is not str or value == "" or value != value.strip():
        _endpoint_fail()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _endpoint_fail()
    if not value.startswith(tuple(f"{driver}://" for driver in _ALLOWED_URL_DRIVERS)):
        _endpoint_fail()
    if "?" in value or "#" in value:
        _endpoint_fail()
    try:
        split_url = urlsplit(value)
        encoded_username = split_url.username
        encoded_database_name = split_url.path.removeprefix("/")
        if encoded_username is None:
            _endpoint_fail()
        owner_role = _strict_unquote_url_component(encoded_username)
        database_name = _strict_unquote_url_component(encoded_database_name)
        parsed = make_url(value)
        port = parsed.port
    except (ArgumentError, TypeError, ValueError):
        _endpoint_fail()
    if (
        parsed.drivername not in _ALLOWED_URL_DRIVERS
        or parsed.password is not None
        or parsed.query
        or parsed.username is None
        or parsed.username != owner_role
        or parsed.host is None
        or port is None
        or parsed.database is None
        or parsed.database != encoded_database_name
    ):
        _endpoint_fail()
    return DatabaseEndpoint(
        host=parsed.host,
        port=port,
        owner_role=owner_role,
        database_name=database_name,
    )


@contextmanager
def _open_maintenance_context(
    arguments: LifecycleArguments,
    endpoint: DatabaseEndpoint,
) -> Iterator[SqlAlchemyDatabaseMaintenanceContext]:
    """构造两个 ``NullPool`` Engine，并在退出时明确释放其生命周期。

    management/target URL 只由同一 endpoint 与同一 owner Secret 结构化派生。普通 migrate
    不读取 app/retention Secret；standalone bootstrap/reset 才读取并解封这两个值。
    """
    owner_password = _read_secret_file(arguments.owner_password_file)
    app_password: SecretStr | None = None
    retention_password: SecretStr | None = None
    reset_policy: ProtectedResetPolicy | None = None
    bootstrap_caller = BootstrapCaller.ROLE_BOOTSTRAP
    if isinstance(arguments, (RoleBootstrapArguments, DbResetArguments)):
        app_password = _read_secret_file(arguments.app_password_file)
        retention_password = _read_secret_file(arguments.retention_password_file)
    if isinstance(arguments, DbResetArguments):
        bootstrap_caller = BootstrapCaller.DB_RESET_POST_CREATE
        reset_policy = arguments.reset_policy

    management_engine = create_engine(
        endpoint.owner_url(owner_password, database_name="postgres"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    target_engine = create_engine(
        endpoint.owner_url(owner_password, database_name=endpoint.database_name),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        published_authority = load_published_alembic_authority(
            Config(_BACKEND_ROOT / "alembic.ini")
        )
        yield SqlAlchemyDatabaseMaintenanceContext(
            management_engine=management_engine,
            target_engine=target_engine,
            target_database_name=endpoint.database_name,
            bootstrap_caller=bootstrap_caller,
            app_password=None if app_password is None else app_password.get_secret_value(),
            retention_password=(
                None if retention_password is None else retention_password.get_secret_value()
            ),
            migration_runner=partial(
                run_alembic_upgrade_on_connection,
                config_path=_BACKEND_ROOT / "alembic.ini",
            ),
            published_authority=published_authority,
            reset_policy=reset_policy,
        )
    finally:
        target_engine.dispose()
        management_engine.dispose()


class CalendarAadOwnerFactsReader:
    """固定calendar one-off专用的短owner读取器；绝不把连接交给应用或provider。

    每次独立读取取得management→target→schema，验证baseline后执行一整个新RR/RO
    facts读取；借用backup/audit持有者时复用其同一物理连接，不建立第二个锁竞争者。
    endpoint/Secret路径仅存在于该CLI组合对象，调用方只能取得CalendarAadFacts。
    """

    def __init__(
        self,
        endpoint: DatabaseEndpoint,
        owner_password_file: Path,
        *,
        holder: ReadOnlyMaintenanceHolder | None = None,
    ) -> None:
        """保存固定目标和Secret文件；构造不读Secret，实际每次读取才建立受控资源。"""
        self._endpoint = endpoint
        self._password_file = owner_password_file
        self._holder = holder

    @classmethod
    def from_environment(cls) -> CalendarAadOwnerFactsReader:
        """只从现有passwordless应用endpoint派生同库固定owner，不接受新的连接来源。"""
        app = _load_database_endpoint()
        if app.owner_role != "ai_employee_app":
            raise CalendarAadRolloutError("calendar_aad_owner_facts_invalid")
        return cls(
            DatabaseEndpoint(app.host, app.port, "ai_employee_owner", app.database_name),
            Path("/run/secrets/postgres_bootstrap_password"),
        )

    async def read_facts(self, *, expected_revision: CalendarAadRevision) -> CalendarAadFacts:
        """等待自己拥有的完整读取线程结束；取消不遗留owner事务或延长旧快照。"""
        return await await_calendar_aad_resource(
            asyncio.create_task(asyncio.to_thread(self._read, expected_revision))
        )

    def _read(self, expected_revision: CalendarAadRevision) -> CalendarAadFacts:
        """完整revision/pair/expiry读取只发生在实际owner RR/RO中，退出后才交付事实。"""
        from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
            read_calendar_aad_facts,
        )

        def read(connection: Connection) -> CalendarAadFacts:
            state = connection.execute(
                text(
                    "SELECT current_user,session_user,current_database(),"
                    "current_setting('transaction_read_only'),"
                    "current_setting('transaction_isolation')"
                )
            ).one()
            if tuple(state) != (
                self._endpoint.owner_role,
                self._endpoint.owner_role,
                self._endpoint.database_name,
                "on",
                "repeatable read",
            ):
                raise CalendarAadRolloutError("calendar_aad_owner_facts_invalid")
            return read_calendar_aad_facts(connection, expected_revision=expected_revision)

        if self._holder is not None:
            self._holder.assert_idle()
            return self._holder.read_only(read)
        with (
            _open_maintenance_context(
                MigrateArguments(self._password_file), self._endpoint
            ) as context,
            context.acquire_management_lifecycle_lock() as management,
            management.acquire_target_lock() as target,
            target.acquire_schema_lifecycle_lock(),
            target.read_only_holder() as holder,
        ):
            return holder.read_only(read)


def run_restore_maintenance(
    *,
    endpoint: DatabaseEndpoint,
    owner_password_file: Path,
    claim: RestoreClaim,
    dump: Path,
    reader: Callable[[Connection], RestoreFingerprint],
    verifier: Callable[[Connection, RestoreFingerprint], None],
    evidence: RestoreEvidenceStore,
    consumer_environment: Mapping[str, str],
    host_guard: Callable[[], None],
    approve_call_ordinal: int | None = None,
    approve_reopen_ordinal: int | None = None,
) -> str:
    """generic/sealed共用既有owner生命周期；只在最终COMMIT未知后新建session核对。

    Args:
        endpoint: 已通过纯文件/镜像/宿主校验的唯一passwordless目标。
        owner_password_file: 固定只读bootstrap Secret文件，不进入generator环境。
        claim: manifest精确kind/source/revision/fingerprint绑定。
        dump: 私有解密目录中的单一native dump。
        reader: 完整内容无关fingerprint reader，始终由持有者控制事务。
        verifier: 操作选定的同session app READ ONLY验证回调。
        evidence: 独立state-v4投影存储，不提供admission或重试授权。
        consumer_environment: 只给固定psql consumer的进程内连接参数。
        host_guard: 重查stopped-services/三开关的宿主握手，不替代数据库事实。
        approve_call_ordinal: 操作者明确批准的下一个真实恢复调用序号。
        approve_reopen_ordinal: 操作者明确批准的下一个最终提交序号。

    Returns:
        完成、未应用或人工处置的固定结果码；任何未知ACK均禁止盲目重放。
    """
    arguments = MigrateArguments(owner_password_file)
    host_guard()
    try:
        with (
            _open_maintenance_context(arguments, endpoint) as context,
            context.acquire_management_lifecycle_lock() as management,
            management.acquire_restore_target_lock(claim) as target,
            target.acquire_schema_lifecycle_lock(),
            target.restore_holder(claim) as holder,
        ):
            host_guard()
            return holder.execute(
                dump=dump,
                reader=reader,
                verifier=verifier,
                evidence=evidence,
                consumer_environment=consumer_environment,
                approve_call_ordinal=approve_call_ordinal,
                approve_reopen_ordinal=approve_reopen_ordinal,
                before_stream=host_guard,
            )
    except RestoreCompletionAckUnknown as unknown:
        # 原context及两条物理session均已退出，不能让连接池或调用者anchor复用旧授权。
        host_guard()
        try:
            with (
                _open_maintenance_context(arguments, endpoint) as context,
                context.acquire_management_lifecycle_lock() as management,
                management.acquire_restore_target_lock(claim) as target,
                target.acquire_schema_lifecycle_lock(),
                target.restore_holder(claim) as holder,
            ):
                return holder.reconcile_completion_ack(unknown, evidence)
        except DatabaseMaintenanceInvariantError:
            return "restore_needs_attention"


def main(arguments: Sequence[str] | None = None) -> int:
    """执行一个 typed lifecycle，并只输出不含 endpoint/Secret 的稳定结果。

    Args:
        arguments: 可选 argv；未提供时由 argparse 使用进程参数。

    Returns:
        完成返回 0；配置、Secret、admission、Alembic 或数据库失败返回 1。
    """
    parsed = _parse_arguments(arguments)
    try:
        endpoint = _load_database_endpoint()
        with _open_maintenance_context(parsed, endpoint) as maintenance_context:
            if isinstance(parsed, RoleBootstrapArguments):
                role_bootstrap_database(maintenance_context)
            elif isinstance(parsed, MigrateArguments):
                migrate_database(maintenance_context)
            elif isinstance(parsed, DbResetArguments):
                reset_then_migrate(maintenance_context)
            else:  # pragma: no cover - parser 的封闭 union 已保证不可达。
                _endpoint_fail()
    except (
        CommandError,
        DatabaseEndpointError,
        DatabaseMaintenanceInvariantError,
        SecretFileError,
        SQLAlchemyError,
        RuntimeError,
        UnicodeError,
        ValueError,
    ):
        # 下层异常可能携带 URL、driver 细节或 catalog 内容；公开 CLI 只返回固定消息。
        print("database lifecycle command failed", file=sys.stderr)
        return 1
    print("database lifecycle command completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
