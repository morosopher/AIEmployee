"""sealed窗口三个只读审计阶段：数据库内hash、精确pair比较和不可覆盖0600证据。

不解密v1、不访问供应商、不写数据库。所有正文/原始scope/credential先在PostgreSQL内
摘要化，文件只保留计数、局部标识摘要和比较结果；当前期限仍由真实typed guard决定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.application.calendar_aad_digests import canonical_utc
from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadArtifact,
    CalendarAadBinding,
    CalendarAadRevision,
    CalendarAadRolloutError,
    parse_rollout_artifact,
    serialize_rollout_artifact,
    verify_rollout,
)
from ai_employee.cli.database_maintenance import (
    CalendarAadOwnerFactsReader,
    DatabaseEndpoint,
    MigrateArguments,
    _load_database_endpoint,
    _open_maintenance_context,
    _read_secret_file,
)
from ai_employee.cli.verify_restored_backup import read_restore_fingerprint
from ai_employee.infrastructure.calendar_aad_publication import discard_calendar_aad_publication
from ai_employee.infrastructure.calendar_aad_resources import await_calendar_aad_resource
from ai_employee.infrastructure.db.database_maintenance import ReadOnlyMaintenanceHolder
from ai_employee.infrastructure.db.postgres_backup_manifest import (
    ValidatedBackupGroup,
    _regular,
    _unique_object,
    image_executable_digests,
    validate_backup_group,
)
from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
    CalendarAadArtifactFile,
    CalendarAadCurrentGuard,
    SqlAlchemyCalendarAadPreflightRepository,
    async_calendar_aad_rollout_lease,
    read_calendar_aad_facts,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory

AUDIT_PHASES = ("pre-migration", "post-migration", "post-resync")
type AuditPhase = Literal["pre-migration", "post-migration", "post-resync"]
type Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class _ClosedModel(BaseModel):
    """审计公开边界只接受固定字段和严格类型，不把额外内容写进artifact。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CalendarAuditEvent(_ClosedModel):
    """一行事件的无内容比较证据；版本字段不参与旧ciphertext的物理摘要。"""

    identity_digest: Digest
    non_aad_digest: Digest
    description_digest: Digest | None
    location_digest: Digest | None
    description_version: Literal[1, 2] | None
    location_version: Literal[1, 2] | None

    @field_validator("description_version", "location_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        """Literal按相等比较会接受True/1.0；先固定JSON类型再判断允许的两个版本值。"""
        if value is not None and type(value) is not int:
            raise ValueError("audit version type invalid")
        return value


class CalendarAuditCursor(_ClosedModel):
    """一个游标的身份/不可变列摘要及受控恢复状态，绝不输出原cursor或scope。"""

    identity_digest: Digest
    pair_digest: Digest | None
    immutable_digest: Digest
    cursor_digest: Digest | None
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    error_digest: Digest | None
    marked: bool


class CalendarAuditSnapshot(_ClosedModel):
    """同一RR/RO快照中全体事件、游标、目录及credential的无内容投影。"""

    events: tuple[CalendarAuditEvent, ...]
    cursors: tuple[CalendarAuditCursor, ...]
    directory_digest: Digest
    credential_digest: Digest


class CalendarAadAuditArtifact(_ClosedModel):
    """绑定原preflight和加密备份manifest的canonical三阶段证据，无操作者截止线。"""

    schema_version: Literal["calendar_aad_0019_audit.v1"] = "calendar_aad_0019_audit.v1"
    phase: AuditPhase
    revision: CalendarAadRevision
    rollout_digest_v1: Digest
    backup_artifact_basename: str
    immutable_image_id: str
    manifest_sha256: Digest
    preflight_sha256: Digest
    effective_deadline: datetime | None
    affected_pair_count: int = Field(ge=0)
    affected_connection_count: int = Field(ge=0)
    pair_digests: tuple[Digest, ...]
    connection_digests: tuple[Digest, ...]
    remaining_historical_v1_fields: int = Field(ge=0)
    snapshot: CalendarAuditSnapshot

    @model_validator(mode="after")
    def check_closed_facts(self) -> Self:
        """计数、phase/revision和每行唯一摘要必须自洽，不能靠artifact伪造完成。"""
        CalendarAadBinding(self.backup_artifact_basename, self.immutable_image_id)
        if self.revision != ("20260809_0018" if self.phase == "pre-migration" else "20260809_0019"):
            raise ValueError("audit revision mismatch")
        if self.effective_deadline is not None:
            canonical_utc(self.effective_deadline)
        for count, values in (
            (self.affected_pair_count, self.pair_digests),
            (self.affected_connection_count, self.connection_digests),
        ):
            if count != len(values) or tuple(sorted(set(values))) != values:
                raise ValueError("audit set mismatch")
        for rows in (self.snapshot.events, self.snapshot.cursors):
            identities = tuple(item.identity_digest for item in rows)
            if identities != tuple(sorted(set(identities))):
                raise ValueError("audit identity mismatch")
        count = sum(
            version == 1
            for event in self.snapshot.events
            for version in (event.description_version, event.location_version)
        )
        if count != self.remaining_historical_v1_fields:
            raise ValueError("audit version count mismatch")
        return self


def serialize_audit_artifact(artifact: CalendarAadAuditArtifact) -> bytes:
    """固定ASCII键序/紧凑格式，完整字节参与相邻sealed输入绑定。"""
    return (
        json.dumps(
            artifact.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def parse_audit_artifact(
    raw: bytes,
    *,
    phase: AuditPhase,
    group: ValidatedBackupGroup,
    preflight: CalendarAadArtifact,
    preflight_sha256: str,
) -> CalendarAadAuditArtifact:
    """纯文件闭合schema与精确preflight/manifest/image绑定，在owner Secret之前运行。"""
    try:
        json.loads(raw, object_pairs_hook=_unique_object)
        artifact = CalendarAadAuditArtifact.model_validate_json(raw)
        if serialize_audit_artifact(artifact) != raw or (
            artifact.phase != phase
            or artifact.manifest_sha256 != group.manifest_sha256
            or artifact.preflight_sha256 != preflight_sha256
            or artifact.immutable_image_id != group.manifest.immutable_image_id
            or artifact.backup_artifact_basename != preflight.backup_artifact_basename
            or artifact.rollout_digest_v1 != preflight.rollout_digest_v1
            or artifact.pair_digests != preflight.pair_digests
            or artifact.connection_digests != preflight.connection_digests
        ):
            raise ValueError("audit binding invalid")
        if artifact.effective_deadline is not None and (
            preflight.rollout_deadline is None
            or artifact.effective_deadline > preflight.rollout_deadline
        ):
            raise ValueError("audit deadline invalid")
        if (artifact.effective_deadline is None) != (preflight.rollout_deadline is None):
            raise ValueError("audit deadline invalid")
        return artifact
    except (ValueError, TypeError, ValidationError):
        raise CalendarAadRolloutError("calendar_aad_audit_artifact_invalid") from None


def load_preflight(group: ValidatedBackupGroup) -> tuple[CalendarAadArtifact, str]:
    """独立加载canonical原preflight，suffix固定且不允许另一个窗口的artifact。"""
    binding = CalendarAadBinding(
        group.dump.name.removesuffix(".dump.enc"), group.manifest.immutable_image_id
    )
    path = group.dump.parent / f"{binding.basename}.calendar-aad-preflight.json"
    raw, digest, _ = _regular(path, limit=1_048_576)
    artifact = parse_rollout_artifact(raw, binding)
    if serialize_rollout_artifact(artifact) != raw:
        raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
    return artifact, digest


def load_audit(group: ValidatedBackupGroup, phase: AuditPhase) -> CalendarAadAuditArtifact:
    """读取某个固定阶段的原始证据，始终重新读取原preflight和manifest绑定。"""
    preflight, digest = load_preflight(group)
    path = group.dump.parent / f"{preflight.backup_artifact_basename}.calendar-aad-{phase}.json"
    raw, _, _ = _regular(path, limit=8_388_608)
    return parse_audit_artifact(
        raw, phase=phase, group=group, preflight=preflight, preflight_sha256=digest
    )


def _hash(expression: str) -> str:
    """expression只能由下面固定SQL模板提供；raw值从不返回Python。"""
    return f"encode(sha256(convert_to(({expression})::text,'UTF8')),'hex')"


def _row_digest(connection: Connection, table: str) -> str:
    """固定目录/credential两表的全行hash聚合；未知表参数拒绝。"""
    if table not in {"provider_calendars", "encrypted_credentials"}:
        raise CalendarAadRolloutError("calendar_aad_audit_failed")
    aggregate = _hash("COALESCE(string_agg(h,'' ORDER BY h COLLATE \"C\"),'')")
    value = connection.scalar(
        text(
            f"SELECT {aggregate} FROM "
            f"(SELECT {_hash('to_jsonb(r)')} AS h FROM public.{table} r) hashes"
        )
    )
    if type(value) is not str:
        raise CalendarAadRolloutError("calendar_aad_audit_failed")
    return value


def read_audit_snapshot(
    connection: Connection, *, revision: CalendarAadRevision
) -> CalendarAuditSnapshot:
    """同一只读事务扫描全部事件/游标，原始内容在数据库内完成摘要后才出边界。"""
    if connection.scalar(text("SHOW transaction_read_only")) != "on":
        raise CalendarAadRolloutError("calendar_aad_audit_readonly_required")
    connection.execute(text("SET LOCAL timezone='UTC'"))
    connection.execute(text("SET LOCAL bytea_output='hex'"))
    connection.execute(text("SET LOCAL DateStyle='ISO, YMD'"))
    connection.execute(text("SET LOCAL IntervalStyle='postgres'"))
    connection.execute(text("SET LOCAL extra_float_digits=3"))
    versions = (
        "description_aad_version AS description_version,location_aad_version AS location_version"
        if revision == "20260809_0019"
        else "NULL::integer AS description_version,NULL::integer AS location_version"
    )
    excluded = "ARRAY['description_ciphertext','description_nonce','description_key_version','location_ciphertext','location_nonce','location_key_version','description_aad_version','location_aad_version']::text[]"
    fields = [
        f"CASE WHEN {field}_ciphertext IS NULL THEN NULL ELSE "
        f"{_hash(f'jsonb_build_array({field}_ciphertext,{field}_nonce,{field}_key_version)')} END AS {field}_digest"
        for field in ("description", "location")
    ]
    events = tuple(
        CalendarAuditEvent.model_validate(dict(row))
        for row in connection.execute(
            text(
                f"SELECT {_hash('jsonb_build_array(id,user_id,connection_id,calendar_id,provider_event_id)')} AS identity_digest,"
                f"{_hash('to_jsonb(e)-' + excluded)} AS non_aad_digest,{','.join(fields)},{versions} "
                "FROM public.calendar_events e ORDER BY identity_digest"
            )
        ).mappings()
    )
    pair = "encode(sha256(convert_to('20260809_0019','UTF8') || decode('00','hex') || convert_to(connection_id::text,'UTF8') || decode('00','hex') || convert_to(scope_key,'UTF8')),'hex')"
    # 正常恢复同时写last_attempt/last_success；migration则必须保持last_attempt完全不变。
    immutable = _hash(
        "to_jsonb(c)-ARRAY['cursor','last_success_at','last_attempt_at','last_error_code']::text[]"
    )
    cursors = tuple(
        CalendarAuditCursor.model_validate(dict(row))
        for row in connection.execute(
            text(
                f"SELECT {_hash('id')} AS identity_digest,"
                f"CASE WHEN resource_kind='calendar' AND scope_key<>'directory' THEN {pair} ELSE NULL END AS pair_digest,"
                f"{immutable} AS immutable_digest,"
                f"CASE WHEN cursor IS NULL THEN NULL ELSE {_hash('cursor')} END AS cursor_digest,last_success_at,last_attempt_at,"
                f"CASE WHEN last_error_code IS NULL THEN NULL ELSE {_hash('last_error_code')} END AS error_digest,"
                "COALESCE(last_error_code='calendar_event_resync_required',false) AS marked "
                "FROM public.sync_cursors c ORDER BY identity_digest"
            )
        ).mappings()
    )
    return CalendarAuditSnapshot(
        events=events,
        cursors=cursors,
        directory_digest=_row_digest(connection, "provider_calendars"),
        credential_digest=_row_digest(connection, "encrypted_credentials"),
    )


def _compare(
    phase: AuditPhase,
    current: CalendarAuditSnapshot,
    previous: CalendarAadAuditArtifact | None,
    preflight: CalendarAadArtifact,
) -> None:
    """按阶段检查精确变化；post-resync允许未返回的旧v1，任何新写字段必须v2。"""
    if phase == "pre-migration":
        return
    if previous is None or previous.phase != (
        "pre-migration" if phase == "post-migration" else "post-migration"
    ):
        raise CalendarAadRolloutError("calendar_aad_audit_predecessor_required")
    old = previous.snapshot
    if current.directory_digest != old.directory_digest:
        raise CalendarAadRolloutError("calendar_aad_audit_directory_changed")
    old_events = {item.identity_digest: item for item in old.events}
    if phase == "post-migration":
        if current.credential_digest != old.credential_digest or set(old_events) != {
            item.identity_digest for item in current.events
        }:
            raise CalendarAadRolloutError("calendar_aad_audit_rows_changed")
        for event in current.events:
            before = old_events[event.identity_digest]
            if (
                event.non_aad_digest != before.non_aad_digest
                or event.description_digest != before.description_digest
                or event.location_digest != before.location_digest
            ):
                raise CalendarAadRolloutError("calendar_aad_audit_ciphertext_changed")
            for field in ("description", "location"):
                if getattr(event, field + "_version") != (
                    1 if getattr(event, field + "_digest") is not None else None
                ):
                    raise CalendarAadRolloutError("calendar_aad_audit_version_invalid")
    else:
        for event in current.events:
            before_optional = old_events.get(event.identity_digest)
            for field in ("description", "location"):
                digest, version = (
                    getattr(event, field + "_digest"),
                    getattr(event, field + "_version"),
                )
                old_digest = (
                    getattr(before_optional, field + "_digest")
                    if before_optional is not None
                    else None
                )
                if digest is not None and (
                    version not in {1, 2} or (digest != old_digest and version != 2)
                ):
                    raise CalendarAadRolloutError("calendar_aad_audit_new_v1")
                if digest is None and version is not None:
                    raise CalendarAadRolloutError("calendar_aad_audit_version_invalid")
    before_cursors = {item.identity_digest: item for item in old.cursors}
    if set(before_cursors) != {item.identity_digest for item in current.cursors}:
        raise CalendarAadRolloutError("calendar_aad_audit_cursor_changed")
    affected = set(preflight.pair_digests)
    seen = set()
    for cursor in current.cursors:
        before_cursor = before_cursors[cursor.identity_digest]
        if cursor.pair_digest in affected:
            seen.add(cursor.pair_digest)
            if (
                cursor.pair_digest != before_cursor.pair_digest
                or cursor.immutable_digest != before_cursor.immutable_digest
            ):
                raise CalendarAadRolloutError("calendar_aad_audit_cursor_changed")
            if phase == "post-migration":
                if (
                    cursor.cursor_digest is not None
                    or cursor.last_success_at is not None
                    or cursor.last_attempt_at != before_cursor.last_attempt_at
                    or not cursor.marked
                ):
                    raise CalendarAadRolloutError("calendar_aad_audit_marker_invalid")
            elif (
                cursor.cursor_digest is None
                or cursor.last_success_at is None
                or cursor.last_attempt_at != cursor.last_success_at
                or cursor.marked
                or cursor.error_digest is not None
            ):
                raise CalendarAadRolloutError("calendar_aad_audit_recovery_incomplete")
        elif cursor != before_cursor:
            raise CalendarAadRolloutError("calendar_aad_audit_unaffected_cursor_changed")
    if seen != affected:
        raise CalendarAadRolloutError("calendar_aad_audit_marker_invalid")


def audit_on_connection(
    connection: Connection,
    *,
    phase: AuditPhase,
    group: ValidatedBackupGroup,
    preflight: CalendarAadArtifact,
    preflight_sha256: str,
    previous: CalendarAadAuditArtifact | None,
    now: datetime,
) -> CalendarAadAuditArtifact:
    """从当前同一RR/RO事务完成版本/事实/deadline和阶段比较，不给caller传入版本授权。"""
    revision: CalendarAadRevision = "20260809_0018" if phase == "pre-migration" else "20260809_0019"
    facts = read_calendar_aad_facts(connection, expected_revision=revision)
    deadline = verify_rollout(preflight, facts, now=now)
    snapshot = read_audit_snapshot(connection, revision=revision)
    if phase == "pre-migration":
        fingerprint = read_restore_fingerprint(connection)
        if (
            fingerprint.revision != group.manifest.alembic_revision
            or fingerprint.digest != group.manifest.restore_fingerprint_v1
        ):
            raise CalendarAadRolloutError("calendar_aad_audit_backup_changed")
    _compare(phase, snapshot, previous, preflight)
    return CalendarAadAuditArtifact(
        phase=phase,
        revision=revision,
        rollout_digest_v1=preflight.rollout_digest_v1,
        backup_artifact_basename=preflight.backup_artifact_basename,
        immutable_image_id=preflight.immutable_image_id,
        manifest_sha256=group.manifest_sha256,
        preflight_sha256=preflight_sha256,
        effective_deadline=deadline,
        affected_pair_count=len(facts.pair_digests),
        affected_connection_count=len(facts.connection_digests),
        pair_digests=facts.pair_digests,
        connection_digests=facts.connection_digests,
        remaining_historical_v1_fields=sum(
            version == 1
            for event in snapshot.events
            for version in (event.description_version, event.location_version)
        ),
        snapshot=snapshot,
    )


@dataclass(frozen=True)
class _StagedAudit:
    """调用方独占的0600临时文件；身份用于取消、期限变化及最终lease失败后的窄补偿。"""

    path: Path
    identity: os.stat_result


def _stage(path: Path, data: bytes) -> _StagedAudit:
    """只写入并fsync临时文件；耗时I/O完成后仍必须重新取得当前guard才可发布。"""
    descriptor, temporary = tempfile.mkstemp(prefix=".calendar-aad-audit-", dir=path.parent)
    identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            try:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                # 写入失败也以仍打开的自有fd记录最后版本；绝不按公开路径猜测所有权。
                identity = os.fstat(stream.fileno())
        return _StagedAudit(Path(temporary), identity)
    except BaseException as failure:
        try:
            discard_calendar_aad_publication(Path(temporary), identity)
        except BaseException as cleanup_failure:
            raise failure from cleanup_failure
        raise


def _publish(path: Path, staged: _StagedAudit, data: bytes) -> os.stat_result | None:
    """guard之后只链接已fsync的文件；已有相同artifact幂等，失败只补偿精确身份。"""
    try:
        raw, _, identity = _regular(staged.path, limit=8_388_608)
        if raw != data or not os.path.samestat(identity, staged.identity):
            raise CalendarAadRolloutError("calendar_aad_audit_staging_changed")
        try:
            os.link(staged.path, path, follow_symlinks=False)
        except FileExistsError:
            raw, _, _ = _regular(path, limit=8_388_608)
            if raw != data:
                raise CalendarAadRolloutError("calendar_aad_audit_collision") from None
            return None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return identity
    except BaseException as failure:
        try:
            discard_calendar_aad_publication(path, staged.identity)
        except BaseException as cleanup_failure:
            raise failure from cleanup_failure
        raise


async def run_audit(
    *,
    phase: AuditPhase,
    group: ValidatedBackupGroup,
    holder: ReadOnlyMaintenanceHolder,
    sessions: ManagedAsyncSessionMaker,
    facts_reader: CalendarAadOwnerFactsReader,
    clock: Callable[[], datetime],
    host_guard: Callable[[], None] | None = None,
) -> CalendarAadAuditArtifact:
    """borrow既有owner holder；原revision lease包围读证据与最终文件提交，不创建provider。"""
    preflight, digest = load_preflight(group)
    binding = CalendarAadBinding(preflight.backup_artifact_basename, preflight.immutable_image_id)
    path = group.dump.parent / f"{binding.basename}.calendar-aad-{phase}.json"
    staging: asyncio.Task[_StagedAudit] | None = None
    publication: asyncio.Task[os.stat_result | None] | None = None
    staging_cleanup_started = False
    publication_cleanup_started = False

    async def clear_staging() -> None:
        """即使外层重复取消也收取线程结果，只清理本次确实创建的临时文件。"""
        nonlocal staging_cleanup_started
        if (
            not staging_cleanup_started
            and staging is not None
            and not staging.cancelled()
            and staging.exception() is None
        ):
            staging_cleanup_started = True
            staged = staging.result()
            await await_calendar_aad_resource(
                asyncio.create_task(
                    asyncio.to_thread(
                        discard_calendar_aad_publication, staged.path, staged.identity
                    )
                )
            )

    async def clear_publication() -> None:
        """只消费一次本次发布receipt；已有相同文件没有receipt，失败也不盲重试补偿。"""
        nonlocal publication_cleanup_started
        if (
            not publication_cleanup_started
            and publication is not None
            and not publication.cancelled()
            and publication.exception() is None
            and publication.result() is not None
        ):
            publication_cleanup_started = True
            identity = publication.result()
            assert identity is not None
            await await_calendar_aad_resource(
                asyncio.create_task(
                    asyncio.to_thread(discard_calendar_aad_publication, path, identity)
                )
            )

    async def compensate() -> None:
        """先撤回最终标志再收尾临时文件；两个自有工作均收至终态，保留首个清理失败。"""
        failure: BaseException | None = None
        for cleanup in (clear_publication, clear_staging):
            try:
                await cleanup()
            except BaseException as error:  # noqa: BLE001 - 先排空所有自有资源，再重抛首个失败或取消。
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure

    try:
        async with async_calendar_aad_rollout_lease(sessions.engine.url) as lease:
            guard = CalendarAadCurrentGuard(
                repository=SqlAlchemyCalendarAadPreflightRepository(
                    sessions, facts_reader=facts_reader
                ),
                artifact_file=CalendarAadArtifactFile(group.dump.parent, binding),
                artifact=preflight,
                lease=lease,
                clock=clock,
                expected_revision="20260809_0018" if phase == "pre-migration" else "20260809_0019",
            )

            async def verify_current() -> datetime | None:
                """先完成宿主停服/镜像复查，再读取当前数据库和期限；回调不提供DB授权。"""
                if host_guard is not None:
                    await await_calendar_aad_resource(
                        asyncio.create_task(asyncio.to_thread(host_guard))
                    )
                return await guard.verify()

            await verify_current()
            previous = (
                None
                if phase == "pre-migration"
                else load_audit(
                    group, "pre-migration" if phase == "post-migration" else "post-migration"
                )
            )
            artifact = await await_calendar_aad_resource(
                asyncio.create_task(
                    asyncio.to_thread(
                        holder.read_only,
                        lambda connection: audit_on_connection(
                            connection,
                            phase=phase,
                            group=group,
                            preflight=preflight,
                            preflight_sha256=digest,
                            previous=previous,
                            now=clock(),
                        ),
                    )
                )
            )
            raw = serialize_audit_artifact(artifact)
            try:
                staging = asyncio.create_task(asyncio.to_thread(_stage, path, raw))
                staged = await await_calendar_aad_resource(staging)
                if await verify_current() != artifact.effective_deadline:
                    raise CalendarAadRolloutError("calendar_aad_audit_deadline_changed")
                publication = asyncio.create_task(asyncio.to_thread(_publish, path, staged, raw))
                await await_calendar_aad_resource(publication)
                await verify_current()
                await clear_staging()
                await verify_current()
            except BaseException as failure:
                try:
                    # guard失败或取消时revision lease仍在；不能先释放lease再撤回刚发布的audit。
                    await compensate()
                except BaseException as cleanup_failure:
                    raise failure from cleanup_failure
                raise
    except BaseException as failure:
        try:
            # lease退出本身失败时，只能在外层owner维护锁仍持有期间按同一receipt窄补偿。
            await compensate()
        except BaseException as cleanup_failure:
            raise failure from cleanup_failure
        raise
    return artifact


def main(arguments: Sequence[str] | None = None) -> int:
    """只接受三个phase和固定backup前缀；纯artifact/image验证后才读固定owner Secret。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=AUDIT_PHASES)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args(arguments)
    try:
        group = validate_backup_group(
            Path(str(args.artifact) + ".dump.enc"),
            supported_revisions=frozenset({"20260809_0018"}),
            expected_image_id=os.environ.get("OPERATIONS_IMMUTABLE_IMAGE_ID", ""),
            expected_executable_digests=image_executable_digests(),
        )
        load_preflight(group)
        phase = cast(AuditPhase, args.phase)
        if phase != "pre-migration":
            load_audit(group, "pre-migration" if phase == "post-migration" else "post-migration")
        from ai_employee.cli.postgres_restore import _host_guard

        _host_guard()
        parsed = _load_database_endpoint()
        endpoint = DatabaseEndpoint(
            parsed.host, parsed.port, "ai_employee_owner", parsed.database_name
        )
        secret = Path("/run/secrets/postgres_bootstrap_password")
        app = _read_secret_file(Path("/run/secrets/app_database_password"))
        sessions = build_session_factory(
            endpoint.owner_url(app, database_name=endpoint.database_name)
            .set(drivername="postgresql+asyncpg", username="ai_employee_app")
            .render_as_string(hide_password=False)
        )

        async def execute(holder: ReadOnlyMaintenanceHolder) -> None:
            task = asyncio.current_task()
            loop = asyncio.get_running_loop()
            if task is not None:
                loop.add_signal_handler(signal.SIGTERM, task.cancel)
            try:
                await run_audit(
                    phase=phase,
                    group=group,
                    holder=holder,
                    sessions=sessions,
                    facts_reader=CalendarAadOwnerFactsReader(endpoint, secret, holder=holder),
                    clock=lambda: datetime.now(UTC),
                    host_guard=_host_guard,
                )
            finally:
                await sessions.dispose()
                loop.remove_signal_handler(signal.SIGTERM)

        with (
            _open_maintenance_context(MigrateArguments(secret), endpoint) as context,
            context.acquire_management_lifecycle_lock() as management,
            management.acquire_target_lock() as target,
            target.acquire_schema_lifecycle_lock(),
            target.read_only_holder() as holder,
        ):
            asyncio.run(execute(holder))
    except (
        OSError,
        ValueError,
        RuntimeError,
        SQLAlchemyError,
        CalendarAadRolloutError,
        asyncio.CancelledError,
    ):
        print("calendar_aad_audit_failed", file=sys.stderr)
        return 1
    print("calendar_aad_audit_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
