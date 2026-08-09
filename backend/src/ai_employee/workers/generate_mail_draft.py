"""执行 body-only 邮件草稿生成，并保存新的本地不可变版本。"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from ai_employee.application.ports.model import ModelGateway, ModelUsage
from ai_employee.application.use_cases.mail_drafts import (
    MailDraftSourceMessage,
    MailDraftUseCase,
    UpdateMailDraftInput,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.model_redaction import redact_for_model
from ai_employee.infrastructure.db.models.briefs import LLMInvocationModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyMailSyncRepository
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.llm.fake import build_model_gateway
from ai_employee.integrations.llm.openai_compatible import ModelGatewayError

MAIL_DRAFT_PROMPT_VERSION = "mail_draft_v1"
MAX_MAIL_DRAFT_CONTEXT_MESSAGES = 3
MAX_MAIL_DRAFT_CONTEXT_CHARACTERS = 12_000

_URL_PATTERN = re.compile(r"(?i)https?://[^\s<>()]+")
_TRACKING_IMAGE_PATTERN = re.compile(r"(?is)<img\b[^>]*>")
_MAILBOX_PATTERN = re.compile(
    r"(?i)(?<![\w.!#$%&'*+/=?^`{|}~-])"
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
_QUOTED_HISTORY_PATTERNS = (
    re.compile(r"(?i)^on .+ wrote:\s*$"),
    re.compile(r"(?i)^from:\s+.+$"),
    re.compile(r"(?i)^-----original message-----$"),
    re.compile(r"^在.+写道：?\s*$"),
)
_SIGNATURE_PREFIXES = (
    "-- ",
    "best regards",
    "kind regards",
    "regards",
    "sent from my ",
    "此致",
)


class MailDraftModelOutput(BaseModel):
    """模型只能生成纯文本正文，不能控制地址、主题、线程或发送。"""

    model_config = ConfigDict(extra="forbid")

    body_text: str = Field(max_length=100_000)


@dataclass(frozen=True, slots=True)
class MailDraftContextMessage:
    """进入纯上下文裁剪函数的一封本地同步消息。

    地址、主题和来源 ID 只用于本地排序/测试，不会序列化进模型请求；模型实际只接收
    清洗后的 ``body_text`` 事实。
    """

    message_id: str
    thread_id: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    body_text: str
    received_at: datetime
    is_spam: bool = False


@dataclass(frozen=True, slots=True)
class MailDraftContext:
    """经过垃圾过滤、脱敏与字符裁剪后的 body-only 模型上下文。"""

    instruction: str
    thread_summary: str
    messages: tuple[MailDraftContextMessage, ...]


@dataclass(frozen=True, slots=True)
class MailDraftGenerationMetadata:
    """可落库且不包含 Prompt、正文或地址的一次模型调用元数据。"""

    provider: str
    model_name: str
    prompt_version: str
    input_hash: str
    output_schema: str
    usage: ModelUsage
    status: str
    error_code: str | None


def build_mail_draft_context(
    *,
    messages: tuple[MailDraftContextMessage, ...] | list[MailDraftContextMessage],
    instruction: str,
    thread_summary: str = "",
    configured_patterns: tuple[str, ...] = (),
) -> MailDraftContext:
    """构建最多三封、正文合计最多 12000 字符的安全模型上下文。

    处理顺序固定为：排除垃圾邮件、按最近时间倒序、移除引用/签名/URL/tracking image、
    执行本地敏感模式脱敏、再按 Unicode 字符数裁剪。地址从指令中移除，消息地址、主题、
    来源 ID 和账户从不进入模型序列化函数。

    Args:
        messages: 已从当前用户本地同步缓存读取的相关消息。
        instruction: 用户本次明确的写作要求。
        thread_summary: M1 已持久化的线程摘要；缺失时为空。
        configured_patterns: 用户配置的额外本地脱敏正则。

    Returns:
        可直接构造 body-only 模型请求的不可变上下文。

    Raises:
        ValueError: 消息时间不带时区，或指令/摘要不是普通字符串。
    """
    if not isinstance(instruction, str) or not isinstance(thread_summary, str):
        raise TypeError("mail draft instruction and thread summary must be strings")
    safe_instruction = _sanitize_model_text(
        instruction,
        configured_patterns=configured_patterns,
        remove_addresses=True,
    )
    safe_summary = _sanitize_model_text(
        thread_summary,
        configured_patterns=configured_patterns,
        remove_addresses=True,
    )
    eligible: list[MailDraftContextMessage] = []
    for message in messages:
        if type(message) is not MailDraftContextMessage:
            raise TypeError("mail draft context messages must use MailDraftContextMessage")
        if message.received_at.tzinfo is None or message.received_at.utcoffset() is None:
            raise ValueError("mail draft context message time must be timezone-aware")
        if message.is_spam:
            continue
        eligible.append(message)
    eligible.sort(
        key=lambda item: (item.received_at.astimezone(UTC), item.message_id),
        reverse=True,
    )

    candidates: list[tuple[MailDraftContextMessage, str]] = []
    for message in eligible:
        if len(candidates) >= MAX_MAIL_DRAFT_CONTEXT_MESSAGES:
            break
        body_text = _sanitize_mail_body(
            message.body_text,
            configured_patterns=configured_patterns,
        )
        if not body_text:
            continue
        candidates.append((message, body_text))

    remaining = MAX_MAIL_DRAFT_CONTEXT_CHARACTERS
    selected: list[MailDraftContextMessage] = []
    for index, (message, body_text) in enumerate(candidates):
        # 为尚未处理的每封相关邮件预留确定性份额，避免最新一封超长正文独占全部预算。
        remaining_messages = len(candidates) - index
        body_text = body_text[: remaining // remaining_messages]
        remaining -= len(body_text)
        selected.append(
            MailDraftContextMessage(
                message_id=message.message_id,
                thread_id=message.thread_id,
                sender="",
                recipients=(),
                subject="",
                body_text=body_text,
                received_at=message.received_at.astimezone(UTC),
                is_spam=False,
            )
        )
    return MailDraftContext(
        instruction=safe_instruction,
        thread_summary=safe_summary,
        messages=tuple(selected),
    )


def mail_draft_model_messages(context: MailDraftContext) -> tuple[dict[str, str], ...]:
    """把安全上下文序列化为不含地址、主题、线程或发送字段的模型消息。"""
    facts = {
        "instruction": context.instruction,
        "thread_summary": context.thread_summary,
        "related_body_facts": [message.body_text for message in context.messages],
    }
    return (
        {"role": "system", "content": load_mail_draft_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                facts,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    )


@lru_cache
def load_mail_draft_prompt() -> str:
    """读取版本化 Prompt 文件；内容只在模型调用内存中使用且不会落库。"""
    path = Path(__file__).resolve().parents[1] / "prompts" / "mail_draft_v1.md"
    return path.read_text(encoding="utf-8")


class GenerateMailDraftTaskStep:
    """在耐久任务租约内执行一次模型草拟，并原子保存版本与调用元数据。"""

    name = "generate_mail_draft"

    def __init__(
        self,
        *,
        session_factory: ManagedAsyncSessionMaker,
        action_cipher: ActionPayloadCipher,
        source_cipher: AeadCipher,
        model_gateway: ModelGateway,
        model_name: str,
        model_redaction_patterns: tuple[str, ...] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存进程资源；模型 I/O 与数据库事务严格分离。

        Args:
            session_factory: Worker 生命周期拥有的数据库会话工厂。
            action_cipher: 本地草稿正文的记录绑定 AEAD。
            source_cipher: 已同步源邮件正文的记录绑定 AEAD。
            model_gateway: 供应商无关结构化模型端口；测试使用 Fake。
            model_name: 已配置模型名，仅记录在元数据中。
            model_redaction_patterns: 模型输入前额外本地脱敏规则。
            clock: 可替换带时区时钟，缺失时使用 UTC 当前时间。
        """
        self._session_factory = session_factory
        self._action_cipher = action_cipher
        self._source_cipher = source_cipher
        self._model_gateway = model_gateway
        self._model_name = model_name
        self._model_redaction_patterns = model_redaction_patterns
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(self, task: LeasedTask) -> None:
        """加载本地事实、调用 body-only 模型，并保存成功版本或稳定失败元数据。

        重复投递先检查同一任务是否已有 LLMInvocation；存在时直接返回。模型失败、超时或
        Schema 校验失败不会修改草稿版本，也不会伪装成邮件发送失败，任务结果只记录安全
        ``generation_status`` 与稳定错误码。
        """
        if task.user_id is None:
            raise ValueError("mail draft generation requires user_id")
        raw_draft_id = task.input_payload.get("draft_id")
        raw_instruction = task.input_payload.get("instruction", "")
        raw_expected_version = task.input_payload.get("expected_version")
        raw_thread_summary = task.input_payload.get("thread_summary", "")
        if not isinstance(raw_draft_id, str) or not isinstance(raw_instruction, str):
            raise TypeError("mail draft generation requires draft_id and instruction")
        if not isinstance(raw_thread_summary, str):
            raise TypeError("mail draft thread_summary must be a string")
        if raw_expected_version is not None and (
            type(raw_expected_version) is not int or raw_expected_version <= 0
        ):
            raise TypeError("mail draft expected_version must be a positive integer")
        draft_id = UUID(raw_draft_id)

        async with self._session_factory() as session:
            existing = await session.scalar(
                select(LLMInvocationModel.id).where(
                    LLMInvocationModel.user_id == task.user_id,
                    LLMInvocationModel.task_id == task.task_id,
                    LLMInvocationModel.prompt_version == MAIL_DRAFT_PROMPT_VERSION,
                )
            )
            if existing is not None:
                return
            drafts = SqlAlchemyMailDraftRepository(session, self._action_cipher)
            current = await drafts.get_current(user_id=task.user_id, draft_id=draft_id)
            if current is None:
                raise ValueError("mail draft generation target was not found")
            context_messages: tuple[MailDraftContextMessage, ...] = ()
            if current.source_thread_id is not None:
                sources = SqlAlchemyMailSyncRepository(session, self._source_cipher)
                local_messages = await sources.list_draft_context_messages(
                    user_id=task.user_id,
                    connection_id=current.connection_id,
                    source_thread_id=current.source_thread_id,
                )
                context_messages = tuple(
                    _context_message_from_source(message) for message in local_messages
                )
            expected_version = raw_expected_version or current.current_version

        context = build_mail_draft_context(
            messages=context_messages,
            instruction=raw_instruction,
            thread_summary=raw_thread_summary,
            configured_patterns=self._model_redaction_patterns,
        )
        model_messages = mail_draft_model_messages(context)
        input_hash = sha256(model_messages[-1]["content"].encode("utf-8")).hexdigest()
        try:
            response = await self._model_gateway.complete(
                model_name=self._model_name,
                prompt_version=MAIL_DRAFT_PROMPT_VERSION,
                messages=model_messages,
                response_model=MailDraftModelOutput,
            )
            output = MailDraftModelOutput.model_validate(response.value)
        except (ModelGatewayError, ValidationError, TypeError, ValueError) as error:
            metadata = MailDraftGenerationMetadata(
                provider=_model_provider_name(self._model_gateway),
                model_name=self._model_name,
                prompt_version=MAIL_DRAFT_PROMPT_VERSION,
                input_hash=input_hash,
                output_schema=MailDraftModelOutput.__name__,
                usage=ModelUsage(),
                status="failed",
                error_code=_model_error_code(error),
            )
            await self._persist_result(
                task=task,
                draft_id=draft_id,
                expected_version=expected_version,
                body_text=None,
                metadata=metadata,
            )
            return

        metadata = MailDraftGenerationMetadata(
            provider=_model_provider_name(self._model_gateway),
            model_name=self._model_name,
            prompt_version=MAIL_DRAFT_PROMPT_VERSION,
            input_hash=input_hash,
            output_schema=MailDraftModelOutput.__name__,
            usage=response.usage,
            status="succeeded",
            error_code=None,
        )
        await self._persist_result(
            task=task,
            draft_id=draft_id,
            expected_version=expected_version,
            body_text=output.body_text,
            metadata=metadata,
        )

    async def _persist_result(
        self,
        *,
        task: LeasedTask,
        draft_id: UUID,
        expected_version: int,
        body_text: str | None,
        metadata: MailDraftGenerationMetadata,
    ) -> None:
        """在一个短事务内幂等保存版本、LLMInvocation、任务摘要和无内容审计。"""
        if task.user_id is None:
            raise ValueError("mail draft generation requires user_id")
        async with self._session_factory.begin() as session:
            task_row = await session.scalar(
                select(TaskRunModel)
                .where(TaskRunModel.id == task.task_id, TaskRunModel.user_id == task.user_id)
                .with_for_update()
            )
            if task_row is None:
                raise ValueError("mail draft generation task was not found")
            existing = await session.scalar(
                select(LLMInvocationModel.id).where(
                    LLMInvocationModel.user_id == task.user_id,
                    LLMInvocationModel.task_id == task.task_id,
                    LLMInvocationModel.prompt_version == MAIL_DRAFT_PROMPT_VERSION,
                )
            )
            if existing is not None:
                return

            final_metadata = metadata
            generated_version: int | None = None
            if body_text is not None:
                repository = SqlAlchemyMailDraftRepository(session, self._action_cipher)
                source_reader = SqlAlchemyMailSyncRepository(session, self._source_cipher)
                use_case = MailDraftUseCase(
                    drafts=repository,
                    connections=repository,
                    sources=source_reader,
                    clock=self._clock,
                )
                try:
                    updated = await use_case.update(
                        UpdateMailDraftInput(
                            user_id=task.user_id,
                            draft_id=draft_id,
                            expected_version=expected_version,
                            body_text=body_text,
                        ),
                        prompt_version=MAIL_DRAFT_PROMPT_VERSION,
                        model_name=self._model_name,
                    )
                except StateConflictError as error:
                    final_metadata = MailDraftGenerationMetadata(
                        provider=metadata.provider,
                        model_name=metadata.model_name,
                        prompt_version=metadata.prompt_version,
                        input_hash=metadata.input_hash,
                        output_schema=metadata.output_schema,
                        usage=metadata.usage,
                        status="failed",
                        error_code=error.error_code,
                    )
                else:
                    generated_version = updated.current_version

            session.add(_invocation_model(task=task, metadata=final_metadata, now=self._now()))
            status = final_metadata.status
            task_row.result_payload = {
                "draft_id": str(draft_id),
                "generation_status": status,
                "generated_version": generated_version,
                "error_code": final_metadata.error_code,
            }
            session.add(
                AuditEventModel(
                    user_id=task.user_id,
                    task_id=task.task_id,
                    event_type=(
                        "mail_draft.generated"
                        if status == "succeeded"
                        else "mail_draft.generation_failed"
                    ),
                    actor_type="system",
                    actor_id=None,
                    event_metadata={
                        "draft_id": str(draft_id),
                        "status": status,
                        "error_code": final_metadata.error_code,
                    },
                )
            )

    def _now(self) -> datetime:
        """读取带时区时钟并规范为 UTC。"""
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("mail draft generation clock must be timezone-aware")
        return value.astimezone(UTC)


def build_generate_mail_draft_task_step(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings,
    metrics: Metrics | None = None,
) -> GenerateMailDraftTaskStep:
    """按 Worker 配置构造草稿生成步骤；测试模式由现有 Fake 网关隔离网络。"""
    source_cipher = AeadCipher.from_file(settings.app_master_key_file)
    return GenerateMailDraftTaskStep(
        session_factory=session_factory,
        action_cipher=ActionPayloadCipher(source_cipher),
        source_cipher=source_cipher,
        model_gateway=build_model_gateway(settings, metrics=metrics),
        model_name=settings.model_name,
        model_redaction_patterns=tuple(settings.model_redaction_patterns),
    )


def _sanitize_mail_body(text: str, *, configured_patterns: tuple[str, ...]) -> str:
    """移除引用、签名、tracking/URL 和明显敏感模式，保留纯文本换行。"""
    without_tracking = _TRACKING_IMAGE_PATTERN.sub("", text)
    without_urls = _URL_PATTERN.sub("", without_tracking)
    kept_lines: list[str] = []
    for line in without_urls.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") or any(
            pattern.match(stripped) for pattern in _QUOTED_HISTORY_PATTERNS
        ):
            break
        if kept_lines and any(stripped.casefold().startswith(prefix) for prefix in _SIGNATURE_PREFIXES):
            break
        kept_lines.append(line.rstrip())
    compact = "\n".join(kept_lines).strip()
    return _sanitize_model_text(
        compact,
        configured_patterns=configured_patterns,
        remove_addresses=False,
    )


def _sanitize_model_text(
    text: str,
    *,
    configured_patterns: tuple[str, ...],
    remove_addresses: bool,
) -> str:
    """执行共同本地脱敏，并可进一步移除邮箱地址。"""
    value = _MAILBOX_PATTERN.sub("[ADDRESS_REMOVED]", text) if remove_addresses else text
    return redact_for_model(value, configured_patterns=configured_patterns).text.strip()


def _context_message_from_source(source: MailDraftSourceMessage) -> MailDraftContextMessage:
    """把本地来源投影复制为纯上下文裁剪输入。"""
    return MailDraftContextMessage(
        message_id=source.message_id,
        thread_id=source.thread_id,
        sender=source.sender,
        recipients=source.recipients,
        subject=source.subject,
        body_text=source.body_text,
        received_at=source.received_at,
        is_spam=any(label.casefold() == "spam" for label in source.labels),
    )


def _model_provider_name(gateway: ModelGateway) -> str:
    """从固定适配器类名派生低敏 provider 代号。"""
    return type(gateway).__name__.removesuffix("Gateway").casefold()


def _model_error_code(error: Exception) -> str:
    """把模型/校验失败收敛为稳定错误码且不保留原始输出。"""
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else "mail_draft_model_output_invalid"


def _invocation_model(
    *,
    task: LeasedTask,
    metadata: MailDraftGenerationMetadata,
    now: datetime,
) -> LLMInvocationModel:
    """构造不含 Prompt、正文或地址的 LLMInvocation ORM 行。"""
    if task.user_id is None:
        raise ValueError("mail draft generation requires user_id")
    return LLMInvocationModel(
        user_id=task.user_id,
        task_id=task.task_id,
        step_id=None,
        provider=metadata.provider,
        model_name=metadata.model_name,
        prompt_version=metadata.prompt_version,
        input_hash=metadata.input_hash,
        output_schema=metadata.output_schema,
        input_tokens=metadata.usage.input_tokens,
        output_tokens=metadata.usage.output_tokens,
        estimated_cost_microusd=metadata.usage.estimated_cost_microusd,
        latency_ms=metadata.usage.latency_ms,
        status=metadata.status,
        error_code=metadata.error_code,
        created_at=now,
    )


__all__ = [
    "MAIL_DRAFT_PROMPT_VERSION",
    "MAX_MAIL_DRAFT_CONTEXT_CHARACTERS",
    "MAX_MAIL_DRAFT_CONTEXT_MESSAGES",
    "GenerateMailDraftTaskStep",
    "MailDraftContext",
    "MailDraftContextMessage",
    "MailDraftGenerationMetadata",
    "MailDraftModelOutput",
    "build_generate_mail_draft_task_step",
    "build_mail_draft_context",
    "load_mail_draft_prompt",
    "mail_draft_model_messages",
]
