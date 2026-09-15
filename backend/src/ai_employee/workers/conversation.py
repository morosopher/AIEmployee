"""执行受控简报/本地草稿对话并持久化最终 assistant 消息。"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from ai_employee.agents.daily_brief.nodes import (
    classify_ambiguous_conversation_intent,
    classify_conversation_intent,
)
from ai_employee.application.ports.model import ModelGateway
from ai_employee.application.use_cases.calendar_proposals import (
    CalendarProposalUseCase,
    parse_calendar_proposal_conversation_request,
)
from ai_employee.application.use_cases.conversations import (
    parse_mail_draft_conversation_request,
    unsupported_response,
)
from ai_employee.application.use_cases.mail_drafts import (
    CreateMailDraftInput,
    MailDraftUseCase,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.mail_actions import MailMode
from ai_employee.domain.tasks import JsonValue, TaskStatus
from ai_employee.infrastructure.db.models.briefs import (
    DailyBriefModel,
    LLMInvocationModel,
    MessageModel,
)
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyMailSyncRepository
from ai_employee.infrastructure.db.repositories.identity import lock_active_user
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepository
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.llm.fake import FakeModelGateway, build_model_gateway


class ConversationTaskStep:
    """处理简报与明确本地草稿意图，绝不提交审批或调用邮件供应商。"""

    name = "conversation_respond"

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        *,
        model_gateway: ModelGateway | None = None,
        model_name: str = "fake",
        model_redaction_patterns: tuple[str, ...] = (),
        action_cipher: ActionPayloadCipher | None = None,
        action_cipher_file: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存短事务资源；主密钥只在明确草稿请求实际发生时延迟读取。"""
        self._session_factory = session_factory
        # 直接构造仅供单测与本地编排使用，必须保持无网络；生产 factory 显式注入配置网关。
        self._model_gateway = model_gateway or FakeModelGateway()
        self._model_name = model_name
        self._model_redaction_patterns = model_redaction_patterns
        self._action_cipher = action_cipher
        self._action_cipher_file = action_cipher_file
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(self, task: LeasedTask) -> None:
        """按窄意图写最终 assistant 与结果 marker，并对重复投递先行短路。

        assistant、可选本地草稿、模型调用元数据和不含正文的 ``result_payload`` 必须位于
        同一最终事务。marker 既供崩溃接管短路，也让排队取消在取得 TaskRun 行锁后证明
        业务结果已提交；Runner 随后只需以既有 owner CAS 收敛任务成功终态。
        """
        if task.user_id is None:
            raise ValueError("conversation.respond requires user_id")
        lease_owner = _required_lease_owner(task)
        raw_conversation_id = task.input_payload.get("conversation_id")
        raw_content = task.input_payload.get("content")
        if not isinstance(raw_conversation_id, str) or not isinstance(raw_content, str):
            raise TypeError("conversation.respond requires conversation_id and content")
        conversation_id = UUID(raw_conversation_id)
        # 至少一次重复投递先在数据库短路，绝不在已完成任务上再次调用模型。
        async with self._session_factory() as session:
            owned_task = await session.scalar(
                select(TaskRunModel).where(
                    TaskRunModel.id == task.task_id,
                    TaskRunModel.user_id == task.user_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                )
            )
            if owned_task is None:
                return
            if owned_task.result_payload is not None:
                return
            existing = await session.scalar(
                select(MessageModel.id).where(
                    MessageModel.user_id == task.user_id,
                    MessageModel.task_id == task.task_id,
                    MessageModel.role == "assistant",
                )
            )
        if existing is not None:
            # 升级前可能已提交 assistant 却没有 marker。接管者必须重新锁定同一 TaskRun，
            # 确认 owner 未变后只补无内容 marker；不能重跑模型或制造第二条回复。
            async with self._session_factory.begin() as session:
                task_row = await session.scalar(
                    select(TaskRunModel)
                    .where(
                        TaskRunModel.id == task.task_id,
                        TaskRunModel.user_id == task.user_id,
                        TaskRunModel.status == TaskStatus.RUNNING.value,
                        TaskRunModel.lease_owner == lease_owner,
                    )
                    .with_for_update()
                )
                if task_row is None or task_row.result_payload is not None:
                    return
                # 遗留 marker 补写同样是普通业务提交，必须在 Task→user 锁内重检屏障。
                if not await lock_active_user(session, user_id=task.user_id):
                    return
                existing = await session.scalar(
                    select(MessageModel.id).where(
                        MessageModel.user_id == task.user_id,
                        MessageModel.task_id == task.task_id,
                        MessageModel.role == "assistant",
                    )
                )
                if existing is not None:
                    task_row.result_payload = _conversation_result_payload(
                        conversation_id=conversation_id,
                        assistant_message_id=existing,
                    )
                    return
        deterministic = classify_conversation_intent(raw_content)
        intent = deterministic["intent"]
        invocation_metadata: list[dict[str, Any]] = []
        if deterministic["reason_code"] == "ambiguous_request":
            intent = (
                await classify_ambiguous_conversation_intent(
                    raw_content,
                    model_gateway=self._model_gateway,
                    model_name=self._model_name,
                    configured_patterns=self._model_redaction_patterns,
                    invocation_metadata=invocation_metadata,
                )
            ).intent
        async with self._session_factory.begin() as session:
            task_row = await session.scalar(
                select(TaskRunModel)
                .where(
                    TaskRunModel.id == task.task_id,
                    TaskRunModel.user_id == task.user_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                )
                .with_for_update()
            )
            if task_row is None:
                # 分类 I/O 期间取消或换 owner 后，旧 Worker 不得写消息、草稿或模型元数据。
                return
            # 模型成功或分类失败都可能产生消息与调用元数据；在全部 DML 前同步删除屏障。
            if not await lock_active_user(session, user_id=task.user_id):
                return
            if task_row.result_payload is not None:
                return
            existing = await session.scalar(
                select(MessageModel.id).where(
                    MessageModel.user_id == task.user_id,
                    MessageModel.task_id == task.task_id,
                    MessageModel.role == "assistant",
                )
            )
            if existing is not None:
                task_row.result_payload = _conversation_result_payload(
                    conversation_id=conversation_id,
                    assistant_message_id=existing,
                )
                return
            if intent == "show_latest_brief":
                brief = await session.scalar(
                    select(DailyBriefModel)
                    .where(DailyBriefModel.user_id == task.user_id)
                    .order_by(DailyBriefModel.local_date.desc(), DailyBriefModel.version.desc())
                )
                text = brief.markdown if brief is not None else "尚无可查看的每日简报。"
            elif intent == "generate_daily_brief":
                generated = await SqlAlchemyTaskRepository(session).create_with_outbox(
                    user_id=task.user_id,
                    kind="daily_brief",
                    input_payload={"schedule_kind": "conversation"},
                    idempotency_key=f"daily_brief:{task.user_id}:conversation:{task.task_id}",
                )
                text = f"已创建每日简报任务：`{generated.task_id}`。"
            elif intent == "prepare_mail_draft":
                request = parse_mail_draft_conversation_request(raw_content)
                if request is None:
                    # 模型即使错误选择该意图，也不能绕过明确文本解析创建动作。
                    text = unsupported_response()
                else:
                    mail_repository = SqlAlchemyMailDraftRepository(
                        session,
                        self._action_payload_cipher(),
                    )
                    use_case = MailDraftUseCase(
                        drafts=mail_repository,
                        connections=mail_repository,
                        sources=SqlAlchemyMailSyncRepository(session),
                        clock=self._clock,
                    )
                    if request.mode is MailMode.NEW:
                        draft = await use_case.create_new(
                            user_id=task.user_id,
                            idempotency_key=f"conversation-mail-draft:{task.task_id}",
                            to=request.to_recipients,
                        )
                    else:
                        if request.source_thread_id is None:
                            raise ValueError("reply conversation command requires source thread")
                        draft = await use_case.create(
                            CreateMailDraftInput(
                                user_id=task.user_id,
                                mode=MailMode.REPLY,
                                idempotency_key=(f"conversation-mail-draft:{task.task_id}"),
                                source_thread_id=str(request.source_thread_id),
                            )
                        )
                    text = (
                        "已创建可编辑的本地邮件草稿："
                        f"[打开草稿](/api/v1/mail/drafts/{draft.draft_id})。"
                        "草稿仍需由你编辑并单独提交审批，当前不会发送。"
                    )
            elif intent == "prepare_calendar_proposal":
                if not parse_calendar_proposal_conversation_request(raw_content):
                    # ConversationIntent 允许模型表达该枚举仅为结构兼容；只有原始文本再次
                    # 通过确定性整句解析时才可创建 shell，模型不能选择账户或写入字段。
                    text = unsupported_response()
                else:
                    calendar_repository = SqlAlchemyCalendarProposalRepository(
                        session,
                        self._action_payload_cipher(),
                    )
                    proposal = await CalendarProposalUseCase(
                        proposals=calendar_repository,
                        calendar=SqlAlchemyCalendarSyncRepository(session),
                        clock=self._clock,
                    ).create_shell(
                        user_id=task.user_id,
                        idempotency_key=f"conversation-calendar-proposal:{task.task_id}",
                    )
                    text = (
                        "已创建可编辑的本地日程提案："
                        f"[打开提案](/api/v1/calendar/proposals/{proposal.proposal_id})。"
                        "账户、时间、参会人和通知策略仍需由你明确确认；"
                        "当前不会创建审批或写入日历。"
                    )
            else:
                text = unsupported_response()
            session.add_all(
                LLMInvocationModel(
                    user_id=task.user_id,
                    task_id=task.task_id,
                    step_id=None,
                    provider=str(metadata["provider"]),
                    model_name=str(metadata["model_name"]),
                    prompt_version=str(metadata["prompt_version"]),
                    input_hash=str(metadata["input_hash"]),
                    output_schema=str(metadata["output_schema"]),
                    input_tokens=int(metadata["input_tokens"]),
                    output_tokens=int(metadata["output_tokens"]),
                    estimated_cost_microusd=metadata["estimated_cost_microusd"],
                    latency_ms=int(metadata["latency_ms"]),
                    status=str(metadata["status"]),
                    error_code=metadata["error_code"],
                    created_at=datetime.now(UTC),
                )
                for metadata in invocation_metadata
            )
            assistant = MessageModel(
                user_id=task.user_id,
                conversation_id=conversation_id,
                role="assistant",
                content_markdown=text,
                task_id=task.task_id,
                created_at=datetime.now(UTC),
            )
            session.add(assistant)
            # UUID 默认值在 INSERT 时生成；显式 flush 后才能把稳定消息 ID 绑定进同一事务 marker。
            await session.flush()
            task_row.result_payload = _conversation_result_payload(
                conversation_id=conversation_id,
                assistant_message_id=assistant.id,
            )

    def _action_payload_cipher(self) -> ActionPayloadCipher:
        """返回本地动作内容 AEAD，并只在首次明确草稿/提案请求时读取 Secret。"""
        if self._action_cipher is not None:
            return self._action_cipher
        if self._action_cipher_file is None:
            raise RuntimeError("action payload encryption is not configured")
        self._action_cipher = ActionPayloadCipher(AeadCipher.from_file(self._action_cipher_file))
        return self._action_cipher


def _conversation_result_payload(
    *,
    conversation_id: UUID,
    assistant_message_id: UUID,
) -> dict[str, JsonValue]:
    """构造不含对话正文、Prompt 或草稿内容的稳定完成 marker。"""
    return {
        "conversation_id": str(conversation_id),
        "assistant_message_id": str(assistant_message_id),
    }


def _required_lease_owner(task: LeasedTask) -> str:
    """返回真实非空租约 owner；直接调用 Worker 未持有租约时 fail closed。"""
    owner = task.lease_owner
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("conversation.respond requires lease_owner")
    return owner


def build_conversation_task_step(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings | None = None,
    metrics: Metrics | None = None,
) -> ConversationTaskStep:
    """构造供 DurableTaskRunner 注册的实际会话回复节点。"""
    if settings is None:
        return ConversationTaskStep(session_factory)
    return ConversationTaskStep(
        session_factory,
        model_gateway=build_model_gateway(settings, metrics=metrics),
        model_name=settings.model_name,
        model_redaction_patterns=tuple(settings.model_redaction_patterns),
        action_cipher_file=settings.app_master_key_file,
    )
