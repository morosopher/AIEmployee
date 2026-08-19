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


def test_create_mail_draft_schema_rejects_invalid_recipient_address() -> None:
    """无效邮箱地址必须在 API Schema 边界被拒绝，不进入领域事务。"""
    with pytest.raises(ValidationError):
        CreateMailDraftRequest(to=["not-an-email-address"])


def test_update_mail_draft_schema_rejects_invalid_recipient_address() -> None:
    """PATCH 出现的地址字段采用与创建请求相同的严格语法边界。"""
    with pytest.raises(ValidationError):
        UpdateMailDraftRequest(version=1, cc=["invalid\n@example.test"])


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
