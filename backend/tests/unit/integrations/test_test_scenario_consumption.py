"""验证 test-support 写入的 Redis 场景由离线 fake 原子消费。"""

from pathlib import Path
from uuid import uuid4

import pytest

from ai_employee.api.routers.test_support import TestScenarioStore as ScenarioStore
from ai_employee.domain.briefs import EmailJudgement
from ai_employee.domain.errors import TransientProviderError, UserActionRequiredError
from ai_employee.integrations.google.fake import (
    FakeCalendarReader,
    FakeGmailReader,
    FakeGoogleOAuthClient,
)
from ai_employee.integrations.llm.fake import FakeModelGateway, ModelGatewayError


class _Redis:
    """以字典模拟 Redis GETDEL，测试原子一次性合约而非供应商网络。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, ex: int) -> None:
        """记录 TTL 参数，生产端点的 TTL 契约由已有路由测试覆盖。"""
        assert ex == 600
        self.values[key] = value

    async def getdel(self, key: str) -> str | None:
        """模拟 Redis 原子读取后删除。"""
        return self.values.pop(key, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "reader_type", "expected_code"),
    [
        ("gmail_429", FakeGmailReader, "google_rate_limited"),
        ("calendar_5xx", FakeCalendarReader, "google_service_unavailable"),
    ],
)
async def test_google_fake_consumes_each_user_scenario_once(
    scenario: str, reader_type: type[FakeGmailReader] | type[FakeCalendarReader], expected_code: str
) -> None:
    """Google fake 首次读取施加暂态错误，第二次读取回到合成 fixture。"""
    user_id = uuid4()
    store = ScenarioStore(_Redis())
    await store.set(user_id=user_id, scenario=scenario)
    fixture_name = (
        "gmail_initial.json" if reader_type is FakeGmailReader else "calendar_initial.json"
    )
    fixture = Path(__file__).parents[2] / "contract" / "fixtures" / fixture_name
    reader = reader_type(
        fixture,
        scenario_consumer=lambda current_user_id: store.consume(user_id=current_user_id),
        user_id=user_id,
    )

    with pytest.raises(TransientProviderError) as raised:
        [page async for page in reader.initial_pages()]
    assert raised.value.error_code == expected_code
    assert raised.value.retry_after == (30 if scenario == "gmail_429" else None)
    assert [page async for page in reader.initial_pages()]


@pytest.mark.asyncio
async def test_oauth_fake_consumes_revocation_once() -> None:
    """撤销场景只能让同一用户的第一笔 OAuth 读取要求重连。"""
    user_id = uuid4()
    store = ScenarioStore(_Redis())
    await store.set(user_id=user_id, scenario="oauth_revoked")
    oauth = FakeGoogleOAuthClient(
        scenario_consumer=lambda current_user_id: store.consume(user_id=current_user_id),
        user_id=user_id,
    )

    with pytest.raises(UserActionRequiredError) as raised:
        await oauth.fetch_account("synthetic")
    assert raised.value.error_code == "google_reauthorization_required"
    assert (await oauth.fetch_account("synthetic")).email == "test-mode@example.test"


@pytest.mark.asyncio
async def test_model_fake_consumes_invalid_twice_scenario_and_preserves_repair_contract() -> None:
    """一次 Redis 场景令模型的初始与唯一修复调用均失败，之后恢复正常。"""
    user_id = uuid4()
    store = ScenarioStore(_Redis())
    await store.set(user_id=user_id, scenario="model_invalid_twice")
    gateway = FakeModelGateway(
        scenario_consumer=lambda current_user_id: store.consume(user_id=current_user_id),
        user_id=user_id,
    )
    request = {
        "model_name": "fake",
        "prompt_version": "daily_brief_v1",
        "messages": [{"role": "user", "content": "thread"}],
        "response_model": EmailJudgement,
    }

    with pytest.raises(ModelGatewayError, match="model_invalid_output"):
        await gateway.complete(**request)
    with pytest.raises(ModelGatewayError, match="model_invalid_output"):
        await gateway.complete(**request)
    assert (await gateway.complete(**request)).value.thread_id == "thread"
