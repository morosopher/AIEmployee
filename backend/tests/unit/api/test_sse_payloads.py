"""验证旧步骤事件在实际 SSE 编码中保留展示契约并继续过滤敏感元数据。"""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from ai_employee.api.sse import DurableTaskEvent, _event
from ai_employee.domain.tasks import JsonValue


def _encoded_step_payload(metadata: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """通过线上编码边界读取合成失败步骤载荷，不依赖数据库、网络或真实时间。"""
    event = DurableTaskEvent(
        id=901,
        task_id=UUID("00000000-0000-0000-0000-000000000401"),
        event="step.failed",
        occurred_at=datetime(2030, 1, 1, tzinfo=UTC),
        step_id=UUID("00000000-0000-0000-0000-000000000402"),
        payload=metadata,
    )
    encoded = _event(event).encode().decode("utf-8")
    data_line = next(line for line in encoded.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))["payload"]


def test_failed_step_sse_preserves_safe_summary_and_error_code() -> None:
    """失败步骤与启动、完成事件使用相同受控字段，稳定错误码和尝试次数可展示。"""
    metadata: dict[str, JsonValue] = {
        "name": "load_sources",
        "sequence": 7,
        "status": "failed",
        "error_code": "provider_unavailable",
        "output_summary": {
            "status": "failed",
            "error_code": "provider_unavailable",
            "attempt_count": 2,
        },
    }

    assert _encoded_step_payload(metadata) == metadata


@pytest.mark.parametrize(
    ("name", "sequence"),
    [
        ("synthetic private title", True),
        ("x" * 81, -1),
        ("person@example.test", 2**31),
    ],
)
def test_step_sse_does_not_restore_uncontrolled_metadata(name: str, sequence: int) -> None:
    """旧事件兼容不能恢复自由文本、非法排序值、正文或摘要中的未知嵌套内容。"""
    metadata: dict[str, JsonValue] = {
        "name": name,
        "sequence": sequence,
        "subject": "synthetic-private-subject",
        "unexpected": {"body_text": "synthetic-private-body"},
        "output_summary": {
            "status": "failed",
            "body_text": "synthetic-private-body",
            "attendees": ["person@example.test"],
            "unexpected": {"status": "succeeded"},
        },
    }

    assert _encoded_step_payload(metadata) == {"output_summary": {"status": "failed"}}
