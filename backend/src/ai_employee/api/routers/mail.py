"""暴露本地加密邮件草稿的用户隔离 REST 边界。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_employee.api.deps import (
    ApiProblem,
    CsrfProtectedSession,
    CurrentSession,
    get_auth_clock,
    get_create_task_use_case,
    get_mail_draft_use_case,
    get_submit_mail_draft_use_case,
)
from ai_employee.application.use_cases.auth import Clock
from ai_employee.application.use_cases.mail_drafts import (
    CreateMailDraftInput,
    MailDraftNotFoundError,
    MailDraftUseCase,
    MailDraftView,
    UpdateMailDraftInput,
)
from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.application.use_cases.trusted_actions import SubmitMailDraftUseCase
from ai_employee.domain.actions import MailDraftStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode, normalize_mailbox_address
from ai_employee.domain.tasks import JsonValue

IdempotencyKeyHeader = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^[^\s\r\n](?:[^\r\n]*[^\s\r\n])?$",
    ),
]


class MailDraftResponse(BaseModel):
    """返回当前用户可解密的精确草稿版本及本地派生建议。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    connection_id: UUID
    mode: MailMode
    source_thread_id: str | None
    source_message_id: str | None
    version: int = Field(ge=1)
    status: MailDraftStatus
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    body_text: str
    prompt_version: str | None
    model_name: str | None
    retain_until: datetime | None
    created_at: datetime | None
    recipient_suggestions: list[str]


class MailDraftListResponse(BaseModel):
    """返回带显式 limit/offset 的当前用户草稿页。"""

    model_config = ConfigDict(extra="forbid")

    items: list[MailDraftResponse]
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class CreateMailDraftRequest(BaseModel):
    """创建空白新邮件或绑定本地来源的回复草稿。"""

    model_config = ConfigDict(extra="forbid")

    mode: MailMode = MailMode.NEW
    connection_id: UUID | None = None
    source_thread_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_message_id: str | None = Field(default=None, min_length=1, max_length=255)
    to: list[str] = Field(default_factory=list, max_length=50)
    cc: list[str] = Field(default_factory=list, max_length=50)
    bcc: list[str] = Field(default_factory=list, max_length=50)
    subject: str = Field(default="", max_length=255)
    body_text: str = Field(default="", max_length=100_000)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def validate_recipient_addresses(cls, values: list[str]) -> list[str]:
        """在开启事务前拒绝非法或含 Header 注入字符的邮箱地址。

        这里仅验证而不改写用户输入；规范化与跨字段去重仍由领域用例统一完成，避免
        API 与对话/Worker 路径形成第二套地址事实。
        """
        for value in values:
            normalize_mailbox_address(value)
        return values

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        """在请求事务前拒绝主题 CR/LF，避免邮件 Header 注入与内部校验异常。"""
        if "\r" in value or "\n" in value:
            raise ValueError("mail subject must not contain CR or LF")
        return value

    @model_validator(mode="after")
    def validate_source_shape(self) -> "CreateMailDraftRequest":
        """镜像应用输入的来源形状不变量，使非法请求统一进入脱敏 422 边界。"""
        if self.mode is MailMode.NEW:
            if self.source_thread_id is not None or self.source_message_id is not None:
                raise ValueError("new mail must not contain source binding")
            return self
        if self.source_thread_id is None and self.source_message_id is None:
            raise ValueError("reply mail requires a source thread or message")
        return self


class UpdateMailDraftRequest(BaseModel):
    """以客户端观察到的当前版本更新一封草稿。"""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    to: list[str] | None = Field(default=None, max_length=50)
    cc: list[str] | None = Field(default=None, max_length=50)
    bcc: list[str] | None = Field(default=None, max_length=50)
    subject: str | None = Field(default=None, max_length=255)
    body_text: str | None = Field(default=None, max_length=100_000)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def validate_recipient_addresses(cls, values: list[str] | None) -> list[str] | None:
        """PATCH 未出现的字段保持为空，出现的地址则执行严格语法校验。"""
        if values is not None:
            for value in values:
                normalize_mailbox_address(value)
        return values

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str | None) -> str | None:
        """PATCH 主题出现时拒绝 CR/LF，与创建和应用输入保持同一安全边界。"""
        if value is not None and ("\r" in value or "\n" in value):
            raise ValueError("mail subject must not contain CR or LF")
        return value


class GenerateMailDraftRequest(BaseModel):
    """创建 body-only 模型草拟任务所需的受控输入。"""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    instruction: str = Field(default="", max_length=12_000)


class SubmitMailDraftRequest(BaseModel):
    """冻结精确草稿版本所需的最小提交输入。"""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class AcceptedTaskResponse(BaseModel):
    """返回已持久创建的长任务标识，不暗示任务已完成。"""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    status: Literal["queued"]


def _missing_mail_draft() -> ApiProblem:
    """统一隐藏不存在和跨用户草稿，防止通过 UUID 探测资源归属。"""
    return ApiProblem(
        404,
        "mail_draft_not_found",
        "Mail draft not found",
        "The requested mail draft was not found.",
    )


def _sensitive_draft_response(
    value: MailDraftView,
    response: Response,
) -> MailDraftResponse:
    """显式投影可解密草稿并禁止浏览器或中间缓存保存敏感正文。"""
    response.headers["Cache-Control"] = "no-store"
    return MailDraftResponse.model_validate(value, from_attributes=True)


def build_mail_router() -> APIRouter:
    """构建七个邮件草稿路由并复用应用用例的用户隔离与版本约束。"""
    router = APIRouter(prefix="/api/v1/mail/drafts", tags=["mail"])

    @router.get("", response_model=MailDraftListResponse)
    async def list_mail_drafts(
        authenticated: CurrentSession,
        response: Response,
        use_case: Annotated[MailDraftUseCase, Depends(get_mail_draft_use_case)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> MailDraftListResponse:
        """按显式 limit/offset 列出当前用户的当前草稿版本。"""
        response.headers["Cache-Control"] = "no-store"
        values = await use_case.list(
            user_id=authenticated.user.id,
            limit=limit,
            offset=offset,
        )
        return MailDraftListResponse(
            items=[
                MailDraftResponse.model_validate(value, from_attributes=True) for value in values
            ],
            limit=limit,
            offset=offset,
        )

    @router.post("", status_code=status.HTTP_201_CREATED, response_model=MailDraftResponse)
    async def create_mail_draft(
        payload: CreateMailDraftRequest,
        authenticated: CsrfProtectedSession,
        response: Response,
        use_case: Annotated[MailDraftUseCase, Depends(get_mail_draft_use_case)],
        idempotency_key: IdempotencyKeyHeader,
    ) -> MailDraftResponse:
        """幂等创建版本一，只写本地加密草稿且不触发供应商草稿或发送。"""
        value = await use_case.create(
            CreateMailDraftInput(
                user_id=authenticated.user.id,
                idempotency_key=idempotency_key,
                **payload.model_dump(),
            )
        )
        return _sensitive_draft_response(value, response)

    @router.get("/{draft_id}", response_model=MailDraftResponse)
    async def get_mail_draft(
        draft_id: UUID,
        authenticated: CurrentSession,
        response: Response,
        use_case: Annotated[MailDraftUseCase, Depends(get_mail_draft_use_case)],
    ) -> MailDraftResponse:
        """读取当前用户可解密的当前版本，不公开历史密文或持久化对象。"""
        try:
            value = await use_case.get(
                user_id=authenticated.user.id,
                draft_id=draft_id,
            )
        except MailDraftNotFoundError:
            raise _missing_mail_draft() from None
        return _sensitive_draft_response(value, response)

    @router.patch("/{draft_id}", response_model=MailDraftResponse)
    async def update_mail_draft(
        draft_id: UUID,
        payload: UpdateMailDraftRequest,
        authenticated: CsrfProtectedSession,
        response: Response,
        use_case: Annotated[MailDraftUseCase, Depends(get_mail_draft_use_case)],
    ) -> MailDraftResponse:
        """以请求中的当前版本 CAS 保存下一不可变版本。"""
        try:
            value = await use_case.update(
                UpdateMailDraftInput(
                    user_id=authenticated.user.id,
                    draft_id=draft_id,
                    **payload.model_dump(exclude_unset=True),
                )
            )
        except MailDraftNotFoundError:
            # Repository 已按 user_id 过滤；统一映射为 404，避免泄露草稿是否属于另一用户。
            raise _missing_mail_draft() from None
        return _sensitive_draft_response(value, response)

    @router.delete("/{draft_id}", response_model=MailDraftResponse)
    async def cancel_mail_draft(
        draft_id: UUID,
        authenticated: CsrfProtectedSession,
        response: Response,
        use_case: Annotated[MailDraftUseCase, Depends(get_mail_draft_use_case)],
    ) -> MailDraftResponse:
        """取消仍处于可编辑态的纯本地草稿，不创建外部副作用。"""
        try:
            value = await use_case.cancel(
                user_id=authenticated.user.id,
                draft_id=draft_id,
            )
        except MailDraftNotFoundError:
            raise _missing_mail_draft() from None
        return _sensitive_draft_response(value, response)

    @router.post(
        "/{draft_id}/generate",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AcceptedTaskResponse,
    )
    async def generate_mail_draft(
        draft_id: UUID,
        payload: Annotated[GenerateMailDraftRequest, Body()],
        authenticated: CsrfProtectedSession,
        tasks: Annotated[CreateTaskUseCase, Depends(get_create_task_use_case)],
        drafts: Annotated[MailDraftUseCase, Depends(get_mail_draft_use_case)],
        idempotency_key: IdempotencyKeyHeader,
    ) -> AcceptedTaskResponse:
        """持久创建 body-only 模型草拟任务，并以客户端键复用 TaskRun。"""
        task_input: dict[str, JsonValue] = {
            "draft_id": str(draft_id),
            "expected_version": payload.version,
            "instruction": payload.instruction,
        }
        # 精确已有任务是已提交的业务事实，必须在读取当前草稿版本前识别；否则草稿后续
        # 编辑会让合法重放错误变成 409。键已绑定其他 kind/input 时该读取直接稳定拒绝。
        replayed = await tasks.replay(
            user_id=authenticated.user.id,
            kind="mail_draft.generate",
            input_payload=task_input,
            idempotency_key=idempotency_key,
        )
        if replayed is not None:
            return AcceptedTaskResponse(task_id=replayed.task_id, status="queued")
        try:
            current = await drafts.get(
                user_id=authenticated.user.id,
                draft_id=draft_id,
            )
        except MailDraftNotFoundError:
            raise _missing_mail_draft() from None
        if current.version != payload.version:
            raise StateConflictError(
                error_code="draft_version_conflict",
                message="mail draft version changed",
            )
        result = await tasks.execute(
            user_id=authenticated.user.id,
            kind="mail_draft.generate",
            input_payload=task_input,
            idempotency_key=idempotency_key,
        )
        return AcceptedTaskResponse(
            task_id=result.task_id,
            # 202 只承诺任务已持久排队；内部 CREATED/QUEUED 等中间状态不属于本契约，
            # 真实进度由任务快照与 SSE 提供，避免路由泄漏调度实现细节。
            status="queued",
        )

    @router.post(
        "/{draft_id}/submit",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AcceptedTaskResponse,
    )
    async def submit_mail_draft(
        draft_id: UUID,
        payload: SubmitMailDraftRequest,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[
            SubmitMailDraftUseCase,
            Depends(get_submit_mail_draft_use_case),
        ],
        drafts: Annotated[MailDraftUseCase, Depends(get_mail_draft_use_case)],
        clock: Annotated[Clock, Depends(get_auth_clock)],
        idempotency_key: IdempotencyKeyHeader,
    ) -> AcceptedTaskResponse:
        """原子冻结精确草稿版本、加密命令和单次人工审批，返回持久任务 ID。"""
        # 先做用户范围读取，把不存在与跨用户资源统一隐藏；提交用例内部的
        # ``draft_version_conflict`` 仅用于已知资源的版本/状态竞争。
        try:
            await drafts.get(
                user_id=authenticated.user.id,
                draft_id=draft_id,
            )
        except MailDraftNotFoundError:
            raise _missing_mail_draft() from None
        result = await use_case.execute(
            user_id=authenticated.user.id,
            draft_id=draft_id,
            expected_version=payload.version,
            idempotency_key=idempotency_key,
            now=clock.now(),
        )
        return AcceptedTaskResponse(task_id=result.task_id, status="queued")

    return router


__all__ = [
    "AcceptedTaskResponse",
    "CreateMailDraftRequest",
    "GenerateMailDraftRequest",
    "MailDraftListResponse",
    "MailDraftResponse",
    "SubmitMailDraftRequest",
    "UpdateMailDraftRequest",
    "build_mail_router",
]
