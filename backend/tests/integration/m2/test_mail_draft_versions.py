"""验证 Task 14 用例与现有不可变草稿 Repository 的组合行为。"""

from datetime import UTC, datetime, time
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from ai_employee.application.use_cases.mail_drafts import (
    CreateMailDraftInput,
    MailDraftUseCase,
    UpdateMailDraftInput,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.actions import MailDraftStatus
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.mail_actions import MailMode
from ai_employee.infrastructure.db.models.actions import MailDraftVersionModel
from ai_employee.infrastructure.db.models.briefs import LLMInvocationModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.llm.fake import FakeModelGateway
from ai_employee.workers.generate_mail_draft import GenerateMailDraftTaskStep


@pytest.mark.asyncio
async def test_mail_draft_patch_creates_one_monotonic_immutable_version(database_url: str) -> None:
    """PATCH 通过 CAS 写入版本 2，旧版本仍保留且正文密文不泄漏。"""
    session_factory = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    cipher = ActionPayloadCipher.from_key(b"m" * 32)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                id=user_id,
                email=f"draft-{user_id}@example.test",
                display_name="Synthetic Draft User",
                password_hash=None,
                timezone="UTC",
                locale="en-US",
                brief_time=time(8, 0),
            )
            connection = OAuthConnectionModel(
                id=connection_id,
                user_id=user_id,
                provider="google",
                provider_account_id=f"draft-{connection_id}",
                account_email="owner@example.test",
                scopes=[],
                status="connected",
            )
            capabilities = tuple(
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=capability.value,
                    status="enabled",
                    actual_scopes=[],
                )
                for capability in (
                    ConnectionCapability.MAIL_READ,
                    ConnectionCapability.MAIL_SEND,
                )
            )
            session.add_all((user, connection, *capabilities))
        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, cipher)
            use_case = MailDraftUseCase(
                drafts=repository,
                connections=repository,
                clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
            )
            created = await use_case.create(
                CreateMailDraftInput(
                    user_id=user_id,
                    mode=MailMode.NEW,
                    idempotency_key="integration-draft-1",
                    connection_id=connection_id,
                    to_recipients=("peer@example.test",),
                    body_text="Synthetic body one",
                )
            )
            updated = await use_case.update(
                UpdateMailDraftInput(
                    user_id=user_id,
                    draft_id=created.draft_id,
                    expected_version=1,
                    body_text="Synthetic body two",
                )
            )
            assert updated.current_version == 2
            assert updated.status is MailDraftStatus.EDITING
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(MailDraftVersionModel).where(
                    MailDraftVersionModel.draft_id == created.draft_id
                )
            )
            ciphertexts = tuple(
                (
                    await session.scalars(
                        select(MailDraftVersionModel.body_ciphertext).where(
                            MailDraftVersionModel.draft_id == created.draft_id
                        )
                    )
                ).all()
            )
        assert count == 2
        assert all(ciphertext is not None for ciphertext in ciphertexts)
        assert all(
            b"Synthetic body one" not in ciphertext
            and b"Synthetic body two" not in ciphertext
            for ciphertext in ciphertexts
            if ciphertext is not None
        )
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_model_failure_keeps_blank_editable_draft_and_stores_only_safe_metadata(
    database_url: str,
) -> None:
    """模型失败不改变草稿版本，并且调用/任务/审计事实都不保存 Prompt 或正文。"""
    session_factory = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    action_cipher = ActionPayloadCipher.from_key(b"f" * 32)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                id=user_id,
                email=f"generation-failure-{user_id}@example.test",
                display_name="Synthetic Generation Failure",
                password_hash=None,
                timezone="UTC",
                locale="en-US",
                brief_time=time(8, 0),
            )
            connection = OAuthConnectionModel(
                id=connection_id,
                user_id=user_id,
                provider="google",
                provider_account_id=f"generation-failure-{connection_id}",
                account_email="owner@example.test",
                scopes=[],
                status="connected",
            )
            session.add_all(
                (
                    user,
                    connection,
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability=ConnectionCapability.MAIL_READ.value,
                        status="enabled",
                        actual_scopes=[],
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability=ConnectionCapability.MAIL_SEND.value,
                        status="enabled",
                        actual_scopes=[],
                    ),
                )
            )
        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, action_cipher)
            use_case = MailDraftUseCase(
                drafts=repository,
                connections=repository,
                clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
            )
            draft = await use_case.create_new(
                user_id=user_id,
                connection_id=connection_id,
                idempotency_key="generation-failure-draft",
            )
            task = TaskRunModel(
                user_id=user_id,
                kind="mail_draft.generate",
                status="running",
                idempotency_key="generation-failure-task",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": 1,
                    "instruction": "Sensitive synthetic instruction that must not be stored",
                },
            )
            session.add(task)
            await session.flush()
            task_id = task.id

        step = GenerateMailDraftTaskStep(
            session_factory=session_factory,
            action_cipher=action_cipher,
            source_cipher=AeadCipher(b"s" * 32),
            model_gateway=FakeModelGateway(scenario="invalid_twice"),
            model_name="fake-mail-model",
            clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
        )
        await step.execute(
            LeasedTask(
                task_id=task_id,
                user_id=user_id,
                kind="mail_draft.generate",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": 1,
                    "instruction": "Sensitive synthetic instruction that must not be stored",
                },
                started_at=datetime(2026, 8, 9, tzinfo=UTC),
            )
        )

        async with session_factory() as session:
            repository = SqlAlchemyMailDraftRepository(session, action_cipher)
            current = await repository.get_current(user_id=user_id, draft_id=draft.draft_id)
            invocation = await session.scalar(
                select(LLMInvocationModel).where(LLMInvocationModel.task_id == task_id)
            )
            task_row = await session.get(TaskRunModel, task_id)
            event = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.task_id == task_id,
                    AuditEventModel.event_type == "mail_draft.generation_failed",
                )
            )
            version_count = await session.scalar(
                select(func.count()).select_from(MailDraftVersionModel).where(
                    MailDraftVersionModel.draft_id == draft.draft_id
                )
            )
        assert current is not None
        assert current.current_version == 1 and current.body_text == ""
        assert current.status is MailDraftStatus.EDITING
        assert version_count == 1
        assert invocation is not None
        assert invocation.prompt_version == "mail_draft_v1"
        assert invocation.output_schema == "MailDraftModelOutput"
        assert invocation.status == "failed"
        assert task_row is not None and task_row.result_payload == {
            "draft_id": str(draft.draft_id),
            "generation_status": "failed",
            "generated_version": None,
            "error_code": "model_invalid_output",
        }
        assert event is not None and event.event_metadata == {
            "draft_id": str(draft.draft_id),
            "status": "failed",
            "error_code": "model_invalid_output",
        }
        persisted_safe_facts = str(
            {
                "invocation": {
                    "prompt_version": invocation.prompt_version,
                    "input_hash": invocation.input_hash,
                    "output_schema": invocation.output_schema,
                    "error_code": invocation.error_code,
                },
                "task_result": task_row.result_payload,
                "audit": event.event_metadata,
            }
        )
        assert "Sensitive synthetic instruction" not in persisted_safe_facts

        async with session_factory.begin() as session:
            success_task = TaskRunModel(
                user_id=user_id,
                kind="mail_draft.generate",
                status="running",
                idempotency_key="generation-success-task",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": 1,
                    "instruction": "Reply with the supplied synthetic facts only",
                },
            )
            session.add(success_task)
            await session.flush()
            success_task_id = success_task.id
        success_step = GenerateMailDraftTaskStep(
            session_factory=session_factory,
            action_cipher=action_cipher,
            source_cipher=AeadCipher(b"s" * 32),
            model_gateway=FakeModelGateway(),
            model_name="fake-mail-model",
            clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
        )
        await success_step.execute(
            LeasedTask(
                task_id=success_task_id,
                user_id=user_id,
                kind="mail_draft.generate",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": 1,
                    "instruction": "Reply with the supplied synthetic facts only",
                },
                started_at=datetime(2026, 8, 9, tzinfo=UTC),
            )
        )
        async with session_factory() as session:
            repository = SqlAlchemyMailDraftRepository(session, action_cipher)
            generated = await repository.get_current(
                user_id=user_id,
                draft_id=draft.draft_id,
            )
            success_invocation = await session.scalar(
                select(LLMInvocationModel).where(
                    LLMInvocationModel.task_id == success_task_id
                )
            )
        assert generated is not None
        assert generated.current_version == 2
        assert generated.body_text == "Synthetic mail draft body."
        assert generated.prompt_version == "mail_draft_v1"
        assert generated.model_name == "fake-mail-model"
        assert success_invocation is not None and success_invocation.status == "succeeded"
    finally:
        await session_factory.dispose()
