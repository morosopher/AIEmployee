"""验证隐私删除用例为同一客户端请求生成稳定且不含原文的请求标识。"""

from __future__ import annotations

from typing import cast
from uuid import UUID

import pytest

from ai_employee.application.use_cases import privacy as privacy_use_cases
from ai_employee.application.use_cases.privacy import RequestAllDataDeletionUseCase
from ai_employee.application.use_cases.tasks import CreateTaskResult, CreateTaskUseCase
from ai_employee.domain.tasks import JsonValue

USER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_USER_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
IDEMPOTENCY_KEY = "privacy-delete:客户端-key"
OTHER_IDEMPOTENCY_KEY = "privacy-delete:other-key"


class _RecordingTaskCreator:
    """记录任务创建输入，以隔离稳定请求标识测试与数据库。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls: list[dict[str, object]] = []

    async def execute(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> CreateTaskResult:
        """保存任务创建参数并返回固定的合成结果。"""
        self.calls.append(
            {
                "user_id": user_id,
                "kind": kind,
                "input_payload": input_payload,
                "idempotency_key": idempotency_key,
            }
        )
        return CreateTaskResult(task_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))


@pytest.mark.asyncio
async def test_all_data_deletion_request_id_is_stable_and_content_free() -> None:
    """同一用户和幂等键重试必须复用完全相同、不可逆推出原文的请求 ID。"""
    creator = _RecordingTaskCreator()
    use_case = RequestAllDataDeletionUseCase(cast(CreateTaskUseCase, creator))

    await use_case.execute(user_id=USER_ID, idempotency_key=IDEMPOTENCY_KEY)
    await use_case.execute(user_id=USER_ID, idempotency_key=IDEMPOTENCY_KEY)

    first_payload = cast(dict[str, JsonValue], creator.calls[0]["input_payload"])
    retry_payload = cast(dict[str, JsonValue], creator.calls[1]["input_payload"])
    first_request_id = first_payload["deletion_request_id"]

    assert first_payload == retry_payload
    assert type(first_request_id) is str
    assert len(first_request_id) == 64
    assert first_request_id == first_request_id.lower()
    assert all(character in "0123456789abcdef" for character in first_request_id)
    assert str(USER_ID) not in first_request_id
    assert IDEMPOTENCY_KEY not in first_request_id


@pytest.mark.asyncio
async def test_all_data_deletion_request_id_is_scoped_to_user_and_key() -> None:
    """不同用户或不同幂等键不得共享全数据删除请求 ID。"""
    creator = _RecordingTaskCreator()
    use_case = RequestAllDataDeletionUseCase(cast(CreateTaskUseCase, creator))

    await use_case.execute(user_id=USER_ID, idempotency_key=IDEMPOTENCY_KEY)
    await use_case.execute(user_id=OTHER_USER_ID, idempotency_key=IDEMPOTENCY_KEY)
    await use_case.execute(user_id=USER_ID, idempotency_key=OTHER_IDEMPOTENCY_KEY)

    request_ids = [
        cast(dict[str, JsonValue], call["input_payload"])["deletion_request_id"]
        for call in creator.calls
    ]

    assert len(set(request_ids)) == 3


@pytest.mark.parametrize(
    ("change", "accepted"),
    [
        ("exact", True),
        ("zero", False),
        ("many", False),
        ("empty_expected_request", False),
        ("wrong_event", False),
        ("null_task", False),
        ("wrong_task", False),
        ("wrong_user", False),
        ("missing_schema", False),
        ("missing_request", False),
        ("extra_metadata", False),
        ("wrong_schema", False),
        ("null_request", False),
        ("number_request", False),
        ("empty_request", False),
        ("wrong_request", False),
    ],
)
def test_deletion_started_authority_requires_one_exact_fact(change: str, accepted: bool) -> None:
    """完整 per-user 事实集只允许唯一精确绑定，任何歧义均不得选择或强制转换赢家。"""
    task_id = UUID("00000000-0000-0000-0000-000000000401")
    other_task_id = UUID("00000000-0000-0000-0000-000000000402")
    metadata: dict[str, JsonValue] = {
        "schema_version": "privacy_deletion_started.v1",
        "request_id": "synthetic-request-1",
    }
    if change == "missing_schema":
        del metadata["schema_version"]
    elif change == "missing_request":
        del metadata["request_id"]
    elif change == "extra_metadata":
        metadata["extra"] = None
    elif change == "wrong_schema":
        metadata["schema_version"] = "privacy_deletion_started.v2"
    elif change in {"null_request", "number_request", "empty_request", "wrong_request"}:
        metadata["request_id"] = {
            "null_request": None,
            "number_request": 1,
            "empty_request": "",
            "wrong_request": "synthetic-request-2",
        }[change]
    fact = privacy_use_cases.PrivacyDeletionStartedFact(
        event_type="task.running" if change == "wrong_event" else "privacy.deletion_started",
        user_id=OTHER_USER_ID if change == "wrong_user" else USER_ID,
        task_id=None
        if change == "null_task"
        else other_task_id
        if change == "wrong_task"
        else task_id,
        event_metadata=metadata,
    )
    facts = [] if change == "zero" else [fact, fact] if change == "many" else [fact]
    result = privacy_use_cases.parse_privacy_deletion_started_authority(
        facts,
        expected_user_id=USER_ID,
        expected_task_id=task_id,
        expected_request_id="" if change == "empty_expected_request" else "synthetic-request-1",
    )
    if accepted:
        assert result == privacy_use_cases.PrivacyDeletionStartedAuthority(
            user_id=USER_ID,
            task_id=task_id,
            request_id="synthetic-request-1",
        )
    else:
        assert result is None
