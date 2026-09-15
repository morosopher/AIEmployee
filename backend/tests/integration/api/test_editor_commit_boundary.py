"""证明本地编辑成功响应只在真实提交后发送，提交失败不能留下虚假 HTTP 成功。

ASGI send 观察点保持真实 FastAPI 路由、yield 依赖、PostgreSQL 与序列化；只在响应头
即将离开应用时从独立会话读版本，确定性重现浏览器 PATCH 后立即 GET 的交错。
"""

from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Connection, event, select
from starlette.types import Message, Receive, Scope, Send

from ai_employee.config import get_settings
from ai_employee.infrastructure.db.models.actions import CalendarChangeProposalModel, MailDraftModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.session import build_session_factory  # noqa: F401
from ai_employee.infrastructure.testing.m2_sources import seed_m2_source

from .conftest import AuthenticatedApiClients
from .test_action_editors import _headers
from .test_calendar_proposals import (
    _calendar_master_key_file,  # noqa: F401
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]


@dataclass(frozen=True)
class _Editor:
    """只保存待观察的类型、内部标识和版本，不保存正文。"""

    kind: Literal["mail", "calendar"]
    path: str
    identifier: UUID
    version: int


async def _create_editor(clients: AuthenticatedApiClients, kind: Literal["mail", "calendar"]) -> _Editor:
    """通过正常创建 API 准备一个未提交审批的合成编辑对象。"""
    source = await seed_m2_source(
        sessions=clients.session_factory, master_key_file=get_settings().app_master_key_file,
        user_id=clients.owner_id, provider="google",
    )
    path = "/api/v1/mail/drafts" if kind == "mail" else "/api/v1/calendar/proposals"
    payload = {"mode": "new", "connection_id": str(source.connection_id)} if kind == "mail" else {
        "operation_kind": "create", "connection_id": str(source.connection_id),
        "calendar_id": source.calendar_id, "title": "Synthetic commit meeting",
        "starts_at": source.starts_at.isoformat(), "ends_at": source.ends_at.isoformat(),
        "timezone": "UTC", "all_day": False, "attendees": [], "notification_policy": "all",
    }
    created = await clients.owner.post(path, headers=_headers(clients.owner), json=payload)
    assert created.status_code == 201
    value = created.json()
    return _Editor(kind, f"{path}/{value['id']}", UUID(value["id"]), value["version"])


async def _version(clients: AuthenticatedApiClients, editor: _Editor) -> int:
    """新会话读取已提交版本，不能复用请求的 ORM identity map。"""
    model = MailDraftModel if editor.kind == "mail" else CalendarChangeProposalModel
    async with clients.session_factory() as session:
        value = await session.scalar(select(model.current_version).where(
            model.id == editor.identifier, model.user_id == clients.owner_id,
        ))
    assert value is not None
    return value


@pytest.mark.parametrize("kind", ["mail", "calendar"])
async def test_editor_response_starts_after_version_is_visible_to_another_session(
    authenticated_api_clients: AuthenticatedApiClients, kind: Literal["mail", "calendar"],
) -> None:
    """在真实 response.start 时立刻读取；成功版本必须已经由另一连接可见。"""
    clients = authenticated_api_clients
    editor = await _create_editor(clients, kind)
    app = cast(FastAPI, clients.owner._transport.app)
    observed: list[int] = []

    async def observing_app(scope: Scope, receive: Receive, send: Send) -> None:
        """仅观察原始 ASGI 响应开始，不替换响应或延迟数据库提交。"""
        async def observe(message: Message) -> None:
            if message["type"] == "http.response.start":
                observed.append(await _version(clients, editor))
            await send(message)

        await app(scope, receive, observe)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observing_app), base_url="https://testserver",
        cookies=clients.owner.cookies,
    ) as client:
        response = await client.patch(editor.path, headers=_headers(clients.owner), json={
            "version": editor.version,
            "subject" if kind == "mail" else "title": "Synthetic committed revision",
        })
    assert response.status_code == 200
    assert observed == [editor.version + 1]


@pytest.mark.parametrize("kind", ["mail", "calendar"])
async def test_editor_commit_failure_rolls_back_before_any_success_response(
    authenticated_api_clients: AuthenticatedApiClients, kind: Literal["mail", "calendar"],
) -> None:
    """实际 commit 边界抛错，客户端只能得到失败，旧版本和原始内容保持不变。"""
    clients = authenticated_api_clients
    editor = await _create_editor(clients, kind)
    app = cast(FastAPI, clients.owner._transport.app)
    engine = app.state.auth_session_factory.engine.sync_engine
    marker = "synthetic_editor_commit_failure"
    failures: list[bool] = []

    def mark_update(connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        """仅标记本例编辑 UPDATE 所在连接，认证等其他事务不注入故障。"""
        if statement.lstrip().upper().startswith(("UPDATE MAIL_DRAFTS", "UPDATE CALENDAR_CHANGE_PROPOSALS")):
            connection.info[marker] = True

    def fail_commit(connection: Connection) -> None:
        """在 DBAPI commit 前失败，由原事务上下文执行真实 rollback。"""
        if connection.info.pop(marker, False):
            failures.append(True)
            raise RuntimeError("synthetic editor commit failure")

    event.listen(engine, "before_cursor_execute", mark_update)
    event.listen(engine, "commit", fail_commit)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://testserver", cookies=clients.owner.cookies,
        ) as client:
            response = await client.patch(editor.path, headers=_headers(clients.owner), json={
                "version": editor.version,
                "subject" if kind == "mail" else "title": "Synthetic rolled back revision",
            })
    finally:
        event.remove(engine, "before_cursor_execute", mark_update)
        event.remove(engine, "commit", fail_commit)
    assert failures == [True]
    assert await _version(clients, editor) == editor.version
    assert response.status_code == 500


async def _buffer(clients: AuthenticatedApiClients) -> int:
    """用独立会话读取真实工作设置，不从 HTTP 自报值推导提交。"""
    async with clients.session_factory() as session:
        value = await session.scalar(select(UserModel.meeting_buffer_minutes).where(UserModel.id == clients.owner_id))
    assert value is not None
    return value


async def test_settings_response_starts_after_working_settings_are_committed(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """工作设置与编辑器共享先提交后响应契约，否则紧随的冲突查询会读旧缓冲。"""
    clients = authenticated_api_clients
    expected = (await _buffer(clients) + 1) % 121
    app = cast(FastAPI, clients.owner._transport.app)
    observed: list[int] = []

    async def observing_app(scope: Scope, receive: Receive, send: Send) -> None:
        """仅在原始 response.start 读取其他会话可见事实。"""
        async def observe(message: Message) -> None:
            if message["type"] == "http.response.start":
                observed.append(await _buffer(clients))
            await send(message)
        await app(scope, receive, observe)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observing_app), base_url="https://testserver",
        cookies=clients.owner.cookies,
    ) as client:
        response = await client.patch("/api/v1/settings", headers=_headers(clients.owner), json={"meeting_buffer_minutes": expected})
    assert response.status_code == 200
    assert observed == [expected]


async def test_settings_commit_failure_returns_failure_and_keeps_original_settings(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """设置提交失败必须在响应前被处理，不能在200后才回滚设置与审计。"""
    clients = authenticated_api_clients
    original = await _buffer(clients)
    app = cast(FastAPI, clients.owner._transport.app)
    engine = app.state.auth_session_factory.engine.sync_engine
    marker = "synthetic_settings_commit_failure"
    failures: list[bool] = []

    def mark_update(connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        """仅对精确工作设置 UPDATE 注入，认证事务保持原样。"""
        if statement.lstrip().upper().startswith("UPDATE USERS") and "meeting_buffer_minutes" in statement:
            connection.info[marker] = True

    def fail_commit(connection: Connection) -> None:
        """在真实 commit 边界失败；原上下文仍负责数据库回滚。"""
        if connection.info.pop(marker, False):
            failures.append(True)
            raise RuntimeError("synthetic settings commit failure")

    event.listen(engine, "before_cursor_execute", mark_update)
    event.listen(engine, "commit", fail_commit)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://testserver", cookies=clients.owner.cookies,
        ) as client:
            response = await client.patch("/api/v1/settings", headers=_headers(clients.owner), json={"meeting_buffer_minutes": (original + 1) % 121})
    finally:
        event.remove(engine, "before_cursor_execute", mark_update)
        event.remove(engine, "commit", fail_commit)
    assert failures == [True]
    assert await _buffer(clients) == original
    assert response.status_code == 500
