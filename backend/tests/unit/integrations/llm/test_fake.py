"""Fake 模型场景可区分且不需要网络。"""

import pytest

from ai_employee.domain.briefs import EmailJudgement
from ai_employee.integrations.llm.fake import FakeModelGateway


@pytest.mark.asyncio
async def test_partial_scenario_has_distinct_metadata() -> None:
    response = await FakeModelGateway(scenario="partial").complete(
        model_name="x",
        prompt_version="x",
        messages=[{"role": "user", "content": "thread"}],
        response_model=EmailJudgement,
    )
    assert response.value.reason_codes == ["fake_partial"]
    assert response.usage.output_tokens == 1
