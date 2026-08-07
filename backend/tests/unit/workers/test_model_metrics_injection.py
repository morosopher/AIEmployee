"""验证 Worker 组合根不会遗漏模型和同步的聚合 metrics。"""

from uuid import uuid4

import pytest

from ai_employee.config import Settings
from ai_employee.workers import conversation, generate_brief


@pytest.mark.parametrize("module", (generate_brief, conversation))
def test_model_task_builders_forward_worker_metrics_to_gateway(
    monkeypatch: pytest.MonkeyPatch, module: object
) -> None:
    """daily brief 与对话均不得遗漏真实模型网关的 metrics 注入。"""
    captured: list[object | None] = []
    worker_metrics = object()

    def build_gateway(_settings: Settings, *, metrics: object | None = None) -> object:
        """替代真实 factory，避免测试读取 Secret 或访问网络。"""
        captured.append(metrics)
        return object()

    monkeypatch.setattr(module, "build_model_gateway", build_gateway)
    settings = Settings(app_test_mode=False)
    if module is generate_brief:
        generate_brief.build_generate_brief_task_step(
            session_factory=object(), settings=settings, metrics=worker_metrics  # type: ignore[arg-type]
        )
    else:
        conversation.build_conversation_task_step(
            session_factory=object(), settings=settings, metrics=worker_metrics  # type: ignore[arg-type]
        )

    assert captured == [worker_metrics]


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_kind", ("mail", "calendar"))
async def test_daily_brief_internal_sync_forwards_worker_metrics(
    monkeypatch: pytest.MonkeyPatch, resource_kind: str
) -> None:
    """简报触发的补偿同步必须与独立同步任务使用同一 Worker 指标实例。"""
    captured: list[object | None] = []
    worker_metrics = object()

    class SyncStep:
        """替代只读同步步骤，隔离本测试与供应商和数据库。"""

        async def execute(self, _task: object) -> None:
            """模拟成功同步，不产生外部副作用。"""

    def build_sync_step(*, metrics: object | None = None, **_kwargs: object) -> SyncStep:
        """记录组合根传给同步步骤的指标实例。"""
        captured.append(metrics)
        return SyncStep()

    monkeypatch.setattr(generate_brief, "build_model_gateway", lambda *_args, **_kwargs: object())
    from ai_employee.workers import sync_calendar, sync_mail

    monkeypatch.setattr(sync_mail, "build_mail_sync_task_step", build_sync_step)
    monkeypatch.setattr(sync_calendar, "build_calendar_sync_task_step", build_sync_step)
    step = generate_brief.build_generate_brief_task_step(
        session_factory=object(),
        settings=Settings(app_test_mode=False),
        metrics=worker_metrics,  # type: ignore[arg-type]
    )

    assert step._sync_source is not None
    await step._sync_source(
        resource_kind,
        uuid4(),
        uuid4(),
        "mailbox" if resource_kind == "mail" else "primary",
    )

    assert captured == [worker_metrics]
