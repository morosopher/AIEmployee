"""封装 Alembic 发布权威、外部 Connection 与逐步对象授权生命周期。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, Protocol

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text

from ai_employee.application.ports.calendar_aad_migration_guard import (
    CalendarAadMigrationGuard,
    invoke_calendar_aad_migration_guard,
)
from ai_employee.infrastructure.db.database_grants import (
    CatalogObjectSnapshot,
    GrantPhase,
    apply_migration_grant_delta,
    verify_object_grants,
    verify_pre_migration_object_grants,
)

_INVARIANT_ERROR_MESSAGE = "alembic migration invariant violation"
_VERSION_TABLE_SQL = """SELECT to_regclass('public.alembic_version') IS NOT NULL
AS version_table_exists"""
_CURRENT_REVISION_SQL = "SELECT version_num FROM public.alembic_version"
_LOWER_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ONLINE_AUTHORITY_ISSUER = object()


class AlembicMigrationInvariantError(RuntimeError):
    """表示发布脚本、Connection 或逐步迁移事实违反冻结边界。

    异常消息固定且不包含数据库 URL、revision 输入、对象名或授权内容，允许 CLI 在不
    泄漏部署事实的前提下归类失败。
    """


def _fail() -> NoReturn:
    """抛出唯一稳定且不含部署内容的 Alembic 不变量错误。"""
    raise AlembicMigrationInvariantError(_INVARIANT_ERROR_MESSAGE)


def _exact_revision(value: object) -> str:
    """读取非空、无 NUL 的 exact revision 文本，不执行隐式字符串转换。"""
    if type(value) is not str or value == "" or "\0" in value:
        _fail()
    return value


@dataclass(frozen=True, slots=True)
class PublishedAlembicAuthority:
    """保存由当前 migration 脚本实际派生的唯一线性发布链。

    ``database_grants`` 可以预先登记下一 revision 的 planned inventory，但只有这里的
    脚本链才有权决定当前可读取、迁移或作为最终 target 的 revision。把 ``base`` 放在
    链首可以统一首次安装与已发布 revision 的 membership/adjacency 校验。

    Args:
        revisions: 从 ``base`` 到唯一 head 的完整、无重复线性 revision tuple。

    Raises:
        AlembicMigrationInvariantError: 容器、顺序、revision 文本或唯一性不合法。
    """

    revisions: tuple[str, ...]

    def __post_init__(self) -> None:
        """在对象创建时一次性冻结完整发布链，禁止宽松 iterable 或重复 revision。"""
        if type(self.revisions) is not tuple or len(self.revisions) < 2:
            _fail()
        validated = tuple(_exact_revision(revision) for revision in self.revisions)
        if validated[0] != "base" or len(set(validated)) != len(validated):
            _fail()
        object.__setattr__(self, "revisions", validated)

    @property
    def head_revision(self) -> str:
        """返回当前脚本链唯一已发布 head。"""
        return self.revisions[-1]

    def contains(self, revision: object) -> bool:
        """返回 exact revision 是否属于当前已发布链；非法类型直接返回 ``False``。"""
        return type(revision) is str and revision in self.revisions

    def are_adjacent(self, source_revision: object, destination_revision: object) -> bool:
        """返回两个已发布 revision 是否在线性链上相邻，允许受控 upgrade/downgrade。"""
        if not self.contains(source_revision) or not self.contains(destination_revision):
            return False
        source_index = self.revisions.index(source_revision)
        destination_index = self.revisions.index(destination_revision)
        return abs(source_index - destination_index) == 1


def load_published_alembic_authority(config: Config) -> PublishedAlembicAuthority:
    """从 ``ScriptDirectory`` 派生唯一线性发布链，不信任 grant inventory 或调用方标签。

    Args:
        config: 指向仓库 Alembic script location 的配置。

    Returns:
        从 ``base`` 到实际唯一 head 的不可变发布权威。

    Raises:
        AlembicMigrationInvariantError: 配置不是 Alembic ``Config``、存在多 head、分支、
            断链、重复 revision 或非文本 revision。
    """
    if not isinstance(config, Config):
        _fail()
    try:
        directory = ScriptDirectory.from_config(config)
        heads = directory.get_heads()
    except Exception as error:
        raise AlembicMigrationInvariantError(_INVARIANT_ERROR_MESSAGE) from error
    if type(heads) is not list or len(heads) != 1:
        _fail()

    current: str | None = _exact_revision(heads[0])
    descending: list[str] = []
    seen: set[str] = set()
    try:
        while current is not None:
            if current in seen:
                _fail()
            script = directory.get_revision(current)
            if script is None or script.revision != current:
                _fail()
            descending.append(current)
            seen.add(current)
            down_revision = script.down_revision
            if down_revision is None:
                current = None
            elif type(down_revision) is str:
                current = _exact_revision(down_revision)
            else:
                # tuple/list down_revision 表示分支或 merge；M2 当前只允许唯一线性链。
                _fail()

        walked = tuple(_exact_revision(script.revision) for script in directory.walk_revisions())
    except AlembicMigrationInvariantError:
        raise
    except Exception as error:
        raise AlembicMigrationInvariantError(_INVARIANT_ERROR_MESSAGE) from error
    if len(walked) != len(descending) or set(walked) != set(descending):
        _fail()
    return PublishedAlembicAuthority(revisions=("base", *reversed(descending)))


def set_alembic_database_url(config: Config, database_url: str) -> None:
    """将数据库 URL 无损写入 Alembic 主配置。

    Alembic 的 ``Config.set_main_option`` 使用 ``ConfigParser`` 的百分号插值规则，
    因此 URL 编码产生的字面 ``%`` 必须在写入时加倍。随后通过 Alembic 读取配置时，
    插值层会把 ``%%`` 还原成单个百分号，使 SQLAlchemy 最终收到调用方提供的原始 URL。
    本函数不解析、连接或记录 URL，避免在迁移配置边界暴露数据库凭据。

    Args:
        config: 接收数据库 URL 的 Alembic 配置对象。
        database_url: 已由调用方提供并完成 URL 编码的 SQLAlchemy 数据库 URL。
    """
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))


def require_external_connection(config: Config) -> Connection:
    """读取 online migration 唯一允许的外部同步 Connection。

    Args:
        config: Alembic 当前配置；Connection 只能位于 ``attributes``。

    Returns:
        调用方已经取得 maintenance/schema locks 的 SQLAlchemy ``Connection``。

    Raises:
        AlembicMigrationInvariantError: 缺失连接，或传入 AsyncConnection/duck object。
    """
    if not isinstance(config, Config):
        _fail()
    connection = config.attributes.get("connection")
    if not isinstance(connection, Connection):
        _fail()
    return connection


def read_current_alembic_revision(
    connection: Connection,
    *,
    authority: PublishedAlembicAuthority,
) -> str:
    """读取唯一当前 revision，并立即用实际发布脚本 membership fail closed。

    ``alembic_version`` 尚未出现时规范化为 ``base``；多行、多 head、NULL、未知或 planned
    revision 都不能进入 object-grant registry 或 migration runner。

    Args:
        connection: 当前外层 Alembic/maintenance 同步 Connection。
        authority: 由当前 script directory 派生的发布权威。

    Returns:
        ``base`` 或唯一已发布 revision。

    Raises:
        AlembicMigrationInvariantError: Connection、catalog 类型、行数或 membership 不合法。
    """
    if not isinstance(connection, Connection) or type(authority) is not PublishedAlembicAuthority:
        _fail()
    exists = connection.execute(text(_VERSION_TABLE_SQL)).scalar_one()
    if type(exists) is not bool:
        _fail()
    if not exists:
        revision = "base"
    else:
        rows = connection.execute(text(_CURRENT_REVISION_SQL)).all()
        if len(rows) != 1 or len(rows[0]) != 1:
            _fail()
        revision = _exact_revision(rows[0][0])
    if not authority.contains(revision):
        _fail()
    return revision


class _MigrationContextProjection(Protocol):
    """描述 ``on_version_apply`` 只需读取的 Alembic context 投影。"""

    connection: Connection


class _MigrationStepProjection(Protocol):
    """描述 ``MigrationInfo`` 只需读取的类型化线性 step 字段。"""

    is_upgrade: bool
    is_stamp: bool
    source_revision_ids: tuple[str, ...]
    destination_revision_ids: tuple[str, ...]


def _single_revision(revisions: object) -> str:
    """把 Alembic 单 head tuple 规范化为 revision；空 tuple 表示 ``base``。"""
    if type(revisions) is not tuple:
        _fail()
    if len(revisions) == 0:
        return "base"
    if len(revisions) != 1:
        _fail()
    return _exact_revision(revisions[0])


@dataclass(slots=True)
class MigrationGrantLifecycle:
    """在一个外层 Alembic transaction 上维护完整 object-grant snapshot chain。

    Args:
        connection: lifecycle 全程唯一允许的同步 SQLAlchemy Connection。
        authority: 当前 migration scripts 的发布权威。
        expected_target_revision: 命令完成时必须精确到达的已发布 target。
        phase: stable database posture 对应的 baseline/active grant registry key。
    """

    connection: Connection
    authority: PublishedAlembicAuthority
    expected_target_revision: str
    phase: GrantPhase
    expected_current_revision: str | None = None
    _online_authority_binding: object | None = field(default=None, repr=False)
    _current_revision: str | None = field(default=None, init=False, repr=False)
    _snapshot: CatalogObjectSnapshot | None = field(default=None, init=False, repr=False)
    _calendar_aad_guard: CalendarAadMigrationGuard | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _finished: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """在任何 catalog read 前验证 Connection、authority、phase 与最终 target。"""
        if (
            not isinstance(self.connection, Connection)
            or type(self.authority) is not PublishedAlembicAuthority
            or type(self.phase) is not GrantPhase
        ):
            _fail()
        self.expected_target_revision = _exact_revision(self.expected_target_revision)
        if self.expected_current_revision is not None:
            self.expected_current_revision = _exact_revision(self.expected_current_revision)
            if not self.authority.contains(self.expected_current_revision):
                _fail()
        if not self.authority.contains(self.expected_target_revision):
            _fail()

    def bind_calendar_aad_guard(self, guard: CalendarAadMigrationGuard) -> None:
        """在 Alembic configure 前冻结 0019 最终回调使用的唯一 guard 实例。

        Args:
            guard: 已由唯一 Config resolver 验证的双阶段迁移守卫。

        Raises:
            AlembicMigrationInvariantError: guard 非协议实例、重复绑定或 lifecycle 已启动。
        """
        if (
            not isinstance(guard, CalendarAadMigrationGuard)
            or self._calendar_aad_guard is not None
            or self._current_revision is not None
            or self._snapshot is not None
            or self._finished
        ):
            _fail()
        self._calendar_aad_guard = guard

    def verify_before_migrations(self) -> None:
        """在首个 operation 前核验 source，并绑定朝 target 前进的唯一相邻 step。

        Resume descriptor 不能自行猜测目标 revision。这里从实际发布链与命令 token 的
        ``expected_target_revision`` 计算下一步；多步 upgrade 只把 immediate next 交给
        preflight，历史 tests-only reverse 则交给相邻前一 revision。当前已在 target 时
        传 ``None``，因此不会误启用任何 crash-candidate admission。
        """
        if self._current_revision is not None or self._snapshot is not None or self._finished:
            _fail()
        revision = read_current_alembic_revision(
            self.connection,
            authority=self.authority,
        )
        if (
            self.expected_current_revision is not None
            and revision != self.expected_current_revision
        ):
            _fail()
        current_index = self.authority.revisions.index(revision)
        target_index = self.authority.revisions.index(self.expected_target_revision)
        if current_index == target_index:
            destination_revision = None
        else:
            step = 1 if current_index < target_index else -1
            destination_revision = self.authority.revisions[current_index + step]
        snapshot = verify_pre_migration_object_grants(
            self.connection,
            revision=revision,
            destination_revision=destination_revision,
            phase=self.phase,
        )
        if snapshot.revision != revision or snapshot.phase is not self.phase:
            _fail()
        self._current_revision = revision
        self._snapshot = snapshot

    def on_version_apply(
        self,
        *,
        ctx: _MigrationContextProjection,
        step: _MigrationStepProjection,
        heads: set[str],
        run_args: dict[str, object],
    ) -> None:
        """在每步 operation/version-row 后应用 delta 并验证完整 destination inventory。

        Alembic 1.16 在当前 step 的 migration function 和 version table mutation 之后、
        外层 transaction finalize 之前调用该 callback。这里先完整验证 metadata 与同一
        Connection，再允许任何 GRANT/REVOKE；downgrade、stamp、branch、多 head、跳步或
        未发布 revision 全部在 mutation 前拒绝。
        """
        current_revision = self._current_revision
        snapshot = self._snapshot
        if current_revision is None or snapshot is None or self._finished:
            _fail()
        if getattr(ctx, "connection", None) is not self.connection:
            _fail()
        if step.is_upgrade is not True or step.is_stamp is not False:
            _fail()
        if type(heads) is not set or type(run_args) is not dict or run_args:
            _fail()

        source_revision = _single_revision(step.source_revision_ids)
        destination_revision = _single_revision(step.destination_revision_ids)
        if source_revision != current_revision or not self.authority.contains(destination_revision):
            _fail()
        expected_heads = set() if destination_revision == "base" else {destination_revision}
        if heads != expected_heads:
            _fail()
        source_index = self.authority.revisions.index(source_revision)
        if (
            source_index + 1 >= len(self.authority.revisions)
            or self.authority.revisions[source_index + 1] != destination_revision
        ):
            _fail()

        apply_migration_grant_delta(
            self.connection,
            source_revision=source_revision,
            destination_revision=destination_revision,
            phase=self.phase,
            before=snapshot,
        )
        destination_snapshot = verify_object_grants(
            self.connection,
            revision=destination_revision,
            phase=self.phase,
        )
        if (
            destination_snapshot.revision != destination_revision
            or destination_snapshot.phase is not self.phase
        ):
            _fail()
        self._current_revision = destination_revision
        self._snapshot = destination_snapshot
        if destination_revision == "20260809_0019":
            calendar_aad_guard = self._calendar_aad_guard
            if calendar_aad_guard is None:
                _fail()
            # destination grant inventory 已完整验证；最终 guard 必须是本 callback 的最后一个
            # 可失败动作，确保拒绝会回滚 DDL/DML、version row 与授权 delta。
            invoke_calendar_aad_migration_guard(
                lambda: calendar_aad_guard.verify(
                    connection=self.connection,
                    phase="before_commit",
                )
            )

    def verify_after_migrations(self) -> None:
        """在外层 transaction commit 前确认 callback chain 与真实 version row 到达 target。"""
        current_revision = self._current_revision
        if current_revision is None or self._snapshot is None or self._finished:
            _fail()
        observed_revision = read_current_alembic_revision(
            self.connection,
            authority=self.authority,
        )
        if (
            observed_revision != current_revision
            or observed_revision != self.expected_target_revision
        ):
            _fail()
        self._finished = True


@dataclass(frozen=True, slots=True, repr=False)
class OnlineMigrationAuthority:
    """绑定一次 online migration 唯一允许消费的连接、目标与 grant lifecycle。

    构造 capability 只能由本模块私有 issuer 完成；公开 Alembic env 只验证并消费，不能
    从 URL、环境变量、裸 ``Connection`` 或缺省 head 自行补齐 authority。生产 issuer
    由持有 management→target→schema locks 的 typed maintenance lease 调用；历史迁移
    契约测试只能经 tests-local helper 显式调用私有 factory。
    """

    connection: Connection
    published_authority: PublishedAlembicAuthority
    expected_target_identity_digest: str
    expected_current_revision: str
    expected_target_revision: str
    phase: GrantPhase
    grant_lifecycle: MigrationGrantLifecycle
    _binding: object = field(repr=False, compare=False)
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """拒绝手工 dataclass 拼装及任一 cross-token lifecycle。"""
        if (
            self._issuer is not _ONLINE_AUTHORITY_ISSUER
            or not isinstance(self.connection, Connection)
            or type(self.published_authority) is not PublishedAlembicAuthority
            or type(self.phase) is not GrantPhase
            or type(self.grant_lifecycle) is not MigrationGrantLifecycle
            or self.grant_lifecycle.connection is not self.connection
            or self.grant_lifecycle.authority != self.published_authority
            or self.grant_lifecycle.expected_current_revision != self.expected_current_revision
            or self.grant_lifecycle.expected_target_revision != self.expected_target_revision
            or self.grant_lifecycle.phase is not self.phase
            or self.grant_lifecycle._online_authority_binding is not self._binding
        ):
            _fail()
        _exact_target_identity_digest(self.expected_target_identity_digest)
        current = _exact_revision(self.expected_current_revision)
        target = _exact_revision(self.expected_target_revision)
        if not self.published_authority.contains(current) or not self.published_authority.contains(
            target
        ):
            _fail()


def _exact_target_identity_digest(value: object) -> str:
    """收窄 target identity 为 canonical lowercase SHA-256，不回显数据库事实。"""
    if type(value) is not str or _LOWER_DIGEST_PATTERN.fullmatch(value) is None:
        _fail()
    return value


def _issue_online_migration_authority(
    *,
    expected_target_revision: object,
    lease: object | None = None,
    **caller_claims: object,
) -> OnlineMigrationAuthority:
    """只从已持三锁且重验 stable admission 的 concrete lease 发行 capability。

    发行边界不再接受裸 ``Connection``、target digest、source revision 或
    grant phase 等 caller claims。这些字段只能从同一 live
    ``_SqlAlchemyTargetMaintenanceLease`` 的 target catalog、已冻结 admission 与实际
    ``PublishedAlembicAuthority`` 派生。``caller_claims`` 仅用于把旧式裸连接调用
    转换为稳定不变量错误，不会被解析或采信。

    Args:
        expected_target_revision: 本次官方 Alembic 命令必须精确到达的已发布 revision。
        lease: 已持 management→target→schema 锁的精确 concrete target lease。
        **caller_claims: 旧式自报字段；任一字段存在都立即拒绝。

    Returns:
        绑定同一 Connection、target identity、source/target revision 与 baseline
        grant lifecycle 的不可拆分 token。

    Raises:
        AlembicMigrationInvariantError: lease 类型/状态、发布链、admission 或 target 不合法。
    """
    if caller_claims:
        _fail()

    # 局部 import 避免 ``database_maintenance`` 在模块加载时反向导入本模块；
    # exact concrete type 是 capability 边界，不接受实现同名方法的 duck object。
    from ai_employee.infrastructure.db.database_maintenance import (
        _SqlAlchemyTargetMaintenanceLease,
        assert_ordinary_migration_admission,
    )

    if (
        type(lease) is not _SqlAlchemyTargetMaintenanceLease
        or lease._schema_lock_held is not True
        or lease._owner_transaction_active is not False
        or lease._migration_admitted_revision is None
    ):
        _fail()
    connection = lease._connection
    published_authority = lease._context._published_authority
    current = _exact_revision(lease._migration_admitted_revision)
    target = _exact_revision(expected_target_revision)
    if (
        not isinstance(connection, Connection)
        or type(published_authority) is not PublishedAlembicAuthority
        or lease.revision != current
        or not published_authority.contains(current)
        or not published_authority.contains(target)
    ):
        _fail()
    observed_revision, state = lease._read_stable_admission(require_revision=current)
    assert_ordinary_migration_admission(state)
    if observed_revision != current:
        _fail()

    target_digest = _exact_target_identity_digest(lease._target.identity.digest_hex)
    # 普通 migration 只能从 pristine/completed idle 的 baseline posture 发行；
    # phase 不再由 caller 传入，避免把 active inventory 伪装成稳定迁移。
    phase = GrantPhase.BASELINE
    binding = object()
    lifecycle = MigrationGrantLifecycle(
        connection=connection,
        authority=published_authority,
        expected_current_revision=current,
        expected_target_revision=target,
        phase=phase,
        _online_authority_binding=binding,
    )
    return OnlineMigrationAuthority(
        connection=connection,
        published_authority=published_authority,
        expected_target_identity_digest=target_digest,
        expected_current_revision=current,
        expected_target_revision=target,
        phase=phase,
        grant_lifecycle=lifecycle,
        _binding=binding,
        _issuer=_ONLINE_AUTHORITY_ISSUER,
    )


def bind_alembic_connection(
    config: Config,
    authority: OnlineMigrationAuthority,
) -> None:
    """把一个已发行 token 的全部字段原子绑定到 Alembic Config。

    裸 ``Connection``、target revision 或调用方自造 lifecycle 均不再是可接受输入；此
    函数只投影 token，env.py 随后会从当前 ScriptDirectory 重建发布链并逐项比较。
    """
    if not isinstance(config, Config) or type(authority) is not OnlineMigrationAuthority:
        _fail()
    # ``__post_init__`` 已验证 issuer 与 lifecycle binding；再次访问完整字段可防止未来
    # refactor 把 Config 投影降级为只含 Connection 的宽松形状。
    authority.__post_init__()
    config.attributes.update(
        {
            "connection": authority.connection,
            "published_authority": authority.published_authority,
            "expected_published_head_revision": authority.published_authority.head_revision,
            "expected_target_identity_digest": authority.expected_target_identity_digest,
            "expected_current_revision": authority.expected_current_revision,
            "expected_target_revision": authority.expected_target_revision,
            "online_migration_authority": authority,
            "migration_grant_lifecycle": authority.grant_lifecycle,
        }
    )


def require_online_migration_authority(config: Config) -> OnlineMigrationAuthority:
    """在 Alembic configure/SQL 前验证不可缺省的完整 online capability。"""
    if not isinstance(config, Config):
        _fail()
    token = config.attributes.get("online_migration_authority")
    connection = config.attributes.get("connection")
    published = config.attributes.get("published_authority")
    expected_head = config.attributes.get("expected_published_head_revision")
    expected_digest = config.attributes.get("expected_target_identity_digest")
    expected_current = config.attributes.get("expected_current_revision")
    expected_target = config.attributes.get("expected_target_revision")
    lifecycle = config.attributes.get("migration_grant_lifecycle")
    if type(token) is not OnlineMigrationAuthority:
        _fail()
    token.__post_init__()
    if (
        connection is not token.connection
        or published != token.published_authority
        or expected_head != token.published_authority.head_revision
        or expected_digest != token.expected_target_identity_digest
        or expected_current != token.expected_current_revision
        or expected_target != token.expected_target_revision
        or lifecycle is not token.grant_lifecycle
    ):
        _fail()
    actual = load_published_alembic_authority(config)
    if actual != token.published_authority or actual.head_revision != expected_head:
        _fail()
    return token


def run_alembic_upgrade_on_connection(
    authority: OnlineMigrationAuthority,
    *,
    config_path: Path,
) -> None:
    """只在调用方已取得 admission 与锁的 target Connection 上升级到发布 head。

    lifecycle orchestration 负责 management→target→schema locks、恢复事实与访问姿态校验；
    本函数重新从 script directory 派生实际 head，绑定同一 live Connection，并且不暴露
    revision、stamp、downgrade 或 repair 参数。

    Args:
        authority: 已由 typed lifecycle lease 发行且绑定目标身份/当前 revision 的 token。
        config_path: 仓库内固定的 Alembic 配置路径，不得来自公开 CLI 参数。
    """
    if not isinstance(config_path, Path):
        _fail()
    config = Config(config_path)
    if type(authority) is not OnlineMigrationAuthority:
        _fail()
    actual = load_published_alembic_authority(config)
    if (
        authority.published_authority != actual
        or authority.expected_target_revision != actual.head_revision
    ):
        _fail()
    bind_alembic_connection(config, authority)
    command.upgrade(config, "head")
