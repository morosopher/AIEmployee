"""适配 0018/0019 可恢复事实、固定 session lease 与 artifact 的持久边界。

SQL 只投影本地恢复条件，不读取日程正文。revision lease 使用独立 NullPool session；
业务事务、provider I/O 和原子文件发布始终分开，连接消失后绝不重入取锁。
"""

import asyncio
import os
import stat
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import URL, Connection, create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from ai_employee.application.calendar_aad_digests import connection_digest_v1
from ai_employee.application.ports.calendar_aad_migration_guard import (
    CalendarAadMigrationConnection,
    CalendarAadMigrationPhase,
)
from ai_employee.application.ports.oauth_refresh import OAuthRefreshError
from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadArtifact,
    CalendarAadBinding,
    CalendarAadFacts,
    CalendarAadGuard,
    CalendarAadPair,
    CalendarAadRevision,
    CalendarAadRolloutError,
    parse_rollout_artifact,
    serialize_rollout_artifact,
    verify_rollout,
)
from ai_employee.infrastructure.calendar_aad_resources import await_calendar_aad_resource
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

_PARTIAL = """SELECT EXISTS (SELECT 1 FROM calendar_events WHERE
num_nonnulls(description_ciphertext,description_nonce,description_key_version) NOT IN (0,3)
OR num_nonnulls(location_ciphertext,location_nonce,location_key_version) NOT IN (0,3))"""
_PAIRS = """SELECT DISTINCT e.user_id, e.connection_id, e.calendar_id,
c.user_id AS owner_id, c.provider, c.status, c.authorization_generation, u.timezone,
cap.id AS capability_id, cal.id AS calendar_row_id, cur.id AS cursor_id,
a.user_id AS access_user, a.ciphertext AS access_cipher, a.nonce AS access_nonce,
a.key_version AS access_version, a.token_expires_at,
r.user_id AS refresh_user, r.ciphertext AS refresh_cipher, r.nonce AS refresh_nonce,
r.key_version AS refresh_version
FROM calendar_events e
LEFT JOIN oauth_connections c ON c.id=e.connection_id
LEFT JOIN users u ON u.id=e.user_id
LEFT JOIN connection_capabilities cap ON cap.connection_id=e.connection_id
 AND cap.user_id=e.user_id AND cap.capability='calendar.read' AND cap.status='enabled'
LEFT JOIN provider_calendars cal ON cal.connection_id=e.connection_id
 AND cal.user_id=e.user_id AND cal.provider_calendar_id=e.calendar_id
LEFT JOIN sync_cursors cur ON cur.connection_id=e.connection_id
 AND cur.resource_kind='calendar' AND cur.scope_key=e.calendar_id AND cur.scope_key<>'directory'
LEFT JOIN encrypted_credentials a ON a.connection_id=e.connection_id AND a.credential_kind='access_token'
LEFT JOIN encrypted_credentials r ON r.connection_id=e.connection_id AND r.credential_kind='refresh_token'
WHERE num_nonnulls(e.description_ciphertext,e.description_nonce,e.description_key_version)=3
 OR num_nonnulls(e.location_ciphertext,e.location_nonce,e.location_key_version)=3
ORDER BY e.connection_id,e.calendar_id,e.user_id"""


def read_calendar_aad_facts(
    connection: Connection, *, expected_revision: CalendarAadRevision
) -> CalendarAadFacts:
    """在调用方的同一真实事务扫描全部 affected pair，禁止通过 caller filter 隐藏缺口。

    Args:
        connection: 当前只读事务或 frozen migration guard 所在的 Connection。
        expected_revision: 仅接受部署顺序中精确 0018 或 0019。

    Returns:
        由 owning connection 与完整 triple 推导的稳定排序集合和当前 access expiry。

    Raises:
        CalendarAadRolloutError: revision、部分 triple、所有权或任一恢复必需行不成立。
    """
    if connection.scalar(text("SELECT version_num FROM alembic_version")) != expected_revision:
        raise CalendarAadRolloutError("calendar_aad_revision_mismatch")
    if connection.scalar(text(_PARTIAL)) is not False:
        raise CalendarAadRolloutError("calendar_aad_local_recoverability_failed")
    pairs: list[CalendarAadPair] = []
    expiries: dict[UUID, datetime | None] = {}
    for row in connection.execute(text(_PAIRS)).mappings():
        user_id, connection_id = row["user_id"], row["connection_id"]
        if (
            not isinstance(user_id, UUID)
            or not isinstance(connection_id, UUID)
            or row["owner_id"] != user_id
            or row["status"] != "connected"
            or row["provider"] not in {"google", "microsoft"}
            or any(row[name] is None for name in ("capability_id", "calendar_row_id", "cursor_id"))
            or type(row["authorization_generation"]) is not int
            or row["authorization_generation"] < 0
            or type(row["timezone"]) is not str
        ):
            raise CalendarAadRolloutError("calendar_aad_local_recoverability_failed")
        for prefix in ("access", "refresh"):
            if (
                row[f"{prefix}_user"] != user_id
                or not isinstance(row[f"{prefix}_cipher"], bytes)
                or not row[f"{prefix}_cipher"]
                or not isinstance(row[f"{prefix}_nonce"], bytes)
                or len(row[f"{prefix}_nonce"]) != 12
                or type(row[f"{prefix}_version"]) is not int
                or row[f"{prefix}_version"] < 1
            ):
                raise CalendarAadRolloutError("calendar_aad_local_recoverability_failed")
        pair = CalendarAadPair(
            user_id,
            connection_id,
            row["calendar_id"],
            row["provider"],
            row["timezone"],
            row["authorization_generation"],
        )
        try:
            _ = pair.digest
        except (ValueError, TypeError):
            raise CalendarAadRolloutError("calendar_aad_local_recoverability_failed") from None
        pairs.append(pair)
        expiry = row["token_expires_at"]
        if expiry is not None and not isinstance(expiry, datetime):
            raise CalendarAadRolloutError("calendar_aad_local_recoverability_failed")
        expiries[connection_id] = expiry
    if len({pair.digest for pair in pairs}) != len(pairs):
        raise CalendarAadRolloutError("calendar_aad_local_recoverability_failed")
    return CalendarAadFacts(expected_revision, tuple(pairs), tuple(sorted(expiries.items())))


class SqlAlchemyCalendarAadPreflightRepository:
    """每次以新短只读事务加载事实，不让先前 token/expiry 缓存延长 rollout。"""

    def __init__(self, sessions: ManagedAsyncSessionMaker) -> None:
        """保存由调用方负责释放的进程级会话工厂。"""
        self._sessions = sessions

    async def read_facts(self, *, expected_revision: CalendarAadRevision) -> CalendarAadFacts:
        """重新查询当前完整 affected set，事务结束后才允许 provider I/O。"""
        async with self._sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            return await session.run_sync(
                lambda sync: read_calendar_aad_facts(
                    sync.connection(), expected_revision=expected_revision
                )
            )

    async def existing_attempt(self, *, pair: CalendarAadPair, rollout_digest: str) -> UUID | None:
        """只定位同 rollout 的唯一 started ID；closure 与 current readiness 仍由共享 coordinator 处理。"""
        async with self._sessions() as session:
            raw = list(
                await session.scalars(
                    select(AuditEventModel.event_metadata["refresh_attempt_id"].astext).where(
                        AuditEventModel.user_id == pair.user_id,
                        AuditEventModel.event_type == "oauth.refresh_started",
                        AuditEventModel.event_metadata["connection_digest"].astext
                        == connection_digest_v1(pair.connection_id),
                        AuditEventModel.event_metadata["source"].astext == "calendar_aad_preflight",
                        AuditEventModel.event_metadata["rollout_digest_v1"].astext
                        == rollout_digest,
                    )
                )
            )
        if len(raw) > 1:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        if not raw:
            return None
        try:
            attempt_id = UUID(raw[0])
            if str(attempt_id) != raw[0]:
                raise ValueError("attempt is not canonical")
        except (ValueError, TypeError, AttributeError):
            raise OAuthRefreshError("oauth_credential_state_conflict") from None
        return attempt_id


class PostgreSQLCalendarAadRolloutLease:
    """固定 revision-global 两 int session lock；basename 不属于锁域。"""

    def __init__(self, connection: Connection) -> None:
        """记录专用物理 session；仅 acquire 成功后允许 artifact/provider 边界使用。"""
        self.connection = connection
        self._pid: int | None = None
        self._verification_lock = asyncio.Lock()

    def acquire(self) -> None:
        """try-lock 一次并结束该短事务，失败方零 provider、零业务持久写入。"""
        if self._pid is not None:
            raise CalendarAadRolloutError("calendar_aad_rollout_locked")
        row = self.connection.execute(
            text("SELECT pg_backend_pid(), pg_try_advisory_lock(20260809,19)")
        ).one()
        if row[1] is not True:
            raise CalendarAadRolloutError("calendar_aad_rollout_locked")
        self._pid = int(row[0])
        self.connection.commit()

    def verify_owned(self) -> None:
        """验证原 backend PID 与实际 pg_locks owner；不能用重连/重入 acquire 掩盖丢锁。"""
        if self.connection.closed or self.connection.invalidated or self._pid is None:
            raise OAuthRefreshError("oauth_refresh_claim_lost")
        try:
            owned = self.connection.scalar(
                text(
                    "SELECT pg_backend_pid()=:pid AND EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE pid=pg_backend_pid() AND locktype='advisory' AND classid=20260809 "
                    "AND objid=19 AND objsubid=2 AND granted AND mode='ExclusiveLock' "
                    "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))"
                ),
                {"pid": self._pid},
            )
            self.connection.commit()
        except (SQLAlchemyError, OSError):
            raise OAuthRefreshError("oauth_refresh_claim_lost") from None
        if owned is not True:
            raise OAuthRefreshError("oauth_refresh_claim_lost")

    async def assert_owned(self) -> None:
        """收完同一同步 session 的校验线程后才允许关闭/复用连接，重复取消也不提前解锁。"""
        async with self._verification_lock:
            pending = asyncio.create_task(asyncio.to_thread(self.verify_owned))
            await await_calendar_aad_resource(pending)


@contextmanager
def calendar_aad_rollout_lease(database_url: URL) -> Iterator[PostgreSQLCalendarAadRolloutLease]:
    """为整个 one-off 保留独立 NullPool session，所有退出路径靠物理连接关闭释放锁。"""
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"), poolclass=NullPool, hide_parameters=True
    )
    try:
        with engine.connect() as connection:
            lease = PostgreSQLCalendarAadRolloutLease(connection)
            lease.acquire()
            yield lease
    finally:
        engine.dispose()


@asynccontextmanager
async def async_calendar_aad_rollout_lease(
    database_url: URL,
) -> AsyncIterator[PostgreSQLCalendarAadRolloutLease]:
    """持有 acquire 到 exit 的全部线程结果，重复取消时也先完成同一 session 的收尾。"""
    context = calendar_aad_rollout_lease(database_url)
    acquisition = asyncio.create_task(asyncio.to_thread(context.__enter__))
    cancellation: asyncio.CancelledError | None = None
    try:
        lease = await await_calendar_aad_resource(acquisition)
        yield lease
    except asyncio.CancelledError as error:
        cancellation = error
        raise
    finally:
        # helper 保证 acquisition 已终结。成功结果即表示本次拥有 context，哪怕赋值被取消
        # 跳过也必须显式 exit；enter 本身失败则由同步 context 的 finally 收尾。
        if not acquisition.cancelled() and acquisition.exception() is None:
            cleanup = asyncio.create_task(asyncio.to_thread(context.__exit__, None, None, None))
            try:
                await await_calendar_aad_resource(cleanup)
            except asyncio.CancelledError:
                if cancellation is not None:
                    raise cancellation
                raise


class CalendarAadArtifactFile:
    """只读写当前 binding 的 0600 文件；碰撞检查必须由持 revision lease 的调用方执行。"""

    def __init__(self, directory: Path, binding: CalendarAadBinding) -> None:
        """构造时只形成路径，不触碰文件，确保失败 try-lock 先于所有 artifact 检查。"""
        self.binding = binding
        self.path = directory / f"{binding.basename}.calendar-aad-preflight.json"

    def read_optional(self) -> CalendarAadArtifact | None:
        """非阻塞 no-follow 打开，并在同一 fd 验证 regular/0600/大小后读取闭合 artifact。

        FIFO 无 writer 时必须立即到达类型拒绝；路径预检查无法替代打开后同 fd 的事实。
        fd 始终由此作用域显式关闭，目录等非法类型不会在 fdopen 前丢失描述符所有权。
        """
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise CalendarAadRolloutError("calendar_aad_artifact_invalid") from None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 1_048_576
            ):
                raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return parse_rollout_artifact(stream.read(1_048_577), self.binding)
        finally:
            os.close(descriptor)

    def read(self) -> CalendarAadArtifact:
        """后续操作必须已有成功 artifact，缺失不能被解读为 zero/bootstrap 豁免。"""
        artifact = self.read_optional()
        if artifact is None:
            raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
        return artifact

    def _stage(self, artifact: CalendarAadArtifact) -> Path:
        """在同目录 fsync 完整临时文件，只有后续当前 guard 成功才允许原子改名。"""
        descriptor, temporary = tempfile.mkstemp(prefix=".calendar-aad-", dir=self.path.parent)
        path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(serialize_rollout_artifact(artifact))
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def _publish(self, temporary: Path, artifact: CalendarAadArtifact) -> bool:
        """保持已有成功文件不可覆写；同 binding 的完全相同重入只复用原 artifact。"""
        existing = self.read_optional()
        if existing is not None:
            if existing != artifact:
                raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
            return False
        os.rename(temporary, self.path)
        try:
            descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            # 仅移除本调用刚 rename 的文件，不能把 fsync 失败暴露为可继续 rollout 的成功证据。
            self.path.unlink(missing_ok=True)
            raise
        return True

    async def publish(self, artifact: CalendarAadArtifact, guard: CalendarAadGuard) -> None:
        """在当前 guard 下发布；任意阶段取消都先收完线程，再清理临时文件并撤回新发布。

        staging/publication Task 保留真实结果所有权。临时清理收到新的取消也必须继续
        撤回本次新文件；既有成功 artifact 的 publication 结果为 False，始终保留。
        """
        staging = asyncio.create_task(asyncio.to_thread(self._stage, artifact))
        publication: asyncio.Task[bool] | None = None
        cancellation: asyncio.CancelledError | None = None
        try:
            try:
                temporary = await await_calendar_aad_resource(staging)
                await guard.verify()
                publication = asyncio.create_task(
                    asyncio.to_thread(self._publish, temporary, artifact)
                )
                await await_calendar_aad_resource(publication)
            except asyncio.CancelledError as error:
                cancellation = error
                raise
            finally:
                if not staging.cancelled() and staging.exception() is None:
                    temporary = staging.result()
                    cleanup = asyncio.create_task(
                        asyncio.to_thread(temporary.unlink, missing_ok=True)
                    )
                    try:
                        await await_calendar_aad_resource(cleanup)
                    except asyncio.CancelledError:
                        if cancellation is not None:
                            raise cancellation
                        raise
        except BaseException as failure:
            # 此分支也覆盖临时文件清理自身失败/取消，不能因上一项 cleanup 抛错而跳过补偿。
            if (
                publication is not None
                and not publication.cancelled()
                and publication.exception() is None
                and publication.result()
            ):
                rollback = asyncio.create_task(asyncio.to_thread(self.path.unlink, missing_ok=True))
                try:
                    await await_calendar_aad_resource(rollback)
                except asyncio.CancelledError:
                    if isinstance(failure, asyncio.CancelledError):
                        raise failure
                    raise
            raise


class CalendarAadCurrentGuard:
    """每次重新读取 artifact 与当前数据库事实，供 preflight、backup、planner 和 Worker 共用。"""

    def __init__(
        self,
        *,
        repository: SqlAlchemyCalendarAadPreflightRepository,
        artifact_file: CalendarAadArtifactFile,
        artifact: CalendarAadArtifact,
        lease: PostgreSQLCalendarAadRolloutLease,
        clock: Callable[[], datetime],
        expected_revision: CalendarAadRevision,
        allow_missing: bool = False,
    ) -> None:
        """绑定本次不可变 artifact 与实际持锁 session；allow_missing 只供首次发布前验证。"""
        self._repository, self._file, self._artifact = repository, artifact_file, artifact
        self._lease, self._clock, self._revision, self._allow_missing = (
            lease,
            clock,
            expected_revision,
            allow_missing,
        )

    async def verify(self) -> datetime | None:
        """以新 current expiry 收紧窗口，并在事实读取后再检查 lease，不能跨等待复用旧证明。"""
        await self._lease.assert_owned()
        artifact = await await_calendar_aad_resource(
            asyncio.create_task(asyncio.to_thread(self._file.read_optional))
        )
        if artifact != self._artifact and not (artifact is None and self._allow_missing):
            raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
        facts = await self._repository.read_facts(expected_revision=self._revision)
        await self._lease.assert_owned()
        return verify_rollout(self._artifact, facts, now=self._clock())


class CalendarAadMigrationArtifactGuard:
    """实现 Task16A 冻结同步协议：两个阶段均在同一迁移 Connection 重读真实事实。"""

    def __init__(
        self,
        *,
        artifact_file: CalendarAadArtifactFile,
        lease: PostgreSQLCalendarAadRolloutLease,
        clock: Callable[[], datetime],
        artifact: CalendarAadArtifact | None = None,
    ) -> None:
        """保存同一 artifact/lease，严禁通过 resolver fallback 或新 guard instance 降级。"""
        self._file, self._lease, self._clock = artifact_file, lease, clock
        self._artifact = artifact

    def verify(
        self, *, connection: CalendarAadMigrationConnection, phase: CalendarAadMigrationPhase
    ) -> None:
        """同步严格返回 None；最终 guard 失败由已有外层 lifecycle 回滚所有 DDL/DML/grant。"""
        if not isinstance(connection, Connection) or phase not in {
            "before_mutation",
            "before_commit",
        }:
            raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
        self._lease.verify_owned()
        artifact = self._file.read()
        if self._artifact is None:
            # 直接注入 guard 的调用方可在首个 mutation phase 冻结；CLI 在更早的入口读取时冻结。
            self._artifact = artifact
        elif self._artifact != artifact:
            raise CalendarAadRolloutError("calendar_aad_artifact_invalid")
        facts = read_calendar_aad_facts(
            connection,
            expected_revision="20260809_0018" if phase == "before_mutation" else "20260809_0019",
        )
        self._lease.verify_owned()
        verify_rollout(artifact, facts, now=self._clock())
