"""CI 基线构造必须绑定真实服务、完整空态与归档；失败保全不能退化为修补或重试。"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from ai_employee.infrastructure.db.database_url import validate_test_database_url

ROOT = Path(__file__).resolve().parents[3]
IMAGE_ID = "sha256:" + "a" * 64
CONTAINER_ID = "b" * 64
DONOR_ID = "c" * 64


def _subject() -> ModuleType:
    """按测试体导入新准备入口，使首次 RED 保留缺失功能而不影响其他测试收集。"""
    return importlib.import_module("tests.integration.ci_database")


def _request(module: ModuleType, tmp_path: Path) -> object:
    """只构造本地合成服务的显式身份，环境布尔值不参与授权。"""
    return module.CiDatabaseRequest(
        repository_root=ROOT,
        directory=tmp_path / "ci-fixture",
        container_id=CONTAINER_ID,
        run_id="30001",
        run_attempt="1",
        job_name="verify",
        target=validate_test_database_url(
            "postgresql+asyncpg://postgres:synthetic-ci-input@127.0.0.1:5432/ai_employee_test"
        ),
    )


def _inspection() -> tuple[dict[str, object], dict[str, object]]:
    """镜像 Docker inspect 的完整使用边界，额外 engine 字段由解析器忽略。"""
    return {
        "Id": CONTAINER_ID,
        "Name": "/synthetic-ci-postgres",
        "Image": IMAGE_ID,
        "State": {"Running": True},
        "Config": {
            "Labels": {
                "ai_employee.ci_run": "30001", "ai_employee.ci_attempt": "1",
                "ai_employee.ci_job": "verify", "ai_employee.ci_role": "target",
            },
            "Env": ["POSTGRES_USER=postgres", "POSTGRES_DB=ai_employee_test"],
        },
        "NetworkSettings": {"Ports": {
            "5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}],
        }},
        "Mounts": [{
            "Type": "volume", "Name": "synthetic-ci-data", "Source": "/synthetic/volume",
            "Destination": "/var/lib/postgresql/data", "RW": True,
        }],
    }, {
        "Name": "synthetic-ci-data", "Driver": "local", "Mountpoint": "/synthetic/volume",
        "Labels": {},
    }


@pytest.mark.parametrize("fault", [
    "container", "run", "attempt", "job", "role", "image", "stopped", "port", "public_port",
    "database", "owner", "mount", "volume", "volume_path",
])
def test_service_binding_rejects_mismatched_engine_facts(tmp_path: Path, fault: str) -> None:
    """任一资源身份差异均拒绝，不能仅凭名字、CI=true 或可连通端口获得写入权。"""
    module = _subject()
    request = _request(module, tmp_path)
    inspected, volume = copy.deepcopy(_inspection())
    match fault:
        case "container": inspected["Id"] = DONOR_ID
        case "run" | "attempt" | "job" | "role":
            inspected["Config"]["Labels"]["ai_employee.ci_" + fault] = "wrong"  # type: ignore[index]
        case "image": inspected["Image"] = "sha256:" + "d" * 64
        case "stopped": inspected["State"]["Running"] = False  # type: ignore[index]
        case "port": inspected["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"] = "5433"  # type: ignore[index]
        case "public_port": inspected["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"  # type: ignore[index]
        case "database": inspected["Config"]["Env"] = ["POSTGRES_USER=postgres", "POSTGRES_DB=other_test"]  # type: ignore[index]
        case "owner": inspected["Config"]["Env"] = ["POSTGRES_USER=other", "POSTGRES_DB=ai_employee_test"]  # type: ignore[index]
        case "mount": inspected["Mounts"][0]["RW"] = False  # type: ignore[index]
        case "volume": volume["Name"] = "another-volume"
        case "volume_path": volume["Mountpoint"] = "/synthetic/other"
    with pytest.raises(module.CiDatabasePreparationError):
        module.bind_service(request, inspected, volume, IMAGE_ID)


def _contents(module: ModuleType) -> object:
    """独立合成完整证明，用不同类别/表的变化验证后验不能只比版本或总行数。"""
    return module.SeedContents(
        revision="20260809_0018", fingerprint="e" * 64,
        schema_categories=((5, "1" * 64), (9, "2" * 64)),
        table_summaries=(("alembic_version", 1, "3" * 64), ("users", 0, "4" * 64)),
        namespace_digest="5" * 64, sequence_values=(("synthetic_sequence", 1, False),),
    )


def _empty(module: ModuleType) -> object:
    """空 target 的全部可比较事实；物理 OID 只在同一目标的前后身份中比较。"""
    return module.DatabaseSnapshot(
        identity=module.DatabaseIdentity("7684000000000000001", 16384, "ai_employee_test", 10, "postgres", 170005),
        owner_role_digest=hashlib.sha256(b"[true,true,true,true,true,true,true,-1,null,null]").hexdigest(),
        role_memberships=(
            ("pg_read_all_settings", "pg_monitor", "postgres", False, True, True),
            ("pg_read_all_stats", "pg_monitor", "postgres", False, True, True),
            ("pg_stat_scan_tables", "pg_monitor", "postgres", False, True, True),
        ),
        database_acl=None, public_schema_owner="pg_database_owner",
        public_schema_acl="{pg_database_owner=UC/pg_database_owner,=U/pg_database_owner}",
        settings_count=0, settings_digest="0" * 64, runtime_roles=(), runtime_roles_digest="0" * 64,
        maintenance_fact_count=0, other_client_count=0, advisory_lock_count=0,
        foreign_schema_count=0, unexpected_role_count=0, public_object_count=0, object_acl_count=0,
        contents=None,
    )


class _SyntheticOperations:
    """仅替换 engine/数据库/进程 I/O；准备顺序、空态判断、CAS 与发布逻辑均为真实实现。"""

    def __init__(self, module: ModuleType, request: object, fault: str | None) -> None:
        self.module, self.request, self.fault = module, request, fault
        self.calls: list[str] = []
        self.target_reads = 0
        self.donor_reads = 0
        self.imported = False
        self.contents = _contents(module)
        self.empty = _empty(module)
        inspected, volume = _inspection()
        self.target = module.CiDatabaseTarget(module.bind_service(request, inspected, volume, IMAGE_ID), request.target)
        donor_binding = replace(self.target.binding, container_id=DONOR_ID, database_name="ai_employee_ci_donor_test")
        self.donor = module.CiDatabaseTarget(donor_binding, validate_test_database_url(
            "postgresql+asyncpg://postgres:synthetic-ci-input@127.0.0.1:5433/ai_employee_ci_donor_test"
        ))

    def inspect_target(self) -> object:
        """真实解析器已经验证合成 inspect，方法只模拟外部 engine 返回时机。"""
        self.calls.append("inspect_target")
        return self.target

    def observe(self, target: object) -> object:
        """每次返回独立观测；在明确阶段注入漂移，而非替换待测断言。"""
        self.calls.append("observe_" + ("target" if target is self.target else "donor"))
        if target is self.target:
            self.target_reads += 1
            snapshot = replace(self.empty, public_object_count=14, contents=self.contents) if self.imported else self.empty
            changes = {
                "dirty_target": {"public_object_count": 1}, "roles": {"runtime_roles": ("ai_employee_app",)},
                "facts": {"maintenance_fact_count": 1}, "clients": {"other_client_count": 1},
                "locks": {"advisory_lock_count": 1}, "settings": {"settings_count": 1},
                "foreign_schema": {"foreign_schema_count": 1}, "acl": {"database_acl": "changed"},
                "owner_attributes": {"owner_role_digest": "9" * 64}, "memberships": {"role_memberships": ()},
                "public_owner": {"public_schema_owner": "postgres"}, "public_acl": {"public_schema_acl": "changed"},
            }
            if self.fault in changes and self.target_reads == 1:
                return replace(snapshot, **changes[self.fault])
            if self.fault == "snapshot_changed" and self.target_reads == 2:
                return replace(snapshot, public_schema_acl="changed")
            if self.imported:
                if self.fault == "post_schema":
                    return replace(snapshot, contents=replace(self.contents, schema_categories=((5, "9" * 64),)))
                if self.fault == "post_data":
                    return replace(snapshot, contents=replace(self.contents, table_summaries=(("alembic_version", 1, "9" * 64),)))
                if self.fault == "post_sequence":
                    return replace(snapshot, contents=replace(self.contents, sequence_values=(("synthetic_sequence", 2, True),)))
                if self.fault in {"post_roles", "post_facts", "post_clients", "post_locks", "post_acl"}:
                    return replace(snapshot, **changes[self.fault.removeprefix("post_")])
            return snapshot
        self.donor_reads += 1
        snapshot = replace(self.empty, identity=replace(self.empty.identity, database_name="ai_employee_ci_donor_test", system_identifier="7684000000000000002"))
        if self.donor_reads > 1:
            snapshot = replace(snapshot, database_acl="donor-baseline", runtime_roles=("ai_employee_app", "ai_employee_retention"), contents=self.contents, public_object_count=14, object_acl_count=3)
        if self.fault == "donor_drift" and self.donor_reads > 2:
            snapshot = replace(snapshot, advisory_lock_count=1)
        return snapshot

    def create_donor(self, image_id: str) -> object:
        """记录创建前的实际顺序；失败测试要求初始脏态下该方法从未到达。"""
        assert image_id == IMAGE_ID
        self.calls.append("create_donor")
        return self.donor

    def initialize_donor(self, donor: object) -> None:
        """外部 typed bootstrap/upgrade 在此被隔离；真实资源回放另行验证。"""
        assert donor is self.donor
        self.calls.append("initialize_donor")

    def export_donor(self, donor: object) -> object:
        """保存真实文件字节供待测哈希边界使用；失败或篡改不由 Fake 帮助验证。"""
        assert donor is self.donor
        self.calls.append("export_donor")
        data = b"PGDMP-synthetic-empty-0018"
        path = self.request.directory / "empty-0018.dump"
        path.write_bytes(data)
        path.chmod(0o600)
        if self.fault == "dump_failure":
            raise RuntimeError("synthetic dump failure")
        archive = self.module.ArchiveRecord(path, len(data), hashlib.sha256(data).hexdigest(), self.contents, "pg_dump (PostgreSQL) 17.5")
        if self.fault == "archive_changed":
            path.write_bytes(b"PGDMP-other-bytes")
        return archive

    def import_archive(self, target: object, archive: object) -> None:
        """记录真实 coordinator 是否到达导入；不执行数据库写入。"""
        assert target is self.target and archive.path.exists()
        self.calls.append("import_archive")
        if self.fault == "import_failure":
            raise RuntimeError("synthetic import failure")
        self.imported = True

    def stop_donor(self, donor: object) -> None:
        """只有全部后验通过才可到达停止；不提供删除或清理方法。"""
        assert donor is self.donor
        self.calls.append("stop_donor")
        if self.fault == "stop_failure":
            raise RuntimeError("synthetic stop failure")


@pytest.mark.parametrize("fault", [
    "dirty_target", "roles", "facts", "clients", "locks", "settings", "foreign_schema", "acl",
    "owner_attributes", "memberships", "public_owner", "public_acl",
    "snapshot_changed", "dump_failure", "archive_changed", "import_failure", "post_schema", "post_data",
    "post_sequence", "post_roles", "post_facts", "post_clients", "post_locks", "post_acl", "donor_drift", "stop_failure",
])
def test_preparation_rejects_drift_without_repair_retry_or_ready_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    """每个真实阶段失败都保留文件，导入最多一次；未通过后验时不得停止或宣告 ready。"""
    module = _subject()
    request = _request(module, tmp_path)
    operations = _SyntheticOperations(module, request, fault)
    monkeypatch.setattr(module, "NativeCiDatabaseOperations", lambda _request: operations)
    with pytest.raises((module.CiDatabasePreparationError, RuntimeError)):
        module.prepare_ci_database(request)
    assert not (request.directory / "ready.json").exists()
    assert (request.directory / "failure.json").is_file()
    assert operations.calls.count("import_archive") <= 1
    if fault in {"dirty_target", "roles", "facts", "clients", "locks", "settings", "foreign_schema", "acl",
                 "owner_attributes", "memberships", "public_owner", "public_acl"}:
        assert "create_donor" not in operations.calls
    if fault != "stop_failure":
        assert "stop_donor" not in operations.calls
    if "export_donor" in operations.calls:
        assert (request.directory / "empty-0018.dump").is_file()


def test_preparation_publishes_only_after_full_postconditions_and_owned_donor_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功必须有完整归档与前后证明，发布发生在唯一导入和 fresh donor 后验之后。"""
    module = _subject()
    request = _request(module, tmp_path)
    operations = _SyntheticOperations(module, request, None)
    monkeypatch.setattr(module, "NativeCiDatabaseOperations", lambda _request: operations)
    module.prepare_ci_database(request)
    ready = json.loads((request.directory / "ready.json").read_text())
    assert ready["result_code"] == "ci_database_anchor_ready"
    assert ready["revision"] == "20260809_0018"
    assert operations.calls.count("import_archive") == operations.calls.count("stop_donor") == 1
    assert operations.calls[-2:] == ["observe_donor", "stop_donor"]
    assert not (request.directory / "failure.json").exists()
    assert (request.directory / "empty-0018.dump").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("fault", ["missing_id", "short_id", "wrong_job", "missing_run", "pg_override", "different_database", "public_host"])
def test_request_requires_explicit_job_identity_and_unambiguous_test_endpoint(tmp_path: Path, fault: str) -> None:
    """CI 布尔标记不能补齐缺失身份；libpq 覆盖和其他数据库在 engine/连接构造前拒绝。"""
    module = _subject()
    environment = {
        "CI": "true", "GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(tmp_path),
        "CI_DATABASE_CONTAINER_ID": CONTAINER_ID, "CI_DATABASE_RUN_ID": "30001",
        "CI_DATABASE_RUN_ATTEMPT": "1", "CI_DATABASE_JOB": "verify",
        "TEST_DATABASE_URL": "postgresql+asyncpg://postgres:synthetic-ci-input@127.0.0.1:5432/ai_employee_test",
    }
    match fault:
        case "missing_id": environment.pop("CI_DATABASE_CONTAINER_ID")
        case "short_id": environment["CI_DATABASE_CONTAINER_ID"] = "b" * 12
        case "wrong_job": environment["CI_DATABASE_JOB"] = "other"
        case "missing_run": environment.pop("CI_DATABASE_RUN_ID")
        case "pg_override": environment["PGHOSTADDR"] = "192.0.2.1"
        case "different_database": environment["TEST_DATABASE_URL"] = environment["TEST_DATABASE_URL"].replace("ai_employee_test", "other_test")
        case "public_host": environment["TEST_DATABASE_URL"] = environment["TEST_DATABASE_URL"].replace("127.0.0.1", "192.0.2.1")
    with pytest.raises(ValueError if fault == "public_host" else module.CiDatabasePreparationError):
        module.read_request(environment, ROOT)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("occupied", ["container", "volume"])
def test_native_donor_never_reuses_preexisting_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, occupied: str,
) -> None:
    """实际 donor 创建分支先证明两类资源不存在；任一已有资源都零 create/run 拒绝。"""
    module = _subject()
    request = _request(module, tmp_path)
    request.directory.mkdir(mode=0o700)
    operations = module.NativeCiDatabaseOperations(request)
    calls: list[tuple[str, ...]] = []

    def engine(*arguments: str, **_options: object) -> bytes:
        """仅响应只读 list；实现若提前创建或重用现有对象，测试立即失败。"""
        calls.append(arguments)
        assert arguments[:2] in {("container", "ls"), ("volume", "ls")}
        return b"existing-resource\n" if arguments[0] == occupied else b""

    monkeypatch.setattr(operations, "_docker", engine)
    with pytest.raises(module.CiDatabasePreparationError):
        operations.create_donor(IMAGE_ID)
    assert len(calls) == 2
    assert (request.directory / "donor-registered.json").exists()
    assert not (request.directory / "donor-secrets").exists()


def test_native_import_keeps_exact_non_destructive_flags_and_cannot_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实导入方法只传一次精确 bytes 和固定非破坏性 argv，第二次尝试被排他记录拒绝。"""
    module = _subject()
    request = _request(module, tmp_path)
    request.directory.mkdir(mode=0o700)
    operations = module.NativeCiDatabaseOperations(request)
    info, volume = _inspection()
    target = module.CiDatabaseTarget(module.bind_service(request, info, volume, IMAGE_ID), request.target)
    data = b"PGDMP-native-import-boundary"
    path = request.directory / "empty-0018.dump"
    path.write_bytes(data)
    path.chmod(0o600)
    archive = module.ArchiveRecord(path, len(data), hashlib.sha256(data).hexdigest(), _contents(module), "pg_dump (PostgreSQL) 17.5")
    calls: list[tuple[tuple[str, ...], bytes]] = []

    def engine(*arguments: str, data: bytes) -> bytes:
        """只替代外部进程，文件验证、固定参数和防重放记录仍执行实际代码。"""
        calls.append((arguments, data))
        return b""

    monkeypatch.setattr(operations, "_revalidate", lambda _target: None)
    monkeypatch.setattr(operations, "_docker", engine)
    operations.import_archive(target, archive)
    with pytest.raises(FileExistsError):
        operations.import_archive(target, archive)
    assert calls == [((
        "exec", "-i", CONTAINER_ID, "pg_restore", "--username=postgres", "--dbname=ai_employee_test",
        "--single-transaction", "--exit-on-error", "--no-owner", "--no-privileges",
    ), data)]


@pytest.mark.parametrize("fault", ["container_name", "container_fixture", "volume_fixture"])
def test_native_donor_requires_exact_registered_name_and_fixture_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    """原生创建分支不能把同 run/job 但不同登记 UUID 的资源接纳为自有 donor。

    仅隔离外部 Docker 进程，登记、Secret 文件、实际 inspect 解析和绑定全都执行
    真实代码。反例分别替换名称或两类 UUID 标签，必须在首次 readiness/数据库访问前拒绝。
    """
    module = _subject()
    request = _request(module, tmp_path)
    request.directory.mkdir(mode=0o700)
    operations = module.NativeCiDatabaseOperations(request)
    calls: list[tuple[str, ...]] = []

    def engine(arguments: tuple[str, ...], *, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        """返回完整合成 engine 使用边界；UUID 取自真实预登记，仅替换待验证的错误字段。"""
        assert data is None
        calls.append(tuple(arguments))
        registered = json.loads((request.directory / "donor-registered.json").read_text())
        inspected, volume = _inspection()
        inspected["Id"] = DONOR_ID
        inspected["Name"] = "/" + registered["container_name"]
        inspected["Config"]["Labels"] = dict(registered["labels"])  # type: ignore[index]
        inspected["Config"]["Env"] = ["POSTGRES_USER=postgres", "POSTGRES_DB=" + registered["database"]]  # type: ignore[index]
        inspected["Mounts"][0]["Name"] = registered["volume"]  # type: ignore[index]
        volume["Name"], volume["Labels"] = registered["volume"], dict(registered["labels"])
        if fault == "container_name":
            inspected["Name"] = "/another-donor"
        elif fault == "container_fixture":
            inspected["Config"]["Labels"]["ai_employee.ci_fixture"] = "another-uuid"  # type: ignore[index]
        else:
            volume["Labels"]["ai_employee.ci_fixture"] = "another-uuid"  # type: ignore[index]
        if arguments[:2] in {("container", "ls"), ("volume", "ls")}:
            output = b""
        elif arguments[:2] == ("volume", "create"):
            output = registered["volume"].encode()
        elif arguments[0] == "run":
            output = DONOR_ID.encode()
        elif arguments == ("inspect", DONOR_ID):
            output = json.dumps([inspected]).encode()
        elif arguments[:2] == ("volume", "inspect"):
            output = json.dumps([volume]).encode()
        else:
            assert arguments[:2] == ("exec", DONOR_ID)
            output = b""
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    monkeypatch.setattr(operations, "_execute", engine)
    with pytest.raises(module.CiDatabasePreparationError):
        operations.create_donor(IMAGE_ID)
    assert not any(arguments[0] == "exec" for arguments in calls)
    assert (request.directory / "donor-created.json").exists()
    assert not (request.directory / "donor-identity.json").exists()
