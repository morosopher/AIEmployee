"""在真正 isolated 0018 上验证主动刷新、固定 revision lease 和无内容 artifact。"""

import asyncio
import json
import stat
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import URL, select, text

from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
from ai_employee.application.ports.calendar import CalendarEvent, CalendarSyncPage
from ai_employee.application.ports.oauth_refresh import OAuthRefreshError, OAuthRefreshRequest
from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import InternalInvariantError
from ai_employee.infrastructure.db.alembic import set_alembic_database_url
from ai_employee.infrastructure.db.models.sources import ProviderCalendarModel, SyncCursorModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories.credential_rotation import (
    SqlAlchemyCredentialRotationRepository,
)
from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
    SqlAlchemyOAuthRefreshCoordinator,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from tests.integration.alembic_commands import run_alembic_upgrade
from tests.integration.m2.test_credential_rotation_repository import (
    CONNECTION_ID,
    USER_ID,
    FakeRefreshProvider,
    persisted_credentials,
    seed_oauth_connection,
    token_response,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
IMAGE = "sha256:" + "0123456789abcdef" * 4
BASENAME = "calendar-aad-0019-synthetic"
SCOPE = "synthetic-calendar-opaque"


async def seed_pair(
    sessions, *, scope=SCOPE, complete=True, connection_id=CONNECTION_ID, user_id=USER_ID
):
    """只向本例创建完整或全空历史三元组，避免以 head ORM 的新增列写入 0018。"""
    async with sessions.begin() as session:
        session.add(
            ProviderCalendarModel(
                user_id=user_id,
                connection_id=connection_id,
                provider_calendar_id=scope,
                name="Synthetic Calendar",
                timezone="UTC",
                is_primary=False,
                access_role="owner",
                can_write=True,
            )
        )
        session.add(
            SyncCursorModel(
                connection_id=connection_id,
                resource_kind="calendar",
                scope_key=scope,
                cursor="synthetic-old-cursor",
                last_success_at=NOW,
            )
        )
        await session.execute(
            text(
                "INSERT INTO calendar_events (id,user_id,connection_id,calendar_id,provider_event_id,"
                "title,description_ciphertext,description_nonce,description_key_version,"
                "location_ciphertext,location_nonce,location_key_version,all_day,transparency,status,"
                "timezone,provider_url,created_at,updated_at) VALUES (:id,:user,:connection,:scope,"
                "'synthetic-event','Synthetic Event',:cipher,:nonce,:version,:cipher,:nonce,:version,"
                "false,'opaque','confirmed','UTC','https://calendar.example.test/event',:now,:now)"
            ),
            {
                "id": uuid4(),
                "user": user_id,
                "connection": connection_id,
                "scope": scope,
                "cipher": b"synthetic-historical-aead" if complete else None,
                "nonce": bytes(range(12)) if complete else None,
                "version": 7 if complete else None,
                "now": NOW,
            },
        )


@dataclass
class RecordingReader:
    """只模拟供应商分页；存储、凭据、lease 与 guard 保持真实。"""

    scopes: list[str] = field(default_factory=list)
    before_page: object = None
    missing_cursor: bool = False
    wrong_scope: bool = False

    async def initial_pages(self, calendar_id):
        """记录精确 scope，允许在网络完成前安排真实数据库并发或时钟变化。"""
        self.scopes.append(calendar_id)
        if self.before_page is not None:
            await self.before_page()
        yield CalendarSyncPage(
            (
                CalendarEvent(
                    event_id="synthetic-event",
                    calendar_id="synthetic-wrong-scope" if self.wrong_scope else calendar_id,
                    title="Synthetic Event",
                    description="Synthetic Description",
                    location="Synthetic Location",
                    starts_at=NOW,
                    ends_at=NOW + timedelta(hours=1),
                    all_day=False,
                    transparency="opaque",
                    status="confirmed",
                    timezone="UTC",
                    recurring_event_id=None,
                    etag="synthetic-etag",
                    provider_url="https://calendar.example.test/event",
                ),
            ),
            None,
            None if self.missing_cursor else "synthetic-new-cursor",
        )

    def directory_pages(self, cursor=None):
        """目录访问永远是恢复/预检错误，禁止用宽泛 owner 替代 exact pair。"""
        raise AssertionError("directory access is forbidden")

    def sync_pages(self, calendar_id, cursor):
        """本轮只允许 initial bounded pages，禁止普通增量 fallback。"""
        raise AssertionError("incremental access is forbidden")


@dataclass
class FakeAdapters:
    """只在 provider-neutral resolver 的网络端注入 Fake，检查使用持久刷新后 access。"""

    provider: FakeRefreshProvider = field(
        default_factory=lambda: FakeRefreshProvider(token_response())
    )
    reader: RecordingReader = field(default_factory=RecordingReader)
    accesses: list[str] = field(default_factory=list, repr=False)

    def oauth(self, provider):
        """返回明确拥有 provider 身份和 scope 映射的标准 refresh Fake。"""
        assert provider == "google"
        return self.provider

    def calendar_reader(self, pair, access_token):
        """供应商 reader 使用传入当前 token，不从过期本地快照偷偷读取。"""
        self.accesses.append(access_token)
        return self.reader


async def seed_microsoft_pair(sessions, cipher):
    """为同一个 opaque 日历 ID 建独立 Microsoft owner，保持供应商模型和凭据 AAD 真实。"""
    from ai_employee.infrastructure.db.models.sources import (
        ConnectionCapabilityModel,
        EncryptedCredentialModel,
        OAuthConnectionModel,
    )

    connection_id = UUID("00000000-0000-0000-0000-000000000202")
    scopes = ["Calendars.Read", "User.Read", "offline_access"]
    async with sessions.begin() as session:
        session.add(
            OAuthConnectionModel(
                id=connection_id,
                user_id=USER_ID,
                provider="microsoft",
                provider_account_id="synthetic-microsoft-account",
                account_email="microsoft@example.test",
                provider_tenant_id="synthetic-microsoft-tenant",
                account_type="work_school",
                scopes=scopes,
                status="connected",
                authorization_generation=2,
            )
        )
        await session.flush()
        session.add(
            ConnectionCapabilityModel(
                user_id=USER_ID,
                connection_id=connection_id,
                capability="calendar.read",
                status="enabled",
                actual_scopes=scopes,
                last_verified_at=NOW,
            )
        )
        for kind in ("access_token", "refresh_token"):
            encrypted = cipher.encrypt(
                f"synthetic-microsoft-{kind}".encode(), f"{USER_ID}:{connection_id}:{kind}".encode()
            )
            session.add(
                EncryptedCredentialModel(
                    user_id=USER_ID,
                    connection_id=connection_id,
                    credential_kind=kind,
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=NOW + timedelta(minutes=5) if kind == "access_token" else None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    await seed_pair(sessions, connection_id=connection_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401])
async def test_calendar_aad_two_providers_same_opaque_scope_use_real_http_readers(
    aad_oauth, tmp_path, status
):
    """真实 Google/Graph reader 只读取精确事件集合；401 不二次刷新或改变本地能力。"""
    import httpx
    import respx

    from ai_employee.application.ports.oauth import OAuthProvider, OAuthTokenSet
    from ai_employee.config import Settings
    from ai_employee.domain.errors import DomainError
    from ai_employee.workers.sync_calendar import CalendarAadReadAdapters

    sessions, cipher, _, _ = aad_oauth
    await seed_pair(sessions)
    await seed_microsoft_pair(sessions, cipher)

    class MicrosoftProvider(FakeRefreshProvider):
        """只替代 OAuth 网络，固定 Microsoft canonical scope 映射。"""

        provider = OAuthProvider.MICROSOFT

        def scopes_for(self, capabilities):
            """M2 delegated 身份加精确 calendar.read，不引入额外写权限。"""
            return frozenset(
                {"User.Read", "offline_access"}
                | (
                    {"Calendars.Read"}
                    if ConnectionCapability.CALENDAR_READ in capabilities
                    else set()
                )
            )

    google = FakeRefreshProvider(token_response())
    microsoft = MicrosoftProvider(
        OAuthTokenSet(
            "synthetic-microsoft-new-access",
            None,
            3600,
            frozenset({"User.Read", "offline_access", "Calendars.Read"}),
        )
    )

    class Readers(CalendarAadReadAdapters):
        """只注入 OAuth 网络 Fake；Calendar 构造和 HTTP 解析全部保持产品路径。"""

        def oauth(self, provider):
            """按数据库 provider 身份选择对应真实协议 Fake。"""
            return google if provider == "google" else microsoft

    adapters = Readers(Settings(app_env="test", app_test_mode=False), clock=lambda: NOW)
    google_url = f"https://www.googleapis.com/calendar/v3/calendars/{SCOPE}/events"
    microsoft_url = f"https://graph.microsoft.com/v1.0/me/calendars/{SCOPE}/calendarView/delta"
    async with sessions() as session:
        before = (
            (
                await session.execute(
                    text("SELECT row_to_json(c)::text FROM connection_capabilities c ORDER BY id")
                )
            )
            .scalars()
            .all()
        )
    with respx.mock(assert_all_called=False) as router:
        google_route = router.get(google_url).mock(
            return_value=httpx.Response(
                status, json={"items": [], "nextSyncToken": "synthetic-final"}
            )
        )
        microsoft_route = router.get(microsoft_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [],
                    "@odata.deltaLink": microsoft_url + "?$deltatoken=synthetic-final",
                },
            )
        )
        if status == 200:
            artifact = await run_fixture(aad_oauth, tmp_path, adapters=adapters)
            assert artifact.affected_connection_count == artifact.affected_pair_count == 2
            assert len(set(artifact.pair_digests)) == 2
            assert google_route.call_count == microsoft_route.call_count == 1
        else:
            with pytest.raises(DomainError):
                await run_fixture(aad_oauth, tmp_path, adapters=adapters)
            assert google_route.call_count == 1
            assert microsoft_route.call_count == 0
            assert not list(tmp_path.iterdir())
        assert all(call.request.method == "GET" for call in router.calls)
    assert google.calls == microsoft.calls == 1
    async with sessions() as session:
        assert (
            await session.execute(
                text("SELECT row_to_json(c)::text FROM connection_capabilities c ORDER BY id")
            )
        ).scalars().all() == before


async def run_fixture(
    aad_oauth, tmp_path, *, adapters=None, clock=lambda: NOW, basename=BASENAME, image=IMAGE
):
    """调用真实 CLI composition 的异步入口，只替换外部 reader 与时钟。"""
    from ai_employee.cli.calendar_aad_preflight_0019 import run_preflight

    sessions, _, coordinator, _ = aad_oauth
    return await run_preflight(
        sessions=sessions,
        coordinator=coordinator,
        adapters=adapters or FakeAdapters(),
        backup_directory=tmp_path,
        basename=basename,
        immutable_image_id=image,
        clock=clock,
    )


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """该模块只迁移 UUID disposable target，保留官方 lifecycle anchor 原样。"""
    yield


@pytest.fixture(autouse=True)
async def isolated_database() -> AsyncIterator[None]:
    """每例自己的 provenance fixture 管理清理，不对共享 anchor 执行 TRUNCATE。"""
    yield


@pytest.fixture
def aad_source(empty_migration_database: URL) -> tuple[URL, Config]:
    """通过既有 typed 三锁 lifecycle 创建真正 0018；业务测试不把准入错误算作 RED。"""
    config = Config(Path(__file__).resolve().parents[3] / "alembic.ini")
    set_alembic_database_url(config, empty_migration_database.render_as_string(hide_password=False))
    run_alembic_upgrade(config, "20260809_0018")
    return empty_migration_database, config


@pytest.fixture
async def aad_oauth(aad_source):
    """向本例 0018 插入合成双凭据，并使用真实 coordinator/CAS 与可替换 UTC 时钟。"""
    url, config = aad_source
    sessions = build_session_factory(url.render_as_string(hide_password=False))
    cipher = AeadCipher(bytes(range(32)), key_version=7)
    await seed_oauth_connection(sessions, cipher)
    coordinator = SqlAlchemyOAuthRefreshCoordinator(
        session_factory=sessions,
        cipher=cipher,
        identity=OAuthRefreshIdentity(bytes(range(32)), key_version=7),
        clock=lambda: NOW,
    )
    try:
        async with sessions() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "20260809_0018"
            )
        yield sessions, cipher, coordinator, config
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["20260809_0018", "20260809_0019"])
async def test_calendar_aad_revision_screen_is_read_only_and_preserves_facts(
    aad_oauth, monkeypatch, revision
):
    """真实 PostgreSQL 筛查仅打开一个只读目标快照，0018/0019 都不改变任何持久事实。

    用已有 lifecycle 准备版本；仅附加 SQL/transaction 观察器，原 published reader 照常
    执行。artifact、凭据刷新、业务写入、maintenance/grant 和供应商路径均不参与筛查。
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool

    from ai_employee.cli import calendar_aad_revision_0019 as module

    sessions, _, _, config = aad_oauth
    await seed_pair(sessions, complete=False)
    if revision == "20260809_0019":
        await asyncio.to_thread(run_alembic_upgrade, config, revision)

    async def persisted_facts():
        """比较所有有关业务表和版本表的完整列，不把无副作用简化为行数不变。"""
        result = {}
        async with sessions() as session:
            for table in (
                "alembic_version",
                "oauth_connections",
                "connection_capabilities",
                "encrypted_credentials",
                "provider_calendars",
                "sync_cursors",
                "calendar_events",
                "task_runs",
                "audit_events",
                "outbox_events",
            ):
                result[table] = tuple(
                    (
                        await session.execute(
                            text(
                                f"SELECT row_to_json(t)::text AS fact FROM {table} t ORDER BY fact"
                            )
                        )
                    ).scalars()
                )
        return result

    before = await persisted_facts()
    authority = module.load_published_alembic_authority(config)
    engine = create_engine(
        sessions.engine.url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 10, "options": "-c statement_timeout=10000"},
    )
    statements = []
    transactions = []
    checkouts = []

    def observe_statement(connection, cursor, statement, parameters, context, executemany):
        """只记录语句类别；不记录绑定参数、凭据、scope 或业务内容。"""
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    def observe_checkout(connection, record, proxy):
        """精确计数本次 screen 使用的目标物理连接。"""
        checkouts.append(1)

    event.listen(engine, "before_cursor_execute", observe_statement)
    event.listen(engine, "checkout", observe_checkout)
    event.listen(engine, "commit", lambda connection: transactions.append("commit"))
    event.listen(engine, "rollback", lambda connection: transactions.append("rollback"))
    original_reader = module.read_current_alembic_revision
    observations = []

    def observed_reader(connection, *, authority):
        """观察真正服务器事务属性后继续调用原只读 reader，不替换版本或 catalog 结果。"""
        observations.append(
            (
                connection.scalar(text("SHOW transaction_read_only")),
                connection.scalar(text("SHOW transaction_isolation")),
                connection.scalar(text("SHOW statement_timeout")),
            )
        )
        return original_reader(connection, authority=authority)

    monkeypatch.setattr(module, "read_current_alembic_revision", observed_reader)
    try:
        assert (
            await asyncio.to_thread(module.read_rollout_revision, engine, authority=authority)
            == revision
        )
    finally:
        engine.dispose()
    assert observations == [("on", "repeatable read", "10s")]
    assert checkouts == [1] and transactions == ["rollback"]
    assert statements.count("SELECT") == 2 and set(statements) <= {"SELECT", "SHOW"}
    assert await persisted_facts() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("loss_boundary", ["started", "confirmed"])
async def test_calendar_aad_outer_lease_loss_after_lock_wait_rolls_back(
    aad_oauth, monkeypatch, loss_boundary: str
) -> None:
    """在真实 snapshot/confirm 完成后释放固定锁，旧 coordinator 会错误提交 started/credential。

    锁与 writer 都是真的；直接使用公开 outer_lease 参数，错误必须保留稳定丢锁分类。
    """
    from ai_employee.infrastructure.db.repositories import oauth_refresh_coordinator as module

    sessions, _, coordinator, _ = aad_oauth
    before = await persisted_credentials(sessions)
    provider = FakeRefreshProvider(token_response())
    async with sessions.engine.connect() as lease_connection:
        assert await lease_connection.scalar(text("SELECT pg_try_advisory_lock(20260809, 19)"))
        await lease_connection.commit()

        class OuterLease:
            """读取同一真实 session 的固定 advisory owner，绝不重入获取锁。"""

            async def assert_owned(self) -> None:
                """确认固定两 int 锁仍存在；缺失通过 accepted coordinator error 边界退出。"""
                owned = await lease_connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() "
                        "AND locktype='advisory' AND classid=20260809 AND objid=19 "
                        "AND objsubid=2 AND granted)"
                    )
                )
                await lease_connection.commit()
                if not owned:
                    raise OAuthRefreshError("oauth_refresh_claim_lost")

        async def lose() -> None:
            """模拟等待数据库行锁期间 revision-global lease 被实际释放。"""
            assert await lease_connection.scalar(text("SELECT pg_advisory_unlock(20260809,19)"))
            await lease_connection.commit()

        if loss_boundary == "started":
            original_load = module.load_refresh_snapshot

            async def load_then_lose(session, request, *, lock):
                """保留真实查询和行锁，仅在返回后制造外层 lease 丢失窗口。"""
                result = await original_load(session, request, lock=lock)
                if lock:
                    await lose()
                return result

            monkeypatch.setattr(module, "load_refresh_snapshot", load_then_lose)
        else:
            original_confirm = SqlAlchemyCredentialRotationRepository.confirm

            async def confirm_then_lose(self, claim, tokens, *, completed_at):
                """执行真实双行 CAS，再于 commit 前释放 outer lease。"""
                result = await original_confirm(self, claim, tokens, completed_at=completed_at)
                await lose()
                return result

            monkeypatch.setattr(
                SqlAlchemyCredentialRotationRepository, "confirm", confirm_then_lose
            )

        with pytest.raises(OAuthRefreshError) as failure:
            await coordinator.refresh(
                OAuthRefreshRequest(
                    user_id=USER_ID,
                    connection_id=CONNECTION_ID,
                    capability=ConnectionCapability.CALENDAR_READ,
                    source="calendar_aad_preflight",
                    rollout_digest_v1="a" * 64,
                ),
                provider,
                outer_lease=OuterLease(),
            )
        assert failure.value.error_code == "oauth_refresh_claim_lost"
        assert await persisted_credentials(sessions) == before
        async with sessions() as session:
            events = list(await session.scalars(select(AuditEventModel.event_type)))
        assert events == ([] if loss_boundary == "started" else ["oauth.refresh_started"])
        assert provider.calls == (0 if loss_boundary == "started" else 1)


@pytest.mark.asyncio
async def test_calendar_aad_preflight_requires_real_rollout_composition(
    aad_oauth, tmp_path
) -> None:
    """尚缺新模块时明确记录为接口缺失；后续同一测试验证 canonical zero artifact。"""
    result = await run_fixture(aad_oauth, tmp_path)
    assert result.result_code == "calendar_aad_preflight_zero"
    assert result.rollout_deadline is None


@pytest.mark.asyncio
async def test_calendar_aad_preflight_refreshes_once_and_never_mutates_calendar(
    aad_oauth, tmp_path
):
    """同一连接两 pair 仅一 refresh；未受影响 scope 不 probe，全部日历表逐字保持。"""
    sessions, _, _, _ = aad_oauth
    await seed_pair(sessions)
    await seed_pair(sessions, scope="synthetic-second-calendar")
    await seed_pair(sessions, scope="synthetic-empty-calendar", complete=False)
    async with sessions.begin() as session:
        session.add(
            SyncCursorModel(
                connection_id=CONNECTION_ID,
                resource_kind="calendar",
                scope_key="directory",
                cursor="synthetic-directory",
            )
        )
    async with sessions() as session:
        before = (
            (
                await session.execute(
                    text("SELECT row_to_json(e)::text FROM calendar_events e ORDER BY id")
                )
            )
            .scalars()
            .all()
        )
        cursors = (
            (
                await session.execute(
                    text("SELECT row_to_json(c)::text FROM sync_cursors c ORDER BY id")
                )
            )
            .scalars()
            .all()
        )
    adapters = FakeAdapters()
    artifact = await run_fixture(aad_oauth, tmp_path, adapters=adapters)
    assert adapters.provider.calls == 1
    assert adapters.reader.scopes == [SCOPE, "synthetic-second-calendar"]
    assert set(adapters.accesses) == {"synthetic-new-access"}
    assert artifact.affected_pair_count == 2
    assert artifact.affected_connection_count == 1
    assert artifact.rollout_deadline == NOW + timedelta(minutes=45)
    path = tmp_path / f"{BASENAME}.calendar-aad-preflight.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    for sensitive in (
        SCOPE,
        "synthetic-new-access",
        "synthetic-old-refresh",
        "Synthetic Description",
        "Synthetic Location",
        "calendar.read",
    ):
        assert sensitive not in raw
    assert set(json.loads(raw)) == {
        "schema_version",
        "source_revision",
        "target_revision",
        "rollout_digest_v1",
        "backup_artifact_basename",
        "immutable_image_id",
        "affected_connection_count",
        "affected_pair_count",
        "connection_digests",
        "pair_digests",
        "earliest_token_expires_at",
        "rollout_deadline",
        "safety_margin_seconds",
        "result_code",
    }
    async with sessions() as session:
        assert (
            await session.execute(
                text("SELECT row_to_json(e)::text FROM calendar_events e ORDER BY id")
            )
        ).scalars().all() == before
        assert (
            await session.execute(
                text("SELECT row_to_json(c)::text FROM sync_cursors c ORDER BY id")
            )
        ).scalars().all() == cursors
        assert await session.scalar(text("SELECT count(*) FROM task_runs")) == 0
        assert await session.scalar(text("SELECT count(*) FROM tool_executions")) == 0
    # artifact 发布后重入必须从 durable matching result/current readiness 恢复，不能第二次 grant。
    again = await run_fixture(aad_oauth, tmp_path, adapters=adapters)
    assert again == artifact
    assert adapters.provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("basename", [BASENAME, "synthetic-another-basename"])
async def test_calendar_aad_fixed_revision_lock_precedes_artifact_and_provider(
    aad_oauth, tmp_path, basename
):
    """不同文件名仍竞争同一 session lease；失败方连畸形 artifact 都不能读取或覆盖。"""
    sessions, _, _, _ = aad_oauth
    await seed_pair(sessions)
    path = tmp_path / f"{basename}.calendar-aad-preflight.json"
    path.write_text("synthetic-invalid-artifact")
    adapters = FakeAdapters()
    before = await persisted_credentials(sessions)
    async with sessions.engine.connect() as holder:
        assert await holder.scalar(text("SELECT pg_try_advisory_lock(20260809,19)"))
        await holder.commit()
        try:
            with pytest.raises(CalendarAadRolloutError) as failure:
                await run_fixture(aad_oauth, tmp_path, adapters=adapters, basename=basename)
            assert failure.value.error_code == "calendar_aad_rollout_locked"
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(20260809,19)"))
            await holder.commit()
    assert adapters.provider.calls == 0
    assert adapters.reader.scopes == []
    assert await persisted_credentials(sessions) == before
    assert path.read_text() == "synthetic-invalid-artifact"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gap", ["partial", "cursor", "calendar", "capability", "refresh", "owner", "disconnected"]
)
async def test_calendar_aad_exact_local_gap_precedes_refresh(aad_oauth, tmp_path, gap):
    """所有本地恢复前提均在主动 refresh 前验证，拒绝半组三元组和错误 owner。"""
    sessions, _, _, _ = aad_oauth
    await seed_pair(sessions)
    statements = {
        "partial": "UPDATE calendar_events SET description_nonce=NULL",
        "cursor": "DELETE FROM sync_cursors",
        "calendar": "DELETE FROM provider_calendars",
        "capability": "UPDATE connection_capabilities SET status='disabled' WHERE capability='calendar.read'",
        "refresh": "DELETE FROM encrypted_credentials WHERE credential_kind='refresh_token'",
        "owner": "UPDATE calendar_events SET user_id='00000000-0000-0000-0000-000000000102'",
        "disconnected": "UPDATE oauth_connections SET status='disconnected'",
    }
    async with sessions.begin() as session:
        await session.execute(text(statements[gap]))
    adapters = FakeAdapters()
    with pytest.raises(CalendarAadRolloutError) as failure:
        await run_fixture(aad_oauth, tmp_path, adapters=adapters)
    assert failure.value.error_code == "calendar_aad_local_recoverability_failed"
    assert adapters.provider.calls == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_page", ["missing_cursor", "wrong_scope"])
async def test_calendar_aad_invalid_probe_leaves_no_artifact_and_never_replays_refresh(
    aad_oauth, tmp_path, invalid_page
):
    """完整分页验证失败只丢弃 provider 内容，重入只复用 durable refresh 结果。"""
    sessions, _, _, _ = aad_oauth
    await seed_pair(sessions)
    reader = RecordingReader(**{invalid_page: True})
    adapters = FakeAdapters(reader=reader)
    with pytest.raises(InternalInvariantError) as failure:
        await run_fixture(aad_oauth, tmp_path, adapters=adapters)
    assert failure.value.error_code == (
        "calendar_final_cursor_missing"
        if invalid_page == "missing_cursor"
        else "calendar_event_scope_mismatch"
    )
    assert not list(tmp_path.iterdir())
    setattr(reader, invalid_page, False)
    await run_fixture(aad_oauth, tmp_path, adapters=adapters)
    assert adapters.provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["probe", "publication"])
async def test_calendar_aad_physical_revision_lease_loss_prevents_publication(
    aad_oauth, tmp_path, monkeypatch, boundary
):
    """实际断开专用lease session；probe后和文件fsync后都不能留下成功artifact。"""
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        CalendarAadArtifactFile,
        PostgreSQLCalendarAadRolloutLease,
    )

    sessions, _, _, _ = aad_oauth
    await seed_pair(sessions)
    leases = []
    acquire = PostgreSQLCalendarAadRolloutLease.acquire

    def record(self):
        """观察实际已取得的同一物理session，保留所有try-lock行为。"""
        acquire(self)
        leases.append(self)

    monkeypatch.setattr(PostgreSQLCalendarAadRolloutLease, "acquire", record)
    adapters = FakeAdapters()
    stage = CalendarAadArtifactFile._stage
    if boundary == "probe":

        async def lose():
            await asyncio.to_thread(leases[0].connection.invalidate)

        adapters.reader.before_page = lose
    else:

        def stage_then_lose(self, artifact):
            """真实临时文件已fsync后才丢锁，紧邻发布的guard仍必须读取原session所有权。"""
            path = stage(self, artifact)
            leases[0].connection.invalidate()
            return path

        monkeypatch.setattr(CalendarAadArtifactFile, "_stage", stage_then_lose)
    with pytest.raises(OAuthRefreshError) as failure:
        await run_fixture(aad_oauth, tmp_path, adapters=adapters)
    assert failure.value.error_code == "oauth_refresh_claim_lost"
    assert adapters.provider.calls == 1 and adapters.reader.scopes == [SCOPE]
    assert not list(tmp_path.iterdir())
    async with sessions() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version")) == "20260809_0018"
        )
        assert (
            await session.scalar(
                text("SELECT last_error_code FROM sync_cursors WHERE scope_key=:scope"),
                {"scope": SCOPE},
            )
            is None
        )
    adapters.reader.before_page = None
    monkeypatch.setattr(CalendarAadArtifactFile, "_stage", stage)
    await run_fixture(aad_oauth, tmp_path, adapters=adapters)
    assert adapters.provider.calls == 1
