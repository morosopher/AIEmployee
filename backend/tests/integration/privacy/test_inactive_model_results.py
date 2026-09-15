"""用真实删除屏障验证模型返回后的业务结果、失败元数据和简报独立持久化事务。"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select, text

from ai_employee.application.ports.model import ModelResponse
from ai_employee.application.use_cases.briefs import PersistDailyBriefUseCase
from ai_employee.application.use_cases.mail_drafts import MailDraftUseCase, UpdateMailDraftInput
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.briefs import ConversationIntent, DailyBriefContent
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.actions import MailDraftModel
from ai_employee.infrastructure.db.models.briefs import (
    DailyBriefModel,
    LLMInvocationModel,
    MessageModel,
)
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.integrations.llm.fake import FakeModelGateway
from ai_employee.workers.generate_brief import GenerateBriefTaskStep
from tests.integration.api.conftest import AuthenticatedApiClients
from tests.integration.api.conftest import (
    authenticated_api_clients as authenticated_api_clients,  # noqa: PLC0414 - 真实认证 fixture。
)
from tests.integration.api.test_conversations import _seed_conversation_runner_task
from tests.integration.m2.test_mail_draft_versions import _seed_generation_runner_task
from tests.integration.microsoft.test_calendar_sync import _seed_microsoft_worker_connection
from tests.integration.microsoft.test_mail_sync import _message, _upsert_repository_message
from tests.integration.privacy.inactive_barrier import (
    assert_facts_unchanged,
    commit_deletion_barrier,
    database_facts,
)

NOW = datetime(2030, 1, 8, 12, tzinfo=UTC)


class _BlockingGateway(FakeModelGateway):
    """只暂停模型网络边界，保留真实响应校验、一次修复和失败分类流程。"""

    def __init__(self, *, fail: bool) -> None:
        """明确选择成功或两次非法结构化结果，不从环境选择场景。"""
        super().__init__(scenario="invalid_twice" if fail else "normal")
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def complete[T: BaseModel](
        self,
        *,
        model_name: str,
        prompt_version: str,
        messages: Sequence[dict[str, str]],
        response_model: type[T],
    ) -> ModelResponse[T]:
        """返回后由生产代码写入结果；对话仅解释能力，避免另一任务创建守卫掩盖缺陷。"""
        self.entered.set()
        await self.release.wait()
        response = await super().complete(
            model_name=model_name,
            prompt_version=prompt_version,
            messages=messages,
            response_model=response_model,
        )
        if response_model is ConversationIntent:
            return ModelResponse(
                value=response_model.model_validate(
                    {
                        "intent": "explain_capabilities",
                        "confidence": 1,
                        "reason_code": "synthetic_capabilities",
                    }
                ),
                usage=response.usage,
            )
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", (False, True), ids=("success", "failed-metadata"))
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_conversation_result_and_failure_metadata_respect_barrier(
    authenticated_api_clients: AuthenticatedApiClients, fail: bool, inactive: bool
) -> None:
    """真实会话/API创建和Task认领完成后，在模型返回前提交barrier；消息和调用元数据一起拒绝。"""
    clients = authenticated_api_clients
    gateway = _BlockingGateway(fail=fail)
    _, task_id, step = await _seed_conversation_runner_task(
        clients,
        content="Could you outline what this assistant can do?",
        gateway=gateway,
        now=NOW,
    )
    leased = await SqlAlchemyTaskExecutionStore(clients.session_factory).acquire(
        task_id=task_id,
        lease_owner="synthetic-model-owner",
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    assert leased is not None
    pending = asyncio.create_task(step.execute(leased))
    try:
        await asyncio.wait_for(gateway.entered.wait(), timeout=5)
        if inactive:
            await commit_deletion_barrier(clients.session_factory, user_id=clients.owner_id)
        before = await database_facts(clients.session_factory)
        gateway.release.set()
        await asyncio.wait_for(pending, timeout=5)
        if inactive:
            assert_facts_unchanged(before, await database_facts(clients.session_factory))
        else:
            async with clients.session_factory() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(MessageModel)
                        .where(MessageModel.task_id == task_id, MessageModel.role == "assistant")
                    )
                    == 1
                )
                invocation = await session.scalar(
                    select(LLMInvocationModel).where(LLMInvocationModel.task_id == task_id)
                )
                assert invocation is not None and invocation.status == (
                    "failed" if fail else "succeeded"
                )
                task = await session.get(TaskRunModel, task_id)
                assert task is not None and task.result_payload is not None
    finally:
        gateway.release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_legacy_conversation_marker_repair_respects_barrier(
    authenticated_api_clients: AuthenticatedApiClients, inactive: bool
) -> None:
    """升级前已有assistant但缺marker时，仅活动用户可补marker，不能依赖正常模型结果守卫。"""
    clients = authenticated_api_clients
    gateway = FakeModelGateway(scenario="normal")
    conversation_id, task_id, step = await _seed_conversation_runner_task(
        clients,
        content="what should I focus on",
        gateway=gateway,
        now=NOW,
    )
    leased = await SqlAlchemyTaskExecutionStore(clients.session_factory).acquire(
        task_id=task_id,
        lease_owner="synthetic-legacy-owner",
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    assert leased is not None
    message_id = uuid4()
    async with clients.session_factory.begin() as session:
        session.add(
            MessageModel(
                id=message_id,
                user_id=clients.owner_id,
                conversation_id=conversation_id,
                task_id=task_id,
                role="assistant",
                content_markdown="Synthetic reply",
                created_at=NOW,
            )
        )
    if inactive:
        await commit_deletion_barrier(clients.session_factory, user_id=clients.owner_id)
    before = await database_facts(clients.session_factory)
    await step.execute(leased)
    if inactive:
        assert_facts_unchanged(before, await database_facts(clients.session_factory))
    else:
        async with clients.session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            assert task is not None and task.result_payload is not None
            assert task.result_payload["assistant_message_id"] == str(message_id)
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("success", "failed-metadata", "version-conflict"))
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_mail_generation_result_and_all_failure_metadata_respect_barrier(
    database_url: str, mode: str, inactive: bool
) -> None:
    """正文、body=None和StateConflict三条结果分支都不能在屏障后追加invocation/marker/audit。"""
    sessions = build_session_factory(database_url)
    action_cipher = ActionPayloadCipher.from_key(b"v" * 32)
    gateway = _BlockingGateway(fail=mode == "failed-metadata")
    try:
        user_id, draft_id, task_id, step = await _seed_generation_runner_task(
            sessions,
            action_cipher=action_cipher,
            gateway=gateway,
            now=NOW,
        )
        leased = await SqlAlchemyTaskExecutionStore(sessions).acquire(
            task_id=task_id,
            lease_owner="synthetic-generation-owner",
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=5),
        )
        assert leased is not None
        pending = asyncio.create_task(step.execute(leased))
        try:
            await asyncio.wait_for(gateway.entered.wait(), timeout=5)
            if mode == "version-conflict":
                async with sessions.begin() as session:
                    repo = SqlAlchemyMailDraftRepository(session, action_cipher)
                    await MailDraftUseCase(drafts=repo, connections=repo, clock=lambda: NOW).update(
                        UpdateMailDraftInput(
                            user_id=user_id,
                            draft_id=draft_id,
                            expected_version=1,
                            body_text="Synthetic manual revision",
                        ),
                    )
            if inactive:
                await commit_deletion_barrier(sessions, user_id=user_id)
            before = await database_facts(sessions)
            gateway.release.set()
            await asyncio.wait_for(pending, timeout=5)
            if inactive:
                assert_facts_unchanged(before, await database_facts(sessions))
            else:
                async with sessions() as session:
                    draft = await session.get(MailDraftModel, draft_id)
                    assert draft is not None and draft.current_version == (
                        1 if mode == "failed-metadata" else 2
                    )
                    invocation = await session.scalar(
                        select(LLMInvocationModel).where(LLMInvocationModel.task_id == task_id)
                    )
                    assert invocation is not None and invocation.status == (
                        "succeeded" if mode == "success" else "failed"
                    )
                    task = await session.get(TaskRunModel, task_id)
                    assert task is not None and task.result_payload is not None
                    assert task.result_payload["generation_status"] == invocation.status
        finally:
            gateway.release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", (False, True), ids=("success", "failed-metadata"))
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_brief_result_after_last_checkpoint_respects_barrier(
    database_url: str, monkeypatch: pytest.MonkeyPatch, fail: bool, inactive: bool
) -> None:
    """真实Graph和最后checkpoint均完成后暂停独立persist，避免误把模型阶段saver拒绝当结果守卫。"""
    sessions, cipher, user_id, connection_id = await _seed_microsoft_worker_connection(database_url)
    entered, release = asyncio.Event(), asyncio.Event()
    original_persist = PersistDailyBriefUseCase.execute

    async def pause_after_graph(
        use_case: PersistDailyBriefUseCase,
        *,
        user_id: UUID,
        task_id: UUID,
        content: DailyBriefContent,
        markdown: str,
        email_analyses: tuple[dict[str, Any], ...] = (),
        model_invocations: tuple[dict[str, Any], ...] = (),
    ) -> UUID:
        """只在既有独立用例调用前暂停，不改Graph、checkpoint saver或数据库写入实现。"""
        assert model_invocations
        entered.set()
        await release.wait()
        return await original_persist(
            use_case,
            user_id=user_id,
            task_id=task_id,
            content=content,
            markdown=markdown,
            email_analyses=email_analyses,
            model_invocations=model_invocations,
        )

    monkeypatch.setattr(PersistDailyBriefUseCase, "execute", pause_after_graph)
    try:
        await _upsert_repository_message(
            sessions,
            cipher,
            user_id=user_id,
            connection_id=connection_id,
            message=_message("brief-source", scope_key="synthetic-scope"),
        )
        task_id = uuid4()
        async with sessions.begin() as session:
            session.add(
                TaskRunModel(
                    id=task_id,
                    user_id=user_id,
                    kind="daily_brief",
                    status="running",
                    idempotency_key=f"brief-barrier:{task_id}",
                    input_payload={},
                )
            )
        step = GenerateBriefTaskStep(
            sessions,
            model_gateway=FakeModelGateway(scenario="invalid_twice" if fail else "normal"),
            now=lambda: NOW,
            checkpoint_database_url=database_url,
        )
        pending = asyncio.create_task(
            step.execute(LeasedTask(task_id, "daily_brief", {}, NOW, user_id=user_id))
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            async with sessions() as session:
                checkpoint_count = await session.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id"),
                    {"thread_id": str(task_id)},
                )
                assert checkpoint_count is not None and checkpoint_count > 0
            if inactive:
                await commit_deletion_barrier(sessions, user_id=user_id)
            before = await database_facts(sessions)
            release.set()
            outcome = (
                await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=5)
            )[0]
            if inactive:
                assert_facts_unchanged(before, await database_facts(sessions))
                assert isinstance(outcome, StateConflictError)
            else:
                assert outcome is None
                async with sessions() as session:
                    assert (
                        await session.scalar(select(func.count()).select_from(DailyBriefModel)) == 1
                    )
                    invocation = await session.scalar(
                        select(LLMInvocationModel).where(LLMInvocationModel.task_id == task_id)
                    )
                    assert invocation is not None and invocation.status == (
                        "failed" if fail else "succeeded"
                    )
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id"),
                        {"thread_id": str(task_id)},
                    )
                    == checkpoint_count
                )
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    finally:
        await sessions.dispose()
