"""验证 Microsoft 初始连接请求只允许非空读取能力子集。"""

import pytest
from pydantic import ValidationError

from ai_employee.api.routers.connections import MicrosoftStartConnectionRequest
from ai_employee.domain.connections import ConnectionCapability


def test_microsoft_start_schema_accepts_single_read_capability() -> None:
    """请求可以只选择邮件或只选择日历，而不被 API 强制扩展为两者。"""
    mail = MicrosoftStartConnectionRequest.model_validate({"capabilities": ["mail.read"]})
    calendar = MicrosoftStartConnectionRequest.model_validate(
        {"capabilities": ["calendar.read"]}
    )

    assert mail.capability_set() == frozenset({ConnectionCapability.MAIL_READ})
    assert calendar.capability_set() == frozenset({ConnectionCapability.CALENDAR_READ})


def test_microsoft_start_schema_defaults_legacy_empty_request_to_both_reads() -> None:
    """省略 body 时兼容旧入口，但默认值只包含两项读取能力。"""
    request = MicrosoftStartConnectionRequest()

    assert request.capability_set() == frozenset(
        {
            ConnectionCapability.MAIL_READ,
            ConnectionCapability.CALENDAR_READ,
        }
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"capabilities": []},
        {"capabilities": ["mail.send"]},
        {"capabilities": ["calendar.write"]},
        {"capabilities": ["contacts.read"]},
    ],
)
def test_microsoft_start_schema_rejects_empty_write_or_unknown_capabilities(
    payload: dict[str, object],
) -> None:
    """空集合、写能力和未知值必须在 API 边界拒绝。"""
    with pytest.raises(ValidationError):
        MicrosoftStartConnectionRequest.model_validate(payload)
