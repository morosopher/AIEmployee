"""冻结 integration suite 两阶段编排、隔离与错误传播契约。"""

from __future__ import annotations

import subprocess
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl as DatabaseUrlValue,
)
from ai_employee.infrastructure.db.database_url import (
    ValidatedTestDatabaseUrl,
    validate_test_database_url,
)

# pytest 以 ``backend`` 为 rootdir；显式加入仓库根目录后才能按与 just 相同的路径导入
# root-level test orchestrator。这里只改变测试 import path，不读取或构造任何数据库 URL。
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.run_integration_tests import (
    LIFECYCLE_FILE_TARGETS,
    LIFECYCLE_NODE_TARGETS,
    REGULAR_TEST_ROOTS,
    IntegrationSuiteInvariantError,
    PytestInvocation,
    build_lifecycle_pytest_arguments,
    build_regular_pytest_arguments,
    orchestrate_integration_tests,
)

from tests.integration import integration_suite as integration_suite_module
from tests.integration.integration_suite import _terminate_pytest_child, run_pytest_child

_ANCHOR_URL = DatabaseUrlValue(
    "postgresql+asyncpg://owner:synthetic-anchor-secret@localhost:5432/ai_employee_test"
)
_REGULAR_URL = DatabaseUrlValue(
    "postgresql+asyncpg://owner:synthetic-regular-secret@localhost:5432/"
    "ai_employee_suite_0123456789abcdef0123456789abcdef_test"
)
_EXPECTED_LIFECYCLE_FILES = (
    "backend/tests/integration/db/test_migrations.py",
    "backend/tests/integration/db/test_database_grants_catalog.py",
    "backend/tests/integration/operations/test_database_maintenance_gate.py",
    "backend/tests/integration/operations/test_postgres_backup_restore.py",
    "backend/tests/integration/retention/test_role_permissions.py",
    "backend/tests/integration/operations/test_calendar_aad_0019_preflight.py",
    "backend/tests/integration/operations/test_calendar_aad_0019_recovery.py",
    "backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py",
)
_EXPECTED_LIFECYCLE_NODES = (
    (
        "backend/tests/integration/m2/test_trusted_action_schema.py::"
        "TestEmptyTrustedActionMigration::"
        "test_database_contains_named_aead_nonce_counter_and_manual_resolution_checks"
    ),
    (
        "backend/tests/integration/m2/test_connection_source_schema.py::"
        "TestEmptyConnectionSourceMigration::"
        "test_m1_google_rows_are_backfilled_without_inventing_source_facts"
    ),
)


class _FakeResources:
    """用事件序列模拟 suite lock、anchor snapshot 与 disposable regular DB。"""

    def __init__(
        self,
        events: list[str],
        *,
        snapshots: tuple[object, object] = ("unchanged", "unchanged"),
        lease: _FakeSuiteLease | None = None,
    ) -> None:
        self._events = events
        self._snapshots = iter(snapshots)
        self.lease = lease or _FakeSuiteLease()

    @contextmanager
    def acquire_suite_lock(
        self,
        anchor: ValidatedTestDatabaseUrl,
    ) -> Iterator[_FakeSuiteLease]:
        """记录全 suite lease 必须包围两个 child phase。"""
        assert anchor.value == _ANCHOR_URL
        self._events.append("suite-lock-acquire")
        try:
            yield self.lease
        finally:
            self._events.append("suite-lock-release")

    def capture_anchor(self, anchor: ValidatedTestDatabaseUrl) -> object:
        """返回测试预设的 content-free anchor 快照。"""
        assert anchor.value == _ANCHOR_URL
        self._events.append("anchor-snapshot")
        return next(self._snapshots)

    @contextmanager
    def provision_regular_database(
        self,
        anchor: ValidatedTestDatabaseUrl,
    ) -> Iterator[DatabaseUrlValue]:
        """模拟 parent-owned UUID DB 的 setup/finally cleanup。"""
        assert anchor.value == _ANCHOR_URL
        self._events.append("regular-provision")
        try:
            yield _REGULAR_URL
        finally:
            self._events.append("regular-cleanup")


class _ReleaseSensitiveResources(_FakeResources):
    """模拟成功退出严格 release、异常退出安全 release 的 suite context。"""

    @contextmanager
    def acquire_suite_lock(
        self,
        anchor: ValidatedTestDatabaseUrl,
    ) -> Iterator[_FakeSuiteLease]:
        """正常 body 注入 unlock 失败；异常 body 保留原控制流异常。"""
        assert anchor.value == _ANCHOR_URL
        self._events.append("suite-lock-acquire")
        try:
            yield self.lease
        except BaseException:
            self._events.append("suite-lock-safe-release")
            raise
        else:
            raise IntegrationSuiteInvariantError("synthetic strict unlock failure")
        finally:
            self._events.append("suite-lock-release")


class _DetailedCleanupResources(_FakeResources):
    """把 disposable database 与 fixed roles 的清理顺序展开为独立事件。"""

    @contextmanager
    def provision_regular_database(
        self,
        anchor: ValidatedTestDatabaseUrl,
    ) -> Iterator[DatabaseUrlValue]:
        """模拟 provenance cleanup 先删精确数据库、再删本轮创建的两个角色。"""
        assert anchor.value == _ANCHOR_URL
        self._events.append("regular-provision")
        try:
            yield _REGULAR_URL
        finally:
            self._events.append("regular-database-drop")
            self._events.append("fixed-runtime-roles-drop")


class _FakeSuiteLease:
    """记录 orchestrator 的显式 phase guard，并可在指定次数注入失锁。"""

    def __init__(self, *, fail_on_verification: int | None = None) -> None:
        self.verification_count = 0
        self.fail_on_verification = fail_on_verification

    def verify(self) -> None:
        """在冻结次数抛出稳定异常，模拟 session/cluster/ownership 漂移。"""
        self.verification_count += 1
        if self.verification_count == self.fail_on_verification:
            raise IntegrationSuiteInvariantError("integration suite lock continuity lost")


class _RunningChild:
    """模拟拒绝 terminate、必须 kill 后才能回收的 pytest child。"""

    def __init__(self) -> None:
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []
        self._wait_step = 0

    def wait(self, timeout: float | None = None) -> int:
        """watchdog 与 terminate wait 均超时，kill 后返回稳定退出码。"""
        self.wait_calls.append(timeout)
        self._wait_step += 1
        if self._wait_step <= 2:
            assert timeout is not None
            raise subprocess.TimeoutExpired(cmd=("synthetic",), timeout=timeout)
        return -9

    def poll(self) -> int | None:
        """在 kill 回收前始终报告仍运行。"""
        return None if self._wait_step < 2 else -9

    def terminate(self) -> None:
        """记录有界 graceful termination。"""
        self.terminate_calls += 1

    def kill(self) -> None:
        """记录 terminate 超时后的强制回收。"""
        self.kill_calls += 1


class _ScriptedChild:
    """按冻结 outcome 顺序模拟 Popen 的异常回收边界。"""

    def __init__(
        self,
        *,
        poll_outcomes: list[int | None | BaseException],
        wait_outcomes: list[int | BaseException],
        terminate_error: OSError | None = None,
        kill_error: OSError | None = None,
    ) -> None:
        self.poll_outcomes = list(poll_outcomes)
        self.wait_outcomes = list(wait_outcomes)
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.poll_calls = 0
        self.wait_calls: list[float | None] = []
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        """返回或抛出下一项 poll outcome；空队列表示仍在运行。"""
        self.poll_calls += 1
        outcome = self.poll_outcomes.pop(0) if self.poll_outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def wait(self, timeout: float | None = None) -> int:
        """返回或抛出下一项 wait outcome，记录每次有界等待。"""
        self.wait_calls.append(timeout)
        if not self.wait_outcomes:
            raise AssertionError("unexpected wait call")
        outcome = self.wait_outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def terminate(self) -> None:
        """记录 terminate，并按配置抛出 OSError。"""
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error

    def kill(self) -> None:
        """记录 kill，并按配置抛出 OSError。"""
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error


class _WatchdogLease:
    """首次启动 guard 成功，child 第一次运行中检查即报告失锁。"""

    def __init__(self) -> None:
        self.verification_count = 0

    def verify(self) -> None:
        """第二次检查抛出不包含 DSN 的稳定基础设施异常。"""
        self.verification_count += 1
        if self.verification_count == 2:
            raise IntegrationSuiteInvariantError("integration suite lock continuity lost")


def _validated_anchor() -> ValidatedTestDatabaseUrl:
    """把合成 anchor 交给与真实入口相同的 fail-closed validator。"""
    return validate_test_database_url(_ANCHOR_URL)


def test_phase_selection_is_explicit_complete_and_disjoint() -> None:
    """regular 必须只排除冻结 lifecycle 集合，lifecycle 则精确收集该集合。"""
    assert REGULAR_TEST_ROOTS == (
        "backend/tests/integration",
        "backend/tests/contract",
        "backend/tests/evals",
    )
    assert LIFECYCLE_FILE_TARGETS == _EXPECTED_LIFECYCLE_FILES
    assert LIFECYCLE_NODE_TARGETS == _EXPECTED_LIFECYCLE_NODES

    regular_arguments = build_regular_pytest_arguments()
    lifecycle_arguments = build_lifecycle_pytest_arguments()

    assert regular_arguments[: len(REGULAR_TEST_ROOTS)] == REGULAR_TEST_ROOTS
    assert {
        argument.removeprefix("--ignore=")
        for argument in regular_arguments
        if argument.startswith("--ignore=")
    } == set(_EXPECTED_LIFECYCLE_FILES)
    assert {
        argument.removeprefix("--deselect=")
        for argument in regular_arguments
        if argument.startswith("--deselect=")
    } == {target.removeprefix("backend/") for target in _EXPECTED_LIFECYCLE_NODES}
    assert lifecycle_arguments == (
        *_EXPECTED_LIFECYCLE_FILES,
        *_EXPECTED_LIFECYCLE_NODES,
        "-q",
    )


def test_two_phase_orchestration_routes_urls_and_hides_secrets_from_argv() -> None:
    """regular 用 UUID URL，lifecycle 用 anchor，任何 DSN 都不得进入 argv/repr。"""
    events: list[str] = []
    invocations: list[PytestInvocation] = []

    def run_child(invocation: PytestInvocation, suite_lease: object) -> int:
        assert isinstance(suite_lease, _FakeSuiteLease)
        events.append(f"child-{invocation.phase}")
        invocations.append(invocation)
        return 0

    result = orchestrate_integration_tests(
        anchor=_validated_anchor(),
        base_environment={
            "TEST_DATABASE_URL": str(_ANCHOR_URL),
            "DATABASE_URL": "must-be-replaced",
            "CHECKPOINT_DATABASE_URL": "must-be-replaced",
            "SYNTHETIC_SENTINEL": "preserved",
        },
        resources=_FakeResources(events),
        run_child=run_child,
        python_executable="/synthetic/python",
        repository_root=Path("/synthetic/repository"),
    )

    assert result == 0
    assert events == [
        "suite-lock-acquire",
        "anchor-snapshot",
        "regular-provision",
        "child-regular",
        "regular-cleanup",
        "anchor-snapshot",
        "child-lifecycle",
        "suite-lock-release",
    ]
    assert [invocation.phase for invocation in invocations] == ["regular", "lifecycle"]
    regular, lifecycle = invocations
    assert regular.argv == (
        "/synthetic/python",
        "-m",
        "pytest",
        *build_regular_pytest_arguments(),
    )
    assert lifecycle.argv == (
        "/synthetic/python",
        "-m",
        "pytest",
        *build_lifecycle_pytest_arguments(),
    )
    assert regular.environment["TEST_DATABASE_URL"] == _REGULAR_URL
    assert regular.environment["DATABASE_URL"] == _REGULAR_URL
    assert regular.environment["CHECKPOINT_DATABASE_URL"].startswith("postgresql://")
    assert lifecycle.environment["TEST_DATABASE_URL"] == _ANCHOR_URL
    assert lifecycle.environment["DATABASE_URL"] == _ANCHOR_URL
    assert lifecycle.environment["CHECKPOINT_DATABASE_URL"].startswith("postgresql://")
    assert regular.environment["SYNTHETIC_SENTINEL"] == "preserved"
    assert lifecycle.environment["SYNTHETIC_SENTINEL"] == "preserved"
    assert regular.cwd == Path("/synthetic/repository")
    assert lifecycle.cwd == Path("/synthetic/repository")

    rendered_arguments = "\n".join((*regular.argv, *lifecycle.argv, repr(regular), repr(lifecycle)))
    assert "synthetic-anchor-secret" not in rendered_arguments
    assert "synthetic-regular-secret" not in rendered_arguments


def test_regular_failure_cleans_up_releases_lock_and_propagates_exact_exit_code() -> None:
    """regular child 非零时先 cleanup/复核 anchor，再原样返回且不启动 lifecycle。"""
    events: list[str] = []

    def run_child(invocation: PytestInvocation, suite_lease: object) -> int:
        assert isinstance(suite_lease, _FakeSuiteLease)
        events.append(f"child-{invocation.phase}")
        return 23

    result = orchestrate_integration_tests(
        anchor=_validated_anchor(),
        base_environment={},
        resources=_FakeResources(events),
        run_child=run_child,
        python_executable="python",
        repository_root=Path("/repository"),
    )

    assert result == 23
    assert events == [
        "suite-lock-acquire",
        "anchor-snapshot",
        "regular-provision",
        "child-regular",
        "regular-cleanup",
        "anchor-snapshot",
        "suite-lock-release",
    ]


def test_regular_failure_code_is_not_rewritten_by_strict_release_failure() -> None:
    """非零 child code 必须让 suite context 走安全释放，再在 context 外原样恢复。"""
    events: list[str] = []

    result = orchestrate_integration_tests(
        anchor=_validated_anchor(),
        base_environment={},
        resources=_ReleaseSensitiveResources(events),
        run_child=lambda invocation, suite_lease: 23,
        python_executable="python",
        repository_root=Path("/repository"),
    )

    assert result == 23
    assert events[-2:] == ["suite-lock-safe-release", "suite-lock-release"]


def test_child_exception_still_cleans_up_and_releases_suite_lock() -> None:
    """child 异常后必须清理临时库/角色、复核 anchor，再释放 suite lock。"""
    events: list[str] = []

    def run_child(invocation: PytestInvocation, suite_lease: object) -> int:
        assert isinstance(suite_lease, _FakeSuiteLease)
        events.append(f"child-{invocation.phase}")
        raise RuntimeError("synthetic runner failure")

    with pytest.raises(RuntimeError, match="synthetic runner failure"):
        orchestrate_integration_tests(
            anchor=_validated_anchor(),
            base_environment={},
            resources=_DetailedCleanupResources(events),
            run_child=run_child,
            python_executable="python",
            repository_root=Path("/repository"),
        )

    assert events == [
        "suite-lock-acquire",
        "anchor-snapshot",
        "regular-provision",
        "child-regular",
        "regular-database-drop",
        "fixed-runtime-roles-drop",
        "anchor-snapshot",
        "suite-lock-release",
    ]


def test_regular_provision_records_safe_roles_after_bootstrap_post_commit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bootstrap commit 后验证失败仍须冻结角色 provenance 并完整清理。"""
    events: list[str] = []
    original_error = RuntimeError("synthetic bootstrap post-commit verification failure")
    roles_committed = False

    class _FakeEngine:
        """记录 management/target Engine 的最终释放。"""

        hide_parameters = True

        def __init__(self, label: str) -> None:
            self._label = label

        def dispose(self) -> None:
            """模拟 NullPool Engine dispose。"""
            events.append(f"dispose-{self._label}")

    class _FakeCleanup:
        """只有安全记录双角色后才允许模拟精确 database/role cleanup。"""

        def __init__(self) -> None:
            self.roles_recorded = False

        def assert_initial_absence(self) -> None:
            """冻结本轮数据库与 fixed roles 初始均不存在。"""
            events.append("assert-initial-absence")

        def create_database(self) -> bool:
            """模拟创建并记录 exact UUID database provenance。"""
            events.append("create-database")
            return True

        def record_created_runtime_roles_if_safe(self) -> bool:
            """仅在合成 commit 后读取并冻结两个 safe runtime roles。"""
            assert roles_committed is True
            self.roles_recorded = True
            events.append("record-safe-runtime-roles")
            return True

        def record_created_runtime_roles(self) -> None:
            """异常路径不得依赖只在正常返回后调用的旧 recorder。"""
            raise AssertionError("unsafe late role recorder was used")

    cleanup = _FakeCleanup()

    @contextmanager
    def fake_managed_cleanup(
        management_engine: object,
        *,
        database_name: str,
    ) -> Iterator[_FakeCleanup]:
        """模拟 fail-closed cleanup，并展开精确数据库/角色删除事件。"""
        del management_engine
        assert database_name.startswith("ai_employee_suite_")
        events.append("cleanup-enter")
        try:
            yield cleanup
        finally:
            if roles_committed and not cleanup.roles_recorded:
                events.append("cleanup-refused-unrecorded-roles")
                raise integration_suite_module.IntegrationSuiteInvariantError(
                    "synthetic cleanup refused unrecorded roles"
                )
            events.extend(
                (
                    "drop-created-database",
                    "drop-created-app-role",
                    "drop-created-retention-role",
                    "cleanup-exit",
                )
            )

    engines = iter((_FakeEngine("management"), _FakeEngine("target")))

    def fake_engine(url: object) -> _FakeEngine:
        """按 production 构造顺序返回 management 与 target Engine。"""
        del url
        return next(engines)

    def fake_role_bootstrap(context: object) -> None:
        """模拟角色事务已 commit，但 post-commit verifier 随后失败。"""
        nonlocal roles_committed
        del context
        roles_committed = True
        events.append("bootstrap-roles-committed")
        raise original_error

    monkeypatch.setattr(
        integration_suite_module.SqlAlchemyIntegrationSuiteResources,
        "_engine",
        staticmethod(fake_engine),
    )
    monkeypatch.setattr(
        integration_suite_module,
        "managed_disposable_database_cleanup",
        fake_managed_cleanup,
    )
    monkeypatch.setattr(
        integration_suite_module,
        "SqlAlchemyDatabaseMaintenanceContext",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        integration_suite_module,
        "role_bootstrap_database",
        fake_role_bootstrap,
    )
    monkeypatch.setattr(
        integration_suite_module,
        "migrate_database",
        lambda context: (_ for _ in ()).throw(
            AssertionError("migration must not run after bootstrap failure")
        ),
    )
    resources = integration_suite_module.SqlAlchemyIntegrationSuiteResources(
        _REPOSITORY_ROOT
    )

    with (
        pytest.raises(RuntimeError) as caught,
        resources.provision_regular_database(_validated_anchor()),
    ):
        raise AssertionError("regular database must not be yielded")

    assert caught.value is original_error
    assert events == [
        "cleanup-enter",
        "assert-initial-absence",
        "create-database",
        "bootstrap-roles-committed",
        "record-safe-runtime-roles",
        "dispose-target",
        "drop-created-database",
        "drop-created-app-role",
        "drop-created-retention-role",
        "cleanup-exit",
        "dispose-management",
    ]


@pytest.mark.parametrize("failure_stage", ("enter", "exit"))
def test_cycle5_regular_context_rechecks_lease_and_anchor_after_provision_failure(
    failure_stage: str,
) -> None:
    """provision enter/exit 异常后仍须复核 lease、anchor 并重抛原异常。"""
    events: list[str] = []
    original_error = RuntimeError(f"synthetic provision {failure_stage} failure")

    class _Lease:
        """记录每次 suite lease 连续性复核。"""

        def verify(self) -> None:
            """当前用例所有复核均成功。"""
            events.append("lease-verify")

    class _Resources:
        """在指定 provision 边界抛出同一个合成异常。"""

        @contextmanager
        def acquire_suite_lock(
            self,
            anchor: ValidatedTestDatabaseUrl,
        ) -> Iterator[_Lease]:
            assert anchor.value == _ANCHOR_URL
            events.append("suite-lock-acquire")
            try:
                yield _Lease()
            finally:
                events.append("suite-lock-release")

        def capture_anchor(self, anchor: ValidatedTestDatabaseUrl) -> object:
            assert anchor.value == _ANCHOR_URL
            events.append("anchor-snapshot")
            return "unchanged"

        @contextmanager
        def provision_regular_database(
            self,
            anchor: ValidatedTestDatabaseUrl,
        ) -> Iterator[DatabaseUrlValue]:
            assert anchor.value == _ANCHOR_URL
            events.append("regular-provision")
            if failure_stage == "enter":
                raise original_error
            try:
                yield _REGULAR_URL
            finally:
                events.append("regular-cleanup")
                raise original_error

    managed_cycle5_context = getattr(
        integration_suite_module,
        "managed_cycle5_regular_database",
        None,
    )
    assert managed_cycle5_context is not None

    with (
        pytest.raises(RuntimeError) as caught,
        managed_cycle5_context(
            anchor=_validated_anchor(),
            resources=_Resources(),
        ) as regular_url,
    ):
        assert regular_url == _REGULAR_URL

    assert caught.value is original_error
    if failure_stage == "enter":
        assert events == [
            "suite-lock-acquire",
            "anchor-snapshot",
            "regular-provision",
            "lease-verify",
            "anchor-snapshot",
            "suite-lock-release",
        ]
    else:
        assert events == [
            "suite-lock-acquire",
            "anchor-snapshot",
            "regular-provision",
            "lease-verify",
            "lease-verify",
            "regular-cleanup",
            "lease-verify",
            "anchor-snapshot",
            "suite-lock-release",
        ]


@pytest.mark.parametrize("failure_stage", ("enter", "body", "exit"))
def test_cycle5_regular_context_finalizes_after_non_exception_base_failure(
    failure_stage: str,
) -> None:
    """任意 BaseException 路径也须在 cleanup 后复核 lease 与 anchor。"""
    events: list[str] = []

    class _SyntheticBaseFailure(BaseException):
        """模拟不属于 Exception 的测试控制流或进程控制异常。"""

    original_error = _SyntheticBaseFailure(
        f"synthetic BaseException {failure_stage} failure"
    )

    class _Lease:
        """记录每次 suite lease 连续性复核。"""

        def verify(self) -> None:
            """当前用例所有 lease 复核均成功。"""
            events.append("lease-verify")

    class _Resources:
        """从 enter/body/exit 三个边界注入同一 BaseException。"""

        @contextmanager
        def acquire_suite_lock(
            self,
            anchor: ValidatedTestDatabaseUrl,
        ) -> Iterator[_Lease]:
            assert anchor.value == _ANCHOR_URL
            events.append("suite-lock-acquire")
            try:
                yield _Lease()
            finally:
                events.append("suite-lock-release")

        def capture_anchor(self, anchor: ValidatedTestDatabaseUrl) -> object:
            assert anchor.value == _ANCHOR_URL
            events.append("anchor-snapshot")
            return "unchanged"

        @contextmanager
        def provision_regular_database(
            self,
            anchor: ValidatedTestDatabaseUrl,
        ) -> Iterator[DatabaseUrlValue]:
            assert anchor.value == _ANCHOR_URL
            events.append("regular-provision")
            if failure_stage == "enter":
                raise original_error
            try:
                yield _REGULAR_URL
            finally:
                events.append("regular-cleanup")
                if failure_stage == "exit":
                    raise original_error

    managed_cycle5_context = getattr(
        integration_suite_module,
        "managed_cycle5_regular_database",
        None,
    )
    assert managed_cycle5_context is not None

    with (
        pytest.raises(_SyntheticBaseFailure) as caught,
        managed_cycle5_context(
            anchor=_validated_anchor(),
            resources=_Resources(),
        ) as regular_url,
    ):
        assert regular_url == _REGULAR_URL
        if failure_stage == "body":
            raise original_error

    assert caught.value is original_error
    if failure_stage == "enter":
        assert events == [
            "suite-lock-acquire",
            "anchor-snapshot",
            "regular-provision",
            "lease-verify",
            "anchor-snapshot",
            "suite-lock-release",
        ]
    else:
        assert events == [
            "suite-lock-acquire",
            "anchor-snapshot",
            "regular-provision",
            "lease-verify",
            "lease-verify",
            "regular-cleanup",
            "lease-verify",
            "anchor-snapshot",
            "suite-lock-release",
        ]


def test_cycle5_regular_context_preserves_provision_and_sanitized_finalization_errors(
) -> None:
    """cleanup 后 lease 与 anchor 同时失败时保留多错误且隐藏底层文本。"""
    events: list[str] = []
    original_error = RuntimeError("synthetic provision exit failure")

    class _Lease:
        """第三次复核模拟 cleanup 后 management lease 驱动失败。"""

        def __init__(self) -> None:
            self._verification_count = 0

        def verify(self) -> None:
            """只在最终复核抛出可能包含敏感底层文本的异常。"""
            self._verification_count += 1
            events.append("lease-verify")
            if self._verification_count == 3:
                raise RuntimeError("synthetic lease endpoint secret")

    class _Resources:
        """provision exit 与第二次 anchor snapshot 分别注入异常。"""

        def __init__(self) -> None:
            self._anchor_count = 0

        @contextmanager
        def acquire_suite_lock(
            self,
            anchor: ValidatedTestDatabaseUrl,
        ) -> Iterator[_Lease]:
            assert anchor.value == _ANCHOR_URL
            events.append("suite-lock-acquire")
            try:
                yield _Lease()
            finally:
                events.append("suite-lock-release")

        def capture_anchor(self, anchor: ValidatedTestDatabaseUrl) -> object:
            assert anchor.value == _ANCHOR_URL
            self._anchor_count += 1
            events.append("anchor-snapshot")
            if self._anchor_count == 2:
                raise RuntimeError("synthetic anchor catalog secret")
            return "unchanged"

        @contextmanager
        def provision_regular_database(
            self,
            anchor: ValidatedTestDatabaseUrl,
        ) -> Iterator[DatabaseUrlValue]:
            assert anchor.value == _ANCHOR_URL
            events.append("regular-provision")
            try:
                yield _REGULAR_URL
            finally:
                events.append("regular-cleanup")
                raise original_error

    managed_cycle5_context = getattr(
        integration_suite_module,
        "managed_cycle5_regular_database",
        None,
    )
    assert managed_cycle5_context is not None

    with (
        pytest.raises(ExceptionGroup) as caught,
        managed_cycle5_context(
            anchor=_validated_anchor(),
            resources=_Resources(),
        ),
    ):
        pass

    assert caught.value.__cause__ is original_error
    assert [str(error) for error in caught.value.exceptions] == [
        "Cycle 5 suite lease verification failed",
        "Cycle 5 anchor verification failed",
    ]
    formatted = "".join(traceback.format_exception(caught.value))
    assert "synthetic lease endpoint secret" not in formatted
    assert "synthetic anchor catalog secret" not in formatted
    assert events == [
        "suite-lock-acquire",
        "anchor-snapshot",
        "regular-provision",
        "lease-verify",
        "lease-verify",
        "regular-cleanup",
        "lease-verify",
        "anchor-snapshot",
        "suite-lock-release",
    ]


@pytest.mark.asyncio
async def test_managed_session_factory_registry_disposes_on_test_body_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试体异常也必须逆序释放本测试创建的全部异步连接池。"""
    events: list[str] = []

    class _FakeManagedSessionFactory:
        """记录 registry 对独立 factory 的释放调用。"""

        def __init__(self, label: str) -> None:
            self._label = label

        async def dispose(self) -> None:
            """模拟真实 ``ManagedAsyncSessionMaker.dispose``。"""
            events.append(f"dispose-{self._label}")

    labels = iter(("first", "second"))

    def fake_build_session_factory(
        database_url: str,
        *,
        task_event_publisher: object | None = None,
    ) -> _FakeManagedSessionFactory:
        """为每次构造返回可独立追踪的合成 factory。"""
        del database_url, task_event_publisher
        label = next(labels)
        events.append(f"build-{label}")
        return _FakeManagedSessionFactory(label)

    monkeypatch.setattr(
        integration_suite_module,
        "build_session_factory",
        fake_build_session_factory,
        raising=False,
    )
    managed_registry = getattr(
        integration_suite_module,
        "managed_session_factory_registry",
        None,
    )
    assert managed_registry is not None, "Cycle 5 缺少异常安全的 session factory registry"

    with pytest.raises(RuntimeError, match="synthetic test body failure"):
        async with managed_registry() as registry:
            registry.build("postgresql+asyncpg://synthetic:first@localhost/one_test")
            registry.build("postgresql+asyncpg://synthetic:second@localhost/two_test")
            raise RuntimeError("synthetic test body failure")

    assert events == [
        "build-first",
        "build-second",
        "dispose-second",
        "dispose-first",
    ]


@pytest.mark.asyncio
async def test_managed_session_factory_registry_disposes_all_after_middle_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个 pool 释放失败也必须逆序尝试全部 factory，并清空 registry。"""
    events: list[str] = []

    class _FakeManagedSessionFactory:
        """按标签记录 dispose，并只让 middle factory 抛出合成异常。"""

        def __init__(self, label: str) -> None:
            self._label = label

        async def dispose(self) -> None:
            """记录释放尝试；middle 模拟单个 Engine dispose 失败。"""
            events.append(f"dispose-{self._label}")
            if self._label == "middle":
                raise RuntimeError("synthetic middle dispose failure")

    labels = iter(("first", "middle", "third"))

    def fake_build_session_factory(
        database_url: str,
        *,
        task_event_publisher: object | None = None,
    ) -> _FakeManagedSessionFactory:
        """为三次 build 返回独立且可追踪的合成 factory。"""
        del database_url, task_event_publisher
        label = next(labels)
        events.append(f"build-{label}")
        return _FakeManagedSessionFactory(label)

    monkeypatch.setattr(
        integration_suite_module,
        "build_session_factory",
        fake_build_session_factory,
        raising=False,
    )
    registry = integration_suite_module.ManagedSessionFactoryRegistry()
    registry.build("postgresql+asyncpg://synthetic:first@localhost/one_test")
    registry.build("postgresql+asyncpg://synthetic:middle@localhost/two_test")
    registry.build("postgresql+asyncpg://synthetic:third@localhost/three_test")

    with pytest.raises(RuntimeError, match="synthetic middle dispose failure"):
        await registry.dispose_all()

    assert events == [
        "build-first",
        "build-middle",
        "build-third",
        "dispose-third",
        "dispose-middle",
        "dispose-first",
    ]
    await registry.dispose_all()
    assert events[-3:] == ["dispose-third", "dispose-middle", "dispose-first"]


def test_lifecycle_failure_propagates_exact_exit_code_after_regular_cleanup() -> None:
    """第二 child 的非零状态不得被 parent 改写或伪装成 regular 成功。"""
    events: list[str] = []

    def run_child(invocation: PytestInvocation, suite_lease: object) -> int:
        assert isinstance(suite_lease, _FakeSuiteLease)
        events.append(f"child-{invocation.phase}")
        return 0 if invocation.phase == "regular" else 17

    result = orchestrate_integration_tests(
        anchor=_validated_anchor(),
        base_environment={},
        resources=_FakeResources(events),
        run_child=run_child,
        python_executable="python",
        repository_root=Path("/repository"),
    )

    assert result == 17
    assert events[-2:] == ["child-lifecycle", "suite-lock-release"]


def test_anchor_change_is_rejected_after_cleanup_before_lifecycle_child() -> None:
    """regular phase 触及 anchor 时必须 fail closed，不能继续跑 roles-absent phase。"""
    events: list[str] = []

    def run_child(invocation: PytestInvocation, suite_lease: object) -> int:
        assert isinstance(suite_lease, _FakeSuiteLease)
        events.append(f"child-{invocation.phase}")
        return 0

    with pytest.raises(IntegrationSuiteInvariantError):
        orchestrate_integration_tests(
            anchor=_validated_anchor(),
            base_environment={},
            resources=_FakeResources(events, snapshots=("before", "after")),
            run_child=run_child,
            python_executable="python",
            repository_root=Path("/repository"),
        )

    assert events == [
        "suite-lock-acquire",
        "anchor-snapshot",
        "regular-provision",
        "child-regular",
        "regular-cleanup",
        "anchor-snapshot",
        "suite-lock-release",
    ]


def test_orchestrator_verifies_suite_lock_at_all_five_phase_boundaries() -> None:
    """regular 前后、cleanup 后及 lifecycle 前后都必须显式重验同一 lease。"""
    events: list[str] = []
    lease = _FakeSuiteLease()

    result = orchestrate_integration_tests(
        anchor=_validated_anchor(),
        base_environment={},
        resources=_FakeResources(events, lease=lease),
        run_child=lambda invocation, suite_lease: 0,
        python_executable="python",
        repository_root=Path("/repository"),
    )

    assert result == 0
    assert lease.verification_count == 5


def test_cleanup_boundary_lock_loss_forbids_lifecycle_child() -> None:
    """regular cleanup 后失锁属于基础设施异常，不能继续进入 roles-absent phase。"""
    events: list[str] = []
    phases: list[str] = []
    lease = _FakeSuiteLease(fail_on_verification=3)

    def run_child(invocation: PytestInvocation, suite_lease: object) -> int:
        assert isinstance(suite_lease, _FakeSuiteLease)
        phases.append(invocation.phase)
        return 0

    with pytest.raises(
        IntegrationSuiteInvariantError,
        match=r"^integration suite lock continuity lost$",
    ):
        orchestrate_integration_tests(
            anchor=_validated_anchor(),
            base_environment={},
            resources=_FakeResources(events, lease=lease),
            run_child=run_child,
            python_executable="python",
            repository_root=Path("/repository"),
        )

    assert phases == ["regular"]
    assert events[-2:] == ["regular-cleanup", "suite-lock-release"]


def test_running_child_is_terminated_killed_and_reaped_when_suite_lock_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行中失锁必须回收 child，并传播不含 invocation/env 的稳定异常。"""
    process = _RunningChild()
    popen_calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def fake_popen(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> _RunningChild:
        popen_calls.append((tuple(argv), cwd, env))
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    invocation = PytestInvocation(
        phase="regular",
        argv=("python", "-m", "pytest", "synthetic-node"),
        environment={"TEST_DATABASE_URL": "postgresql://synthetic-secret-marker"},
        cwd=Path("/repository"),
    )
    lease = _WatchdogLease()

    with pytest.raises(
        IntegrationSuiteInvariantError,
        match=r"^integration suite lock continuity lost$",
    ) as caught:
        run_pytest_child(invocation, lease)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls[-1] is not None
    assert lease.verification_count == 2
    assert popen_calls == [
        (
            invocation.argv,
            invocation.cwd,
            dict(invocation.environment),
        )
    ]
    assert "synthetic-secret-marker" not in str(caught.value)
    assert "synthetic-secret-marker" not in repr(invocation)


def test_orchestrator_forbids_lifecycle_when_running_regular_child_loses_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watchdog 基础设施异常必须回收 regular child，并阻止 lifecycle Popen。"""
    events: list[str] = []
    process = _RunningChild()
    popen_phases: list[str] = []

    def fake_popen(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> _RunningChild:
        del cwd, env
        popen_phases.append("regular" if "backend/tests/contract" in argv else "lifecycle")
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    lease = _FakeSuiteLease(fail_on_verification=3)

    with pytest.raises(
        IntegrationSuiteInvariantError,
        match=r"^integration suite lock continuity lost$",
    ):
        orchestrate_integration_tests(
            anchor=_validated_anchor(),
            base_environment={},
            resources=_FakeResources(events, lease=lease),
            run_child=run_pytest_child,
            python_executable="python",
            repository_root=Path("/repository"),
        )

    assert popen_phases == ["regular"]
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert events[-3:] == [
        "regular-cleanup",
        "anchor-snapshot",
        "suite-lock-release",
    ]


def test_terminate_oserror_still_waits_kills_and_proves_child_reaped() -> None:
    """terminate OSError 不能提前返回；仍须有界 wait、kill 与最终 reap。"""
    process = _ScriptedChild(
        poll_outcomes=[None],
        wait_outcomes=[
            subprocess.TimeoutExpired(cmd=("synthetic",), timeout=5.0),
            -9,
        ],
        terminate_error=OSError("synthetic terminate error"),
    )

    _terminate_pytest_child(cast(subprocess.Popen[bytes], process))

    assert process.terminate_calls == 1
    assert process.wait_calls == [5.0, 5.0]
    assert process.kill_calls == 1


def test_kill_oserror_still_waits_and_accepts_proven_exit() -> None:
    """kill OSError 后也必须最终 wait；只有返回 exit code 才能视为已回收。"""
    process = _ScriptedChild(
        poll_outcomes=[None],
        wait_outcomes=[
            subprocess.TimeoutExpired(cmd=("synthetic",), timeout=5.0),
            143,
        ],
        kill_error=OSError("synthetic kill error"),
    )

    _terminate_pytest_child(cast(subprocess.Popen[bytes], process))

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == [5.0, 5.0]


def test_poll_oserror_continues_with_terminate_and_bounded_wait() -> None:
    """初始 poll OSError 不能泄漏或跳过回收；terminate wait 可证明退出。"""
    process = _ScriptedChild(
        poll_outcomes=[OSError("synthetic poll error")],
        wait_outcomes=[0],
    )

    _terminate_pytest_child(cast(subprocess.Popen[bytes], process))

    assert process.poll_calls == 1
    assert process.terminate_calls == 1
    assert process.wait_calls == [5.0]
    assert process.kill_calls == 0


def test_final_wait_timeout_without_reliable_exit_fails_closed() -> None:
    """kill 后 wait 与最终 poll 均不能证明退出时，必须抛稳定 cleanup invariant。"""
    process = _ScriptedChild(
        poll_outcomes=[None, None],
        wait_outcomes=[
            subprocess.TimeoutExpired(cmd=("synthetic",), timeout=5.0),
            subprocess.TimeoutExpired(cmd=("synthetic",), timeout=5.0),
        ],
    )

    with pytest.raises(
        IntegrationSuiteInvariantError,
        match=r"^integration child cleanup could not be confirmed$",
    ):
        _terminate_pytest_child(cast(subprocess.Popen[bytes], process))

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == [5.0, 5.0]
    assert process.poll_calls == 2


def test_unreaped_child_cleanup_invariant_chains_original_lease_error_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无法证明回收时 cleanup invariant 为主异常，并保留原稳定 lease 异常链。"""
    process = _ScriptedChild(
        poll_outcomes=[None, None],
        wait_outcomes=[
            subprocess.TimeoutExpired(cmd=("synthetic",), timeout=0.25),
            subprocess.TimeoutExpired(cmd=("synthetic",), timeout=5.0),
            subprocess.TimeoutExpired(cmd=("synthetic",), timeout=5.0),
        ],
    )

    def fake_popen(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> _ScriptedChild:
        del argv, cwd, env
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    invocation = PytestInvocation(
        phase="regular",
        argv=("python", "-m", "pytest", "synthetic-node"),
        environment={"TEST_DATABASE_URL": "postgresql://synthetic-secret-marker"},
        cwd=Path("/repository"),
    )

    with pytest.raises(
        IntegrationSuiteInvariantError,
        match=r"^integration child cleanup could not be confirmed$",
    ) as caught:
        run_pytest_child(invocation, _WatchdogLease())

    assert isinstance(caught.value.__cause__, IntegrationSuiteInvariantError)
    assert str(caught.value.__cause__) == "integration suite lock continuity lost"
    assert "synthetic-secret-marker" not in str(caught.value)
    assert "synthetic-secret-marker" not in str(caught.value.__cause__)
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == [0.25, 5.0, 5.0]
