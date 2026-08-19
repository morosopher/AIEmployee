"""邮件草稿 REST API 的认证、版本、幂等与敏感响应集成测试。"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from ai_employee.application.ports.trusted_actions import ApprovalPreflightResult
from ai_employee.config import get_settings
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    canonical_provider_identity_key,
)
from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl as ValidatedTestDatabaseUrl,
)
from ai_employee.infrastructure.db.models.actions import MailDraftModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EmailMessageModel,
    EmailThreadModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import ApprovalRequestModel, TaskRunModel
from ai_employee.infrastructure.db.session import build_session_factory  # noqa: F401
from ai_employee.integrations.registry import ProviderAdapterRegistry
from ai_employee.main import create_app

from .conftest import AuthenticatedApiClients

# Cycle 5 fixture 通过模块属性替换该 builder 以追踪并释放连接池；即使测试函数不直接调用，
# 也必须保留这个精确名称供 fixture 的反射式生命周期钩子使用。
# Task 15 集成测试必须使用 Cycle 5 regular head；该本地覆盖会让 anchor 只读复核后
# 创建一次 disposable 数据库，避免 pytest 的通用 fixture 误把 0018 anchor 当成迁移目标。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """把本模块绑定到受生命周期保护的 regular head 数据库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """复用 regular fixture 已完成的迁移，不在本模块再次触碰共享 anchor。"""
    del cycle5_regular_database_url
    yield


@pytest.fixture(autouse=True)
def _mail_master_key_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """为每个 API 用例注入合成主密钥，避免读取部署 Secret 或真实凭据。"""
    master_key = tmp_path / "task15-master-key"
    master_key.write_text(
        "bW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW0=",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_MASTER_KEY_FILE", str(master_key))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_send_connection(
    clients: AuthenticatedApiClients,
    *,
    provider_account_id: str | None = None,
) -> UUID:
    """写入当前用户自有且具备邮件读写能力的合成连接。"""
    async with clients.session_factory.begin() as session:
        connection = OAuthConnectionModel(
            user_id=clients.owner_id,
            provider="google",
            provider_account_id=(provider_account_id or f"task15-provider-{clients.owner_id}"),
            provider_tenant_id="",
            account_type="google",
            account_email="task15-owner@example.test",
            scopes=["scope:mail.read", "scope:mail.send"],
            status="connected",
            last_error_code=None,
        )
        session.add(connection)
        await session.flush()
        session.add_all(
            ConnectionCapabilityModel(
                user_id=clients.owner_id,
                connection_id=connection.id,
                capability=capability.value,
                status=CapabilityStatus.ENABLED.value,
                actual_scopes=[f"scope:{capability.value}"],
                last_verified_at=datetime(2030, 1, 1, tzinfo=UTC),
                last_error_code=None,
            )
            for capability in (
                ConnectionCapability.MAIL_READ,
                ConnectionCapability.MAIL_SEND,
            )
        )
        return connection.id


class _SyntheticMailPreflight:
    """记录纯内存 mail.send 审批预检调用，不访问任何供应商网络。"""

    provider = "google"

    def __init__(self) -> None:
        self.calls = 0

    def validate_for_approval(self, command: object) -> ApprovalPreflightResult:
        """只确认严格命令动作，返回不含动态内容的空警告集合。"""
        assert getattr(command, "action", None) == "mail.send"
        self.calls += 1
        return ApprovalPreflightResult()


def _enable_synthetic_mail_submission(
    clients: AuthenticatedApiClients,
    *,
    provider_account_id: str,
) -> _SyntheticMailPreflight:
    """在首个 submit 前安装受精确账户限制的写策略与冻结 preflight registry。"""
    transport = cast(httpx.ASGITransport, clients.owner._transport)
    app = cast(FastAPI, transport.app)
    settings = app.state.auth_settings
    app.state.auth_settings = settings.model_copy(
        update={
            "external_writes_enabled": True,
            "google_writes_enabled": True,
            "write_test_account_allowlist": [
                canonical_provider_identity_key(
                    "google",
                    "",
                    provider_account_id,
                )
            ],
        }
    )
    preflight = _SyntheticMailPreflight()
    app.state.trusted_action_preflight_registry = ProviderAdapterRegistry(
        google_mail_preflight=preflight
    )
    return preflight


@pytest.mark.asyncio
async def test_mail_draft_routes_are_registered() -> None:
    """应用必须注册邮件草稿的七个稳定 REST 路径。"""
    paths = {
        route.path
        for included in create_app().routes
        for route in getattr(getattr(included, "original_router", included), "routes", (included,))
        if hasattr(route, "path")
    }

    assert {
        "/api/v1/mail/drafts",
        "/api/v1/mail/drafts/{draft_id}",
        "/api/v1/mail/drafts/{draft_id}/generate",
        "/api/v1/mail/drafts/{draft_id}/submit",
    }.issubset(paths)


@pytest.mark.asyncio
async def test_patch_mail_draft_contract_has_versioned_resource() -> None:
    """PATCH 契约应由已注册路由处理，而不是返回基础设施级 404。"""
    app = create_app()
    route_paths = {
        route.path
        for included in app.routes
        for route in getattr(getattr(included, "original_router", included), "routes", (included,))
        if hasattr(route, "path")
    }
    assert "/api/v1/mail/drafts/{draft_id}" in route_paths


@pytest.mark.asyncio
async def test_create_mail_draft_returns_local_resource_without_cache(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """创建只持久化本地版本一，并禁止缓存包含纯文本正文的响应。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""

    response = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-draft",
        },
        json={
            "connection_id": str(connection_id),
            "to": ["recipient@example.test"],
            "subject": "Synthetic subject",
            "body_text": "Synthetic body",
        },
    )

    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    assert payload == {
        "id": payload["id"],
        "connection_id": str(connection_id),
        "mode": "new",
        "source_thread_id": None,
        "source_message_id": None,
        "version": 1,
        "status": "editing",
        "to": ["recipient@example.test"],
        "cc": [],
        "bcc": [],
        "subject": "Synthetic subject",
        "body_text": "Synthetic body",
        "prompt_version": None,
        "model_name": None,
        "retain_until": payload["retain_until"],
        "created_at": payload["created_at"],
        "recipient_suggestions": [],
    }


@pytest.mark.asyncio
async def test_get_mail_draft_returns_current_decrypted_version_without_cache(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """GET 只返回当前用户可解密的当前版本，并禁止缓存正文。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-get",
        },
        json={
            "connection_id": str(connection_id),
            "to": ["reader@example.test"],
            "body_text": "Synthetic readable body",
        },
    )
    assert created.status_code == 201

    response = await clients.owner.get(f"/api/v1/mail/drafts/{created.json()['id']}")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == created.json()


@pytest.mark.asyncio
async def test_patch_mail_draft_requires_current_version(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """PATCH 以客户端观察到的当前版本 CAS 创建下一不可变版本。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-patch",
        },
        json={
            "connection_id": str(connection_id),
            "body_text": "Synthetic original body",
        },
    )
    assert created.status_code == 201

    response = await clients.owner.patch(
        f"/api/v1/mail/drafts/{created.json()['id']}",
        headers={"X-CSRF-Token": csrf},
        json={"version": 1, "body_text": "Updated synthetic body"},
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["version"] == 2
    assert response.json()["body_text"] == "Updated synthetic body"


@pytest.mark.asyncio
async def test_stale_draft_patch_returns_stable_problem(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """陈旧版本不能覆盖当前草稿，并返回前端可恢复的稳定冲突码。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-stale-patch",
        },
        json={"connection_id": str(connection_id)},
    )
    assert created.status_code == 201
    draft_path = f"/api/v1/mail/drafts/{created.json()['id']}"
    advanced = await clients.owner.patch(
        draft_path,
        headers={"X-CSRF-Token": csrf},
        json={"version": 1, "body_text": "Current synthetic body"},
    )
    assert advanced.status_code == 200

    response = await clients.owner.patch(
        draft_path,
        headers={"X-CSRF-Token": csrf},
        json={"version": 1, "body_text": "Stale synthetic body"},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "draft_version_conflict"


@pytest.mark.asyncio
async def test_foreign_mail_draft_is_hidden_as_not_found(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """跨用户草稿与随机 UUID 使用同一 404，防止资源存在性探测。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-isolation",
        },
        json={"connection_id": str(connection_id)},
    )
    assert created.status_code == 201

    response = await clients.other.get(f"/api/v1/mail/drafts/{created.json()['id']}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "mail_draft_not_found"


@pytest.mark.asyncio
async def test_foreign_mail_draft_patch_is_hidden_as_not_found(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """PATCH 也必须隐藏跨用户草稿，不能把归属失败暴露为 500 或版本冲突。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    owner_csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": owner_csrf,
            "Idempotency-Key": "task15-create-for-foreign-patch",
        },
        json={"connection_id": str(connection_id)},
    )
    assert created.status_code == 201

    other_csrf = clients.other.cookies.get("ai_employee_csrf") or ""
    response = await clients.other.patch(
        f"/api/v1/mail/drafts/{created.json()['id']}",
        headers={"X-CSRF-Token": other_csrf},
        json={"version": 1, "body_text": "Synthetic foreign patch"},
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "mail_draft_not_found"


@pytest.mark.asyncio
async def test_list_mail_drafts_returns_distinct_bounded_pages_without_cache(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """列表使用显式 limit/offset 分页，并禁止缓存任一草稿正文。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created_ids: set[str] = set()
    for sequence in (1, 2):
        created = await clients.owner.post(
            "/api/v1/mail/drafts",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"task15-create-for-list-{sequence}",
            },
            json={
                "connection_id": str(connection_id),
                "body_text": f"Synthetic list body {sequence}",
            },
        )
        assert created.status_code == 201
        created_ids.add(created.json()["id"])

    first_page = await clients.owner.get("/api/v1/mail/drafts?limit=1&offset=0")
    second_page = await clients.owner.get("/api/v1/mail/drafts?limit=1&offset=1")

    assert first_page.status_code == second_page.status_code == 200
    assert first_page.headers["Cache-Control"] == "no-store"
    assert second_page.headers["Cache-Control"] == "no-store"
    assert first_page.json()["limit"] == second_page.json()["limit"] == 1
    assert first_page.json()["offset"] == 0
    assert second_page.json()["offset"] == 1
    page_ids = {
        first_page.json()["items"][0]["id"],
        second_page.json()["items"][0]["id"],
    }
    assert page_ids == created_ids


@pytest.mark.asyncio
async def test_delete_mail_draft_cancels_editing_resource_without_cache(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """DELETE 只取消本地可编辑草稿，并返回禁止缓存的最终资源。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-cancel",
        },
        json={
            "connection_id": str(connection_id),
            "body_text": "Synthetic cancellable body",
        },
    )
    assert created.status_code == 201

    response = await clients.owner.delete(
        f"/api/v1/mail/drafts/{created.json()['id']}",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["status"] == "cancelled"
    assert response.json()["body_text"] == "Synthetic cancellable body"


@pytest.mark.asyncio
async def test_create_mail_draft_replays_same_resource_for_same_idempotency_key(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """同一创建键与同一规范载荷重放时复用已有版本一资源。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    headers = {
        "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
        "Idempotency-Key": "task15-create-replay",
    }
    payload = {
        "connection_id": str(connection_id),
        "to": ["replay@example.test"],
        "body_text": "Synthetic replay body",
    }

    first = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers=headers,
        json=payload,
    )
    second = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers=headers,
        json=payload,
    )

    assert first.status_code == second.status_code == 201
    assert first.headers["Cache-Control"] == second.headers["Cache-Control"] == "no-store"
    assert second.json() == first.json()


@pytest.mark.asyncio
async def test_create_mail_draft_exact_replay_ignores_later_connection_capability_change(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """创建重放先认已有请求事实，后续能力撤销不能把原 201 改写为动态校验失败。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    headers = {
        "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
        "Idempotency-Key": "task15-create-replay-after-capability-change",
    }
    payload = {
        "connection_id": str(connection_id),
        "to": ["stable-replay@example.test"],
        "subject": "Stable synthetic replay",
        "body_text": "Stable synthetic body",
    }
    first = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers=headers,
        json=payload,
    )
    assert first.status_code == 201
    async with clients.session_factory.begin() as session:
        await session.execute(
            update(ConnectionCapabilityModel)
            .where(
                ConnectionCapabilityModel.user_id == clients.owner_id,
                ConnectionCapabilityModel.connection_id == connection_id,
            )
            .values(status=CapabilityStatus.DISABLED.value)
        )
        # 默认选择也属于动态配置；清空它可证明显式请求的重放不依赖当前用户设置。
        await session.execute(
            update(UserModel)
            .where(UserModel.id == clients.owner_id)
            .values(default_mail_connection_id=None)
        )

    replayed = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers=headers,
        json=payload,
    )

    assert replayed.status_code == 201
    assert replayed.json() == first.json()


@pytest.mark.asyncio
async def test_create_mail_draft_rejects_idempotency_key_payload_mismatch(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """同一创建键不能授权不同正文，冲突响应也不得回显敏感载荷。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    headers = {
        "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
        "Idempotency-Key": "task15-create-mismatch",
    }
    first = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers=headers,
        json={
            "connection_id": str(connection_id),
            "body_text": "Synthetic first body",
        },
    )
    assert first.status_code == 201

    response = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers=headers,
        json={
            "connection_id": str(connection_id),
            "body_text": "Synthetic conflicting body",
        },
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "idempotency_key_payload_mismatch"
    assert "Synthetic" not in response.text


@pytest.mark.asyncio
async def test_create_mail_draft_maps_total_recipient_limit_to_422(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """To/CC/BCC 去重后的总人数超过 50 时返回可修正的 422。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    response = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": "task15-recipient-limit",
        },
        json={
            "connection_id": str(connection_id),
            "to": [f"to-{index}@example.test" for index in range(25)],
            "cc": [f"cc-{index}@example.test" for index in range(26)],
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "mail_recipient_limit_exceeded"


@pytest.mark.asyncio
async def test_generate_mail_draft_returns_idempotent_persisted_task(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """generate 返回持久 queued 任务，并以同一客户端键复用精确任务。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-generate",
        },
        json={"connection_id": str(connection_id)},
    )
    assert created.status_code == 201
    draft_id = created.json()["id"]
    generate_headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "task15-generate-draft",
    }
    generate_payload = {
        "version": 1,
        "instruction": "Write a concise synthetic reply.",
    }

    first = await clients.owner.post(
        f"/api/v1/mail/drafts/{draft_id}/generate",
        headers=generate_headers,
        json=generate_payload,
    )
    second = await clients.owner.post(
        f"/api/v1/mail/drafts/{draft_id}/generate",
        headers=generate_headers,
        json=generate_payload,
    )

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert first.json()["status"] == "queued"
    async with clients.session_factory() as session:
        task = await session.scalar(
            select(TaskRunModel).where(
                TaskRunModel.id == UUID(first.json()["task_id"]),
                TaskRunModel.user_id == clients.owner_id,
            )
        )
    assert task is not None
    assert task.kind == "mail_draft.generate"
    assert task.input_payload == {
        "draft_id": draft_id,
        "expected_version": 1,
        "instruction": "Write a concise synthetic reply.",
    }


@pytest.mark.asyncio
async def test_generate_mail_draft_rejects_same_key_with_different_instruction(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """generate 幂等键必须绑定完整任务输入，不能返回另一条 instruction 的任务。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-generate-mismatch",
        },
        json={"connection_id": str(connection_id)},
    )
    assert created.status_code == 201
    path = f"/api/v1/mail/drafts/{created.json()['id']}/generate"
    headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "task15-generate-payload-mismatch",
    }
    first = await clients.owner.post(
        path,
        headers=headers,
        json={"version": 1, "instruction": "First synthetic instruction"},
    )
    assert first.status_code == 202

    response = await clients.owner.post(
        path,
        headers=headers,
        json={"version": 1, "instruction": "Different synthetic instruction"},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "idempotency_key_payload_mismatch"


@pytest.mark.asyncio
async def test_generate_mail_draft_exact_replay_survives_later_draft_version(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """精确 generate 重放绑定首次版本事实，草稿后续编辑不能把它降级为 409。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-generate-late-replay",
        },
        json={"connection_id": str(connection_id)},
    )
    assert created.status_code == 201
    draft_path = f"/api/v1/mail/drafts/{created.json()['id']}"
    generate_path = f"{draft_path}/generate"
    generate_headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "task15-generate-late-replay",
    }
    generate_payload = {"version": 1, "instruction": "Stable synthetic instruction"}
    first = await clients.owner.post(
        generate_path,
        headers=generate_headers,
        json=generate_payload,
    )
    assert first.status_code == 202
    advanced = await clients.owner.patch(
        draft_path,
        headers={"X-CSRF-Token": csrf},
        json={"version": 1, "body_text": "Synthetic version two"},
    )
    assert advanced.status_code == 200

    replayed = await clients.owner.post(
        generate_path,
        headers=generate_headers,
        json=generate_payload,
    )

    assert replayed.status_code == 202
    assert replayed.json() == first.json()


@pytest.mark.asyncio
async def test_generate_mail_draft_rejects_key_bound_to_another_task_kind(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """用户级任务键已绑定其他 kind 时，generate 必须拒绝而非返回无关任务。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-cross-kind-generate",
        },
        json={"connection_id": str(connection_id)},
    )
    assert created.status_code == 201
    shared_key = "task15-generate-cross-kind-key"
    transport = cast(httpx.ASGITransport, clients.owner._transport)
    app = cast(FastAPI, transport.app)
    unrelated = await app.state.create_task_use_case.execute(
        user_id=clients.owner_id,
        kind="synthetic.other",
        input_payload={"synthetic": "unrelated"},
        idempotency_key=shared_key,
    )

    response = await clients.owner.post(
        f"/api/v1/mail/drafts/{created.json()['id']}/generate",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": shared_key,
        },
        json={"version": 1, "instruction": "Synthetic instruction"},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "idempotency_key_payload_mismatch"
    assert response.json().get("task_id") != str(unrelated.task_id)


@pytest.mark.asyncio
async def test_generate_mail_draft_hides_foreign_resource_without_task(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """generate 必须先证明草稿归属，不能为跨用户 UUID 创建失败任务。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": "task15-create-for-foreign-generate",
        },
        json={"connection_id": str(connection_id)},
    )
    assert created.status_code == 201

    response = await clients.other.post(
        f"/api/v1/mail/drafts/{created.json()['id']}/generate",
        headers={
            "X-CSRF-Token": clients.other.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": "task15-foreign-generate",
        },
        json={"version": 1, "instruction": "Synthetic instruction"},
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "mail_draft_not_found"
    async with clients.session_factory() as session:
        foreign_task = await session.scalar(
            select(TaskRunModel.id).where(
                TaskRunModel.user_id == clients.other_id,
                TaskRunModel.idempotency_key == "task15-foreign-generate",
            )
        )
    assert foreign_task is None


@pytest.mark.asyncio
async def test_generate_mail_draft_rejects_stale_version_before_task_creation(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """generate 只能绑定当前不可变版本，陈旧请求不能留下无效任务。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-stale-generate",
        },
        json={"connection_id": str(connection_id)},
    )
    assert created.status_code == 201
    draft_path = f"/api/v1/mail/drafts/{created.json()['id']}"
    advanced = await clients.owner.patch(
        draft_path,
        headers={"X-CSRF-Token": csrf},
        json={"version": 1, "body_text": "Version two"},
    )
    assert advanced.status_code == 200

    response = await clients.owner.post(
        f"{draft_path}/generate",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-stale-generate",
        },
        json={"version": 1, "instruction": "Synthetic instruction"},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "draft_version_conflict"
    async with clients.session_factory() as session:
        task_id = await session.scalar(
            select(TaskRunModel.id).where(
                TaskRunModel.user_id == clients.owner_id,
                TaskRunModel.idempotency_key == "task15-stale-generate",
            )
        )
    assert task_id is None


@pytest.mark.asyncio
async def test_submit_mail_draft_fails_closed_before_creating_approval(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """默认关闭真实写入时，submit 不得执行 preflight 或留下审批任务。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-disabled-submit",
        },
        json={
            "connection_id": str(connection_id),
            "to": ["submit-disabled@example.test"],
            "body_text": "Synthetic disabled submit body",
        },
    )
    assert created.status_code == 201

    response = await clients.owner.post(
        f"/api/v1/mail/drafts/{created.json()['id']}/submit",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-disabled-submit",
        },
        json={"version": 1},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "external_writes_disabled"
    async with clients.session_factory() as session:
        task_id = await session.scalar(
            select(TaskRunModel.id).where(
                TaskRunModel.user_id == clients.owner_id,
                TaskRunModel.idempotency_key == "task15-disabled-submit",
            )
        )
        approval_id = await session.scalar(
            select(ApprovalRequestModel.id)
            .join(TaskRunModel, TaskRunModel.id == ApprovalRequestModel.task_id)
            .where(
                TaskRunModel.user_id == clients.owner_id,
            )
        )
    assert task_id is None
    assert approval_id is None


@pytest.mark.asyncio
async def test_submit_mail_draft_returns_one_idempotent_frozen_task(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """submit 成功时原子冻结一份加密审批，同键重放不再次 preflight。"""
    clients = authenticated_api_clients
    provider_account_id = "task15-write-account"
    preflight = _enable_synthetic_mail_submission(
        clients,
        provider_account_id=provider_account_id,
    )
    connection_id = await _seed_send_connection(
        clients,
        provider_account_id=provider_account_id,
    )
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-submit",
        },
        json={
            "connection_id": str(connection_id),
            "to": ["submit@example.test"],
            "subject": "Synthetic submit subject",
            "body_text": "Synthetic submit body",
        },
    )
    assert created.status_code == 201
    submit_headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "task15-submit-draft",
    }
    submit_path = f"/api/v1/mail/drafts/{created.json()['id']}/submit"

    first = await clients.owner.post(
        submit_path,
        headers=submit_headers,
        json={"version": 1},
    )
    second = await clients.owner.post(
        submit_path,
        headers=submit_headers,
        json={"version": 1},
    )

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert first.json()["status"] == "queued"
    assert preflight.calls == 1
    task_id = UUID(first.json()["task_id"])
    async with clients.session_factory() as session:
        task = await session.get(TaskRunModel, task_id)
        approval = await session.scalar(
            select(ApprovalRequestModel).where(
                ApprovalRequestModel.task_id == task_id,
            )
        )
        draft = await session.get(MailDraftModel, UUID(created.json()["id"]))
    assert task is not None
    assert task.kind == "trusted_action"
    assert task.status == "queued"
    assert approval is not None
    assert approval.action == "mail.send"
    assert approval.payload == {
        "storage": "encrypted",
        "schema_version": "mail_send.v1",
    }
    assert approval.payload_ciphertext is not None
    assert draft is not None and draft.status == "awaiting_approval"


@pytest.mark.asyncio
async def test_submit_mail_draft_hides_foreign_resource_without_task(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """submit 先按用户读取草稿，跨用户 UUID 不得降级为版本冲突或创建任务。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": "task15-create-for-foreign-submit",
        },
        json={
            "connection_id": str(connection_id),
            "to": ["foreign-submit@example.test"],
        },
    )
    assert created.status_code == 201

    response = await clients.other.post(
        f"/api/v1/mail/drafts/{created.json()['id']}/submit",
        headers={
            "X-CSRF-Token": clients.other.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": "task15-foreign-submit",
        },
        json={"version": 1},
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "mail_draft_not_found"
    async with clients.session_factory() as session:
        foreign_task = await session.scalar(
            select(TaskRunModel.id).where(
                TaskRunModel.user_id == clients.other_id,
                TaskRunModel.idempotency_key == "task15-foreign-submit",
            )
        )
    assert foreign_task is None


@pytest.mark.asyncio
async def test_submit_mail_draft_rejects_idempotency_key_for_another_draft(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """提交键冻结一封草稿后，不能被重用于另一封草稿或版本。"""
    clients = authenticated_api_clients
    provider_account_id = "task15-submit-mismatch-account"
    _enable_synthetic_mail_submission(
        clients,
        provider_account_id=provider_account_id,
    )
    connection_id = await _seed_send_connection(
        clients,
        provider_account_id=provider_account_id,
    )
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    draft_ids: list[str] = []
    for sequence in (1, 2):
        created = await clients.owner.post(
            "/api/v1/mail/drafts",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"task15-create-submit-mismatch-{sequence}",
            },
            json={
                "connection_id": str(connection_id),
                "to": [f"submit-mismatch-{sequence}@example.test"],
            },
        )
        assert created.status_code == 201
        draft_ids.append(created.json()["id"])
    submit_headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "task15-submit-mismatch",
    }
    first = await clients.owner.post(
        f"/api/v1/mail/drafts/{draft_ids[0]}/submit",
        headers=submit_headers,
        json={"version": 1},
    )
    assert first.status_code == 202

    response = await clients.owner.post(
        f"/api/v1/mail/drafts/{draft_ids[1]}/submit",
        headers=submit_headers,
        json={"version": 1},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "idempotency_key_payload_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload"),
    (
        ("post", "/api/v1/mail/drafts", {}),
        ("patch", f"/api/v1/mail/drafts/{UUID(int=1)}", {"version": 1}),
        ("delete", f"/api/v1/mail/drafts/{UUID(int=1)}", None),
        (
            "post",
            f"/api/v1/mail/drafts/{UUID(int=1)}/generate",
            {"version": 1},
        ),
        (
            "post",
            f"/api/v1/mail/drafts/{UUID(int=1)}/submit",
            {"version": 1},
        ),
    ),
    ids=("create", "patch", "cancel", "generate", "submit"),
)
async def test_mail_draft_mutations_require_csrf_before_business_checks(
    authenticated_api_clients: AuthenticatedApiClients,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    """所有邮件修改入口统一先验证 Cookie 会话对应的 CSRF Header。"""
    response = await authenticated_api_clients.owner.request(
        method,
        path,
        headers={"Idempotency-Key": "task15-missing-csrf"},
        json=payload,
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "csrf_rejected"


@pytest.mark.asyncio
async def test_create_mail_draft_rejects_invalid_address_and_unknown_field_safely(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """请求 Schema 在业务事务前拒绝非法地址和额外字段，且不回显输入。"""
    clients = authenticated_api_clients
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    invalid_address = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-invalid-address",
        },
        json={"to": ["not-an-address"]},
    )
    unknown_field = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-unknown-field",
        },
        json={"unexpected_body_copy": "Sensitive synthetic input"},
    )

    for response in (invalid_address, unknown_field):
        assert response.status_code == 422
        assert response.json()["error_code"] == "request_validation_failed"
        assert "not-an-address" not in response.text
        assert "Sensitive synthetic input" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload", "requires_idempotency_key"),
    (
        (
            "post",
            "/api/v1/mail/drafts",
            {"mode": "reply", "body_text": "TASK15_REPLY_SOURCE_SENSITIVE"},
            True,
        ),
        (
            "post",
            "/api/v1/mail/drafts",
            {
                "mode": "new",
                "source_thread_id": "TASK15_NEW_SOURCE_SENSITIVE",
            },
            True,
        ),
        (
            "post",
            "/api/v1/mail/drafts",
            {"subject": "Synthetic\r\nTASK15_CREATE_SUBJECT_SENSITIVE"},
            True,
        ),
        (
            "patch",
            f"/api/v1/mail/drafts/{UUID(int=1)}",
            {
                "version": 1,
                "subject": "Synthetic\r\nTASK15_PATCH_SUBJECT_SENSITIVE",
            },
            False,
        ),
    ),
    ids=("reply-without-source", "new-with-source", "create-subject-crlf", "patch-subject-crlf"),
)
async def test_mail_draft_request_boundary_rejects_invalid_source_and_subject_safely(
    authenticated_api_clients: AuthenticatedApiClients,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
    payload: dict[str, object],
    requires_idempotency_key: bool,
) -> None:
    """来源形状和主题注入必须脱敏返回 422，且日志不复制请求中的合成敏感值。"""
    clients = authenticated_api_clients
    headers = {"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""}
    if requires_idempotency_key:
        headers["Idempotency-Key"] = "task15-invalid-request-boundary"
    marker = next(
        value for value in payload.values() if isinstance(value, str) and "SENSITIVE" in value
    )

    response = await clients.owner.request(method, path, headers=headers, json=payload)

    assert response.status_code == 422
    assert response.json()["error_code"] == "request_validation_failed"
    assert marker not in response.text
    assert marker not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "source_payload", "marker"),
    (
        (
            "reply",
            {"source_thread_id": " TASK15_PADDED_THREAD_SENSITIVE "},
            "TASK15_PADDED_THREAD_SENSITIVE",
        ),
        (
            "reply_all",
            {"source_message_id": "TASK15_MESSAGE_SENSITIVE\rbreak"},
            "TASK15_MESSAGE_SENSITIVE",
        ),
    ),
    ids=("reply-padded-thread", "reply-all-multiline-message"),
)
async def test_mail_draft_request_boundary_rejects_invalid_source_identifier_safely(
    authenticated_api_clients: AuthenticatedApiClients,
    caplog: pytest.LogCaptureFixture,
    mode: str,
    source_payload: dict[str, str],
    marker: str,
) -> None:
    """非法回复来源必须由 FastAPI 脱敏为 422，不能进入会记录输入值的通用 500。"""
    clients = authenticated_api_clients
    response = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": "task15-invalid-source-identifier",
        },
        json={"mode": mode, **source_payload},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "request_validation_failed"
    assert marker not in response.text
    assert marker not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    (
        ("/api/v1/mail/drafts", {}),
        (f"/api/v1/mail/drafts/{UUID(int=1)}/generate", {"version": 1}),
        (f"/api/v1/mail/drafts/{UUID(int=1)}/submit", {"version": 1}),
    ),
    ids=("create", "generate", "submit"),
)
async def test_mail_draft_creation_actions_require_idempotency_key(
    authenticated_api_clients: AuthenticatedApiClients,
    path: str,
    payload: dict[str, object],
) -> None:
    """所有创建新持久事实的入口要求非空、受 Header 长度约束的客户端键。"""
    clients = authenticated_api_clients
    response = await clients.owner.post(
        path,
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
        },
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "request_validation_failed"


@pytest.mark.asyncio
async def test_cancelled_mail_draft_cannot_be_cancelled_again(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """取消是受状态机约束的一次性收敛，不是幂等删除或物理删除。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-double-cancel",
        },
        json={"connection_id": str(connection_id)},
    )
    assert created.status_code == 201
    path = f"/api/v1/mail/drafts/{created.json()['id']}"
    first = await clients.owner.delete(path, headers={"X-CSRF-Token": csrf})
    assert first.status_code == 200

    response = await clients.owner.delete(
        path,
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "mail_draft_not_editable"


@pytest.mark.asyncio
async def test_reply_creation_rejects_foreign_source_thread_binding(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """回复来源必须属于当前用户，跨用户 provider ID 统一视为绑定冲突。"""
    clients = authenticated_api_clients
    async with clients.session_factory.begin() as session:
        connection = OAuthConnectionModel(
            user_id=clients.other_id,
            provider="google",
            provider_account_id="task15-foreign-source-account",
            provider_tenant_id="",
            account_type="google",
            account_email="task15-other@example.test",
            scopes=["scope:mail.read", "scope:mail.send"],
            status="connected",
            last_error_code=None,
        )
        session.add(connection)
        await session.flush()
        session.add_all(
            ConnectionCapabilityModel(
                user_id=clients.other_id,
                connection_id=connection.id,
                capability=capability.value,
                status=CapabilityStatus.ENABLED.value,
                actual_scopes=[f"scope:{capability.value}"],
                last_verified_at=datetime(2030, 1, 1, tzinfo=UTC),
                last_error_code=None,
            )
            for capability in (
                ConnectionCapability.MAIL_READ,
                ConnectionCapability.MAIL_SEND,
            )
        )
        thread = EmailThreadModel(
            user_id=clients.other_id,
            connection_id=connection.id,
            provider_thread_id="task15-foreign-thread",
            subject="Synthetic foreign subject",
            participants=[],
            latest_message_at=datetime(2030, 1, 1, tzinfo=UTC),
            provider_url="https://provider.example.test/foreign-thread",
        )
        session.add(thread)
        await session.flush()
        session.add(
            EmailMessageModel(
                user_id=clients.other_id,
                connection_id=connection.id,
                thread_id=thread.id,
                provider_message_id="task15-foreign-message",
                received_at=datetime(2030, 1, 1, tzinfo=UTC),
                mailbox_scope_key="mailbox",
                sender={"email": "foreign-sender@example.test"},
                recipients=[{"email": "task15-other@example.test"}],
                subject="Synthetic foreign subject",
                snippet="Synthetic foreign snippet",
                labels=["INBOX"],
                headers={},
                provider_url="https://provider.example.test/foreign-message",
            )
        )

    response = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or "",
            "Idempotency-Key": "task15-foreign-source-binding",
        },
        json={
            "mode": "reply",
            "source_thread_id": "task15-foreign-thread",
            "source_message_id": "task15-foreign-message",
        },
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "mail_thread_binding_conflict"


@pytest.mark.asyncio
async def test_submit_mail_draft_rejects_stale_version_without_task(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """submit 只能冻结客户端观察到的当前版本，陈旧版本不创建审批任务。"""
    clients = authenticated_api_clients
    connection_id = await _seed_send_connection(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post(
        "/api/v1/mail/drafts",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-create-for-stale-submit",
        },
        json={
            "connection_id": str(connection_id),
            "to": ["stale-submit@example.test"],
        },
    )
    assert created.status_code == 201
    draft_path = f"/api/v1/mail/drafts/{created.json()['id']}"
    advanced = await clients.owner.patch(
        draft_path,
        headers={"X-CSRF-Token": csrf},
        json={"version": 1, "body_text": "Synthetic version two"},
    )
    assert advanced.status_code == 200

    response = await clients.owner.post(
        f"{draft_path}/submit",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "task15-stale-submit",
        },
        json={"version": 1},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "draft_version_conflict"
    async with clients.session_factory() as session:
        task_id = await session.scalar(
            select(TaskRunModel.id).where(
                TaskRunModel.user_id == clients.owner_id,
                TaskRunModel.idempotency_key == "task15-stale-submit",
            )
        )
    assert task_id is None
