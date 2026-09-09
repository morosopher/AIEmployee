"""冻结 database restore catalog/current-setting 与 v4 call phase-shape parser。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from hashlib import sha256
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import dialect

from ai_employee.infrastructure.db.database_access import (
    APP_RUNTIME_ROLE_NAME,
    RETENTION_RUNTIME_ROLE_NAME,
    DatabaseAccessSnapshot,
    DatabaseAclProfile,
    DatabaseAclTuple,
    RuntimeRoleSnapshot,
)

ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
TARGET_DIGEST = "a" * 64
SOURCE_DIGEST = "b" * 64
EXPECTED_FINGERPRINT = "c" * 64
OBSERVED_FINGERPRINT = "d" * 64
PRE_FINGERPRINT = "e" * 64
GATE_ESTABLISHED_AT = "2026-08-11T01:02:03.123456Z"
CALL_STARTED_AT = "2026-08-11T01:02:04.123456Z"
BACKEND_STARTED_AT = "2026-08-11T01:02:05.123456Z"
COMPLETED_AT = "2026-08-11T01:03:04.654321Z"
COMPLETION_AUTHORITY_DOMAIN = b"ai_employee.restore_completion_authority.v1\0"


def test_completion_metadata_rejects_partial_or_inexact_post_authority() -> None:
    """审计只能从完整completed call构造；非完成phase或观察值漂移均无插入授权。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    assert hasattr(module, "restore_completion_metadata"), (
        "restore has no full-call audit validator"
    )
    with pytest.raises(module.DatabaseMaintenanceInvariantError):
        module.restore_completion_metadata(
            "|".join(_call_fields("restore_succeeded", "real_observed"))
        )


def test_real_target_lease_owns_private_restore_and_backup_lifetimes() -> None:
    """新入口必须扩展既有真实lease，而不是另建锁/session或接受外部anchor。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    assert hasattr(module._SqlAlchemyTargetMaintenanceLease, "restore_holder"), (
        "restore has no same-holder session lifetime"
    )
    assert hasattr(module._SqlAlchemyTargetMaintenanceLease, "backup_holder"), (
        "backup has no lifecycle-bound lock holder"
    )


class _BackupCleanupConnection:
    """在确定的rollback/unlock边界注入错误，不模拟权限、catalog或其他数据库职责。"""

    def __init__(self, cleanup_step: str | None, failure: RuntimeError) -> None:
        self.cleanup_step = cleanup_step
        self.failure = failure
        self.transaction_active = False
        self.cleanup_calls: list[str] = []

    def execute(self, statement: object) -> None:
        """只接受两个实际scope的固定acquire SQL，保持真实autobegin清理顺序。"""
        assert str(statement) in {
            "SELECT pg_advisory_lock(20260806, 274)",
            "SELECT pg_advisory_lock_shared(20260806,143)",
        }
        self.transaction_active = True

    def in_transaction(self) -> bool:
        """公开当前模拟事务状态，让生产commit/rollback helper按原条件执行。"""
        return self.transaction_active

    def commit(self) -> None:
        """结束一次读事务；测试在进入主体时清空acquire阶段记录。"""
        self.cleanup_calls.append("commit")
        self.transaction_active = False

    def rollback(self) -> None:
        """确定性模拟清理rollback失效，异常对象身份由测试独立持有。"""
        self.cleanup_calls.append("rollback")
        if self.cleanup_step == "rollback":
            raise self.failure
        self.transaction_active = False

    def scalar(self, statement: object) -> bool:
        """只允许两个scope自己的固定unlock；失败不伪造成功释放或隐式重试。"""
        assert str(statement) in {
            "SELECT pg_advisory_unlock(20260806, 274)",
            "SELECT pg_advisory_unlock_shared(20260806,143)",
        }
        self.cleanup_calls.append("unlock")
        if self.cleanup_step == "unlock":
            raise self.failure
        self.transaction_active = True
        return True


@pytest.mark.parametrize("scope", ("backup", "schema"))
@pytest.mark.parametrize("primary_kind", ("error", "cancel", None))
@pytest.mark.parametrize("cleanup_step", ("rollback", "unlock", None))
def test_backup_scopes_preserve_first_failure_and_invalidate_flags(
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    primary_kind: str | None,
    cleanup_step: str | None,
) -> None:
    """主体错误/取消与清理双重失败时保留同一个首异常；所有退出均撤销本地scope标记。

    只替换外部物理连接建立和先验admission，两个真实contextmanager及其事务helper
    全部执行。若恢复裸finally，八个双重失败参数会观测到cleanup覆盖主体异常；无主体
    错误和无cleanup错误的参数是既有行为控制，不能把它们算作新的行为RED。
    """
    import ai_employee.infrastructure.db.database_maintenance as module

    cleanup_failure = RuntimeError("synthetic backup cleanup failure")
    connection = _BackupCleanupConnection(cleanup_step, cleanup_failure)
    target = cast(
        module._SqlAlchemyTargetMaintenanceLease,
        SimpleNamespace(
            _connection=cast(Connection, connection),
            _live=True,
            _restore_claim=None,
            _backup_lock_held=False,
            _schema_lock_held=False,
            _schema_shared=False,
        ),
    )
    holders: list[module.BackupMaintenanceHolder] = []

    def initialize(holder, lease) -> None:
        """注入连接边界而保留实际BackupMaintenanceHolder及它拥有的scope行为。"""
        holder._target = lease
        holder.connection = cast(Connection, connection)
        holder._live = True
        holders.append(holder)

    def assert_live(holder) -> None:
        """仅模拟已取得的外部锁证明，本地holder失效后仍拒绝重用。"""
        assert holder._live and target._live

    monkeypatch.setattr(module._OperationsHolder, "__init__", initialize)
    monkeypatch.setattr(module._OperationsHolder, "assert_live", assert_live)
    monkeypatch.setattr(module.ReadOnlyMaintenanceHolder, "assert_idle", assert_live)
    if scope == "backup":
        context = module._SqlAlchemyTargetMaintenanceLease.backup_holder(target)
    else:
        target._backup_lock_held = True
        context = module.BackupMaintenanceHolder(target).shared_schema()
    primary: BaseException | None = (
        asyncio.CancelledError("synthetic backup cancellation")
        if primary_kind == "cancel"
        else RuntimeError("synthetic backup body failure")
        if primary_kind == "error"
        else None
    )
    observed: BaseException | None = None
    try:
        with context:
            assert target._backup_lock_held
            assert target._schema_lock_held is (scope == "schema")
            connection.cleanup_calls.clear()
            connection.transaction_active = True
            if primary is not None:
                raise primary
    except (RuntimeError, asyncio.CancelledError) as failure:
        observed = failure

    assert len(holders) == 1
    if scope == "backup":
        assert holders[0]._live is False and target._backup_lock_held is False
    else:
        assert target._schema_lock_held is False and target._schema_shared is False
    expected_calls = {
        "rollback": ["rollback"],
        "unlock": ["rollback", "unlock"],
        None: ["rollback", "unlock", "commit"],
    }
    assert connection.cleanup_calls == expected_calls[cleanup_step]
    expected = primary if primary is not None else cleanup_failure if cleanup_step else None
    assert observed is expected
    if primary is not None and cleanup_step is not None:
        assert observed.__cause__ is cleanup_failure


def test_restore_target_acquisition_does_not_read_revision_before_backend_exit_proof() -> None:
    """普通target acquire会读revision；restore必须有使用同一锁而延后此读的typed分支。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    assert hasattr(module._SqlAlchemyManagementLifecycleLease, "acquire_restore_target_lock"), (
        "ordinary target acquisition reads revision before restore backend exit proof"
    )


class _FakeRestoreMappingResult:
    """提供 restore-fact reader 所需的 buffered mapping 行。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        """返回独立列表，防止 production reader 修改测试输入。"""
        return list(self._rows)


class _FakeRestoreResult:
    """模拟同步 SQLAlchemy ``Result.mappings()``。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeRestoreMappingResult:
        """返回只支持 ``all`` 的最小 mapping result。"""
        return _FakeRestoreMappingResult(self._rows)


class _FakeRestoreConnection:
    """记录唯一只读 SQL 调用并返回预置 catalog/current-setting 行。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _FakeRestoreResult:
        """保存 SQL 与参数，不实现 mutation 或 transaction API。"""
        self.calls.append((str(statement), {} if parameters is None else dict(parameters)))
        return _FakeRestoreResult(self._rows)


class _HolderGucConnection:
    """忠实模拟PG只在自身ALTER DATABASE SET时注册session占位且回滚不撤销注册。"""

    def __init__(self) -> None:
        self.catalog: dict[str, str] = {}
        self.session: list[str | None] = [None, None, None]
        self.dialect = dialect()
        self.statements: list[str] = []
        self.after_set: Callable[[], None] = lambda: None

    def in_transaction(self) -> bool:
        """此Fake不承担事务授权；只测试实际holder在单条SET两侧的检查。"""
        return False

    def execute(self, statement: object, parameters: dict[str, object] | None = None) -> object:
        """保留catalog/session各自状态；只有已执行SET才允许本键NULL注册为空字符串。"""
        from ai_employee.infrastructure.db.database_maintenance import (
            _RESTORE_FACT_KEYS,
            DATABASE_RESTORE_FACTS_SQL,
        )

        sql = str(statement)
        self.statements.append(sql)
        if sql == DATABASE_RESTORE_FACTS_SQL:
            return _FakeRestoreResult(
                [
                    _restore_fact_row(
                        setconfig=[f"{key}={value}" for key, value in self.catalog.items()] or None,
                        setrole=0 if self.catalog else None,
                        current_gate=self.session[0],
                        current_call=self.session[1],
                        current_completion=self.session[2],
                    )
                ]
            )
        if sql.startswith("SELECT current_setting("):
            return SimpleNamespace(one=lambda: tuple(self.session))
        if sql.startswith("ALTER DATABASE"):
            if " RESET " in sql:
                key = sql.split(" RESET ", 1)[1]
                self.catalog.pop(key, None)
            else:
                key, value = sql.split(" SET ", 1)[1].split(" TO ", 1)
                self.catalog[key] = value[1:-1].replace("''", "'")
                slot = _RESTORE_FACT_KEYS.index(key)
                if self.session[slot] is None:
                    self.session[slot] = ""
                self.after_set()
            return SimpleNamespace()
        raise AssertionError("unexpected holder SQL")


def _guc_holder(monkeypatch: pytest.MonkeyPatch, connection: _HolderGucConnection):
    """只替换锁/catalog外围依赖，实际constructor/read_facts/_set_facts保持原实现。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    target = SimpleNamespace(
        _schema_lock_held=True,
        _schema_shared=False,
        _target=SimpleNamespace(
            database_oid=1,
            database_name="synthetic_restore_test",
            identity=SimpleNamespace(digest_hex=TARGET_DIGEST),
        ),
    )

    def initialize(holder, lease):
        holder._target = lease
        holder.connection = cast(Connection, connection)
        holder._live = True

    def assert_live(holder):
        if not holder._live:
            raise module.DatabaseMaintenanceInvariantError("database_maintenance_invariant")

    monkeypatch.setattr(module._OperationsHolder, "__init__", initialize)
    monkeypatch.setattr(module._OperationsHolder, "assert_live", assert_live)
    monkeypatch.setattr(module._OperationsHolder, "_access", lambda _: None)
    return module.RestoreMaintenanceHolder(
        cast(module._SqlAlchemyTargetMaintenanceLease, target),
        module.RestoreClaim(
            "generic",
            SOURCE_DIGEST,
            module.RestoreFingerprint("20260809_0018", EXPECTED_FINGERPRINT),
        ),
    )


def test_holder_own_guc_registration_survives_rollback_and_completes_all_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gate/call/completion逐键首次SET及RESET可以闭环，回滚仅回退catalog。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    connection = _HolderGucConnection()
    holder = _guc_holder(monkeypatch, connection)
    pristine = module.DatabaseRestoreFacts(None, None, None)
    active = module.DatabaseRestoreFacts(_gate(), _call("gate_established", "empty"), None)
    holder._set_facts(pristine, active)
    assert connection.session == ["", "", None]
    assert holder._session_anchor == pristine
    # PostgreSQL回滚保留session注册；同一物理holder不能重新把它猜成初始NULL。
    connection.catalog.clear()
    assert holder.read_facts() == pristine
    holder._set_facts(pristine, active)
    completed_fields = _call_fields("completed", "direct_observed")
    completed_fields[19] = EXPECTED_FINGERPRINT
    completed_fields[21] = "0" * 64
    completed_fields[21] = sha256(
        COMPLETION_AUTHORITY_DOMAIN + "|".join(completed_fields).encode("ascii")
    ).hexdigest()
    completed_call = "|".join(completed_fields)
    completed = module.DatabaseRestoreFacts(
        None,
        completed_call,
        "restore_completion:v1:" + completed_call.split("|")[-1],
    )
    holder._set_facts(active, completed)
    assert connection.session == ["", "", ""]
    assert holder.read_facts() == completed
    assert holder._session_anchor == pristine
    # 新invocation仍以新session当前值严格对catalog，不继承旧holder的注册记录。
    connection.session = [None, completed_call, completed.restore_completion]
    fresh = _guc_holder(monkeypatch, connection)
    assert fresh.read_facts() == completed


@pytest.mark.parametrize("mutation", ("wrong_key", "nonempty", "uncertain"))
def test_holder_rejects_unproven_set_effect_and_invalidates(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """SET之后另一键注册、非空覆盖或statement未知都永久失效，不刷新原anchor。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    connection = _HolderGucConnection()
    holder = _guc_holder(monkeypatch, connection)
    pristine = module.DatabaseRestoreFacts(None, None, None)

    def interfere() -> None:
        if mutation == "wrong_key":
            connection.session[2] = ""
        elif mutation == "nonempty":
            connection.session[0] = "session_override"
        else:
            raise OSError("synthetic uncertain statement")

    connection.after_set = interfere
    with pytest.raises((module.DatabaseMaintenanceInvariantError, OSError)):
        holder._set_facts(
            pristine,
            module.DatabaseRestoreFacts(_gate(), _call("gate_established", "empty"), None),
        )
    assert holder._live is False
    assert holder._session_anchor == pristine


def test_holder_unsolicited_registration_is_rejected_before_any_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未经本holder SET产生的空占位与普通reader一样拒绝，且没有目录写入。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    connection = _HolderGucConnection()
    holder = _guc_holder(monkeypatch, connection)
    connection.session[0] = ""
    with pytest.raises(module.DatabaseMaintenanceInvariantError):
        holder._set_facts(
            module.DatabaseRestoreFacts(None, None, None),
            module.DatabaseRestoreFacts(_gate(), _call("gate_established", "empty"), None),
        )
    assert not any(sql.startswith("ALTER DATABASE") for sql in connection.statements)
    assert holder._live is False


@pytest.mark.parametrize(
    "release", ("_release_single_session_lock", "_release_schema_session_lock")
)
def test_lock_cleanup_never_reconnects_an_invalidated_owner(release: str) -> None:
    """ACK未知后清理不得在invalidated SQLAlchemy Connection上执行SQL并静默重连。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    statements: list[str] = []
    connection = SimpleNamespace(
        closed=False,
        invalidated=True,
        in_transaction=lambda: False,
        execute=lambda sql, parameters: (
            statements.append(str(sql)) or SimpleNamespace(scalar_one=lambda: True)
        ),
    )
    kwargs = {"lock_key": 17} if release == "_release_single_session_lock" else {}
    with pytest.raises(module.DatabaseMaintenanceInvariantError):
        getattr(module, release)(cast(Connection, connection), **kwargs)
    assert statements == []


def test_completion_ack_unknown_is_a_typed_fresh_holder_reconcile_boundary() -> None:
    """最终commit与body失败必须分开；未知ACK只能把精确前后tuple交给新holder核对。"""
    import ai_employee.cli.database_maintenance as cli
    import ai_employee.infrastructure.db.database_maintenance as module

    assert hasattr(module, "RestoreCompletionAckUnknown"), "restore completion loses ACK authority"
    assert hasattr(module.RestoreMaintenanceHolder, "reconcile_completion_ack"), (
        "completion ACK has no exact fresh-holder predicate"
    )
    assert hasattr(cli, "run_restore_maintenance"), (
        "restore bypasses the common CLI lifecycle/ACK coordinator"
    )


def _completion_pair():
    """生成exact-post direct-call的合法最终提交前后tuple；没有外部文件或凭据。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    fields = _call_fields("reopen_committing", "direct_observed")
    fields[19] = EXPECTED_FINGERPRINT
    before = module.DatabaseRestoreFacts(_gate(), "|".join(fields), None)
    fields[9], fields[20], fields[21] = "completed", COMPLETED_AT, "0" * 64
    fields[21] = sha256(COMPLETION_AUTHORITY_DOMAIN + "|".join(fields).encode("ascii")).hexdigest()
    return before, module.DatabaseRestoreFacts(
        None, "|".join(fields), "restore_completion:v1:" + fields[21]
    )


@pytest.mark.parametrize("cancelled", (False, True))
def test_completion_commit_failure_invalidates_physical_owner_and_preserves_cancellation(
    cancelled: bool,
) -> None:
    """COMMIT之后断链与取消均关闭原session；取消不能被伪装成普通ACK重试结果。"""
    import asyncio

    import ai_employee.infrastructure.db.database_maintenance as module

    holder = object.__new__(module.RestoreMaintenanceHolder)
    invalidations: list[bool] = []
    holder.connection = cast(
        Connection, SimpleNamespace(invalidate=lambda: invalidations.append(True))
    )
    holder._live = True
    failure = asyncio.CancelledError() if cancelled else OSError("synthetic commit ACK loss")

    def commit() -> None:
        raise failure

    before, completed = _completion_pair()
    with pytest.raises(
        asyncio.CancelledError if cancelled else module.RestoreCompletionAckUnknown
    ) as caught:
        holder._commit_completion(SimpleNamespace(commit=commit), before, completed)
    assert invalidations == [True] and holder._live is False
    if cancelled:
        assert caught.value is failure
    else:
        assert caught.value.before == before and caught.value.completed == completed


@pytest.mark.parametrize("outcome", ("completed", "before", "other"))
def test_fresh_ack_reconcile_never_replays_verifier_or_stream(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """完整admission后的tuple才判定ACK；已完成零迁移，未应用只落独立reopenphase。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    before, completed = _completion_pair()
    unknown = module.RestoreCompletionAckUnknown(before, completed)
    holder = object.__new__(module.RestoreMaintenanceHolder)
    observed = completed if outcome == "completed" else before
    if outcome == "other":
        observed = module.DatabaseRestoreFacts(None, None, None)
    transitions: list[tuple[object, object]] = []
    projections: list[str] = []
    monkeypatch.setattr(holder, "admit", lambda: observed)
    monkeypatch.setattr(
        holder,
        "_transition",
        lambda old, new, evidence: transitions.append((old, new)) or new,
    )
    evidence = SimpleNamespace(
        publish=lambda facts, result: projections.append(result),
        archive=lambda facts: "synthetic-archive",
    )
    result = holder.reconcile_completion_ack(unknown, evidence)
    assert (
        result
        == {
            "completed": "restore_completed",
            "before": "restore_reopen_not_applied",
            "other": "restore_needs_attention",
        }[outcome]
    )
    if outcome == "before":
        assert len(transitions) == 1 and transitions[0][0] == before
        assert holder._call(transitions[0][1]).phase.value == "reopen_not_applied"
    else:
        assert transitions == []
    assert projections == (["restore_completed"] if outcome == "completed" else [])


def test_owned_cleanup_keeps_first_completion_failure() -> None:
    """schema/target退出另有异常也不替换最早ACK未知对象，防止CLI丢失新session核对入口。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    before, completed = _completion_pair()
    first = module.RestoreCompletionAckUnknown(before, completed)

    def cleanup() -> None:
        raise OSError("synthetic close failure")

    with (
        pytest.raises(module.RestoreCompletionAckUnknown) as caught,
        module._preserving_owned_cleanup(cleanup),
    ):
        raise first
    assert caught.value is first and isinstance(caught.value.__cause__, OSError)


def test_registered_live_backend_blocks_revision_read_without_owned_unfed_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ready/started/unknown的revision核对先证明登记backend退出，不能靠旧target revision。"""
    import ai_employee.infrastructure.db.database_maintenance as module

    holder = object.__new__(module.RestoreMaintenanceHolder)
    holder.connection = cast(Connection, SimpleNamespace())
    holder._target = SimpleNamespace(_context=SimpleNamespace(_published_authority=None))
    facts = module.DatabaseRestoreFacts(_gate(), _call("restore_started", "backend"), None)
    reads: list[str] = []
    monkeypatch.setattr(holder, "_backend_exited", lambda call: False)

    def read_revision(*args, **kwargs):
        reads.append("revision")
        raise AssertionError("registered backend has not exited")

    monkeypatch.setattr(module, "_read_current_revision", read_revision)
    with pytest.raises(module.DatabaseMaintenanceInvariantError):
        holder._verify_inventory(facts)
    assert reads == []


def test_catalog_transition_proves_existing_grants_before_first_authority_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """grants漂移应产生零ALTER尝试，而非先写authority后依赖rollback掩盖错误。"""
    from contextlib import nullcontext

    import ai_employee.infrastructure.db.database_maintenance as module

    holder = object.__new__(module.RestoreMaintenanceHolder)
    holder.connection = cast(
        Connection, SimpleNamespace(in_transaction=lambda: False, begin=nullcontext)
    )
    facts = module.DatabaseRestoreFacts(_gate(), _call("gate_established", "empty"), None)
    writes: list[object] = []

    def refuse(*args, **kwargs):
        raise module.DatabaseMaintenanceInvariantError("synthetic grant drift")

    monkeypatch.setattr(holder, "_verify_inventory", refuse)
    monkeypatch.setattr(holder, "_set_facts", lambda *args: writes.append(args))
    with pytest.raises(module.DatabaseMaintenanceInvariantError):
        holder._transition(facts, facts, SimpleNamespace())
    assert writes == []


def _restore_fact_row(
    *,
    setconfig: list[str] | None,
    setrole: int | None = 0,
    current_gate: object = None,
    current_call: object = None,
    current_completion: object = None,
) -> dict[str, object]:
    """构造 catalog row 与同 session 三个 ``current_setting`` 观察值。"""
    return {
        "setrole": setrole,
        "setconfig": setconfig,
        "current_maintenance_gate": current_gate,
        "current_restore_call_authority": current_call,
        "current_restore_completion": current_completion,
    }


def _safe_role(name: str, oid: int) -> RuntimeRoleSnapshot:
    """构造 parser admission 使用的 exact safe runtime role。"""
    return RuntimeRoleSnapshot(
        role_name=name,
        role_oid=oid,
        can_login=True,
        inherits=True,
        is_superuser=False,
        can_create_database=False,
        can_create_role=False,
        can_replicate=False,
        bypasses_rls=False,
        connection_limit=-1,
        valid_until=None,
        config=None,
    )


def _access_snapshot(profile: DatabaseAclProfile) -> DatabaseAccessSnapshot:
    """构造 active/baseline admission 的完整 database ACL 与角色快照。"""
    owner_oid = 101
    app_oid = 201
    retention_oid = 202
    tuples = [
        DatabaseAclTuple(owner_oid, owner_oid, "CREATE", False),
        DatabaseAclTuple(owner_oid, owner_oid, "CONNECT", False),
        DatabaseAclTuple(owner_oid, owner_oid, "TEMPORARY", False),
    ]
    if profile is DatabaseAclProfile.BASELINE:
        tuples.extend(
            (
                DatabaseAclTuple(app_oid, owner_oid, "CONNECT", False),
                DatabaseAclTuple(retention_oid, owner_oid, "CONNECT", False),
            )
        )
    return DatabaseAccessSnapshot(
        target_database_oid=401,
        owner_oid=owner_oid,
        acl_profile=profile,
        acl_tuples=tuple(sorted(tuples)),
        app_role=_safe_role(APP_RUNTIME_ROLE_NAME, app_oid),
        retention_role=_safe_role(RETENTION_RUNTIME_ROLE_NAME, retention_oid),
        memberships=(),
    )


def _base_call_fields(phase: str) -> list[str]:
    """按冻结字段顺序构造尚未填入 phase-dependent facts 的 v4 authority。"""
    return [
        "restore-call:v4",
        ATTEMPT_ID,
        "generic",
        TARGET_DIGEST,
        SOURCE_DIGEST,
        SOURCE_DIGEST,
        "-",
        "0",
        "0",
        phase,
        GATE_ESTABLISHED_AT,
        "-",
        "-",
        "-",
        "-",
        "-",
        "20260809_0018",
        EXPECTED_FINGERPRINT,
        "-",
        "-",
        "-",
        "-",
    ]


def _call_fields(phase: str, shape: str) -> list[str]:
    """构造一个指定 phase 合法 shape，供正例与单字段负例共享。"""
    fields = _base_call_fields(phase)
    if shape in {"starting", "backend", "real_observed", "needs_starting", "needs_backend"}:
        fields[7] = "1"
        fields[11] = CALL_STARTED_AT
        fields[14] = "20260809_0018"
        fields[15] = PRE_FINGERPRINT
    if shape in {"backend", "real_observed", "needs_backend"}:
        fields[12] = "4321"
        fields[13] = BACKEND_STARTED_AT
    if shape in {"direct_observed", "real_observed", "needs_direct", "needs_real"}:
        fields[18] = "20260809_0018"
        fields[19] = OBSERVED_FINGERPRINT
    if shape in {"needs_real"}:
        fields[7] = "1"
        fields[11] = CALL_STARTED_AT
        fields[12] = "4321"
        fields[13] = BACKEND_STARTED_AT
        fields[14] = "20260809_0018"
        fields[15] = PRE_FINGERPRINT
    if phase in {"reopen_committing", "reopen_not_applied", "completed"} or shape in {
        "needs_direct",
        "needs_real",
    }:
        fields[8] = "1"
    if phase == "completed":
        fields[20] = COMPLETED_AT
        fields[21] = "0" * 64
        digest = sha256(COMPLETION_AUTHORITY_DOMAIN + "|".join(fields).encode("ascii")).hexdigest()
        fields[21] = digest
    return fields


def _call(phase: str, shape: str) -> str:
    """序列化一个测试用 canonical v4 call。"""
    return "|".join(_call_fields(phase, shape))


def _gate() -> str:
    """构造与测试 call 同 attempt/kind/target/source 绑定的 canonical gate。"""
    return f"restore:v1:{ATTEMPT_ID}:generic:{TARGET_DIGEST}:{SOURCE_DIGEST}"


def _transition_validator() -> Callable[[str, str], object]:
    """新 writer 复用唯一 parser；缺失时明确报告跨记录单调性契约尚未实现。"""
    import ai_employee.infrastructure.db.database_maintenance as maintenance

    validator = getattr(maintenance, "validate_restore_call_transition", None)
    assert callable(validator), "restore call CAS has no phase/ordinal/fact-preservation validator"
    return validator


def test_restore_retry_restarts_only_backend_facts_with_fresh_call_time() -> None:
    """合法 retry 增加 ordinal 并清空旧 backend；同 ordinal 和继承旧 PID 都拒绝。"""
    from ai_employee.infrastructure.db.database_maintenance import DatabaseMaintenanceInvariantError

    validate = _transition_validator()
    previous = _call_fields("restore_not_applied", "backend")
    following = previous.copy()
    following[7] = "2"
    following[9] = "restore_backend_starting"
    following[11] = "2026-08-11T01:03:05.123456Z"
    following[12] = following[13] = "-"
    validate("|".join(previous), "|".join(following))
    for index, value in ((7, "1"), (11, CALL_STARTED_AT), (12, "4321"), (15, "f" * 64)):
        invalid = following.copy()
        invalid[index] = value
        with pytest.raises(DatabaseMaintenanceInvariantError):
            validate("|".join(previous), "|".join(invalid))


@pytest.mark.parametrize("index", (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15, 16, 17))
def test_started_transition_preserves_every_already_frozen_fact(index: int) -> None:
    """ready→started 只改 phase；任何已冻结绑定/时间/pre/backend 改写都先于 CAS 失败。"""
    from ai_employee.infrastructure.db.database_maintenance import DatabaseMaintenanceInvariantError

    validate = _transition_validator()
    previous = _call_fields("restore_backend_ready", "backend")
    following = previous.copy()
    following[9] = "restore_started"
    validate("|".join(previous), "|".join(following))
    following[index] = "-" if following[index] != "-" else "f" * 64
    with pytest.raises(DatabaseMaintenanceInvariantError):
        validate("|".join(previous), "|".join(following))


def test_direct_exact_post_zero_ordinal_is_distinct_and_cannot_allocate_backend() -> None:
    """exact-post 直接边保持 call/backend/pre absent，且 observed 必须等于 manifest。"""
    from ai_employee.infrastructure.db.database_maintenance import DatabaseMaintenanceInvariantError

    validate = _transition_validator()
    previous = _base_call_fields("gate_established")
    following = _call_fields("restore_succeeded", "direct_observed")
    following[19] = EXPECTED_FINGERPRINT
    validate("|".join(previous), "|".join(following))
    following[19] = OBSERVED_FINGERPRINT
    with pytest.raises(DatabaseMaintenanceInvariantError):
        validate("|".join(previous), "|".join(following))


@pytest.mark.parametrize(
    ("previous_phase", "previous_shape", "following_phase", "following_shape"),
    (
        ("gate_established", "empty", "restore_started", "backend"),
        ("restore_backend_starting", "starting", "restore_not_applied", "backend"),
        ("restore_succeeded", "direct_observed", "completed", "direct_observed"),
        ("completed", "direct_observed", "reopen_committing", "direct_observed"),
        ("needs_attention", "needs_backend", "restore_backend_starting", "starting"),
        ("grants_succeeded", "direct_observed", "grants_succeeded", "direct_observed"),
    ),
)
def test_restore_call_rejects_jumps_terminal_exits_and_self_transitions(
    previous_phase: str,
    previous_shape: str,
    following_phase: str,
    following_shape: str,
) -> None:
    """冻结图没有的边即使两条 call 各自可解析，也不能成为写入 authority。"""
    from ai_employee.infrastructure.db.database_maintenance import DatabaseMaintenanceInvariantError

    validate = _transition_validator()
    with pytest.raises(DatabaseMaintenanceInvariantError):
        validate(_call(previous_phase, previous_shape), _call(following_phase, following_shape))


def test_restore_fact_reader_requires_catalog_and_current_setting_identity() -> None:
    """catalog absent 时同 session 三个 setting 也必须精确 absent。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DATABASE_RESTORE_FACTS_SQL,
        DatabaseRestoreFacts,
        read_database_restore_facts,
    )

    connection = _FakeRestoreConnection([_restore_fact_row(setrole=None, setconfig=None)])

    facts = read_database_restore_facts(
        cast(Connection, connection),
        target_database_oid=401,
    )

    assert facts == DatabaseRestoreFacts(None, None, None)
    assert connection.calls == [(DATABASE_RESTORE_FACTS_SQL, {"target_database_oid": 401})]
    executed_sql = connection.calls[0][0]
    assert executed_sql.count("current_setting(") == 3


@pytest.mark.parametrize(
    ("current_field", "catalog_key"),
    (
        pytest.param(
            "current_maintenance_gate",
            "ai_employee.maintenance_gate",
            id="maintenance-gate",
        ),
        pytest.param(
            "current_restore_call_authority",
            "ai_employee.restore_call_authority",
            id="restore-call",
        ),
        pytest.param(
            "current_restore_completion",
            "ai_employee.restore_completion",
            id="completion",
        ),
    ),
)
def test_restore_fact_reader_rejects_stale_current_setting(
    current_field: str,
    catalog_key: str,
) -> None:
    """catalog 已存在但当前 session 仍为旧值时必须在首写前拒绝。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        read_database_restore_facts,
    )

    row = _restore_fact_row(setconfig=[f"{catalog_key}=catalog-value"])
    row[current_field] = "stale-session-value"
    connection = _FakeRestoreConnection([row])

    with pytest.raises(
        DatabaseMaintenanceInvariantError,
        match=r"^database maintenance invariant violation$",
    ):
        read_database_restore_facts(
            cast(Connection, connection),
            target_database_oid=401,
        )


@pytest.mark.parametrize(
    "current_field",
    (
        "current_maintenance_gate",
        "current_restore_call_authority",
        "current_restore_completion",
    ),
)
def test_restore_fact_reader_rejects_pgoptions_session_override(
    current_field: str,
) -> None:
    """catalog absent 时，PGOPTIONS/session-local 注入的任一 authority 都必须拒绝。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        read_database_restore_facts,
    )

    row = _restore_fact_row(setrole=None, setconfig=None)
    row[current_field] = "session-injected-value"
    connection = _FakeRestoreConnection([row])

    with pytest.raises(
        DatabaseMaintenanceInvariantError,
        match=r"^database maintenance invariant violation$",
    ):
        read_database_restore_facts(
            cast(Connection, connection),
            target_database_oid=401,
        )


VALID_ACTIVE_PHASE_SHAPES = (
    ("gate_established", "gate"),
    ("restore_backend_starting", "starting"),
    ("restore_backend_ready", "backend"),
    ("restore_started", "backend"),
    ("restore_outcome_unknown", "backend"),
    ("restore_not_applied", "backend"),
    ("restore_succeeded", "direct_observed"),
    ("restore_succeeded", "real_observed"),
    ("grants_succeeded", "direct_observed"),
    ("grants_succeeded", "real_observed"),
    ("verified", "direct_observed"),
    ("verified", "real_observed"),
    ("reopen_committing", "direct_observed"),
    ("reopen_committing", "real_observed"),
    ("reopen_not_applied", "direct_observed"),
    ("reopen_not_applied", "real_observed"),
    ("needs_attention", "needs_starting"),
    ("needs_attention", "needs_backend"),
    ("needs_attention", "needs_direct"),
    ("needs_attention", "needs_real"),
)


@pytest.mark.parametrize(
    ("phase", "shape"),
    tuple(
        pytest.param(phase, shape, id=f"{phase}-{shape}")
        for phase, shape in VALID_ACTIVE_PHASE_SHAPES
    ),
)
def test_restore_call_v4_accepts_every_active_phase_shape(
    phase: str,
    shape: str,
) -> None:
    """每个 active graph phase 只以规格允许的 direct/real/predecessor shape admission。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseAdmissionState,
        DatabaseRestoreFacts,
        classify_database_admission,
    )

    state = classify_database_admission(
        facts=DatabaseRestoreFacts(_gate(), _call(phase, shape), None),
        access_snapshot=_access_snapshot(DatabaseAclProfile.ACTIVE),
        object_grants_match=True,
        expected_target_identity_digest=TARGET_DIGEST,
    )

    assert state is DatabaseAdmissionState.ACTIVE


@pytest.mark.parametrize(
    "shape",
    (
        pytest.param("direct_observed", id="direct-exact-post"),
        pytest.param("real_observed", id="real-call"),
    ),
)
def test_restore_call_v4_accepts_both_completed_shapes(shape: str) -> None:
    """completed 支持 ordinal-0 direct exact-post 与 ordinal>=1 real-call 两种 shape。"""
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseAdmissionState,
        DatabaseRestoreFacts,
        classify_database_admission,
    )

    fields = _call_fields("completed", shape)
    completion = f"restore_completion:v1:{fields[21]}"
    state = classify_database_admission(
        facts=DatabaseRestoreFacts(None, "|".join(fields), completion),
        access_snapshot=_access_snapshot(DatabaseAclProfile.BASELINE),
        object_grants_match=True,
        expected_target_identity_digest=TARGET_DIGEST,
    )

    assert state is DatabaseAdmissionState.COMPLETED_IDLE


def _set_field(index: int, value: str) -> Callable[[list[str]], None]:
    """返回只改一个 authority 字段的显式负例 mutator。"""

    def mutate(fields: list[str]) -> None:
        fields[index] = value

    return mutate


INVALID_PHASE_SHAPES = (
    ("gate_established", "gate", _set_field(7, "1"), "gate-call-ordinal"),
    ("gate_established", "gate", _set_field(11, CALL_STARTED_AT), "gate-call-start"),
    ("restore_backend_starting", "starting", _set_field(7, "0"), "starting-ordinal-zero"),
    ("restore_backend_starting", "starting", _set_field(11, "-"), "starting-call-start"),
    ("restore_backend_starting", "starting", _set_field(12, "4321"), "starting-backend"),
    ("restore_backend_starting", "starting", _set_field(14, "-"), "starting-pre"),
    ("restore_backend_ready", "backend", _set_field(12, "-"), "ready-backend-pid"),
    ("restore_backend_ready", "backend", _set_field(13, "-"), "ready-backend-start"),
    ("restore_started", "backend", _set_field(18, "20260809_0018"), "started-observed"),
    ("restore_outcome_unknown", "backend", _set_field(20, COMPLETED_AT), "unknown-completed"),
    ("restore_not_applied", "backend", _set_field(8, "1"), "not-applied-reopen"),
    ("restore_succeeded", "direct_observed", _set_field(18, "-"), "direct-observed"),
    ("restore_succeeded", "direct_observed", _set_field(14, "20260809_0018"), "direct-pre"),
    ("grants_succeeded", "real_observed", _set_field(11, "-"), "real-call-start"),
    ("grants_succeeded", "real_observed", _set_field(12, "0"), "real-backend-pid"),
    ("verified", "real_observed", _set_field(19, "-"), "observed-pair"),
    ("reopen_committing", "direct_observed", _set_field(8, "0"), "reopen-ordinal"),
    ("reopen_not_applied", "real_observed", _set_field(8, "0"), "reopen-not-applied"),
    ("needs_attention", "gate", lambda fields: None, "needs-gate-shape"),
    ("needs_attention", "needs_starting", _set_field(18, "20260809_0018"), "needs-mixed"),
    ("completed", "direct_observed", _set_field(8, "0"), "completed-reopen"),
    ("completed", "direct_observed", _set_field(20, "-"), "completed-at"),
    ("completed", "real_observed", _set_field(21, "f" * 64), "completed-digest"),
    ("unknown_phase", "gate", lambda fields: None, "unknown-phase"),
)


@pytest.mark.parametrize(
    ("phase", "shape", "mutate", "case_id"),
    tuple(
        pytest.param(phase, shape, mutate, case_id, id=case_id)
        for phase, shape, mutate, case_id in INVALID_PHASE_SHAPES
    ),
)
def test_restore_call_v4_rejects_every_off_matrix_phase_shape(
    phase: str,
    shape: str,
    mutate: Callable[[list[str]], None],
    case_id: str,
) -> None:
    """任一 phase-dependent dash/value/ordinal/PID/digest 偏差都返回内容安全错误。"""
    del case_id
    from ai_employee.infrastructure.db.database_maintenance import (
        DatabaseMaintenanceInvariantError,
        _parse_call_authority,
    )

    fields = _call_fields(phase, shape)
    mutate(fields)

    with pytest.raises(
        DatabaseMaintenanceInvariantError,
        match=r"^database maintenance invariant violation$",
    ):
        _parse_call_authority("|".join(fields))
