"""普通 PostgreSQL 备份三件套的闭合协议及有归属证据的本地发布。

manifest 是唯一完成标记；目录项发布顺序固定为 dump、checksum、manifest。纯文件
验证不读取 Secret、数据库或网络。调用方在线程执行有界本地 I/O，并持有数据库备份
锁及目录 flock。补偿复用已审计的私有 custody 算法，只撤回 receipt 指明的本次文件；
保留的 `.calendar-aad-custody-*` 永远不属于备份 orphan/retention 清理范围。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast
from uuid import UUID

from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadGuard,
    CalendarAadRolloutError,
)
from ai_employee.infrastructure.calendar_aad_publication import (
    _return_without_replacement,
    discard_calendar_aad_publication,
)

MANIFEST_SCHEMA = "ai_employee.postgres_backup_manifest.v1"
ORPHAN_GRACE_SECONDS = 3600
# 路径是镜像内契约，不允许 manifest 自行扩展可执行集合或转向宿主可写文件。
OPERATIONS_EXECUTABLES = (
    "/app/scripts/backup-postgres.sh",
    "/app/scripts/restore-postgres.sh",
    "/app/scripts/restore-calendar-aad-0018.sh",
    "/app/scripts/init-db-roles.sh",
    "/app/scripts/convert-legacy-backup.sh",
    "/app/scripts/scavenge-legacy-backups.sh",
    "/app/scripts/audit-calendar-aad-0019.sh",
    "/app/scripts/run-postgres-operations.py",
    "/app/scripts/legacy-backup-operations.py",
    "/app/backend/src/ai_employee/cli/database_maintenance.py",
    "/app/backend/src/ai_employee/cli/postgres_backup.py",
    "/app/backend/src/ai_employee/cli/postgres_restore.py",
    "/app/backend/src/ai_employee/cli/verify_restored_backup.py",
    "/app/backend/src/ai_employee/cli/calendar_aad_verify_restored_0018.py",
    "/app/backend/src/ai_employee/cli/calendar_aad_audit_0019.py",
    "/app/backend/src/ai_employee/cli/calendar_aad_backup_0019.py",
    "/app/backend/src/ai_employee/infrastructure/db/database_access.py",
    "/app/backend/src/ai_employee/infrastructure/db/database_grants.py",
    "/app/backend/src/ai_employee/infrastructure/db/database_maintenance.py",
    "/app/backend/src/ai_employee/infrastructure/db/postgres_restore_stream.py",
    "/app/backend/src/ai_employee/infrastructure/db/postgres_backup_manifest.py",
)
_KEYS = frozenset(
    {
        "schema",
        "dump_basename",
        "created_at",
        "postgres_server_version",
        "alembic_revision",
        "dump_sha256",
        "dump_size",
        "checksum_filename",
        "immutable_image_id",
        "build_metadata",
        "restore_fingerprint_v1",
        "executable_digests",
    }
)
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z")
_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")


class BackupManifestError(RuntimeError):
    """三件套验证、发布或补偿失败；错误码不携带路径、正文、Secret 或原始诊断。"""


@dataclass(frozen=True)
class BackupManifest:
    """已验证的版本化清单；映射只读，不能在验证后替换图像或可执行摘要。"""

    dump_basename: str
    created_at: str
    postgres_server_version: str
    alembic_revision: str
    dump_sha256: str
    dump_size: int
    checksum_filename: str
    immutable_image_id: str
    build_metadata: Mapping[str, str]
    restore_fingerprint_v1: str
    executable_digests: Mapping[str, str]

    def canonical_bytes(self) -> bytes:
        """生成排序、ASCII、无空白歧义且只有一个尾换行的最终 manifest 字节。"""
        return _canonical(
            {
                "schema": MANIFEST_SCHEMA,
                "dump_basename": self.dump_basename,
                "created_at": self.created_at,
                "postgres_server_version": self.postgres_server_version,
                "alembic_revision": self.alembic_revision,
                "dump_sha256": self.dump_sha256,
                "dump_size": self.dump_size,
                "checksum_filename": self.checksum_filename,
                "immutable_image_id": self.immutable_image_id,
                "build_metadata": dict(self.build_metadata),
                "restore_fingerprint_v1": self.restore_fingerprint_v1,
                "executable_digests": dict(self.executable_digests),
            }
        )


@dataclass(frozen=True)
class BackupFileIdentity:
    """固定regular文件身份；atime可由校验自身推进，其他身份/元数据仍须精确相等。"""

    path: Path
    stat: os.stat_result = field(compare=False, repr=False)
    _comparison_identity: tuple[int, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """显式冻结纳秒级变更事实，避免stat_result默认相等比较把只读atime更新当作替换。"""
        info = self.stat
        object.__setattr__(
            self,
            "_comparison_identity",
            (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_uid,
                info.st_gid,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_nlink,
            ),
        )


@dataclass(frozen=True)
class ValidatedBackupGroup:
    """绑定三个文件及 manifest SHA-256；不替代稍后执行前的重新验证。"""

    dump: Path
    manifest: BackupManifest
    manifest_sha256: str
    members: tuple[BackupFileIdentity, ...]


@dataclass(frozen=True)
class BackupGroupPublication:
    """只证明本次本地发布的不可变 receipt；不能从 basename 推断删除权限。"""

    members: tuple[BackupFileIdentity, ...]
    remote: RemoteBackupPublication | None = None


@dataclass(frozen=True)
class RemoteBackupObject:
    """私有target目录内一个remote文件的内容无关事实；非协作写入不在协议保证内。"""

    path: str
    size: int
    sha256: str
    modified_at: datetime


@dataclass(frozen=True)
class RemoteBackupPublication:
    """本次独占target/run命名空间的精确库存；不声称rclone提供条件删除或CAS。"""

    target_digest: str
    run_id: str
    members: tuple[RemoteBackupObject, ...]


class BackupRemote(Protocol):
    """现有rclone的封闭适配；所有调用由同一backup DB锁和本地flock生命周期包围。"""

    def inventory(self, target_digest: str) -> tuple[RemoteBackupObject, ...]:
        """返回目标下完整文件库存；不支持哈希或查询不确定时必须失败。"""
        ...

    def copy(self, source: Path, target_digest: str, relative_path: str) -> None:
        """只做immutable-copy到本次独占run；不能覆盖共享flat对象。"""
        ...

    def read(self, target_digest: str, relative_path: str, *, limit: int) -> bytes:
        """仅用于有界manifest/checksum字节，不读取明文或打印remote响应。"""
        ...

    def delete(self, target_digest: str, relative_path: str) -> None:
        """锁内精确重验后删除指定私有文件；不提供通配、purge或跨prefix操作。"""
        ...


class BackupGroupPublisher(Protocol):
    """完整27D父流程给sealed hook的窄发布接口；归属延续到最终revision lease退出。"""

    staging_root: Path

    async def publish(
        self, staged: Path, target: Path, guard: CalendarAadGuard
    ) -> BackupGroupPublication:
        """发布完整组并完成remote/retention；返回前的失败由本接口自己完成补偿。"""
        ...

    async def discard(self, receipt: BackupGroupPublication) -> None:
        """仅撤回receipt绑定的本次发布；外层management/backup/flock仍须保持持有。"""
        ...


def validate_backup_basename(value: str) -> str:
    """验证不带 `.dump.enc` 的输出 basename，排除路径、隐藏目录和点段歧义。"""
    if type(value) is not str or _BASENAME.fullmatch(value) is None or ".." in value:
        raise BackupManifestError("backup_basename_invalid")
    return value


def _canonical(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BackupManifestError("backup_manifest_invalid")
        result[key] = value
    return result


def _string(value: object) -> str:
    if type(value) is not str:
        raise BackupManifestError("backup_manifest_invalid")
    return cast(str, value)


def _hex(value: object) -> str:
    result = _string(value)
    if _HEX.fullmatch(result) is None:
        raise BackupManifestError("backup_manifest_invalid")
    return result


def _string_mapping(value: object, keys: frozenset[str]) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BackupManifestError("backup_manifest_invalid")
    return {_string(key): _string(member) for key, member in value.items()}


def parse_backup_manifest(data: bytes) -> BackupManifest:
    """严格解析有界 canonical 清单，不接触数据库、Secret 或解密工具。

    Args:
        data: 精确清单字节，最多64KiB；重复键、未知键、宽松数字与编码均拒绝。

    Returns:
        闭合 schema 的不可变事实；revision/image 的外部信任绑定另由组验证完成。
    """
    try:
        if not 0 < len(data) <= 65536:
            raise BackupManifestError("backup_manifest_invalid")
        raw: object = json.loads(data.decode("ascii"), object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or set(raw) != _KEYS or raw["schema"] != MANIFEST_SCHEMA:
            raise BackupManifestError("backup_manifest_invalid")
        if _canonical(raw) != data:
            raise BackupManifestError("backup_manifest_invalid")
        name = _string(raw["dump_basename"])
        if not name.endswith(".dump.enc"):
            raise BackupManifestError("backup_manifest_invalid")
        validate_backup_basename(name.removesuffix(".dump.enc"))
        created = _string(raw["created_at"])
        if _UTC.fullmatch(created) is None or datetime.fromisoformat(created).tzinfo != UTC:
            raise BackupManifestError("backup_manifest_invalid")
        version = _string(raw["postgres_server_version"])
        if re.fullmatch(r"(?:1[2-9]|[2-9][0-9])\.[0-9]{1,3}(?:\.[0-9]{1,3})?", version) is None:
            raise BackupManifestError("backup_manifest_invalid")
        revision = _string(raw["alembic_revision"])
        if re.fullmatch(r"[0-9]{8}_[0-9]{4}", revision) is None:
            raise BackupManifestError("backup_manifest_invalid")
        size = raw["dump_size"]
        if type(size) is not int or not 0 < size < 2**63:
            raise BackupManifestError("backup_manifest_invalid")
        checksum = _string(raw["checksum_filename"])
        image = _string(raw["immutable_image_id"])
        if checksum != name + ".sha256" or not image.startswith("sha256:"):
            raise BackupManifestError("backup_manifest_invalid")
        _hex(image.removeprefix("sha256:"))
        metadata = _string_mapping(raw["build_metadata"], frozenset({"release_version"}))
        if (
            re.fullmatch(
                r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}(?:[-+.][A-Za-z0-9.]{1,32})?",
                metadata["release_version"],
            )
            is None
        ):
            raise BackupManifestError("backup_manifest_invalid")
        executables = _string_mapping(raw["executable_digests"], frozenset(OPERATIONS_EXECUTABLES))
        for digest in executables.values():
            _hex(digest)
        return BackupManifest(
            name,
            created,
            version,
            revision,
            _hex(raw["dump_sha256"]),
            size,
            checksum,
            image,
            MappingProxyType(metadata),
            _hex(raw["restore_fingerprint_v1"]),
            MappingProxyType(executables),
        )
    except (UnicodeError, ValueError, TypeError, KeyError, OverflowError) as failure:
        raise BackupManifestError("backup_manifest_invalid") from failure


def _regular(path: Path, *, limit: int | None = None) -> tuple[bytes, str, os.stat_result]:
    """以同一 no-follow fd 校验所有者/0600并流式哈希，避免把替换文件或FIFO当备份。

    祖先路径也逐级禁止 symlink。大 dump 仅返回摘要，不把密文整体载入内存；小清单及
    checksum 才按固定上限返回内容。结束时检查 fd 与目录项仍是起始的同一版本。
    """
    try:
        if not path.is_absolute() or ".." in path.parts:
            raise BackupManifestError("backup_file_invalid")
        for parent in path.parents:
            if parent.is_symlink() or not parent.is_dir():
                raise BackupManifestError("backup_file_invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid != os.geteuid()
                or before.st_size <= 0
                or (limit is not None and before.st_size > limit)
            ):
                raise BackupManifestError("backup_file_invalid")
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
                    if limit is not None:
                        chunks.append(chunk)
            after = os.fstat(descriptor)
            current = path.lstat()
            if any(not _same_file(before, candidate) for candidate in (after, current)):
                raise BackupManifestError("backup_file_changed")
            return b"".join(chunks), digest.hexdigest(), before
        finally:
            os.close(descriptor)
    except OSError as failure:
        raise BackupManifestError("backup_file_invalid") from failure


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        os.path.samestat(first, second)
        and first.st_mode == second.st_mode
        and first.st_uid == second.st_uid
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def validate_backup_group(
    dump: Path,
    *,
    supported_revisions: frozenset[str],
    expected_image_id: str,
    expected_executable_digests: Mapping[str, str],
) -> ValidatedBackupGroup:
    """在任何 owner Secret/服务/连接前交叉验证相邻三件套与实际本地镜像。

    Args:
        dump: 显式 encrypted dump，必须是当前所有者的0600普通文件。
        supported_revisions: 当前镜像冻结的可恢复 revision 集合。
        expected_image_id: 宿主对本地镜像解析的不可变 sha256 内容ID，禁止 tag 代替。
        expected_executable_digests: 实际不可变镜像内固定路径的精确摘要映射。

    Raises:
        BackupManifestError: 任一文件、版本、镜像、校验和或完整性绑定不匹配。
    """
    dump = dump.absolute()
    manifest_path = Path(f"{dump}.manifest.json")
    checksum_path = Path(f"{dump}.sha256")
    raw, manifest_digest, manifest_stat = _regular(manifest_path, limit=65536)
    manifest = parse_backup_manifest(raw)
    if (
        manifest.dump_basename != dump.name
        or manifest.alembic_revision not in supported_revisions
        or manifest.immutable_image_id != expected_image_id
        or dict(manifest.executable_digests) != dict(expected_executable_digests)
    ):
        raise BackupManifestError("backup_manifest_binding_mismatch")
    _, dump_digest, dump_stat = _regular(dump)
    checksum, _, checksum_stat = _regular(checksum_path, limit=1024)
    expected_checksum = (
        f"{dump_digest}  {dump.name}\n{manifest_digest}  {manifest_path.name}\n".encode("ascii")
    )
    if (
        dump_digest != manifest.dump_sha256
        or dump_stat.st_size != manifest.dump_size
        or checksum != expected_checksum
    ):
        raise BackupManifestError("backup_group_digest_mismatch")
    members = tuple(
        BackupFileIdentity(path, identity)
        for path, identity in (
            (dump, dump_stat),
            (checksum_path, checksum_stat),
            (manifest_path, manifest_stat),
        )
    )
    for member in members:
        if not _same_file(member.stat, member.path.lstat()):
            raise BackupManifestError("backup_file_changed")
    return ValidatedBackupGroup(dump, manifest, manifest_digest, members)


def image_executable_digests() -> Mapping[str, str]:
    """只从固定镜像路径计算运行代码摘要；缺文件/链接失败，绝不读取 owner Secret。"""
    result: dict[str, str] = {}
    try:
        for name in OPERATIONS_EXECUTABLES:
            path = Path(name)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise BackupManifestError("backup_image_executable_invalid")
                result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as failure:
        raise BackupManifestError("backup_image_executable_invalid") from failure
    return MappingProxyType(result)


def validate_generic_backup_group(
    dump: Path,
    *,
    current_image_id: str,
    executable_digests: Mapping[str, str],
) -> ValidatedBackupGroup:
    """普通灾备允许相同受管代码的镜像重建，历史manifest来源保持原样。

    当前ID仍须由host实际解析并pin，完整代码摘要和支持revision均严格相等；底层依赖
    兼容性由当前镜像发布门禁承担。sealed/audit不能调用本入口来放宽原镜像绑定。
    """
    if re.fullmatch(r"sha256:[0-9a-f]{64}", current_image_id) is None:
        raise BackupManifestError("backup_image_invalid")
    raw, _, _ = _regular(Path(f"{dump}.manifest.json"), limit=65536)
    manifest = parse_backup_manifest(raw)
    return validate_backup_group(
        dump,
        supported_revisions=frozenset({"20260809_0018", "20260809_0019"}),
        expected_image_id=manifest.immutable_image_id,
        expected_executable_digests=executable_digests,
    )


def write_backup_group(staged_dump: Path, manifest: BackupManifest) -> ValidatedBackupGroup:
    """在调用方独占 staging 内写最终清单和双条checksum，文件/目录均fsync后返回。

    只接受真实 dump 摘要已绑定的清单；禁止凭空给旧格式字节补一个 manifest 入口。
    legacy 转换只能先实际恢复、验证，再调用正常 backup producer 到这个内部边界。
    """
    manifest_path = Path(f"{staged_dump}.manifest.json")
    data = manifest.canonical_bytes()
    parse_backup_manifest(data)
    checksum = f"{manifest.dump_sha256}  {staged_dump.name}\n{hashlib.sha256(data).hexdigest()}  {manifest_path.name}\n".encode(
        "ascii"
    )
    for path, content in ((manifest_path, data), (Path(f"{staged_dump}.sha256"), checksum)):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    directory = os.open(staged_dump.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return validate_backup_group(
        staged_dump,
        supported_revisions=frozenset({manifest.alembic_revision}),
        expected_image_id=manifest.immutable_image_id,
        expected_executable_digests=manifest.executable_digests,
    )


def publish_backup_group(staged_dump: Path, destination_dir: Path) -> BackupGroupPublication:
    """以 no-clobber 硬链接和 manifest-last 发布，失败只补偿本次receipt的成员。

    同目录/文件系统 staging 由持锁备份父流程提供；所有最终名先检查且每次 link 仍
    原子拒绝覆盖。receipt 在 sealed hook 最终 guard/lease exit 前不能丢弃。
    """
    data, _, _ = _regular(Path(f"{staged_dump}.manifest.json"), limit=65536)
    manifest = parse_backup_manifest(data)
    group = validate_backup_group(
        staged_dump,
        supported_revisions=frozenset({manifest.alembic_revision}),
        expected_image_id=manifest.immutable_image_id,
        expected_executable_digests=manifest.executable_digests,
    )
    members: list[BackupFileIdentity] = []
    try:
        if any(os.path.lexists(destination_dir / item.path.name) for item in group.members):
            raise BackupManifestError("backup_group_collision")
        for item in group.members:
            destination = destination_dir / item.path.name
            # 在系统调用前保留身份；即使调用已成功却报告错误，也只处理已证明归属者。
            members.append(BackupFileIdentity(destination, item.stat))
            os.link(item.path, destination, follow_symlinks=False)
            if not _same_file(item.stat, destination.lstat()):
                raise BackupManifestError("backup_file_changed")
        descriptor = os.open(destination_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException as failure:
        try:
            discard_backup_publication(BackupGroupPublication(tuple(members)))
        except BackupManifestError as cleanup_failure:
            raise BackupManifestError("backup_publication_failed") from cleanup_failure
        if isinstance(failure, OSError):
            raise BackupManifestError("backup_publication_failed") from failure
        raise
    return BackupGroupPublication(tuple(members))


def discard_backup_publication(receipt: BackupGroupPublication) -> None:
    """先撤回本次manifest marker，再收齐其他成员；外来替换或不确定捕获必须保留。

    只适配已审计的本地custody补偿，不给orphan、retention或remote增加删除权限。
    即便一个成员失败也处理另外成员，并在末尾传播第一个稳定错误。
    """
    failure: CalendarAadRolloutError | None = None
    for member in reversed(receipt.members):
        try:
            discard_calendar_aad_publication(member.path, member.stat)
        except CalendarAadRolloutError as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise BackupManifestError("backup_publication_compensation_failed") from failure


def _remote_run(value: str) -> str:
    """只接受canonical UUID目录，拒绝嵌套路径、别名和其他工具遗留布局。"""
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as failure:
        raise BackupManifestError("backup_remote_namespace_invalid") from failure
    return value


def _remote_inventory(remote: BackupRemote, target: str) -> tuple[RemoteBackupObject, ...]:
    """验证查询形状及唯一relative key；未知布局由上层保留而非猜测归属。"""
    _hex(target)
    result = remote.inventory(target)
    if not isinstance(result, tuple):
        raise BackupManifestError("backup_remote_inventory_invalid")
    for item in result:
        if (
            type(item) is not RemoteBackupObject
            or type(item.path) is not str
            or not item.path
            or item.path.startswith("/")
            or ".." in item.path.split("/")
            or "\\" in item.path
            or any(part in {"", "."} for part in item.path.split("/"))
            or type(item.size) is not int
            or item.size < 0
            or not isinstance(item.modified_at, datetime)
            or item.modified_at.tzinfo != UTC
        ):
            raise BackupManifestError("backup_remote_inventory_invalid")
        _hex(item.sha256)
    if len({item.path for item in result}) != len(result):
        raise BackupManifestError("backup_remote_inventory_invalid")
    return tuple(sorted(result, key=lambda item: item.path))


def _prefix_members(
    inventory: tuple[RemoteBackupObject, ...], run_id: str
) -> tuple[RemoteBackupObject, ...]:
    return tuple(item for item in inventory if item.path.startswith(run_id + "/"))


def _expected_remote(group: ValidatedBackupGroup, run_id: str) -> dict[str, tuple[int, str]]:
    """精确三成员内容，不把mtime或rclone的同名成功当作完整组证明。"""
    values: dict[str, tuple[int, str]] = {}
    for item in group.members:
        _, digest, info = _regular(item.path, limit=65536 if item.path != group.dump else None)
        if not _same_file(item.stat, info):
            raise BackupManifestError("backup_file_changed")
        values[f"{run_id}/{item.path.name}"] = (info.st_size, digest)
    return values


def _remote_exact(
    members: tuple[RemoteBackupObject, ...], expected: Mapping[str, tuple[int, str]]
) -> bool:
    return {item.path: (item.size, item.sha256) for item in members} == dict(expected)


def discard_remote_backup(
    remote: BackupRemote,
    receipt: RemoteBackupPublication,
    *,
    assert_locked: Callable[[], None],
) -> None:
    """manifest先撤回，每次重查完整库存后删除；此比较不是remote原子CAS。

    协作前提是所有repo写入持同一数据库锁且不写其他invocation的私有prefix。任何
    外来对象、mtime/hash变化、未知删除结果均保留剩余prefix并失败，禁止purge兜底。
    """
    _remote_run(receipt.run_id)
    remaining = {item.path: item for item in receipt.members}
    ordered = sorted(remaining, key=lambda name: (not name.endswith(".manifest.json"), name))
    for path in ordered:
        assert_locked()
        current = _prefix_members(_remote_inventory(remote, receipt.target_digest), receipt.run_id)
        if {item.path: item for item in current} != remaining:
            raise BackupManifestError("backup_remote_changed")
        remote.delete(receipt.target_digest, path)
        remaining.pop(path)
        following = _prefix_members(
            _remote_inventory(remote, receipt.target_digest), receipt.run_id
        )
        if {item.path: item for item in following} != remaining:
            raise BackupManifestError("backup_remote_delete_unknown")


def publish_remote_backup(
    group: ValidatedBackupGroup,
    remote: BackupRemote,
    *,
    target_digest: str,
    run_id: str,
    assert_locked: Callable[[], None],
) -> RemoteBackupPublication:
    """同target跨host查basename冲突，独占canonical run内dump/checksum先、manifest最后。

    只有验证完整三件套才交付receipt；交付前失败会尝试处理自己的确切已写prefix，
    不确定状态原样保留并传播首错。另一个target不共用此basename域。
    """
    _remote_run(run_id)
    assert_locked()
    inventory = _remote_inventory(remote, target_digest)
    names = {item.path.name for item in group.members}
    if any(item.path.rsplit("/", 1)[-1] in names for item in inventory) or _prefix_members(
        inventory, run_id
    ):
        raise BackupManifestError("backup_remote_collision")
    expected = _expected_remote(group, run_id)
    written: dict[str, tuple[int, str]] = {}
    try:
        for item in group.members:
            assert_locked()
            current = _prefix_members(_remote_inventory(remote, target_digest), run_id)
            if not _remote_exact(current, written):
                raise BackupManifestError("backup_remote_changed")
            path = f"{run_id}/{item.path.name}"
            # 先记录可能已应用的唯一预期内容，ACK失败时只允许这个有界集合被补偿。
            written[path] = expected[path]
            remote.copy(item.path, target_digest, path)
            current = _prefix_members(_remote_inventory(remote, target_digest), run_id)
            if not _remote_exact(current, written):
                raise BackupManifestError("backup_remote_incomplete")
        return RemoteBackupPublication(target_digest, run_id, current)
    except BaseException as failure:
        try:
            current = _prefix_members(_remote_inventory(remote, target_digest), run_id)
            if any(written.get(item.path) != (item.size, item.sha256) for item in current):
                raise BackupManifestError("backup_remote_compensation_uncertain")
            discard_remote_backup(
                remote,
                RemoteBackupPublication(target_digest, run_id, current),
                assert_locked=assert_locked,
            )
        except BaseException as cleanup_failure:
            raise failure from cleanup_failure
        raise


def retained_backup_names(groups: Mapping[str, datetime]) -> frozenset[str]:
    """按UTC保留最近七个日点及更早四个ISO周点；同日/同周只取最新完整组。"""
    daily: set[str] = set()
    weekly: set[tuple[int, int]] = set()
    keep: set[str] = set()
    for name, created in sorted(groups.items(), key=lambda item: (item[1], item[0]), reverse=True):
        if created.tzinfo != UTC:
            raise BackupManifestError("backup_time_invalid")
        day = created.date().isoformat()
        year, week, _ = created.isocalendar()
        if day not in daily and len(daily) < 7:
            daily.add(day)
            keep.add(name)
        elif day not in daily and (year, week) not in weekly and len(weekly) < 4:
            weekly.add((year, week))
            keep.add(name)
    return frozenset(keep)


def cleanup_remote_backups(
    remote: BackupRemote,
    *,
    target_digest: str,
    current_run: str,
    now: datetime,
    assert_locked: Callable[[], None],
) -> None:
    """仅处理已验证target/run三件套和过期不完整prefix；未知flat/legacy布局保留。

    每次删除前重读精确库存、marker、hash和mtime，3600秒为固定grace。所有remote
    删除仍依赖协作独占namespace；失败后不能恢复已经按保留策略删除的旧完整组。
    """
    if now.tzinfo != UTC:
        raise BackupManifestError("backup_time_invalid")
    _remote_run(current_run)
    assert_locked()
    inventory = _remote_inventory(remote, target_digest)
    runs = sorted({item.path.split("/", 1)[0] for item in inventory if "/" in item.path})
    complete: dict[str, tuple[datetime, RemoteBackupPublication]] = {}
    orphans: list[RemoteBackupPublication] = []
    for run_id in runs:
        try:
            _remote_run(run_id)
        except BackupManifestError:
            continue
        members = _prefix_members(inventory, run_id)
        markers = [item for item in members if item.path.endswith(".manifest.json")]
        if len(markers) == 1:
            marker = markers[0]
            try:
                raw = remote.read(target_digest, marker.path, limit=65536)
                manifest = parse_backup_manifest(raw)
                checksum_name = f"{run_id}/{manifest.checksum_filename}"
                checksum = remote.read(target_digest, checksum_name, limit=1024)
                expected_checksum = (
                    f"{manifest.dump_sha256}  {manifest.dump_basename}\n"
                    f"{hashlib.sha256(raw).hexdigest()}  {manifest.dump_basename}.manifest.json\n"
                ).encode("ascii")
                expected = {
                    f"{run_id}/{manifest.dump_basename}": (
                        manifest.dump_size,
                        manifest.dump_sha256,
                    ),
                    checksum_name: (len(checksum), hashlib.sha256(checksum).hexdigest()),
                    f"{run_id}/{manifest.dump_basename}.manifest.json": (
                        len(raw),
                        hashlib.sha256(raw).hexdigest(),
                    ),
                }
                if checksum != expected_checksum or not _remote_exact(members, expected):
                    raise BackupManifestError("backup_remote_group_invalid")
            except (BackupManifestError, OSError):
                # 有marker但不合法的组是人工检查对象，不能降格为orphan删除。
                continue
            complete[run_id] = (
                datetime.fromisoformat(manifest.created_at),
                RemoteBackupPublication(target_digest, run_id, members),
            )
        elif not markers and members and run_id != current_run:
            names = {item.path.removeprefix(run_id + "/") for item in members}
            basenames = {name.removesuffix(".sha256").removesuffix(".dump.enc") for name in names}
            if len(basenames) != 1:
                continue
            basename = next(iter(basenames))
            try:
                validate_backup_basename(basename)
            except BackupManifestError:
                continue
            if not names.issubset({f"{basename}.dump.enc", f"{basename}.dump.enc.sha256"}):
                continue
            if (
                max((now - item.modified_at).total_seconds() for item in members)
                < ORPHAN_GRACE_SECONDS
            ):
                continue
            if any(
                (now - item.modified_at).total_seconds() < ORPHAN_GRACE_SECONDS for item in members
            ):
                continue
            orphans.append(RemoteBackupPublication(target_digest, run_id, members))
    keep = retained_backup_names({run: value[0] for run, value in complete.items()})
    for run, (_, receipt) in complete.items():
        if run not in keep and run != current_run:
            discard_remote_backup(remote, receipt, assert_locked=assert_locked)
    for receipt in orphans:
        assert_locked()
        current = _prefix_members(_remote_inventory(remote, target_digest), receipt.run_id)
        if (
            current != receipt.members
            or any(
                (now - item.modified_at).total_seconds() < ORPHAN_GRACE_SECONDS for item in current
            )
            or any(item.path.endswith(".manifest.json") for item in current)
        ):
            raise BackupManifestError("backup_remote_changed")
        discard_remote_backup(remote, receipt, assert_locked=assert_locked)


@dataclass(frozen=True)
class _CleanupCandidate:
    """独立于publication receipt的删除资格：完整保留组或已过grace的封闭孤儿库存。"""

    kind: str
    root: Path
    members: tuple[BackupFileIdentity, ...]
    dump_name: str | None = None
    directory_identity: os.stat_result | None = None


def _group_at(dump: Path) -> ValidatedBackupGroup:
    """删除资格也要完整交叉验证旧组；旧镜像可以不同，但其闭合元数据不能伪造。"""
    raw, _, _ = _regular(Path(f"{dump}.manifest.json"), limit=65536)
    manifest = parse_backup_manifest(raw)
    return validate_backup_group(
        dump,
        supported_revisions=frozenset({"20260809_0018", "20260809_0019"}),
        expected_image_id=manifest.immutable_image_id,
        expected_executable_digests=manifest.executable_digests,
    )


def _old_enough(info: os.stat_result, now: datetime) -> bool:
    return now.timestamp() - info.st_mtime >= ORPHAN_GRACE_SECONDS


def _partial_candidate(directory: Path, now: datetime) -> _CleanupCandidate | None:
    """只认一个canonical .partial.UUID中的固定producer文件，禁止递归进入未知内容。"""
    try:
        _remote_run(directory.name.removeprefix(".partial."))
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
            or not _old_enough(info, now)
        ):
            return None
        entries = tuple(sorted(directory.iterdir()))
        if len(entries) > 4:
            return None
        members: list[BackupFileIdentity] = []
        basenames: set[str] = set()
        plaintext = 0
        for path in entries:
            name = path.name
            if re.fullmatch(r"\.ai-employee-backup\.[A-Za-z0-9]{6}\.dump", name):
                plaintext += 1
                if plaintext > 1:
                    return None
            else:
                stem = name.removesuffix(".manifest.json").removesuffix(".sha256")
                if not stem.endswith(".dump.enc"):
                    return None
                validate_backup_basename(stem.removesuffix(".dump.enc"))
                if name not in {stem, stem + ".sha256", stem + ".manifest.json"}:
                    return None
                basenames.add(stem)
            item = path.lstat()
            if (
                not stat.S_ISREG(item.st_mode)
                or stat.S_IMODE(item.st_mode) != 0o600
                or item.st_uid != os.geteuid()
                or not _old_enough(item, now)
            ):
                return None
            members.append(BackupFileIdentity(path, item))
        if len(basenames) > 1 or any(
            os.path.lexists(directory.parent / f"{name}.manifest.json") for name in basenames
        ):
            return None
        return _CleanupCandidate(
            "partial", directory, tuple(members), next(iter(basenames), None), info
        )
    except (OSError, BackupManifestError):
        return None


def _incomplete_candidate(dump: Path, now: datetime) -> _CleanupCandidate | None:
    """只认普通时间戳命名空间的过期未完成组；完整旧式checksum pair保持原样。"""
    if not dump.name.startswith("ai_employee-") or not dump.name.endswith(".dump.enc"):
        return None
    try:
        validate_backup_basename(dump.name.removesuffix(".dump.enc"))
        if os.path.lexists(Path(f"{dump}.manifest.json")):
            return None
        entries = sorted(dump.parent.glob(dump.name + "*"))
        if not entries or any(
            path.name not in {dump.name, dump.name + ".sha256"} for path in entries
        ):
            return None
        members = []
        for path in entries:
            _, _, info = _regular(path, limit=1024 if path.name.endswith(".sha256") else None)
            if not _old_enough(info, now):
                return None
            members.append(BackupFileIdentity(path, info))
        checksum_path = Path(f"{dump}.sha256")
        if dump.exists() and checksum_path.exists():
            checksum, _, _ = _regular(checksum_path, limit=1024)
            _, digest, _ = _regular(dump)
            if checksum == f"{digest}  {dump.name}\n".encode("ascii"):
                return None
        return _CleanupCandidate("incomplete", dump.parent, tuple(members), dump.name)
    except (OSError, BackupManifestError):
        return None


def _revalidate_cleanup(candidate: _CleanupCandidate, now: datetime) -> None:
    """捕获前重读完整库存与mtime/marker；先前资格不能授权后来出现的目录项。"""
    if candidate.kind == "complete":
        assert candidate.dump_name is not None
        current = _group_at(candidate.root / candidate.dump_name)
        members = current.members
    elif candidate.kind == "partial":
        partial = _partial_candidate(candidate.root, now)
        if (
            partial is None
            or partial.directory_identity is None
            or candidate.directory_identity is None
        ):
            raise BackupManifestError("backup_cleanup_changed")
        if not _same_file(partial.directory_identity, candidate.directory_identity):
            raise BackupManifestError("backup_cleanup_changed")
        members = partial.members
    else:
        assert candidate.dump_name is not None
        incomplete = _incomplete_candidate(candidate.root / candidate.dump_name, now)
        if incomplete is None:
            raise BackupManifestError("backup_cleanup_changed")
        members = incomplete.members
    if len(members) != len(candidate.members) or any(
        old.path.name != new.path.name or not _same_file(old.stat, new.stat)
        for old, new in zip(candidate.members, members, strict=True)
    ):
        raise BackupManifestError("backup_cleanup_changed")


def _capture_cleanup(
    candidate: _CleanupCandidate, *, now: datetime, assert_locked: Callable[[], None]
) -> None:
    """整组先捕获并复核，再仅在私有fd删；失败无覆盖返还，未知对象留在独立custody。

     这不是三文件原子删除。公共名可能短暂缺失，失败可能留下需人工恢复的原basename；
    0700和fd不防御恶意同UID侵入或预先持有写fd。调用方须持双锁到本同步作用域收敛。
    """
    assert_locked()
    _revalidate_cleanup(candidate, now)
    parent = candidate.root.parent if candidate.kind == "partial" else candidate.root
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    custody: Path | None = None
    custody_identity: os.stat_result | None = None
    custody_fd = -1
    placeholders: dict[str, os.stat_result] = {}
    selected = (
        (BackupFileIdentity(candidate.root, candidate.directory_identity),)
        if candidate.directory_identity is not None
        else tuple(
            sorted(
                candidate.members,
                key=lambda item: (not item.path.name.endswith(".manifest.json"), item.path.name),
            )
        )
    )
    selected_names = {member.path.name for member in selected}
    failure_seen: BaseException | None = None
    try:
        parent_identity = os.fstat(parent_fd)
        if not os.path.samestat(parent_identity, parent.lstat()):
            raise BackupManifestError("backup_cleanup_changed")
        custody = Path(tempfile.mkdtemp(prefix=".postgres-backup-custody-", dir=parent))
        custody_fd = os.open(custody, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        custody_identity = os.fstat(custody_fd)
        if not os.path.samestat(parent_identity, parent.lstat()):
            raise BackupManifestError("backup_cleanup_changed")
        try:
            for member in selected:
                name = member.path.name
                if candidate.kind == "partial":
                    os.mkdir(name, 0o700, dir_fd=custody_fd)
                    placeholders[name] = os.stat(name, dir_fd=custody_fd, follow_symlinks=False)
                else:
                    descriptor = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=custody_fd,
                    )
                    try:
                        placeholders[name] = os.fstat(descriptor)
                    finally:
                        os.close(descriptor)
                os.rename(name, name, src_dir_fd=parent_fd, dst_dir_fd=custody_fd)
                os.fsync(parent_fd)
                os.fsync(custody_fd)
                current = os.stat(name, dir_fd=custody_fd, follow_symlinks=False)
                if not _same_file(current, member.stat):
                    raise BackupManifestError("backup_cleanup_changed")
            assert_locked()
            if (
                not os.path.samestat(parent_identity, parent.lstat())
                or not os.path.samestat(custody_identity, custody.lstat())
                or set(os.listdir(custody_fd)) != selected_names
            ):
                raise BackupManifestError("backup_cleanup_changed")
            if any(os.path.lexists(parent / member.path.name) for member in selected):
                raise BackupManifestError("backup_cleanup_changed")
            if candidate.dump_name is not None and any(parent.glob(candidate.dump_name + "*")):
                raise BackupManifestError("backup_cleanup_changed")
            if candidate.kind == "complete":
                assert candidate.dump_name is not None
                _group_at(custody / candidate.dump_name)
            if candidate.kind == "partial":
                inner_path = custody / candidate.root.name
                inner_fd = os.open(inner_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    if set(os.listdir(inner_fd)) != {item.path.name for item in candidate.members}:
                        raise BackupManifestError("backup_cleanup_changed")
                    for member in candidate.members:
                        current = os.stat(member.path.name, dir_fd=inner_fd, follow_symlinks=False)
                        if not _same_file(current, member.stat) or not _old_enough(current, now):
                            raise BackupManifestError("backup_cleanup_changed")
                    for member in candidate.members:
                        # 删除也在private fd内逐次重验，未知新增文件会中止并留下原basename。
                        assert_locked()
                        current = os.stat(member.path.name, dir_fd=inner_fd, follow_symlinks=False)
                        if not _same_file(current, member.stat) or not _old_enough(current, now):
                            raise BackupManifestError("backup_cleanup_changed")
                        os.unlink(member.path.name, dir_fd=inner_fd)
                    os.fsync(inner_fd)
                finally:
                    os.close(inner_fd)
                os.rmdir(candidate.root.name, dir_fd=custody_fd)
            else:
                for member in selected:
                    assert_locked()
                    if set(os.listdir(custody_fd)) != selected_names:
                        raise BackupManifestError("backup_cleanup_changed")
                    if candidate.dump_name is not None and any(
                        parent.glob(candidate.dump_name + "*")
                    ):
                        raise BackupManifestError("backup_cleanup_changed")
                    current = os.stat(member.path.name, dir_fd=custody_fd, follow_symlinks=False)
                    if not _same_file(current, member.stat):
                        raise BackupManifestError("backup_cleanup_changed")
                    os.unlink(member.path.name, dir_fd=custody_fd)
                    selected_names.remove(member.path.name)
            os.fsync(parent_fd)
            os.fsync(custody_fd)
        except BaseException as failure:
            cleanup_failure: BaseException | None = None
            for name in os.listdir(custody_fd):
                try:
                    if name not in {member.path.name for member in selected}:
                        # 私有空间中未知内容也不能移动到public或清掉；只保留原现场。
                        raise BackupManifestError("backup_cleanup_unknown_member")
                    current = os.stat(name, dir_fd=custody_fd, follow_symlinks=False)
                    placeholder = placeholders.get(name)
                    if placeholder is not None and _same_file(current, placeholder):
                        if stat.S_ISDIR(current.st_mode):
                            os.rmdir(name, dir_fd=custody_fd)
                        else:
                            os.unlink(name, dir_fd=custody_fd)
                    else:
                        _return_without_replacement(name, custody_fd, parent_fd)
                except (OSError, BackupManifestError) as error:
                    if cleanup_failure is None:
                        cleanup_failure = error
            try:
                os.fsync(parent_fd)
                os.fsync(custody_fd)
            except OSError as error:
                if cleanup_failure is None:
                    cleanup_failure = error
            if cleanup_failure is not None:
                raise failure from cleanup_failure
            raise
    except BaseException as failure:
        failure_seen = failure
        if isinstance(failure, OSError):
            raise BackupManifestError("backup_cleanup_failed") from failure
        raise
    finally:
        try:
            if custody_fd >= 0:
                os.close(custody_fd)
            if custody is not None:
                try:
                    # 只rmdir已空且身份仍匹配的自有目录；首错优先，不把未清理伪装为成功。
                    if custody_identity is None or not os.path.samestat(
                        custody_identity, custody.lstat()
                    ):
                        raise BackupManifestError("backup_cleanup_changed")
                    custody.rmdir()
                    os.fsync(parent_fd)
                except (OSError, BackupManifestError) as cleanup_failure:
                    if failure_seen is not None:
                        raise failure_seen from cleanup_failure
                    raise BackupManifestError("backup_cleanup_failed") from cleanup_failure
        finally:
            os.close(parent_fd)


def cleanup_local_backups(
    *,
    directory: Path,
    current_run: str,
    now: datetime,
    assert_locked: Callable[[], None],
    current_dump: str | None = None,
) -> None:
    """双锁内选择7日/4周完整组及固定grace孤儿；所有custody/state/registry均不枚举。

    既有完整legacy pair和未知布局保持原样。实际删除前独立重验删除资格、整组捕获、
    身份复核；发生错误必须停止新清理，不能因为备份已发布而返回成功。
    """
    if now.tzinfo != UTC:
        raise BackupManifestError("backup_time_invalid")
    _remote_run(current_run)
    assert_locked()
    complete: dict[str, ValidatedBackupGroup] = {}
    for marker in sorted(directory.glob("*.dump.enc.manifest.json")):
        try:
            group = _group_at(Path(str(marker).removesuffix(".manifest.json")))
        except (BackupManifestError, OSError):
            continue
        complete[group.dump.name] = group
    keep = retained_backup_names(
        {
            name: datetime.fromisoformat(group.manifest.created_at)
            for name, group in complete.items()
        }
    )
    for name, group in complete.items():
        if name not in keep and name != current_dump:
            _capture_cleanup(
                _CleanupCandidate("complete", directory, group.members, name),
                now=now,
                assert_locked=assert_locked,
            )
    stems = {
        path.name.removesuffix(".sha256") for path in directory.glob("ai_employee-*.dump.enc*")
    }
    for stem in sorted(stems):
        if stem == current_dump:
            continue
        candidate = _incomplete_candidate(directory / stem, now)
        if candidate is not None:
            _capture_cleanup(candidate, now=now, assert_locked=assert_locked)
    for path in sorted(directory.glob(".partial.*")):
        if path.name == f".partial.{current_run}":
            continue
        candidate = _partial_candidate(path, now)
        if candidate is not None:
            _capture_cleanup(candidate, now=now, assert_locked=assert_locked)
