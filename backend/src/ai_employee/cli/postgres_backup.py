"""完整普通/窗口备份父流程：同一owner生命周期、MVCC dump绑定、三件套和整组清理。

宿主先固定不可变镜像，镜像内只读文件检查先于Secret。全程锁序为management→target→
backup数据库锁→目录flock→shared schema；sealed再取得原有非阻塞revision lease。
导出快照只覆盖fingerprint/pg_dump，结束后以新事务检查revision/schema/lease才发布。
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadGuard,
    CalendarAadRolloutError,
)
from ai_employee.cli.calendar_aad_backup_0019 import run_backup_shell, run_guarded_backup
from ai_employee.cli.database_maintenance import (
    CalendarAadOwnerFactsReader,
    DatabaseEndpoint,
    MigrateArguments,
    _load_database_endpoint,
    _open_maintenance_context,
    _read_secret_file,
)
from ai_employee.cli.verify_restored_backup import (
    ExportedBackupSnapshot,
    _schema_digest,
    export_backup_snapshot,
)
from ai_employee.infrastructure.calendar_aad_resources import await_calendar_aad_resource
from ai_employee.infrastructure.db.database_maintenance import BackupMaintenanceHolder
from ai_employee.infrastructure.db.postgres_backup_manifest import (
    BackupGroupPublication,
    BackupManifest,
    BackupManifestError,
    BackupRemote,
    RemoteBackupObject,
    RemoteBackupPublication,
    ValidatedBackupGroup,
    _regular,
    _same_file,
    cleanup_local_backups,
    cleanup_remote_backups,
    discard_backup_publication,
    discard_remote_backup,
    image_executable_digests,
    publish_backup_group,
    publish_remote_backup,
    validate_backup_basename,
    write_backup_group,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory


@dataclass(frozen=True)
class BackupRequest:
    """来自固定组合根的安全输出/镜像事实；不承载密码或可执行命令。"""

    directory: Path
    basename: str
    immutable_image_id: str
    executable_digests: Mapping[str, str]
    release_version: str
    passphrase_file: Path
    sealed: bool = False

    def __post_init__(self) -> None:
        """参数错误在任何owner生命周期前拒绝，目录权限在取得目录锁时重新确认。"""
        validate_backup_basename(self.basename)
        if (
            not self.directory.is_absolute()
            or self.directory == Path("/")
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.immutable_image_id) is None
        ):
            raise BackupManifestError("backup_request_invalid")


class _DirectoryLease:
    """固定本地目录fd和flock fd；每次critical检查拒绝目录或锁文件被替换。"""

    def __init__(self, directory: Path, descriptor: int, lock: int) -> None:
        self.directory, self.descriptor, self.lock = directory, descriptor, lock
        self._directory_identity = os.fstat(descriptor)
        self._lock_identity = os.fstat(lock)

    def assert_live(self) -> None:
        """仍需数据库锁共同约束跨host；此处仅证明本次本地fd/公开路径未漂移。"""
        if not os.path.samestat(self._directory_identity, self.directory.lstat()) or not _same_file(
            self._lock_identity, (self.directory / ".ai-employee-backup.lock").lstat()
        ):
            raise BackupManifestError("backup_directory_lock_lost")
        # 对已持有的同一open-file-description重申排他锁不创造跨进程恢复authority。
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def _directory_lease(directory: Path) -> Iterator[_DirectoryLease]:
    """在数据库backup锁后打开0700真实目录并取得固定0600 flock，退出前处理完所有文件。"""
    for path in (directory, *directory.parents):
        if path.is_symlink():
            raise BackupManifestError("backup_directory_invalid")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = -1
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BackupManifestError("backup_directory_invalid")
        lock = os.open(
            ".ai-employee-backup.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=descriptor,
        )
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BackupManifestError("backup_directory_invalid")
        fcntl.flock(lock, fcntl.LOCK_EX)
        lease = _DirectoryLease(directory, descriptor, lock)
        lease.assert_live()
        yield lease
    finally:
        if lock >= 0:
            os.close(lock)
        os.close(descriptor)


class RcloneBackupRemote:
    """既有rclone工具的固定命令适配；stderr/配置/remote路径均不输出或持久记录。

    每个文件通过流式cat计算SHA256，以兼容不提供原生SHA256的remote。copyto使用
    immutable；删除资格由上层私有target/run库存协议提供，本适配没有purge能力。
    """

    def __init__(self, root: str) -> None:
        """要求显式remote子目录，避免把整个remote根或含凭据URL当作清理域。"""
        if re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_.\-/]+", root) is None or any(
            part in {"", ".", ".."} for part in root.split(":", 1)[1].split("/")
        ):
            raise BackupManifestError("backup_remote_root_invalid")
        self._root = root

    def _path(self, target: str, relative: str = "") -> str:
        if re.fullmatch(r"[0-9a-f]{64}", target) is None:
            raise BackupManifestError("backup_remote_namespace_invalid")
        if relative and (
            relative.startswith("/") or ".." in relative.split("/") or "\\" in relative
        ):
            raise BackupManifestError("backup_remote_namespace_invalid")
        return f"{self._root}/{target}" + (f"/{relative}" if relative else "")

    @staticmethod
    def _run(*arguments: str, allow_missing_directory: bool = False) -> bytes:
        result = subprocess.run(
            ["rclone", *arguments], capture_output=True, check=False, timeout=300
        )
        if result.returncode == 3 and allow_missing_directory:
            return b"[]"
        if result.returncode:
            raise BackupManifestError("backup_remote_command_failed")
        return result.stdout

    def _digest(self, path: str) -> str:
        """只流式读取密文/清单以hash，进程失败不回显可能包含remote内部信息的诊断。"""
        raw = self._run("hashsum", "SHA-256", "--download", path)
        lines = raw.decode("utf-8").splitlines()
        if len(lines) != 1 or re.fullmatch(r"[0-9a-f]{64}  .+", lines[0]) is None:
            raise BackupManifestError("backup_remote_inventory_invalid")
        return lines[0][:64]

    def inventory(self, target_digest: str) -> tuple[RemoteBackupObject, ...]:
        """读取整个target命名空间；目录不存在视作空只由rclone的成功空结果表示。"""
        raw: object = json.loads(
            self._run(
                "lsjson",
                "--recursive",
                "--files-only",
                self._path(target_digest),
                allow_missing_directory=True,
            )
        )
        if not isinstance(raw, list):
            raise BackupManifestError("backup_remote_inventory_invalid")
        result = []
        for value in raw:
            if not isinstance(value, dict):
                raise BackupManifestError("backup_remote_inventory_invalid")
            path, size, modified = value.get("Path"), value.get("Size"), value.get("ModTime")
            if type(path) is not str or type(size) is not int or type(modified) is not str:
                raise BackupManifestError("backup_remote_inventory_invalid")
            result.append(
                RemoteBackupObject(
                    path,
                    size,
                    self._digest(self._path(target_digest, path)),
                    datetime.fromisoformat(modified).astimezone(UTC),
                )
            )
        return tuple(result)

    def copy(self, source: Path, target_digest: str, relative_path: str) -> None:
        """只使用immutable copyto，不把最终prefix当共享flat目录。"""
        self._run("copyto", "--immutable", str(source), self._path(target_digest, relative_path))

    def read(self, target_digest: str, relative_path: str, *, limit: int) -> bytes:
        """通过固定byte-count上界读取小清单，超长或不完整由上层schema/hash检查拒绝。"""
        data = self._run("cat", "--count", str(limit + 1), self._path(target_digest, relative_path))
        if len(data) > limit:
            raise BackupManifestError("backup_remote_file_invalid")
        return data

    def delete(self, target_digest: str, relative_path: str) -> None:
        """只能精确删除一个已验证文件，禁止recursive/purge等扩大删除域的fallback。"""
        self._run("deletefile", self._path(target_digest, relative_path))


class _Publisher:
    """完整备份父流程的唯一producer/publisher；持有本地及远程receipt直到交付或补偿。"""

    def __init__(
        self,
        request: BackupRequest,
        holder: BackupMaintenanceHolder,
        directory_lease: _DirectoryLease,
        staging: Path,
        run_id: str,
        dump_environment: Mapping[str, str],
        remote: BackupRemote | None,
        host_guard: Callable[[], None] | None = None,
    ) -> None:
        self.request, self.holder, self.directory_lease = request, holder, directory_lease
        self.staging_root, self.run_id = staging, run_id
        self.environment, self.remote = dump_environment, remote
        self.host_guard = host_guard
        self.snapshot: ExportedBackupSnapshot | None = None
        self.publication: BackupGroupPublication | None = None

    def assert_locked(self) -> None:
        """数据库physical session与本地fd必须同时保持有效。"""
        self.holder.assert_live()
        self.directory_lease.assert_live()

    def _fresh(self) -> None:
        """在导出事务结束后重新读取revision和全schema；此处不用business旧快照作CAS。"""
        if self.host_guard is not None:
            self.host_guard()
        self.assert_locked()
        self.holder.assert_idle()
        snapshot = self.snapshot
        if snapshot is None:
            raise BackupManifestError("backup_snapshot_missing")
        observed = self.holder.read_only(
            lambda connection: (
                tuple(connection.scalars(text("SELECT version_num FROM public.alembic_version"))),
                _schema_digest(connection, revision=snapshot.fingerprint.revision),
            )
        )
        if observed != ((snapshot.fingerprint.revision,), snapshot.schema_digest):
            raise BackupManifestError("backup_schema_changed")

    async def produce(self, path: Path) -> None:
        """export transaction只覆盖实际pg_dump/fingerprint，取消时先结束子进程再释放快照。"""
        await await_calendar_aad_resource(
            asyncio.create_task(asyncio.to_thread(self.holder.assert_idle))
        )
        context = export_backup_snapshot(self.holder.connection)
        entered = asyncio.create_task(asyncio.to_thread(context.__enter__))
        failure: BaseException | None = None
        try:
            self.snapshot = await await_calendar_aad_resource(entered)
            await run_backup_shell(
                "produce", path, snapshot=self.snapshot.snapshot_id, environment=self.environment
            )
        except BaseException as error:
            failure = error
            raise
        finally:
            if not entered.cancelled() and entered.exception() is None:
                try:
                    await await_calendar_aad_resource(
                        asyncio.create_task(
                            asyncio.to_thread(
                                context.__exit__,
                                type(failure) if failure else None,
                                failure,
                                failure.__traceback__ if failure else None,
                            )
                        )
                    )
                except BaseException as cleanup_failure:
                    if failure is not None:
                        raise failure from cleanup_failure
                    raise
        await await_calendar_aad_resource(asyncio.create_task(asyncio.to_thread(self._fresh)))

    def _prepare(self, staged: Path) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            raise BackupManifestError("backup_snapshot_missing")
        _, digest, info = _regular(staged)
        write_backup_group(
            staged,
            BackupManifest(
                staged.name,
                snapshot.created_at,
                snapshot.postgres_server_version,
                snapshot.fingerprint.revision,
                digest,
                info.st_size,
                staged.name + ".sha256",
                self.request.immutable_image_id,
                {"release_version": self.request.release_version},
                snapshot.fingerprint.digest,
                self.request.executable_digests,
            ),
        )

    async def publish(
        self, staged: Path, target: Path, guard: CalendarAadGuard
    ) -> BackupGroupPublication:
        """单一checksum/manifest链；远程失败仍失败，普通组可保留，sealed失败须补偿本次组。"""
        if staged != self.staging_root / target.name or target.parent != self.request.directory:
            raise BackupManifestError("backup_publication_target_invalid")
        receipt: BackupGroupPublication | None = None
        preserve_local = False
        await await_calendar_aad_resource(
            asyncio.create_task(asyncio.to_thread(self._prepare, staged))
        )
        await await_calendar_aad_resource(asyncio.create_task(asyncio.to_thread(self._fresh)))
        await guard.verify()
        publication = asyncio.create_task(
            asyncio.to_thread(publish_backup_group, staged, target.parent)
        )
        try:
            receipt = await await_calendar_aad_resource(publication)
            if self.remote is not None:
                remote_task: asyncio.Task[RemoteBackupPublication] | None = None
                try:
                    group = await await_calendar_aad_resource(
                        asyncio.create_task(asyncio.to_thread(_published_group, target))
                    )
                    remote_task = asyncio.create_task(
                        asyncio.to_thread(
                            publish_remote_backup,
                            group,
                            self.remote,
                            target_digest=self.holder.target_identity_digest,
                            run_id=self.run_id,
                            assert_locked=self.assert_locked,
                        )
                    )
                    remote_receipt = await await_calendar_aad_resource(remote_task)
                    receipt = replace(receipt, remote=remote_receipt)
                except BaseException as failure:
                    # drain只延迟取消，不交付结果；从原Task取回已成功发布的remote receipt。
                    if (
                        remote_task is not None
                        and not remote_task.cancelled()
                        and remote_task.exception() is None
                    ):
                        receipt = replace(receipt, remote=remote_task.result())
                    preserve_local = (
                        not self.request.sealed
                        and not isinstance(failure, asyncio.CancelledError)
                        and receipt.remote is None
                    )
                    raise
            await guard.verify()
            await await_calendar_aad_resource(asyncio.create_task(asyncio.to_thread(self._fresh)))
            now = datetime.now(UTC)
            await await_calendar_aad_resource(
                asyncio.create_task(
                    asyncio.to_thread(
                        cleanup_local_backups,
                        directory=self.request.directory,
                        current_run=self.run_id,
                        current_dump=target.name,
                        now=now,
                        assert_locked=self.assert_locked,
                    )
                )
            )
            if self.remote is not None:
                await await_calendar_aad_resource(
                    asyncio.create_task(
                        asyncio.to_thread(
                            cleanup_remote_backups,
                            self.remote,
                            target_digest=self.holder.target_identity_digest,
                            current_run=self.run_id,
                            now=now,
                            assert_locked=self.assert_locked,
                        )
                    )
                )
            await guard.verify()
            await await_calendar_aad_resource(asyncio.create_task(asyncio.to_thread(self._fresh)))
            self.publication = receipt
            return receipt
        except BaseException as failure:
            if receipt is None and not publication.cancelled() and publication.exception() is None:
                receipt = publication.result()
            if receipt is not None and not preserve_local:
                try:
                    await await_calendar_aad_resource(asyncio.create_task(self.discard(receipt)))
                except BaseException as cleanup_failure:
                    raise failure from cleanup_failure
            raise

    async def discard(self, receipt: BackupGroupPublication) -> None:
        """同一owned线程收齐local/remote补偿，后续取消不能跳过另一份receipt。"""

        def discard() -> None:
            failure: BaseException | None = None
            self.assert_locked()
            if receipt.remote is not None and self.remote is not None:
                try:
                    discard_remote_backup(
                        self.remote, receipt.remote, assert_locked=self.assert_locked
                    )
                except BaseException as error:  # noqa: BLE001 - 收齐两份owned receipt后重抛首错。
                    failure = error
            try:
                discard_backup_publication(receipt)
            except BaseException as error:  # noqa: BLE001 - 不吞错，末尾按首次失败重抛。
                if failure is None:
                    failure = error
            if failure is not None:
                raise failure

        try:
            await await_calendar_aad_resource(asyncio.create_task(asyncio.to_thread(discard)))
        finally:
            if self.publication == receipt:
                self.publication = None


def _published_group(path: Path) -> ValidatedBackupGroup:
    """重新读取刚发布的完整组，禁止在remote上传前使用已漂移的本地成员。"""
    from ai_employee.infrastructure.db.postgres_backup_manifest import _group_at

    return _group_at(path)


class _OrdinaryGuard:
    """普通backup没有日历artifact，但在每个统一guard边界检查真实持锁事实。"""

    def __init__(self, publisher: _Publisher) -> None:
        self._publisher = publisher

    async def verify(self) -> datetime | None:
        await await_calendar_aad_resource(
            asyncio.create_task(asyncio.to_thread(self._publisher._fresh))
        )
        return None


async def _run_locked(
    publisher: _Publisher, endpoint: DatabaseEndpoint, owner_password_file: Path
) -> None:
    """真实子进程统一从该loop管理；TERM也走取消drain及外层锁内补偿。"""
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is not None:
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
        except (RuntimeError, ValueError):
            pass  # 测试在线程调用时由拥有该线程的外层负责取消收尾。
    request = publisher.request
    sessions: ManagedAsyncSessionMaker | None = None
    try:
        if request.sealed:
            app_password = _read_secret_file(Path("/run/secrets/app_database_password"))
            url = endpoint.owner_url(app_password, database_name=endpoint.database_name).set(
                drivername="postgresql+asyncpg", username="ai_employee_app"
            )
            sessions = build_session_factory(url.render_as_string(hide_password=False))
            await run_guarded_backup(
                sessions=sessions,
                backup_directory=request.directory,
                basename=request.basename,
                immutable_image_id=request.immutable_image_id,
                clock=lambda: datetime.now(UTC),
                producer=publisher.produce,
                publisher=publisher,
                facts_reader=CalendarAadOwnerFactsReader(
                    endpoint, owner_password_file, holder=publisher.holder
                ),
            )
        else:
            target = request.directory / f"{request.basename}.dump.enc"
            staged = publisher.staging_root / target.name
            await publisher.produce(staged)
            await publisher.publish(staged, target, _OrdinaryGuard(publisher))
    finally:
        if sessions is not None:
            await sessions.dispose()
        try:
            loop.remove_signal_handler(signal.SIGTERM)
        except (RuntimeError, ValueError):
            pass


def run_backup(
    request: BackupRequest,
    *,
    endpoint: DatabaseEndpoint,
    owner_password_file: Path,
    remote: BackupRemote | None = None,
    host_guard: Callable[[], None] | None = None,
) -> Path:
    """在完整management/target/backup/flock/shared-schema内生产、发布、remote和清理。

    Args:
        request: 宿主/镜像纯校验过的输出和镜像绑定，不允许任意命令。
        endpoint: 同一固定owner endpoint；legacy只从它的隔离组合根传入一次性目标。
        owner_password_file: 固定Secret只读文件，凭据只进入私有producer环境。
        remote: 现有rclone或测试Fake，生产启用时完整组失败使整个命令失败。
    """
    owner = _read_secret_file(owner_password_file)
    _read_secret_file(request.passphrase_file)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PG")
        and not any(
            word in name.upper() for word in ("DATABASE_URL", "TOKEN", "SECRET", "PASSWORD", "KEY")
        )
    }
    environment.update(
        {
            "PGHOST": endpoint.host,
            "PGPORT": str(endpoint.port),
            "PGDATABASE": endpoint.database_name,
            "PGUSER": endpoint.owner_role,
            "PGPASSWORD": owner.get_secret_value(),
            "PGAPPNAME": "ai_employee_backup",
            "BACKUP_PASSPHRASE_FILE": str(request.passphrase_file),
        }
    )
    run_id = str(uuid4())
    with (
        _open_maintenance_context(MigrateArguments(owner_password_file), endpoint) as context,
        context.acquire_management_lifecycle_lock() as management,
        management.acquire_target_lock() as target,
        target.backup_holder() as holder,
        _directory_lease(request.directory) as directory_lease,
    ):
        target_path = request.directory / f"{request.basename}.dump.enc"
        if any(
            os.path.lexists(Path(str(target_path) + suffix))
            for suffix in ("", ".sha256", ".manifest.json")
        ):
            raise BackupManifestError("backup_group_collision")
        staging = request.directory / f".partial.{run_id}"
        staging.mkdir(mode=0o700)
        identity = staging.lstat()
        publisher: _Publisher | None = None
        failure: BaseException | None = None
        try:
            with holder.shared_schema():
                publisher = _Publisher(
                    request,
                    holder,
                    directory_lease,
                    staging,
                    run_id,
                    environment,
                    remote,
                    host_guard,
                )
                asyncio.run(_run_locked(publisher, endpoint, owner_password_file))
        except BaseException as error:
            failure = error
            # shared-schema退出也可能失败；backup数据库锁和flock仍持有，receipt不能先丢。
            if publisher is not None and publisher.publication is not None:
                try:
                    asyncio.run(publisher.discard(publisher.publication))
                except BaseException as cleanup_failure:
                    raise error from cleanup_failure
            raise
        finally:
            try:
                # 独占private staging只允许本次固定文件，绝不递归删除后来进入的未知内容。
                if not os.path.samestat(staging.lstat(), identity):
                    raise BackupManifestError("backup_staging_changed")
                descriptor = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    names = os.listdir(descriptor)
                    allowed = {
                        target_path.name + suffix for suffix in ("", ".sha256", ".manifest.json")
                    }
                    if any(name not in allowed for name in names):
                        raise BackupManifestError("backup_staging_changed")
                    facts = {
                        name: os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        for name in names
                    }
                    if any(
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600
                        for info in facts.values()
                    ):
                        raise BackupManifestError("backup_staging_changed")
                    for name, info in facts.items():
                        if not _same_file(
                            info, os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        ):
                            raise BackupManifestError("backup_staging_changed")
                        os.unlink(name, dir_fd=descriptor)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                staging.rmdir()
                os.fsync(directory_lease.descriptor)
            except BaseException as cleanup_failure:
                if failure is not None:
                    raise failure from cleanup_failure
                if publisher is not None and publisher.publication is not None:
                    try:
                        asyncio.run(publisher.discard(publisher.publication))
                    except BaseException as compensation_failure:
                        raise cleanup_failure from compensation_failure
                raise
    return target_path


def main(arguments: Sequence[str] | None = None) -> int:
    """固定无参数镜像入口；普通时间戳命名，显式窗口basename只走真实sealed guard。"""
    try:
        if arguments if arguments is not None else sys.argv[1:]:
            raise BackupManifestError("backup_arguments_invalid")
        executables = image_executable_digests()
        supplied = os.environ.get("BACKUP_ARTIFACT_BASENAME")
        request = BackupRequest(
            Path(os.environ.get("BACKUP_DIR", "")),
            supplied
            if supplied is not None
            else datetime.now(UTC).strftime("ai_employee-%Y%m%dT%H%M%S.%fZ"),
            os.environ.get("BACKUP_IMMUTABLE_IMAGE_ID", ""),
            executables,
            version("ai-employee"),
            Path("/run/secrets/backup_passphrase"),
            sealed=supplied is not None,
        )
        parsed = _load_database_endpoint()
        if parsed.owner_role not in {"ai_employee_owner", "ai_employee_app"}:
            raise BackupManifestError("backup_endpoint_invalid")
        endpoint = replace(parsed, owner_role="ai_employee_owner")
        remote_name = os.environ.get("BACKUP_RCLONE_REMOTE", "")
        app_env = os.environ.get("APP_ENV")
        if app_env not in {"development", "test", "production"} or (
            app_env == "production" and not remote_name
        ):
            raise BackupManifestError("backup_environment_invalid")
        if request.sealed and any(
            os.environ.get(flag, "false") != "false"
            for flag in (
                "EXTERNAL_WRITES_ENABLED",
                "GOOGLE_WRITES_ENABLED",
                "MICROSOFT_WRITES_ENABLED",
            )
        ):
            raise BackupManifestError("backup_write_switch_enabled")
        from ai_employee.cli.postgres_restore import _host_guard

        if request.sealed:
            _host_guard()
        run_backup(
            request,
            endpoint=endpoint,
            owner_password_file=Path("/run/secrets/postgres_bootstrap_password"),
            remote=RcloneBackupRemote(remote_name) if remote_name else None,
            host_guard=_host_guard if request.sealed else None,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        SQLAlchemyError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
        CalendarAadRolloutError,
    ):
        print("postgres_backup_failed", file=sys.stderr)
        return 1
    print("postgres_backup_completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
