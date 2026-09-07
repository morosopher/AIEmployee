"""用真实 Uvicorn 子进程和 PostgreSQL 验证 callback 查询零泄露及原子失败审计。

只使用回环监听、测试数据库、临时合成 Secret 与内置离线 OAuth adapter。子进程原始
stdout/stderr 只在临时文件中检查，失败时也不把 canary、凭据或完整日志输出到 pytest。
"""

import asyncio
import base64
import hashlib
import json
import os
import shlex
import socket
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, select

from ai_employee.application.use_cases.connections import (
    ConnectionsUseCase,
    OAuthStateRejectedError,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStoreFactory
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher

USER_ID = UUID("00000000-0000-0000-0000-000000000101")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000202")
ERROR_ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000701")
CODE_ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000702")
NOW = datetime(2030, 1, 1, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[4]
ERROR_STATE = base64.urlsafe_b64encode(b"e" * 32).rstrip(b"=").decode("ascii")
CODE_STATE = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class FixedClock:
    """让事务测试与真实应用子进程都使用明确 UTC 时间，不依赖宿主日期。"""

    def now(self) -> datetime:
        """返回合成回调时刻。"""
        return NOW


async def _seed_attempts(
    sessions: ManagedAsyncSessionMaker,
    *,
    bound: bool = False,
) -> None:
    """建立合成用户和规范 state/AEAD 尝试；bound 分支用于能力失败事务回滚证明。"""
    cipher = AeadCipher(b"k" * 32)
    async with sessions.begin() as session:
        session.add(
            UserModel(
                id=USER_ID,
                email="owner@example.test",
                display_name="Synthetic Owner",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
        )
        await session.flush()
        if bound:
            session.add(
                OAuthConnectionModel(
                    id=CONNECTION_ID,
                    user_id=USER_ID,
                    provider="microsoft",
                    provider_account_id="synthetic-tenant:synthetic-user",
                    provider_tenant_id="synthetic-tenant",
                    account_type="work_school",
                    account_email="owner@example.test",
                    scopes=["Mail.Read"],
                    status="connected",
                    authorization_generation=2,
                )
            )
            await session.flush()
            session.add(
                ConnectionCapabilityModel(
                    user_id=USER_ID,
                    connection_id=CONNECTION_ID,
                    capability="mail.read",
                    status="authorizing",
                    actual_scopes=["Mail.Read"],
                    last_verified_at=NOW - timedelta(days=1),
                )
            )
        for attempt_id, state in ((ERROR_ATTEMPT_ID, ERROR_STATE), (CODE_ATTEMPT_ID, CODE_STATE)):
            encrypted = cipher.encrypt(
                b"v" * 43, f"{USER_ID}:{attempt_id}:pkce_verifier".encode("ascii")
            )
            session.add(
                OAuthAttemptModel(
                    id=attempt_id,
                    user_id=USER_ID,
                    provider="microsoft",
                    state_hash=hashlib.sha256(state.encode("ascii")).digest(),
                    encrypted_pkce_verifier=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    requested_capabilities=["mail.read"],
                    target_connection_id=CONNECTION_ID if bound else None,
                    target_authorization_generation=2 if bound else None,
                    created_at=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                )
            )


def _real_development_uvicorn_options() -> list[str]:
    """从真实 dev recipe 继承日志参数，仅替换 reload/网络绑定以隔离测试进程。"""
    candidates = [
        line
        for line in (ROOT / "justfiles/dev.just").read_text().splitlines()
        if "uvicorn ai_employee.main:app" in line and not line.lstrip().startswith("#")
    ]
    assert len(candidates) == 1
    words = shlex.split(candidates[0])
    options = words[words.index("ai_employee.main:app") + 1 :]
    isolated: list[str] = []
    index = 0
    while index < len(options):
        option = options[index]
        if option in {"--host", "--port"}:
            index += 2
        elif option == "--reload":
            index += 1
        else:
            isolated.append(option)
            index += 1
    return isolated


@pytest.mark.asyncio
async def test_real_uvicorn_oauth_query_has_zero_log_matches_and_keeps_error_audit(
    database_url: str,
    tmp_path: Path,
) -> None:
    """真实 callback 经过 Uvicorn、路由、用例和数据库；分别使用合法 code/error 形状。"""
    sessions = build_session_factory(database_url)
    await _seed_attempts(sessions)
    master = tmp_path / "master"
    master.write_text(base64.urlsafe_b64encode(b"k" * 32).decode("ascii"), encoding="utf-8")
    # 包装模块直接导出真实 main.app，只替换已有 Clock 端口；Uvicorn/路由/数据库和
    # 实际启动日志选项保持真实，state 使用正常十分钟窗口而非延长有效期掩盖时钟依赖。
    (tmp_path / "task27a_canary_app.py").write_text(
        '"""仅向真实应用注入合成 Clock，保持生产 ASGI 与日志链路。"""\n'
        "from datetime import datetime\n"
        "from ai_employee.main import app\n"
        "class FixedClock:\n"
        '    """子进程只使用固定 UTC 时间验证一次性 OAuth state。"""\n'
        "    def now(self) -> datetime:\n"
        '        """返回与数据库 fixture 相同的合成时刻。"""\n'
        f"        return datetime.fromisoformat({NOW.isoformat()!r})\n"
        "app.state.auth_clock = FixedClock()\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "APP_TEST_MODE": "true",
            "DATABASE_URL": database_url,
            "APP_MASTER_KEY_FILE": str(master),
            "METRICS_ENABLED": "false",
            "OTEL_ENABLED": "false",
            "EXTERNAL_WRITES_ENABLED": "false",
            "GOOGLE_WRITES_ENABLED": "false",
            "MICROSOFT_WRITES_ENABLED": "false",
        }
    )
    stdout_path, stderr_path = tmp_path / "stdout", tmp_path / "stderr"
    canaries = (
        "synthetic-code-canary-27a",
        ERROR_STATE,
        CODE_STATE,
        "synthetic-description-canary-27a",
    )
    process: asyncio.subprocess.Process | None = None
    try:
        with (
            socket.socket() as listener,
            stdout_path.open("wb") as stdout,
            stderr_path.open("wb") as stderr,
        ):
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "uvicorn",
                "task27a_canary_app:app",
                *_real_development_uvicorn_options(),
                "--app-dir",
                str(tmp_path),
                "--fd",
                str(listener.fileno()),
                cwd=ROOT,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                pass_fds=(listener.fileno(),),
            )
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False
            ) as client:
                async with asyncio.timeout(15):
                    while True:
                        assert process.returncode is None, "synthetic API exited before readiness"
                        try:
                            response = await client.get("/api/v1/system/health", timeout=0.2)
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.05)
                code = await client.get(
                    "/api/v1/connections/microsoft/callback",
                    params={"code": canaries[0], "state": CODE_STATE},
                )
                denied = await client.get(
                    "/api/v1/connections/microsoft/callback",
                    params={
                        "error": "access_denied",
                        "state": ERROR_STATE,
                        "error_description": canaries[3],
                    },
                )
                replay = await client.get(
                    "/api/v1/connections/microsoft/callback",
                    params={
                        "error": "access_denied",
                        "state": ERROR_STATE,
                        "error_description": canaries[3],
                    },
                )
                assert code.status_code == 200
                # Task27A 保留现有供应商 Problem 分类；Task27B 再收紧统一分类矩阵。
                assert denied.is_client_error
                assert (
                    replay.status_code == 400
                    and replay.json()["error_code"] == "oauth_state_rejected"
                )
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=10)
        outputs = (stdout_path.read_text(), stderr_path.read_text())
        leak_count = sum(output.count(value) for output in outputs for value in canaries)
        assert leak_count == 0, "OAuth query leaked through a real process log channel"
        json_logs = []
        for output in outputs:
            for line in output.splitlines():
                if line.startswith("{"):
                    json_logs.append(json.loads(line))
        assert any(
            row.get("event") == "ai_employee.oauth.authorization_failed" for row in json_logs
        )
        forbidden_keys = {"url", "raw_url", "query", "query_string", "request_target"}
        assert all(not (forbidden_keys & row.keys()) for row in json_logs)
        async with sessions() as session:
            attempts = (await session.scalars(select(OAuthAttemptModel))).all()
            audit = (
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.event_type == "oauth.authorization_failed"
                    )
                )
            ).all()
        assert all(row.consumed_at == NOW for row in attempts)
        assert len(audit) == 1 and audit[0].user_id == USER_ID and audit[0].task_id is None
        assert audit[0].event_metadata == {
            "provider": "microsoft",
            "oauth_attempt_id": str(ERROR_ATTEMPT_ID),
            "error_code": "oauth_authorization_failed",
        }
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
        await sessions.dispose()


@pytest.mark.asyncio
async def test_callback_error_consumption_capability_and_audit_rollback_together(
    database_url: str,
) -> None:
    """审计 INSERT 故障必须撤销 state 消费与能力变化，成功重试只留下单份审计事实。"""
    sessions = build_session_factory(database_url)
    try:
        await _seed_attempts(sessions, bound=True)
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions), AeadCipher(b"k" * 32), {}, FixedClock()
        )

        def fail_audit_insert(mapper, connection, target) -> None:
            """仅在真实失败审计 INSERT 前注入故障，不替代用例或事务行为。"""
            del mapper, connection
            if target.event_type == "oauth.authorization_failed":
                raise RuntimeError("synthetic audit insert failure")

        event.listen(AuditEventModel, "before_insert", fail_audit_insert)
        try:
            with pytest.raises(RuntimeError, match="synthetic audit insert failure"):
                await use_case.callback_error(provider="microsoft", state=ERROR_STATE)
        finally:
            event.remove(AuditEventModel, "before_insert", fail_audit_insert)
        async with sessions() as session:
            attempt = await session.get(OAuthAttemptModel, ERROR_ATTEMPT_ID)
            capability = await session.scalar(select(ConnectionCapabilityModel))
            assert attempt is not None and attempt.consumed_at is None
            assert capability is not None and capability.status == "authorizing"
            assert (await session.scalars(select(AuditEventModel))).all() == []
        await use_case.callback_error(provider="microsoft", state=ERROR_STATE)
        with pytest.raises(OAuthStateRejectedError):
            await use_case.callback_error(provider="microsoft", state=ERROR_STATE)
        async with sessions() as session:
            capability = await session.scalar(select(ConnectionCapabilityModel))
            assert capability is not None and capability.status == "action_required"
            assert capability.actual_scopes == ["Mail.Read"]
            assert capability.last_verified_at == NOW - timedelta(days=1)
            assert len((await session.scalars(select(AuditEventModel))).all()) == 1
    finally:
        await sessions.dispose()
