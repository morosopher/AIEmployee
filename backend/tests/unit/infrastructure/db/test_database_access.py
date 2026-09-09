"""冻结数据库 ACL、运行时角色与 bootstrap 候选的只读闭集契约。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_employee.infrastructure.db.database_access import (
    APP_RUNTIME_ROLE_NAME,
    DATABASE_ACL_SQL,
    RETENTION_RUNTIME_ROLE_NAME,
    BootstrapCaller,
    BootstrapCandidate,
    DatabaseAccessInvariantError,
    DatabaseAccessMutationPlan,
    DatabaseAccessSnapshot,
    DatabaseAclAction,
    DatabaseAclProfile,
    DatabaseAclTuple,
    RoleMembershipTuple,
    RuntimeRoleAction,
    RuntimeRoleSnapshot,
    _nonnegative_int,
    _positive_int,
    bootstrap_candidate_to_baseline,
    classify_bootstrap_candidate,
    classify_database_acl_profile,
    read_database_access_snapshot,
    transition_active_to_baseline,
    transition_baseline_to_active,
)

EXPECTED_DATABASE_ACL_SQL = """SELECT acl.grantee, acl.grantor, acl.privilege_type, acl.is_grantable
FROM pg_database AS db
CROSS JOIN LATERAL aclexplode(
    COALESCE(db.datacl, acldefault('d', db.datdba))
) AS acl
WHERE db.oid = :target_database_oid"""

TARGET_DATABASE_OID = 401
OWNER_OID = 101
APP_ROLE_OID = 201
RETENTION_ROLE_OID = 202
OTHER_ROLE_OID = 303
MAX_POSTGRESQL_OID = (1 << 32) - 1


def _acl_tuple(
    grantee_oid: int,
    privilege_type: str,
    *,
    grantor_oid: int = OWNER_OID,
    is_grantable: bool = False,
) -> DatabaseAclTuple:
    """构造独立于生产分类器的 ACL tuple 测试输入。"""
    return DatabaseAclTuple(
        grantee_oid=grantee_oid,
        grantor_oid=grantor_oid,
        privilege_type=privilege_type,
        is_grantable=is_grantable,
    )


OWNER_ACL = (
    _acl_tuple(OWNER_OID, "CREATE"),
    _acl_tuple(OWNER_OID, "CONNECT"),
    _acl_tuple(OWNER_OID, "TEMPORARY"),
)
FRESH_DEFAULT_ACL = tuple(
    sorted(
        (
            *OWNER_ACL,
            _acl_tuple(0, "CONNECT"),
            _acl_tuple(0, "TEMPORARY"),
        )
    )
)
PRE_PROTOCOL_LEGACY_ACL = tuple(
    sorted(
        (
            *FRESH_DEFAULT_ACL,
            _acl_tuple(APP_ROLE_OID, "CONNECT"),
            _acl_tuple(RETENTION_ROLE_OID, "CONNECT"),
        )
    )
)
BASELINE_ACL = tuple(
    sorted(
        (
            *OWNER_ACL,
            _acl_tuple(APP_ROLE_OID, "CONNECT"),
            _acl_tuple(RETENTION_ROLE_OID, "CONNECT"),
        )
    )
)
ACTIVE_ACL = tuple(sorted(OWNER_ACL))

PROFILE_ACLS = {
    DatabaseAclProfile.FRESH_DEFAULT: FRESH_DEFAULT_ACL,
    DatabaseAclProfile.PRE_PROTOCOL_LEGACY: PRE_PROTOCOL_LEGACY_ACL,
    DatabaseAclProfile.BASELINE: BASELINE_ACL,
    DatabaseAclProfile.ACTIVE: ACTIVE_ACL,
}


def _safe_role(*, role_name: str, role_oid: int) -> RuntimeRoleSnapshot:
    """构造精确满足冻结 safe posture 的运行时角色。"""
    return RuntimeRoleSnapshot(
        role_name=role_name,
        role_oid=role_oid,
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


SAFE_APP_ROLE = _safe_role(role_name=APP_RUNTIME_ROLE_NAME, role_oid=APP_ROLE_OID)
SAFE_RETENTION_ROLE = _safe_role(
    role_name=RETENTION_RUNTIME_ROLE_NAME,
    role_oid=RETENTION_ROLE_OID,
)


def _snapshot(
    profile: DatabaseAclProfile,
    *,
    acl_tuples: tuple[DatabaseAclTuple, ...] | None = None,
    app_role: RuntimeRoleSnapshot | None = SAFE_APP_ROLE,
    retention_role: RuntimeRoleSnapshot | None = SAFE_RETENTION_ROLE,
    memberships: tuple[RoleMembershipTuple, ...] = (),
) -> DatabaseAccessSnapshot:
    """构造声明 profile 与完整 catalog 事实并存的合成快照。"""
    return DatabaseAccessSnapshot(
        target_database_oid=TARGET_DATABASE_OID,
        owner_oid=OWNER_OID,
        acl_profile=profile,
        acl_tuples=PROFILE_ACLS[profile] if acl_tuples is None else acl_tuples,
        app_role=app_role,
        retention_role=retention_role,
        memberships=memberships,
    )


def _classify_candidate(
    snapshot: DatabaseAccessSnapshot,
    *,
    caller: BootstrapCaller = BootstrapCaller.ROLE_BOOTSTRAP,
    restore_facts_absent: bool = True,
    has_active_non_owner_sessions: bool = False,
    object_grants_match: bool = True,
) -> BootstrapCandidate:
    """使用全部外部闭集证据调用唯一 bootstrap 分类边界。"""
    return classify_bootstrap_candidate(
        snapshot,
        caller=caller,
        restore_facts_absent=restore_facts_absent,
        has_active_non_owner_sessions=has_active_non_owner_sessions,
        object_grants_match=object_grants_match,
    )


def _assert_invariant(callable_: Callable[[], object]) -> None:
    """断言所有漂移只产生稳定、无内容的 typed invariant error。"""
    with pytest.raises(
        DatabaseAccessInvariantError,
        match=r"^database access invariant violation$",
    ):
        callable_()


def _owner_only_acl(owner_oid: int) -> tuple[DatabaseAclTuple, ...]:
    """按独立测试常量构造指定 owner 的 active ACL。"""
    return tuple(
        sorted(
            (
                DatabaseAclTuple(owner_oid, owner_oid, "CREATE", False),
                DatabaseAclTuple(owner_oid, owner_oid, "CONNECT", False),
                DatabaseAclTuple(owner_oid, owner_oid, "TEMPORARY", False),
            )
        )
    )


def test_database_acl_postgresql_oid_bounds_are_exact() -> None:
    """普通 OID 与 PUBLIC-capable grantee 必须使用 uint32 的精确闭区间。"""
    assert _positive_int(MAX_POSTGRESQL_OID) == MAX_POSTGRESQL_OID
    assert _nonnegative_int(0) == 0
    assert _nonnegative_int(MAX_POSTGRESQL_OID) == MAX_POSTGRESQL_OID

    for invalid_oid in (0, MAX_POSTGRESQL_OID + 1, -1, True):
        _assert_invariant(lambda invalid_oid=invalid_oid: _positive_int(invalid_oid))
    for invalid_grantee_oid in (MAX_POSTGRESQL_OID + 1, -1, True):
        _assert_invariant(
            lambda invalid_grantee_oid=invalid_grantee_oid: _nonnegative_int(invalid_grantee_oid)
        )


def test_database_acl_postgresql_oid_max_is_accepted_across_closed_paths() -> None:
    """uint32 最大值可作为 owner、runtime role 与 target OID 进入合法闭集。"""
    assert (
        classify_database_acl_profile(
            owner_oid=MAX_POSTGRESQL_OID,
            app_role_oid=APP_ROLE_OID,
            retention_role_oid=RETENTION_ROLE_OID,
            acl_tuples=_owner_only_acl(MAX_POSTGRESQL_OID),
        )
        is DatabaseAclProfile.ACTIVE
    )

    max_role_snapshot = _snapshot(
        DatabaseAclProfile.FRESH_DEFAULT,
        app_role=replace(SAFE_APP_ROLE, role_oid=MAX_POSTGRESQL_OID),
    )
    assert _classify_candidate(max_role_snapshot).roles_exist is True

    max_target_snapshot = replace(
        _snapshot(DatabaseAclProfile.BASELINE),
        target_database_oid=MAX_POSTGRESQL_OID,
    )
    assert (
        transition_baseline_to_active(max_target_snapshot).target_profile
        is DatabaseAclProfile.ACTIVE
    )


@pytest.mark.parametrize(
    "operation",
    (
        pytest.param(
            lambda: classify_database_acl_profile(
                owner_oid=MAX_POSTGRESQL_OID + 1,
                app_role_oid=APP_ROLE_OID,
                retention_role_oid=RETENTION_ROLE_OID,
                acl_tuples=_owner_only_acl(MAX_POSTGRESQL_OID + 1),
            ),
            id="classifier-owner-overflow",
        ),
        pytest.param(
            lambda: _classify_candidate(
                _snapshot(
                    DatabaseAclProfile.FRESH_DEFAULT,
                    app_role=replace(
                        SAFE_APP_ROLE,
                        role_oid=MAX_POSTGRESQL_OID + 1,
                    ),
                )
            ),
            id="candidate-role-overflow",
        ),
        pytest.param(
            lambda: transition_baseline_to_active(
                replace(
                    _snapshot(DatabaseAclProfile.BASELINE),
                    target_database_oid=MAX_POSTGRESQL_OID + 1,
                )
            ),
            id="planning-target-overflow",
        ),
    ),
)
def test_database_acl_postgresql_oid_overflow_rejects_closed_paths(
    operation: Callable[[], object],
) -> None:
    """classifier、candidate 与 planning 不能把 uint32 overflow 当作合法 OID。"""
    _assert_invariant(operation)


def test_database_acl_snapshot_shrinks_fields_before_sorting() -> None:
    """异常 dataclass 字段必须先收窄，不能从 canonical 排序逸出原生 TypeError。"""
    malformed_acl = (
        replace(
            BASELINE_ACL[0],
            grantee_oid=cast(int, "not-an-oid"),
        ),
        *BASELINE_ACL[1:],
    )
    snapshot = replace(
        _snapshot(DatabaseAclProfile.BASELINE),
        acl_tuples=malformed_acl,
    )

    _assert_invariant(lambda: transition_baseline_to_active(snapshot))


@pytest.mark.parametrize(
    ("profile", "acl_tuples"),
    tuple(PROFILE_ACLS.items()),
)
def test_database_acl_profiles_are_exact_sorted_multisets(
    profile: DatabaseAclProfile,
    acl_tuples: tuple[DatabaseAclTuple, ...],
) -> None:
    """四种合法 profile 必须由完整 multiset 唯一分类。"""
    assert (
        classify_database_acl_profile(
            owner_oid=OWNER_OID,
            app_role_oid=APP_ROLE_OID,
            retention_role_oid=RETENTION_ROLE_OID,
            acl_tuples=tuple(reversed(acl_tuples)),
        )
        is profile
    )
    assert all(tuple_.is_grantable is False for tuple_ in acl_tuples)


INVALID_ACL_CASES = (
    pytest.param(BASELINE_ACL + (_acl_tuple(APP_ROLE_OID, "CONNECT"),), id="duplicate-tuple"),
    pytest.param(BASELINE_ACL[:-1], id="missing-tuple"),
    pytest.param(
        tuple(sorted((*BASELINE_ACL, _acl_tuple(0, "CREATE")))),
        id="public-create",
    ),
    pytest.param(
        tuple(sorted((*BASELINE_ACL, _acl_tuple(APP_ROLE_OID, "CREATE")))),
        id="app-create",
    ),
    pytest.param(
        tuple(sorted((*BASELINE_ACL, _acl_tuple(APP_ROLE_OID, "TEMPORARY")))),
        id="app-temporary",
    ),
    pytest.param(
        tuple(sorted((*BASELINE_ACL, _acl_tuple(RETENTION_ROLE_OID, "CREATE")))),
        id="retention-create",
    ),
    pytest.param(
        tuple(sorted((*BASELINE_ACL, _acl_tuple(RETENTION_ROLE_OID, "TEMPORARY")))),
        id="retention-temporary",
    ),
    pytest.param(
        tuple(sorted((*BASELINE_ACL, _acl_tuple(OTHER_ROLE_OID, "CONNECT")))),
        id="extra-connect-grantee",
    ),
    pytest.param(
        tuple(sorted((*BASELINE_ACL, _acl_tuple(OTHER_ROLE_OID, "CREATE")))),
        id="unknown-grantee",
    ),
    pytest.param(
        tuple(sorted((*BASELINE_ACL, _acl_tuple(OWNER_OID, "USAGE")))),
        id="unknown-privilege",
    ),
    pytest.param(
        tuple(
            sorted(
                (
                    replace(BASELINE_ACL[0], grantor_oid=OTHER_ROLE_OID),
                    *BASELINE_ACL[1:],
                )
            )
        ),
        id="wrong-grantor",
    ),
    pytest.param(
        tuple(sorted((*FRESH_DEFAULT_ACL, _acl_tuple(APP_ROLE_OID, "CONNECT")))),
        id="partial-legacy-extra",
    ),
)


@pytest.mark.parametrize("acl_tuples", INVALID_ACL_CASES)
def test_database_acl_complete_multiset_rejects_every_drift(
    acl_tuples: tuple[DatabaseAclTuple, ...],
) -> None:
    """额外、缺失、重复、未知或错误 grantor 的 tuple 必须 fail closed。"""
    _assert_invariant(
        lambda: classify_database_acl_profile(
            owner_oid=OWNER_OID,
            app_role_oid=APP_ROLE_OID,
            retention_role_oid=RETENTION_ROLE_OID,
            acl_tuples=acl_tuples,
        )
    )


GRANT_OPTION_CASES = tuple(
    pytest.param(profile, index, id=f"{profile.value}-{index}")
    for profile, acl_tuples in PROFILE_ACLS.items()
    for index in range(len(acl_tuples))
)


@pytest.mark.parametrize(("profile", "index"), GRANT_OPTION_CASES)
def test_database_acl_any_grant_option_is_rejected(
    profile: DatabaseAclProfile,
    index: int,
) -> None:
    """包括 owner 在内的任一显式 grant option 都不是合法正规化结果。"""
    acl_tuples = list(PROFILE_ACLS[profile])
    acl_tuples[index] = replace(acl_tuples[index], is_grantable=True)

    _assert_invariant(
        lambda: classify_database_acl_profile(
            owner_oid=OWNER_OID,
            app_role_oid=APP_ROLE_OID,
            retention_role_oid=RETENTION_ROLE_OID,
            acl_tuples=tuple(acl_tuples),
        )
    )


@pytest.mark.parametrize(
    ("profile", "roles_exist"),
    (
        pytest.param(DatabaseAclProfile.FRESH_DEFAULT, False, id="fresh-roles-absent"),
        pytest.param(DatabaseAclProfile.FRESH_DEFAULT, True, id="fresh-roles-safe"),
        pytest.param(DatabaseAclProfile.PRE_PROTOCOL_LEGACY, True, id="legacy-roles-safe"),
    ),
)
@pytest.mark.parametrize("caller", tuple(BootstrapCaller))
def test_bootstrap_candidate_accepts_only_three_role_acl_shapes(
    profile: DatabaseAclProfile,
    roles_exist: bool,
    caller: BootstrapCaller,
) -> None:
    """两个合法 caller 只能得到三种冻结 candidate，并返回准确 roles_exist。"""
    snapshot = _snapshot(
        profile,
        app_role=SAFE_APP_ROLE if roles_exist else None,
        retention_role=SAFE_RETENTION_ROLE if roles_exist else None,
    )

    candidate = _classify_candidate(snapshot, caller=caller)

    assert candidate == BootstrapCandidate(
        caller=caller,
        source_profile=profile,
        roles_exist=roles_exist,
        snapshot=snapshot,
    )


UNSAFE_ROLE_CHANGES = (
    pytest.param({"can_login": False}, id="nologin"),
    pytest.param({"inherits": False}, id="noinherit"),
    pytest.param({"is_superuser": True}, id="superuser"),
    pytest.param({"can_create_database": True}, id="createdb"),
    pytest.param({"can_create_role": True}, id="createrole"),
    pytest.param({"can_replicate": True}, id="replication"),
    pytest.param({"bypasses_rls": True}, id="bypassrls"),
    pytest.param({"connection_limit": 0}, id="connection-limit"),
    pytest.param(
        {"valid_until": datetime(2030, 1, 1, tzinfo=UTC)},
        id="valid-until",
    ),
    pytest.param({"config": ()}, id="empty-config"),
    pytest.param({"config": ("statement_timeout=1s",)}, id="nonempty-config"),
    pytest.param({"role_oid": 0}, id="invalid-oid"),
)


@pytest.mark.parametrize("slot", ("app_role", "retention_role"))
@pytest.mark.parametrize("changes", UNSAFE_ROLE_CHANGES)
def test_runtime_role_each_attribute_flip_rejects_bootstrap_candidate(
    slot: str,
    changes: dict[str, object],
) -> None:
    """任一 app/retention 属性偏离 safe posture 都必须在计划前拒绝。"""
    snapshot = _snapshot(DatabaseAclProfile.FRESH_DEFAULT)
    role = cast(RuntimeRoleSnapshot, getattr(snapshot, slot))
    unsafe_role = replace(role, **changes)

    _assert_invariant(lambda: _classify_candidate(replace(snapshot, **{slot: unsafe_role})))


@pytest.mark.parametrize(
    ("slot", "wrong_name"),
    (
        pytest.param("app_role", RETENTION_RUNTIME_ROLE_NAME, id="app-name"),
        pytest.param("retention_role", APP_RUNTIME_ROLE_NAME, id="retention-name"),
    ),
)
def test_runtime_role_exact_names_are_required(
    slot: str,
    wrong_name: str,
) -> None:
    """角色名不能互换、大小写折叠或由调用方自行正规化。"""
    snapshot = _snapshot(DatabaseAclProfile.FRESH_DEFAULT)
    role = cast(RuntimeRoleSnapshot, getattr(snapshot, slot))

    _assert_invariant(
        lambda: _classify_candidate(
            replace(snapshot, **{slot: replace(role, role_name=wrong_name)})
        )
    )


@pytest.mark.parametrize("slot", ("app_role", "retention_role"))
def test_runtime_role_oid_must_match_acl_grantee(slot: str) -> None:
    """legacy ACL 的 grantee OID 必须与同名 runtime role 快照精确一致。"""
    snapshot = _snapshot(DatabaseAclProfile.PRE_PROTOCOL_LEGACY)
    role = cast(RuntimeRoleSnapshot, getattr(snapshot, slot))

    _assert_invariant(
        lambda: _classify_candidate(
            replace(snapshot, **{slot: replace(role, role_oid=role.role_oid + 1000)})
        )
    )


@pytest.mark.parametrize(
    ("profile", "app_role", "retention_role"),
    (
        pytest.param(
            DatabaseAclProfile.FRESH_DEFAULT,
            SAFE_APP_ROLE,
            None,
            id="fresh-app-only",
        ),
        pytest.param(
            DatabaseAclProfile.FRESH_DEFAULT,
            None,
            SAFE_RETENTION_ROLE,
            id="fresh-retention-only",
        ),
        pytest.param(
            DatabaseAclProfile.PRE_PROTOCOL_LEGACY,
            SAFE_APP_ROLE,
            None,
            id="legacy-retention-missing",
        ),
        pytest.param(
            DatabaseAclProfile.PRE_PROTOCOL_LEGACY,
            None,
            SAFE_RETENTION_ROLE,
            id="legacy-app-missing",
        ),
        pytest.param(
            DatabaseAclProfile.PRE_PROTOCOL_LEGACY,
            None,
            None,
            id="legacy-both-missing",
        ),
    ),
)
def test_runtime_role_mixed_or_missing_pairs_are_rejected(
    profile: DatabaseAclProfile,
    app_role: RuntimeRoleSnapshot | None,
    retention_role: RuntimeRoleSnapshot | None,
) -> None:
    """fresh 只允许同时 absent/safe，legacy 只允许两者同时 safe。"""
    _assert_invariant(
        lambda: _classify_candidate(
            _snapshot(profile, app_role=app_role, retention_role=retention_role)
        )
    )


MEMBERSHIP_CASES = tuple(
    pytest.param(
        RoleMembershipTuple(
            role_oid=role_oid,
            member_oid=member_oid,
            grantor_oid=OWNER_OID,
            admin_option=admin_option,
        ),
        id=f"{direction}-{runtime_name}-admin-{admin_option}",
    )
    for runtime_name, runtime_oid in (
        ("app", APP_ROLE_OID),
        ("retention", RETENTION_ROLE_OID),
    )
    for direction, role_oid, member_oid in (
        ("outbound", runtime_oid, OTHER_ROLE_OID),
        ("inbound", OTHER_ROLE_OID, runtime_oid),
    )
    for admin_option in (False, True)
)


@pytest.mark.parametrize("membership", MEMBERSHIP_CASES)
def test_runtime_role_any_membership_direction_is_rejected(
    membership: RoleMembershipTuple,
) -> None:
    """A/R 作为 roleid 或 member 的任一行都破坏 zero-membership posture。"""
    snapshot = _snapshot(DatabaseAclProfile.FRESH_DEFAULT, memberships=(membership,))
    _assert_invariant(lambda: _classify_candidate(snapshot))


@pytest.mark.parametrize(
    "snapshot",
    (
        pytest.param(_snapshot(DatabaseAclProfile.BASELINE), id="baseline"),
        pytest.param(_snapshot(DatabaseAclProfile.ACTIVE), id="active"),
    ),
)
def test_bootstrap_candidate_rejects_steady_postures(
    snapshot: DatabaseAccessSnapshot,
) -> None:
    """baseline/active 是 steady posture，绝不能伪装成瞬态 candidate。"""
    _assert_invariant(lambda: _classify_candidate(snapshot))


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param({"restore_facts_absent": False}, id="restore-fact-present"),
        pytest.param({"has_active_non_owner_sessions": True}, id="active-session"),
        pytest.param({"object_grants_match": False}, id="object-grant-drift"),
    ),
)
def test_bootstrap_candidate_rejects_external_closed_evidence(
    overrides: dict[str, bool],
) -> None:
    """外部事实只在 classifier 进入，任一不安全值均不得产生 Candidate。"""
    snapshot = _snapshot(
        DatabaseAclProfile.FRESH_DEFAULT,
        app_role=None,
        retention_role=None,
    )
    _assert_invariant(lambda: _classify_candidate(snapshot, **overrides))


@pytest.mark.parametrize(
    ("argument", "value"),
    (
        pytest.param("restore_facts_absent", 1, id="restore-facts-non-bool"),
        pytest.param("has_active_non_owner_sessions", 0, id="sessions-non-bool"),
        pytest.param("object_grants_match", 1, id="object-grants-non-bool"),
    ),
)
def test_bootstrap_candidate_rejects_non_boolean_closed_inputs(
    argument: str,
    value: object,
) -> None:
    """bool 闭集不能接受整数真值或其他宽松 truthy/falsy 输入。"""
    snapshot = _snapshot(
        DatabaseAclProfile.FRESH_DEFAULT,
        app_role=None,
        retention_role=None,
    )
    overrides = {argument: value}
    _assert_invariant(lambda: _classify_candidate(snapshot, **overrides))


def test_bootstrap_candidate_rejects_raw_string_caller() -> None:
    """与枚举值相同的裸字符串也不是受批准 caller authority。"""
    snapshot = _snapshot(
        DatabaseAclProfile.FRESH_DEFAULT,
        app_role=None,
        retention_role=None,
    )
    _assert_invariant(
        lambda: _classify_candidate(
            snapshot,
            caller=cast(BootstrapCaller, "role_bootstrap"),
        )
    )


@pytest.mark.parametrize(
    ("profile", "roles_exist", "expected_role_action"),
    (
        pytest.param(
            DatabaseAclProfile.FRESH_DEFAULT,
            False,
            RuntimeRoleAction.CREATE_BOTH_SAFE,
            id="fresh-create-both",
        ),
        pytest.param(
            DatabaseAclProfile.FRESH_DEFAULT,
            True,
            RuntimeRoleAction.ROTATE_BOTH_PASSWORDS,
            id="fresh-rotate-both",
        ),
        pytest.param(
            DatabaseAclProfile.PRE_PROTOCOL_LEGACY,
            True,
            RuntimeRoleAction.ROTATE_BOTH_PASSWORDS,
            id="legacy-rotate-both",
        ),
    ),
)
def test_bootstrap_candidate_plan_is_narrow_and_atomic_for_both_roles(
    profile: DatabaseAclProfile,
    roles_exist: bool,
    expected_role_action: RuntimeRoleAction,
) -> None:
    """candidate→baseline 只能同时创建或同时轮换两个角色，并执行闭集 ACL 动作。"""
    snapshot = _snapshot(
        profile,
        app_role=SAFE_APP_ROLE if roles_exist else None,
        retention_role=SAFE_RETENTION_ROLE if roles_exist else None,
    )
    candidate = _classify_candidate(snapshot)

    assert bootstrap_candidate_to_baseline(candidate) == DatabaseAccessMutationPlan(
        source_profile=profile,
        target_profile=DatabaseAclProfile.BASELINE,
        role_action=expected_role_action,
        acl_action=DatabaseAclAction.CANDIDATE_TO_BASELINE,
    )


@pytest.mark.parametrize(
    "candidate_mutator",
    (
        pytest.param(
            lambda candidate: replace(candidate, caller=cast(BootstrapCaller, "role_bootstrap")),
            id="raw-caller",
        ),
        pytest.param(
            lambda candidate: replace(candidate, source_profile=DatabaseAclProfile.BASELINE),
            id="source-profile-mismatch",
        ),
        pytest.param(
            lambda candidate: replace(candidate, roles_exist=True),
            id="roles-exist-mismatch",
        ),
        pytest.param(
            lambda candidate: replace(
                candidate,
                snapshot=replace(
                    candidate.snapshot,
                    acl_profile=DatabaseAclProfile.BASELINE,
                ),
            ),
            id="snapshot-profile-mismatch",
        ),
        pytest.param(
            lambda candidate: replace(
                candidate,
                snapshot=replace(
                    candidate.snapshot,
                    memberships=(
                        RoleMembershipTuple(
                            role_oid=APP_ROLE_OID,
                            member_oid=OTHER_ROLE_OID,
                            grantor_oid=OWNER_OID,
                            admin_option=False,
                        ),
                    ),
                ),
            ),
            id="membership",
        ),
        pytest.param(
            lambda candidate: replace(
                candidate,
                snapshot=replace(
                    candidate.snapshot,
                    acl_tuples=tuple(
                        sorted(
                            (
                                replace(FRESH_DEFAULT_ACL[0], grantor_oid=OTHER_ROLE_OID),
                                *FRESH_DEFAULT_ACL[1:],
                            )
                        )
                    ),
                ),
            ),
            id="wrong-grantor",
        ),
    ),
)
def test_bootstrap_candidate_manual_dataclass_forgery_cannot_produce_plan(
    candidate_mutator: Callable[[BootstrapCandidate], BootstrapCandidate],
) -> None:
    """计划器必须重验 Candidate 内部一致性，不能信任可手工构造的 dataclass。"""
    candidate = _classify_candidate(
        _snapshot(
            DatabaseAclProfile.FRESH_DEFAULT,
            app_role=None,
            retention_role=None,
        )
    )
    forged = candidate_mutator(candidate)
    _assert_invariant(lambda: bootstrap_candidate_to_baseline(forged))


@pytest.mark.parametrize(
    ("snapshot", "planner", "expected_plan"),
    (
        pytest.param(
            _snapshot(DatabaseAclProfile.BASELINE),
            transition_baseline_to_active,
            DatabaseAccessMutationPlan(
                source_profile=DatabaseAclProfile.BASELINE,
                target_profile=DatabaseAclProfile.ACTIVE,
                role_action=RuntimeRoleAction.NONE,
                acl_action=DatabaseAclAction.BASELINE_TO_ACTIVE,
            ),
            id="baseline-to-active",
        ),
        pytest.param(
            _snapshot(DatabaseAclProfile.ACTIVE),
            transition_active_to_baseline,
            DatabaseAccessMutationPlan(
                source_profile=DatabaseAclProfile.ACTIVE,
                target_profile=DatabaseAclProfile.BASELINE,
                role_action=RuntimeRoleAction.NONE,
                acl_action=DatabaseAclAction.ACTIVE_TO_BASELINE,
            ),
            id="active-to-baseline",
        ),
    ),
)
def test_database_acl_steady_transition_plans_are_closed(
    snapshot: DatabaseAccessSnapshot,
    planner: Callable[[DatabaseAccessSnapshot], DatabaseAccessMutationPlan],
    expected_plan: DatabaseAccessMutationPlan,
) -> None:
    """steady transition 只能表达固定 source→target，不携带 SQL 或任意权限列表。"""
    assert planner(snapshot) == expected_plan


@pytest.mark.parametrize(
    ("planner", "snapshot"),
    (
        pytest.param(
            transition_baseline_to_active,
            _snapshot(DatabaseAclProfile.ACTIVE),
            id="baseline-planner-wrong-source",
        ),
        pytest.param(
            transition_active_to_baseline,
            _snapshot(DatabaseAclProfile.BASELINE),
            id="active-planner-wrong-source",
        ),
        pytest.param(
            transition_baseline_to_active,
            replace(
                _snapshot(DatabaseAclProfile.BASELINE),
                app_role=replace(SAFE_APP_ROLE, is_superuser=True),
            ),
            id="unsafe-role",
        ),
        pytest.param(
            transition_active_to_baseline,
            replace(
                _snapshot(DatabaseAclProfile.ACTIVE),
                memberships=(
                    RoleMembershipTuple(
                        role_oid=OTHER_ROLE_OID,
                        member_oid=RETENTION_ROLE_OID,
                        grantor_oid=OWNER_OID,
                        admin_option=True,
                    ),
                ),
            ),
            id="membership",
        ),
        pytest.param(
            transition_baseline_to_active,
            replace(
                _snapshot(DatabaseAclProfile.BASELINE),
                acl_tuples=tuple(sorted((*BASELINE_ACL, _acl_tuple(OTHER_ROLE_OID, "CONNECT")))),
            ),
            id="extra-tuple",
        ),
        pytest.param(
            transition_active_to_baseline,
            replace(
                _snapshot(DatabaseAclProfile.ACTIVE),
                acl_tuples=(replace(ACTIVE_ACL[0], grantor_oid=OTHER_ROLE_OID), *ACTIVE_ACL[1:]),
            ),
            id="wrong-grantor",
        ),
        pytest.param(
            transition_baseline_to_active,
            replace(
                _snapshot(DatabaseAclProfile.BASELINE),
                acl_tuples=cast(
                    tuple[DatabaseAclTuple, ...],
                    (BASELINE_ACL[0], object()),
                ),
            ),
            id="unknown-acl-row-type",
        ),
    ),
)
def test_database_acl_steady_transition_rejects_forged_snapshot(
    planner: Callable[[DatabaseAccessSnapshot], DatabaseAccessMutationPlan],
    snapshot: DatabaseAccessSnapshot,
) -> None:
    """错误 source、unsafe role、membership 或 ACL 漂移均不得返回 steady plan。"""
    _assert_invariant(lambda: planner(snapshot))


class _FakeMappingResult:
    """模拟 buffered Result 的 mapping 读取，不隐藏 reader 的 SQL 顺序。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        """返回当前查询全部合成 catalog row。"""
        return list(self._rows)


class _FakeResult:
    """为 SQLAlchemy ``Result.mappings()`` 提供最小测试替身。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMappingResult:
        """切换到明确列名的 mapping 视图。"""
        return _FakeMappingResult(self._rows)


class _FakeAsyncConnection:
    """记录只读 catalog SQL，并按查询边界返回独立合成事实。"""

    def __init__(
        self,
        *,
        owner_rows: list[dict[str, object]] | None = None,
        acl_rows: list[dict[str, object]] | None = None,
        role_rows: list[dict[str, object]] | None = None,
        membership_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.owner_rows = owner_rows if owner_rows is not None else [self._owner_row()]
        self.acl_rows = acl_rows if acl_rows is not None else self._acl_rows(FRESH_DEFAULT_ACL)
        self.role_rows = role_rows if role_rows is not None else []
        self.membership_rows = membership_rows if membership_rows is not None else []
        self.calls: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def _owner_row() -> dict[str, object]:
        """返回目标数据库唯一 owner catalog row。"""
        return {
            "target_database_oid": TARGET_DATABASE_OID,
            "owner_oid": OWNER_OID,
        }

    @staticmethod
    def _acl_rows(acl_tuples: tuple[DatabaseAclTuple, ...]) -> list[dict[str, object]]:
        """把强类型 tuple 转成 canonical query 的原始列名。"""
        return [
            {
                "grantee": tuple_.grantee_oid,
                "grantor": tuple_.grantor_oid,
                "privilege_type": tuple_.privilege_type,
                "is_grantable": tuple_.is_grantable,
            }
            for tuple_ in acl_tuples
        ]

    async def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _FakeResult:
        """按 SQL 文本选择结果，同时冻结绑定参数而不执行任何 mutation。"""
        sql = str(statement)
        params = {} if parameters is None else dict(parameters)
        self.calls.append((sql, params))
        if sql == EXPECTED_DATABASE_ACL_SQL:
            return _FakeResult(self.acl_rows)
        if "db.datdba AS owner_oid" in sql:
            return _FakeResult(self.owner_rows)
        if "FROM pg_auth_members AS membership" in sql:
            return _FakeResult(self.membership_rows)
        if "FROM pg_roles AS runtime_role" in sql:
            return _FakeResult(self.role_rows)
        raise AssertionError(f"unexpected read-only catalog SQL: {sql}")


class _FakeSyncConnection:
    """为 migration/maintenance 同步边界复用同一组 canonical catalog 行。"""

    def __init__(
        self,
        *,
        owner_rows: list[dict[str, object]] | None = None,
        acl_rows: list[dict[str, object]] | None = None,
        role_rows: list[dict[str, object]] | None = None,
        membership_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.owner_rows = (
            owner_rows if owner_rows is not None else [_FakeAsyncConnection._owner_row()]
        )
        self.acl_rows = (
            acl_rows if acl_rows is not None else _FakeAsyncConnection._acl_rows(FRESH_DEFAULT_ACL)
        )
        self.role_rows = role_rows if role_rows is not None else []
        self.membership_rows = membership_rows if membership_rows is not None else []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.dialect = postgresql_dialect()

    @property
    def mutation_calls(self) -> list[tuple[str, dict[str, object]]]:
        """返回排除四个 canonical access SELECT 的全部调用。"""
        return [
            call
            for call in self.calls
            if call[0] != EXPECTED_DATABASE_ACL_SQL
            and "db.datdba AS owner_oid" not in call[0]
            and "FROM pg_auth_members AS membership" not in call[0]
            and "FROM pg_roles AS runtime_role" not in call[0]
        ]

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _FakeResult:
        """同步返回 catalog 行；未知语句只记录，供 mutation contract 断言。"""
        sql = str(statement)
        params = {} if parameters is None else dict(parameters)
        self.calls.append((sql, params))
        if sql == EXPECTED_DATABASE_ACL_SQL:
            return _FakeResult(self.acl_rows)
        if "db.datdba AS owner_oid" in sql:
            return _FakeResult(self.owner_rows)
        if "FROM pg_auth_members AS membership" in sql:
            return _FakeResult(self.membership_rows)
        if "FROM pg_roles AS runtime_role" in sql:
            return _FakeResult(self.role_rows)
        return _FakeResult([])


def _role_row(role: RuntimeRoleSnapshot, *, config: object = None) -> dict[str, object]:
    """把 runtime role 转成 reader 查询使用的别名列。"""
    return {
        "role_name": role.role_name,
        "role_oid": role.role_oid,
        "can_login": role.can_login,
        "inherits": role.inherits,
        "is_superuser": role.is_superuser,
        "can_create_database": role.can_create_database,
        "can_create_role": role.can_create_role,
        "can_replicate": role.can_replicate,
        "bypasses_rls": role.bypasses_rls,
        "connection_limit": role.connection_limit,
        "valid_until": role.valid_until,
        "config": config,
    }


def test_database_access_sync_reader_reuses_exact_async_catalog_contract() -> None:
    """同步 maintenance reader 必须复用同一 SQL/parser，而不是建立第二套 ACL 逻辑。"""
    from ai_employee.infrastructure.db.database_access import (
        read_database_access_snapshot_sync,
    )

    connection = _FakeSyncConnection()
    snapshot = read_database_access_snapshot_sync(
        cast(Connection, connection),
        target_database_oid=TARGET_DATABASE_OID,
    )

    assert snapshot.acl_profile is DatabaseAclProfile.FRESH_DEFAULT
    assert snapshot.acl_tuples == FRESH_DEFAULT_ACL
    assert [sql for sql, _ in connection.calls] == [
        next(sql for sql, _ in connection.calls if "db.datdba AS owner_oid" in sql),
        next(sql for sql, _ in connection.calls if "FROM pg_roles AS runtime_role" in sql),
        EXPECTED_DATABASE_ACL_SQL,
        next(sql for sql, _ in connection.calls if "FROM pg_auth_members AS membership" in sql),
    ]
    assert connection.mutation_calls == []


def test_safe_runtime_role_mutator_validates_candidate_before_first_sql() -> None:
    """forged Candidate 或 invalid Secret 必须在创建临时 helper/角色前零写拒绝。"""
    from ai_employee.infrastructure.db.database_access import (
        apply_safe_runtime_roles,
    )

    candidate = _classify_candidate(
        _snapshot(
            DatabaseAclProfile.FRESH_DEFAULT,
            app_role=None,
            retention_role=None,
        )
    )
    invalid_candidates = (
        replace(candidate, roles_exist=True),
        replace(candidate, source_profile=DatabaseAclProfile.BASELINE),
    )
    for invalid_candidate in invalid_candidates:
        connection = _FakeSyncConnection()
        _assert_invariant(
            lambda invalid_candidate=invalid_candidate, connection=connection: (
                apply_safe_runtime_roles(
                    cast(Connection, connection),
                    invalid_candidate,
                    app_password="synthetic-app-secret",
                    retention_password="synthetic-retention-secret",
                )
            )
        )
        assert connection.calls == []

    invalid_password_pairs = (
        ("", "synthetic-retention-secret"),
        ("contains\0nul", "synthetic-retention-secret"),
        ("synthetic-app-secret", ""),
        ("synthetic-app-secret", "contains\0nul"),
    )
    for app_password, retention_password in invalid_password_pairs:
        connection = _FakeSyncConnection()
        _assert_invariant(
            lambda app_password=app_password, retention_password=retention_password, connection=connection: (
                apply_safe_runtime_roles(
                    cast(Connection, connection),
                    candidate,
                    app_password=app_password,
                    retention_password=retention_password,
                )
            )
        )
        assert connection.calls == []


def test_safe_runtime_role_mutator_binds_secrets_and_uses_explicit_safe_attributes() -> None:
    """角色密码只作为 bind value，CREATE 路径必须逐项写明冻结 safe attributes。"""
    from ai_employee.infrastructure.db.database_access import (
        apply_safe_runtime_roles,
    )

    candidate = _classify_candidate(
        _snapshot(
            DatabaseAclProfile.FRESH_DEFAULT,
            app_role=None,
            retention_role=None,
        )
    )
    connection = _FakeSyncConnection()
    app_secret = "synthetic-app-secret"
    retention_secret = "synthetic-retention-secret"

    apply_safe_runtime_roles(
        cast(Connection, connection),
        candidate,
        app_password=app_secret,
        retention_password=retention_secret,
    )

    assert len(connection.calls) == 3
    helper_sql = connection.calls[0][0]
    for clause in (
        "LOGIN",
        "NOSUPERUSER",
        "INHERIT",
        "NOCREATEROLE",
        "NOCREATEDB",
        "NOREPLICATION",
        "NOBYPASSRLS",
        "CONNECTION LIMIT -1",
    ):
        assert clause in helper_sql
    rendered_sql = "\n".join(sql for sql, _ in connection.calls)
    assert app_secret not in rendered_sql
    assert retention_secret not in rendered_sql
    assert connection.calls[1][1] == {
        "role_name": APP_RUNTIME_ROLE_NAME,
        "role_password": app_secret,
        "create_role": True,
    }
    assert connection.calls[2][1] == {
        "role_name": RETENTION_RUNTIME_ROLE_NAME,
        "role_password": retention_secret,
        "create_role": True,
    }


def test_safe_runtime_role_mutator_only_rotates_existing_safe_role_passwords() -> None:
    """既有角色经 safe posture 重验后只能同时轮换密码，不能重建或放宽属性。"""
    from ai_employee.infrastructure.db.database_access import (
        apply_safe_runtime_roles,
    )

    candidate = _classify_candidate(_snapshot(DatabaseAclProfile.FRESH_DEFAULT))
    connection = _FakeSyncConnection()

    apply_safe_runtime_roles(
        cast(Connection, connection),
        candidate,
        app_password="synthetic-app-secret",
        retention_password="synthetic-retention-secret",
    )

    assert len(connection.calls) == 3
    assert "ALTER ROLE %I PASSWORD %L" in connection.calls[0][0]
    assert [parameters["create_role"] for _, parameters in connection.calls[1:]] == [
        False,
        False,
    ]


def test_stable_idle_password_rotation_accepts_only_exact_baseline_snapshot() -> None:
    """steady role-bootstrap 只能在 exact baseline 上同时轮换两个 safe 角色密码。"""
    from ai_employee.infrastructure.db.database_access import (
        rotate_safe_runtime_role_passwords,
    )

    baseline = _snapshot(DatabaseAclProfile.BASELINE)
    connection = _FakeSyncConnection()
    rotate_safe_runtime_role_passwords(
        cast(Connection, connection),
        baseline,
        app_password="synthetic-app-secret",
        retention_password="synthetic-retention-secret",
    )

    assert len(connection.calls) == 3
    assert [parameters["create_role"] for _, parameters in connection.calls[1:]] == [
        False,
        False,
    ]

    for rejected_snapshot in (
        _snapshot(DatabaseAclProfile.ACTIVE),
        replace(baseline, memberships=(RoleMembershipTuple(201, 301, OWNER_OID, False),)),
    ):
        rejected_connection = _FakeSyncConnection()
        _assert_invariant(
            lambda rejected_snapshot=rejected_snapshot, rejected_connection=rejected_connection: (
                rotate_safe_runtime_role_passwords(
                    cast(Connection, rejected_connection),
                    rejected_snapshot,
                    app_password="synthetic-app-secret",
                    retention_password="synthetic-retention-secret",
                )
            )
        )
        assert rejected_connection.calls == []


def test_baseline_connect_reassertion_plan_and_mutator_are_narrow() -> None:
    """steady reassert 只能表达 baseline 自环，并只发两条 quoted CONNECT GRANT。"""
    import ai_employee.infrastructure.db.database_access as database_access_module

    planner = getattr(database_access_module, "plan_baseline_connect_reassertion", None)
    mutator = getattr(database_access_module, "apply_baseline_connect_reassertion", None)
    assert callable(planner)
    assert callable(mutator)

    baseline = _snapshot(DatabaseAclProfile.BASELINE)
    plan = planner(baseline)
    assert plan == DatabaseAccessMutationPlan(
        source_profile=DatabaseAclProfile.BASELINE,
        target_profile=DatabaseAclProfile.BASELINE,
        role_action=RuntimeRoleAction.NONE,
        acl_action=DatabaseAclAction.BASELINE_CONNECT_REASSERT,
    )

    connection = _FakeSyncConnection()
    mutator(
        cast(Connection, connection),
        target_database_name='synthetic"database',
        snapshot=baseline,
    )
    assert [sql for sql, _ in connection.calls] == [
        'GRANT CONNECT ON DATABASE "synthetic""database" TO "ai_employee_app"',
        'GRANT CONNECT ON DATABASE "synthetic""database" TO "ai_employee_retention"',
    ]
    assert all(parameters == {} for _, parameters in connection.calls)


def test_baseline_connect_reassertion_rejects_drift_before_first_sql() -> None:
    """active、unsafe role 或 membership 漂移都不能触发任何 CONNECT SQL。"""
    import ai_employee.infrastructure.db.database_access as database_access_module

    mutator = getattr(database_access_module, "apply_baseline_connect_reassertion", None)
    assert callable(mutator)

    baseline = _snapshot(DatabaseAclProfile.BASELINE)
    rejected_snapshots = (
        _snapshot(DatabaseAclProfile.ACTIVE),
        replace(
            baseline,
            app_role=replace(SAFE_APP_ROLE, is_superuser=True),
        ),
        replace(
            baseline,
            memberships=(RoleMembershipTuple(APP_ROLE_OID, OTHER_ROLE_OID, OWNER_OID, False),),
        ),
    )
    for rejected_snapshot in rejected_snapshots:
        connection = _FakeSyncConnection()
        _assert_invariant(
            lambda connection=connection, rejected_snapshot=rejected_snapshot: mutator(
                cast(Connection, connection),
                target_database_name="synthetic_database",
                snapshot=rejected_snapshot,
            )
        )
        assert connection.calls == []


@pytest.mark.parametrize(
    ("source", "destination", "verb"),
    (
        (DatabaseAclProfile.BASELINE, DatabaseAclProfile.ACTIVE, "REVOKE"),
        (DatabaseAclProfile.ACTIVE, DatabaseAclProfile.BASELINE, "GRANT"),
    ),
)
def test_restore_acl_mutator_changes_only_the_two_runtime_connect_tuples(
    source: DatabaseAclProfile,
    destination: DatabaseAclProfile,
    verb: str,
) -> None:
    """restore 入闸/开闸不能重建角色、修复 PUBLIC 或扩大数据库权限。"""
    import ai_employee.infrastructure.db.database_access as access

    mutator = getattr(access, "apply_restore_database_acl_transition", None)
    assert callable(mutator), "restore has no exact baseline-active ACL mutator"
    connection = _FakeSyncConnection()
    mutator(
        cast(Connection, connection),
        target_database_name="synthetic_database",
        snapshot=_snapshot(source),
        destination=destination,
    )
    preposition = "FROM" if verb == "REVOKE" else "TO"
    assert [sql for sql, _ in connection.calls] == [
        f'{verb} CONNECT ON DATABASE "synthetic_database" {preposition} "ai_employee_app"',
        f'{verb} CONNECT ON DATABASE "synthetic_database" {preposition} "ai_employee_retention"',
    ]
    drift = replace(_snapshot(source), app_role=replace(SAFE_APP_ROLE, is_superuser=True))
    rejected = _FakeSyncConnection()
    _assert_invariant(
        lambda: mutator(
            cast(Connection, rejected),
            target_database_name="synthetic_database",
            snapshot=drift,
            destination=destination,
        )
    )
    assert rejected.calls == []


def test_database_acl_mutator_allows_only_exact_candidate_to_baseline_plan() -> None:
    """ACL mutator 只能撤销 PUBLIC 并授予两条 owner/non-grantable CONNECT。"""
    from ai_employee.infrastructure.db.database_access import (
        apply_database_acl_mutation,
    )

    candidate = _classify_candidate(
        _snapshot(
            DatabaseAclProfile.FRESH_DEFAULT,
            app_role=None,
            retention_role=None,
        )
    )
    plan = bootstrap_candidate_to_baseline(candidate)
    connection = _FakeSyncConnection()

    apply_database_acl_mutation(
        cast(Connection, connection),
        target_database_name='synthetic"database',
        plan=plan,
    )

    assert [sql for sql, _ in connection.calls] == [
        'REVOKE ALL PRIVILEGES ON DATABASE "synthetic""database" FROM PUBLIC',
        'GRANT CONNECT ON DATABASE "synthetic""database" TO "ai_employee_app"',
        'GRANT CONNECT ON DATABASE "synthetic""database" TO "ai_employee_retention"',
    ]
    assert all(parameters == {} for _, parameters in connection.calls)

    forged_plans = (
        replace(plan, source_profile=DatabaseAclProfile.BASELINE),
        replace(plan, target_profile=DatabaseAclProfile.ACTIVE),
        replace(plan, role_action=RuntimeRoleAction.NONE),
        replace(plan, acl_action=DatabaseAclAction.BASELINE_TO_ACTIVE),
    )
    for forged_plan in forged_plans:
        rejected_connection = _FakeSyncConnection()
        _assert_invariant(
            lambda forged_plan=forged_plan, rejected_connection=rejected_connection: (
                apply_database_acl_mutation(
                    cast(Connection, rejected_connection),
                    target_database_name="synthetic_database",
                    plan=forged_plan,
                )
            )
        )
        assert rejected_connection.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_target_oid",
    (0, MAX_POSTGRESQL_OID + 1, -1, True),
)
async def test_database_acl_reader_rejects_invalid_target_oid_before_sql(
    invalid_target_oid: object,
) -> None:
    """非法 target OID 必须在 ``AsyncConnection.execute`` 前稳定拒绝。"""
    connection = _FakeAsyncConnection()

    with pytest.raises(
        DatabaseAccessInvariantError,
        match=r"^database access invariant violation$",
    ):
        await read_database_access_snapshot(
            cast(AsyncConnection, connection),
            target_database_oid=cast(int, invalid_target_oid),
        )

    assert connection.calls == []


@pytest.mark.asyncio
async def test_database_acl_reader_accepts_max_target_oid() -> None:
    """reader 必须接受合法 uint32 最大 target OID，并继续 canonical 只读采样。"""
    connection = _FakeAsyncConnection(
        owner_rows=[
            {
                "target_database_oid": MAX_POSTGRESQL_OID,
                "owner_oid": OWNER_OID,
            }
        ]
    )

    snapshot = await read_database_access_snapshot(
        cast(AsyncConnection, connection),
        target_database_oid=MAX_POSTGRESQL_OID,
    )

    assert snapshot.target_database_oid == MAX_POSTGRESQL_OID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection", "expected_call_count"),
    (
        pytest.param(
            _FakeAsyncConnection(
                owner_rows=[
                    {
                        "target_database_oid": TARGET_DATABASE_OID,
                        "owner_oid": MAX_POSTGRESQL_OID + 1,
                    }
                ]
            ),
            1,
            id="owner-overflow",
        ),
        pytest.param(
            _FakeAsyncConnection(
                role_rows=[
                    _role_row(
                        replace(
                            SAFE_APP_ROLE,
                            role_oid=MAX_POSTGRESQL_OID + 1,
                        )
                    )
                ]
            ),
            2,
            id="runtime-role-overflow",
        ),
        pytest.param(
            _FakeAsyncConnection(
                acl_rows=_FakeAsyncConnection._acl_rows(BASELINE_ACL),
                role_rows=[
                    _role_row(SAFE_APP_ROLE),
                    _role_row(SAFE_RETENTION_ROLE),
                ],
                membership_rows=[
                    {
                        "role_oid": APP_ROLE_OID,
                        "member_oid": OTHER_ROLE_OID,
                        "grantor_oid": MAX_POSTGRESQL_OID + 1,
                        "admin_option": False,
                    }
                ],
            ),
            4,
            id="membership-grantor-overflow",
        ),
    ),
)
async def test_runtime_role_reader_rejects_catalog_oid_overflow_at_source(
    connection: _FakeAsyncConnection,
    expected_call_count: int,
) -> None:
    """owner、role、membership OID overflow 必须在各自 reader 边界立即拒绝。"""
    with pytest.raises(
        DatabaseAccessInvariantError,
        match=r"^database access invariant violation$",
    ):
        await read_database_access_snapshot(
            cast(AsyncConnection, connection),
            target_database_oid=TARGET_DATABASE_OID,
        )

    assert len(connection.calls) == expected_call_count


@pytest.mark.asyncio
async def test_database_acl_reader_executes_exact_canonical_sql() -> None:
    """reader 必须以 TextClause + dict 参数执行逐字冻结的 ACL SQL。"""
    connection = _FakeAsyncConnection()

    snapshot = await read_database_access_snapshot(
        cast(AsyncConnection, connection),
        target_database_oid=TARGET_DATABASE_OID,
    )

    assert DATABASE_ACL_SQL == EXPECTED_DATABASE_ACL_SQL
    assert snapshot.acl_profile is DatabaseAclProfile.FRESH_DEFAULT
    assert snapshot.acl_tuples == FRESH_DEFAULT_ACL
    assert (
        EXPECTED_DATABASE_ACL_SQL,
        {"target_database_oid": TARGET_DATABASE_OID},
    ) in connection.calls
    assert all(tuple_.is_grantable is False for tuple_ in snapshot.acl_tuples)


@pytest.mark.asyncio
async def test_runtime_role_reader_normalizes_rolconfig_list_to_tuple() -> None:
    """第三方 ``list[str]`` 边界必须规范化为不可变 tuple，空数组仍是不安全事实。"""
    connection = _FakeAsyncConnection(
        acl_rows=_FakeAsyncConnection._acl_rows(BASELINE_ACL),
        role_rows=[
            _role_row(SAFE_APP_ROLE, config=[]),
            _role_row(SAFE_RETENTION_ROLE),
        ],
    )

    snapshot = await read_database_access_snapshot(
        cast(AsyncConnection, connection),
        target_database_oid=TARGET_DATABASE_OID,
    )

    assert snapshot.app_role is not None
    assert snapshot.app_role.config == ()
    _assert_invariant(lambda: transition_baseline_to_active(snapshot))


@pytest.mark.asyncio
async def test_runtime_role_reader_never_reads_password_catalog() -> None:
    """角色 reader 只能读取 pg_roles 的公开 posture，绝不访问密码字段或 pg_authid。"""
    connection = _FakeAsyncConnection(
        acl_rows=_FakeAsyncConnection._acl_rows(PRE_PROTOCOL_LEGACY_ACL),
        role_rows=[_role_row(SAFE_APP_ROLE), _role_row(SAFE_RETENTION_ROLE)],
    )

    await read_database_access_snapshot(
        cast(AsyncConnection, connection),
        target_database_oid=TARGET_DATABASE_OID,
    )

    executed_sql = "\n".join(sql.lower() for sql, _ in connection.calls)
    assert "rolpassword" not in executed_sql
    assert "pg_authid" not in executed_sql
    assert "password" not in executed_sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection",
    (
        pytest.param(_FakeAsyncConnection(owner_rows=[]), id="owner-missing"),
        pytest.param(
            _FakeAsyncConnection(
                owner_rows=[
                    _FakeAsyncConnection._owner_row(),
                    _FakeAsyncConnection._owner_row(),
                ]
            ),
            id="owner-duplicate",
        ),
        pytest.param(
            _FakeAsyncConnection(role_rows=[_role_row(SAFE_APP_ROLE), _role_row(SAFE_APP_ROLE)]),
            id="role-duplicate",
        ),
        pytest.param(
            _FakeAsyncConnection(
                role_rows=[_role_row(replace(SAFE_APP_ROLE, role_name="unexpected_runtime_role"))]
            ),
            id="unknown-role-row",
        ),
        pytest.param(
            _FakeAsyncConnection(
                membership_rows=[
                    {
                        "role_oid": APP_ROLE_OID,
                        "member_oid": OTHER_ROLE_OID,
                        "grantor_oid": OWNER_OID,
                        "admin_option": False,
                    },
                    {
                        "role_oid": APP_ROLE_OID,
                        "member_oid": OTHER_ROLE_OID,
                        "grantor_oid": OWNER_OID,
                        "admin_option": False,
                    },
                ]
            ),
            id="membership-duplicate",
        ),
    ),
)
async def test_runtime_role_reader_rejects_unresolved_or_duplicate_catalog_facts(
    connection: _FakeAsyncConnection,
) -> None:
    """owner、role 与 membership 的缺失/重复/未知表示都不能被宽松折叠。"""
    with pytest.raises(
        DatabaseAccessInvariantError,
        match=r"^database access invariant violation$",
    ):
        await read_database_access_snapshot(
            cast(AsyncConnection, connection),
            target_database_oid=TARGET_DATABASE_OID,
        )
