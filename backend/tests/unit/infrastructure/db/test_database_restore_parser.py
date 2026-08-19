"""冻结 database restore catalog/current-setting 与 v4 call phase-shape parser。"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from typing import cast

import pytest
from sqlalchemy import Connection

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
