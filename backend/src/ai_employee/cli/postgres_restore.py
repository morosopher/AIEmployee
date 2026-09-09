"""镜像内generic/sealed恢复组合根；纯文件/镜像验证先于一切owner Secret和连接。

两种入口各自验证输入，仅共用已有maintenance holder与受监督SQL stream。state-v4
只是可在另一主机重建的内容无关projection，不为重试、ordinal或reopen提供authority。
所有公开输出是固定结果码或host guard握手，不输出驱动/openssl原始诊断。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.cli.database_maintenance import (
    _load_database_endpoint,
    _read_secret_file,
    run_restore_maintenance,
)
from ai_employee.cli.verify_restored_backup import (
    SUPPORTED_RESTORE_REVISIONS,
    read_restore_fingerprint,
    verify_restored_backup,
)
from ai_employee.infrastructure.db.database_maintenance import (
    DatabaseRestoreFacts,
    RestoreClaim,
    RestoreFingerprint,
    _parse_call_authority,
    _validate_active_authority,
    _validate_completed_authority,
)
from ai_employee.infrastructure.db.postgres_backup_manifest import (
    ValidatedBackupGroup,
    image_executable_digests,
    validate_backup_group,
    validate_generic_backup_group,
)

RESTORE_STATE_CONTAINER_PATH = Path("/var/lib/ai-employee/restore-state")
_STATE_SCHEMA = "ai_employee.postgres_restore_state.v4"
_ALREADY_SCHEMA = "ai_employee.postgres_restore_already_applied.v1"


class RestoreInputError(RuntimeError):
    """输入/state/host握手不满足闭合协议；消息只有稳定错误码。"""


def _canonical(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")


def _check_directory(path: Path) -> None:
    """不解析symlink来掩盖路径：逐级拒绝链接，并验证专用末级目录0700和精确owner。"""
    if not path.is_absolute() or ".." in path.parts:
        raise RestoreInputError("restore_state_directory_invalid")
    for parent in (path, *path.parents):
        if parent.is_symlink() or not parent.is_dir():
            raise RestoreInputError("restore_state_directory_invalid")
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RestoreInputError("restore_state_directory_invalid")


def _safe_read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 16384
        ):
            raise RestoreInputError("restore_state_file_invalid")
        data = os.read(descriptor, 16385)
        if len(data) != info.st_size or not os.path.samestat(info, path.lstat()):
            raise RestoreInputError("restore_state_file_invalid")
        return data
    finally:
        os.close(descriptor)


def _atomic_state(path: Path, data: bytes, *, immutable: bool) -> None:
    """0600同目录temp→fsync→rename/link→目录fsync；不可变证据只接受已有精确字节。"""
    _check_directory(path.parent)
    if os.path.lexists(path):
        existing = _safe_read(path)
        if existing == data:
            return
        if immutable:
            raise RestoreInputError("restore_state_archive_conflict")
        try:
            raw: object = json.loads(existing)
            if isinstance(raw, dict) and raw.get("result_code") == "restore_completed":
                raise RestoreInputError("restore_state_archive_conflict")
        except (ValueError, UnicodeError):
            # 腐坏/陈旧投影可由真实catalog重建；只有已完成归档禁止被覆盖。
            pass
    descriptor, temporary = tempfile.mkstemp(prefix=".projection-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                if _safe_read(path) != data:
                    raise RestoreInputError("restore_state_archive_conflict") from None
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class RestoreStateStore:
    """在独立0700目录保存0600状态；不读取projection来判定下一步或建立锁。"""

    def __init__(self, directory: Path, read_only_roots: tuple[Path, ...]) -> None:
        """验证state绝对路径/owner/mode/no-symlink与全部只读输入树双向不重叠。"""
        _check_directory(directory)
        for source in read_only_roots:
            resolved = source.resolve(strict=True)
            if (
                directory == resolved
                or directory.is_relative_to(resolved)
                or resolved.is_relative_to(directory)
            ):
                raise RestoreInputError("restore_state_source_overlap")
        self.directory = directory

    def _projection(self, facts: DatabaseRestoreFacts, result: str) -> tuple[Path, bytes]:
        """只从完整canonical catalog值派生文件名和内容，拒绝不绑定的残缺tuple。"""
        if facts.restore_call_authority is None or re.fullmatch(r"[a-z_]{1,64}", result) is None:
            raise RestoreInputError("restore_state_facts_invalid")
        call = _parse_call_authority(facts.restore_call_authority)
        if facts.maintenance_gate is not None:
            _validate_active_authority(facts.maintenance_gate, facts.restore_call_authority)
            if facts.restore_completion is not None:
                raise RestoreInputError("restore_state_facts_invalid")
        elif facts.restore_completion is not None:
            _validate_completed_authority(facts.restore_call_authority, facts.restore_completion)
        else:
            raise RestoreInputError("restore_state_facts_invalid")
        parent = self.directory / call.target_digest
        parent.mkdir(mode=0o700, exist_ok=True)
        _check_directory(parent)
        content = _canonical(
            {
                "schema": _STATE_SCHEMA,
                "target_identity_digest_v1": call.target_digest,
                "attempt_id": call.attempt_uuid,
                "kind": call.kind,
                "source_binding_digest_v1": call.source_digest,
                "maintenance_gate": facts.maintenance_gate,
                "restore_call_authority": facts.restore_call_authority,
                "restore_completion": facts.restore_completion,
                "result_code": result,
            }
        )
        return parent / f"{call.attempt_uuid}.json", content

    def publish(self, facts: DatabaseRestoreFacts, result: str) -> None:
        """原子更新非终态projection；成功completed文件之后只允许字节相同的幂等读取。"""
        path, data = self._projection(facts, result)
        _atomic_state(path, data, immutable=False)

    def archive(self, facts: DatabaseRestoreFacts) -> str:
        """先从catalog重建完成投影，再把该精确字节作为不可覆盖的旧pair归档。"""
        if (
            facts.maintenance_gate is not None
            or facts.restore_completion is None
            or facts.restore_call_authority is None
        ):
            raise RestoreInputError("restore_state_archive_invalid")
        _validate_completed_authority(facts.restore_call_authority, facts.restore_completion)
        self.publish(facts, "restore_completed")
        path, data = self._projection(facts, "restore_completed")
        _atomic_state(path, data, immutable=True)
        return hashlib.sha256(data).hexdigest()

    def verify_archive(self, facts: DatabaseRestoreFacts, digest: str) -> None:
        """在新gate事务内逐字重新核对归档，不把文件摘要提升为数据库authority。"""
        path, data = self._projection(facts, "restore_completed")
        if _safe_read(path) != data or hashlib.sha256(data).hexdigest() != digest:
            raise RestoreInputError("restore_state_archive_conflict")

    def already_applied(
        self, target: str, claim: RestoreClaim, observed: RestoreFingerprint
    ) -> None:
        """按target/kind/source无冲突发布确定性零写证据，没有时间戳或随机attempt。"""
        if re.fullmatch(r"[0-9a-f]{64}", target) is None or observed != claim.expected:
            raise RestoreInputError("restore_already_applied_invalid")
        path = self.directory / f"{target}.{claim.kind}.{claim.source_digest}.already-applied.json"
        data = _canonical(
            {
                "schema": _ALREADY_SCHEMA,
                "target_identity_digest_v1": target,
                "kind": claim.kind,
                "source_binding_digest_v1": claim.source_digest,
                "manifest_sha256": claim.source_digest,
                "expected_revision": claim.expected.revision,
                "expected_restore_fingerprint_v1": claim.expected.digest,
                "observed_revision": observed.revision,
                "observed_restore_fingerprint_v1": observed.digest,
                "result_code": "restore_already_applied",
            }
        )
        _atomic_state(path, data, immutable=True)


def validate_restore_input(
    kind: Literal["generic", "sealed_0018"], dump: Path
) -> ValidatedBackupGroup:
    """generic纯三件套验证；sealed额外输入在它自己的validator内完成，随后才允许Secret。"""
    image_id = os.environ.get("OPERATIONS_IMMUTABLE_IMAGE_ID", "")
    if kind == "generic":
        return validate_generic_backup_group(
            dump, current_image_id=image_id, executable_digests=image_executable_digests()
        )
    if kind != "sealed_0018":
        raise RestoreInputError("restore_kind_invalid")
    group = validate_backup_group(
        dump,
        supported_revisions=SUPPORTED_RESTORE_REVISIONS,
        expected_image_id=image_id,
        expected_executable_digests=image_executable_digests(),
    )
    if kind == "sealed_0018":
        from ai_employee.cli.calendar_aad_verify_restored_0018 import validate_sealed_restore_input

        validate_sealed_restore_input(group)
    return group


def _host_guard() -> None:
    """只从本调用匿名stdin接收宿主重查后的确认；EOF/额外字节均在下一步写入前拒绝。

    宿主有Docker可见性，owner容器不挂Docker socket。此握手仅补足服务/开关race检查，
    不替代数据库catalog/lease authority，用户env或state文件不能跳过它。
    """
    print("operations.guard.request", flush=True)
    if sys.stdin.readline(128) != "operations.guard.confirmed\n":
        raise RestoreInputError("restore_host_guard_failed")


def _execute(
    kind: Literal["generic", "sealed_0018"],
    dump: Path,
    *,
    approve_call: int | None,
    approve_reopen: int | None,
) -> str:
    group = validate_restore_input(kind, dump)
    if not os.statvfs(group.dump.parent).f_flag & os.ST_RDONLY:
        raise RestoreInputError("restore_backup_mount_not_readonly")
    store = RestoreStateStore(RESTORE_STATE_CONTAINER_PATH, (group.dump.parent,))
    _host_guard()
    if os.environ.get("APP_ENV") not in {"development", "test", "production"}:
        raise RestoreInputError("restore_environment_invalid")
    if (
        os.environ.get("APP_ENV") == "production"
        and os.environ.get("ALLOW_PRODUCTION_RESTORE") != "yes"
    ):
        raise RestoreInputError("restore_production_confirmation_required")
    if any(
        os.environ.get(key, "false") != "false"
        for key in ("EXTERNAL_WRITES_ENABLED", "GOOGLE_WRITES_ENABLED", "MICROSOFT_WRITES_ENABLED")
    ):
        raise RestoreInputError("restore_write_switch_enabled")
    endpoint = _load_database_endpoint()
    if endpoint.owner_role != "ai_employee_owner":
        raise RestoreInputError("restore_owner_invalid")
    owner_file = Path("/run/secrets/postgres_bootstrap_password")
    passphrase_file = Path(
        os.environ.get("BACKUP_PASSPHRASE_FILE", "/run/secrets/backup_passphrase")
    )
    _read_secret_file(passphrase_file)
    owner_password = _read_secret_file(owner_file)
    claim = RestoreClaim(
        kind,
        group.manifest_sha256,
        RestoreFingerprint(group.manifest.alembic_revision, group.manifest.restore_fingerprint_v1),
    )
    with tempfile.TemporaryDirectory(prefix="postgres-restore-") as directory:
        plain = Path(directory) / "restore.dump"
        result = subprocess.run(
            [
                "openssl",
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-pass",
                f"file:{passphrase_file}",
                "-in",
                str(group.dump),
                "-out",
                str(plain),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise RestoreInputError("restore_decryption_failed")
        plain.chmod(0o600)
        # 同一受控endpoint注入psql环境；SQL generator的实现另行丢弃全部PG*/Secret。
        environment = {
            "PATH": os.environ["PATH"],
            "PGHOST": endpoint.host,
            "PGPORT": str(endpoint.port),
            "PGDATABASE": endpoint.database_name,
            "PGUSER": endpoint.owner_role,
            "PGPASSWORD": owner_password.get_secret_value(),
        }
        verifier: Callable[[Connection, RestoreFingerprint], None] = verify_restored_backup
        if kind == "sealed_0018":
            from ai_employee.cli.calendar_aad_verify_restored_0018 import sealed_restore_verifier

            verifier = sealed_restore_verifier(group)
        return run_restore_maintenance(
            endpoint=endpoint,
            owner_password_file=owner_file,
            claim=claim,
            dump=plain,
            reader=read_restore_fingerprint,
            verifier=verifier,
            evidence=store,
            consumer_environment=environment,
            host_guard=_host_guard,
            approve_call_ordinal=approve_call,
            approve_reopen_ordinal=approve_reopen,
        )


def parse_restore_ordinal_options(arguments: Sequence[str]) -> tuple[int | None, int | None]:
    """host/容器共用闭合argv语法；只允许一项canonical ASCII正整数，不从环境读取批准。"""
    if not arguments:
        return None, None
    if (
        len(arguments) != 2
        or arguments[0] not in {"--approve-call-ordinal", "--approve-reopen-ordinal"}
        or re.fullmatch(r"[1-9][0-9]{0,5}", arguments[1]) is None
    ):
        raise RestoreInputError("restore_ordinal_invalid")
    value = int(arguments[1])
    return (value, None) if arguments[0] == "--approve-call-ordinal" else (None, value)


def main(arguments: Sequence[str] | None = None) -> int:
    """固定validate/execute入口；ordinal批准仅允许新的显式正整数，不提供repair/skip选项。"""
    try:
        args = list(arguments) if arguments is not None else sys.argv[1:]
        if (
            len(args) < 3
            or args[0] not in {"validate", "execute"}
            or args[1] not in {"generic", "sealed_0018"}
        ):
            raise RestoreInputError("restore_arguments_invalid")
        approve_call, approve_reopen = parse_restore_ordinal_options(args[3:])
        kind = cast(Literal["generic", "sealed_0018"], args[1])
        if args[0] == "validate":
            if approve_call is not None or approve_reopen is not None:
                raise RestoreInputError("restore_validation_arguments_invalid")
            validate_restore_input(kind, Path(args[2]))
            result = "restore_input_valid"
        else:
            result = _execute(
                kind,
                Path(args[2]),
                approve_call=approve_call,
                approve_reopen=approve_reopen,
            )
    except (OSError, ValueError, SQLAlchemyError, RuntimeError):
        print("restore_failed", file=sys.stderr)
        return 1
    print(result, flush=True)
    return (
        0
        if result in {"restore_input_valid", "restore_completed", "restore_already_applied"}
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
