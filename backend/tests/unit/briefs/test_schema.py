"""每日简报公开 Schema 的边界测试。"""

import pytest
from pydantic import ValidationError

from ai_employee.domain.briefs import BriefItem, EmailJudgement


def test_schema_rejects_invalid_urgency_and_missing_source() -> None:
    with pytest.raises(ValidationError):
        EmailJudgement(thread_id="x", category="other", urgency="later", confidence=1)
    with pytest.raises(ValidationError):
        BriefItem(section="unknown", title="x", body_markdown="x", source_refs=[])


def test_email_judgement_rejects_unsupported_category() -> None:
    with pytest.raises(ValidationError):
        EmailJudgement(thread_id="x", category="send_email", confidence=1)
