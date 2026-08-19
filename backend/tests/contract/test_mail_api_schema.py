"""邮件草稿 API OpenAPI 路径契约的 RED 测试。"""

import pytest
from pydantic import ValidationError

from ai_employee.api.routers.mail import (
    AcceptedTaskResponse,
    CreateMailDraftRequest,
    GenerateMailDraftRequest,
    MailDraftListResponse,
    MailDraftResponse,
    SubmitMailDraftRequest,
    UpdateMailDraftRequest,
)
from ai_employee.main import create_app


@pytest.mark.asyncio
async def test_mail_api_exposes_all_draft_operations_in_openapi() -> None:
    """OpenAPI 必须公开列表、创建、读取、编辑、取消和两个异步操作。"""
    paths = create_app().openapi()["paths"]
    assert "/api/v1/mail/drafts" in paths
    assert set(paths["/api/v1/mail/drafts"]) >= {"get", "post"}
    assert set(paths["/api/v1/mail/drafts/{draft_id}"]) >= {"get", "patch", "delete"}
    assert set(paths["/api/v1/mail/drafts/{draft_id}/generate"]) >= {"post"}
    assert set(paths["/api/v1/mail/drafts/{draft_id}/submit"]) >= {"post"}


@pytest.mark.parametrize(
    ("path", "method"),
    (
        ("/api/v1/mail/drafts", "post"),
        ("/api/v1/mail/drafts/{draft_id}/generate", "post"),
        ("/api/v1/mail/drafts/{draft_id}/submit", "post"),
    ),
    ids=("create", "generate", "submit"),
)
def test_mail_creation_actions_publish_required_bounded_idempotency_header(
    path: str,
    method: str,
) -> None:
    """OpenAPI 必须把三个创建事实入口的幂等键声明为必需且非 nullable。"""
    operation = create_app().openapi()["paths"][path][method]
    parameter = next(
        item
        for item in operation["parameters"]
        if item["in"] == "header" and item["name"] == "Idempotency-Key"
    )

    assert parameter["required"] is True
    assert parameter["schema"]["type"] == "string"
    assert parameter["schema"]["minLength"] == 1
    assert parameter["schema"]["maxLength"] == 255
    assert "nullable" not in parameter["schema"]
    assert "anyOf" not in parameter["schema"]


def test_create_mail_draft_schema_rejects_invalid_recipient_address() -> None:
    """无效邮箱地址必须在 API Schema 边界被拒绝，不进入领域事务。"""
    with pytest.raises(ValidationError):
        CreateMailDraftRequest(to=["not-an-email-address"])


def test_update_mail_draft_schema_rejects_invalid_recipient_address() -> None:
    """PATCH 出现的地址字段采用与创建请求相同的严格语法边界。"""
    with pytest.raises(ValidationError):
        UpdateMailDraftRequest(version=1, cc=["invalid\n@example.test"])


@pytest.mark.parametrize(
    "payload",
    (
        {"mode": "reply"},
        {"mode": "reply_all"},
        {"mode": "new", "source_thread_id": "synthetic-thread"},
        {"mode": "new", "source_message_id": "synthetic-message"},
    ),
)
def test_create_mail_draft_schema_rejects_invalid_source_shape(
    payload: dict[str, str],
) -> None:
    """API Schema 镜像来源形状不变量，禁止把内部 Pydantic 错误升级为 500。"""
    with pytest.raises(ValidationError):
        CreateMailDraftRequest(**payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_thread_id", " padded-thread "),
        ("source_thread_id", "thread\rbreak"),
        ("source_thread_id", "thread\nbreak"),
        ("source_message_id", " padded-message "),
        ("source_message_id", "message\rbreak"),
        ("source_message_id", "message\nbreak"),
    ),
)
def test_create_mail_draft_schema_rejects_unpadded_or_multiline_source_identifier(
    field: str,
    value: str,
) -> None:
    """公开来源标识符必须原样保持 opaque，但拒绝首尾空白与 CR/LF。"""
    with pytest.raises(ValidationError):
        CreateMailDraftRequest(mode="reply", **{field: value})


def test_create_mail_draft_schema_rejects_subject_header_injection() -> None:
    """创建主题含 CR/LF 时必须在 HTTP 请求边界拒绝。"""
    with pytest.raises(ValidationError):
        CreateMailDraftRequest(subject="Synthetic subject\r\nX-Marker: sensitive")


def test_update_mail_draft_schema_rejects_subject_header_injection() -> None:
    """PATCH 主题采用与创建相同的单行约束，不能把异常留给应用层。"""
    with pytest.raises(ValidationError):
        UpdateMailDraftRequest(
            version=1,
            subject="Synthetic subject\r\nX-Marker: sensitive",
        )


@pytest.mark.parametrize(
    "schema",
    (
        CreateMailDraftRequest,
        UpdateMailDraftRequest,
        GenerateMailDraftRequest,
        SubmitMailDraftRequest,
        MailDraftResponse,
        MailDraftListResponse,
        AcceptedTaskResponse,
    ),
)
def test_mail_api_models_forbid_unknown_fields(schema: type[object]) -> None:
    """公开邮件 API 模型必须拒绝未审查字段，避免静默扩大协议。"""
    assert schema.model_config.get("extra") == "forbid"  # type: ignore[attr-defined]
