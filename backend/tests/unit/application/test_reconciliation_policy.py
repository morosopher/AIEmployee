"""验证未知结果策略、人工 CAS 输入边界和只读 Worker 入口。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_employee.application.use_cases.action_views import (
    ManualResolutionUseCase,
    RequestActionReconciliationUseCase,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.application.use_cases.trusted_actions import (
    TrustedActionGraphFacts,
    reconciliation_delay,
    validate_provider_url,
)
from ai_employee.workers.reconcile_actions import (
    ReconcileActionsTaskStep,
    _release_reconciliation_lease,
)


@pytest.mark.parametrize(
    ("attempt", "delay_seconds"),
    [(0, 1), (1, 5), (2, 30), (3, 120)],
)
def test_reconciliation_schedule_is_bounded(attempt: int, delay_seconds: int) -> None:
    """四次自动核对必须使用冻结的 1/5/30/120 秒退避。"""
    assert reconciliation_delay(attempt) == timedelta(seconds=delay_seconds)


def test_reconciliation_schedule_rejects_attempts_outside_bound() -> None:
    """超出四次预算或负数尝试不能产生未定义的调度。"""
    with pytest.raises(ValueError):
        reconciliation_delay(-1)
    with pytest.raises(ValueError):
        reconciliation_delay(4)


def test_provider_url_rejects_encoded_addresses_and_query_credentials() -> None:
    """URL 只保留无地址/凭据的 HTTPS opaque 链接，单/双重编码也必须被扫描。"""
    assert validate_provider_url("https://provider.example.test/item/opaque") == (
        "https://provider.example.test/item/opaque"
    )
    for candidate in (
        "http://provider.example.test/item",
        "https://provider.example.test/item/user@example.test",
        "https://provider.example.test/item/user%40example.test",
        "https://provider.example.test/item/user%2540example.test",
        "https://provider.example.test/item?recipient=user%40example.test",
        "https://provider.example.test/item?access_token=synthetic-secret",
        "https://provider.example.test/item?password=synthetic-secret",
        "https://provider.example.test/item?token",
        "https://provider.example.test/item;token=synthetic-secret",
        "https://provider.example.test/item?%74%6f%6b%65%6e=synthetic-secret",
        "https://provider.example.test/item?token%3Dsynthetic-secret",
        "https://provider.example.test/item/%00",
    ):
        assert validate_provider_url(candidate) is None


@pytest.mark.asyncio
async def test_manual_resolution_rejects_non_string_resolution_without_hash_error() -> None:
    """人工结论必须先收窄为字符串，列表/字典不能触发 membership TypeError。"""
    transaction = _Transaction()
    use_case = ManualResolutionUseCase(_TransactionFactory(transaction))
    user_id = uuid4()
    now = datetime(2030, 1, 1, tzinfo=UTC)
    for invalid in ([], {}, 1, True, None):
        with pytest.raises(ValueError):
            await use_case.execute(
                user_id=user_id,
                task_id=uuid4(),
                task_version="0",
                resolution=invalid,  # type: ignore[arg-type]
                now=now,
            )


class _Transaction:
    """记录人工/重开用例传入的规范参数，不连接外部系统。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.manual_calls: list[dict[str, object]] = []
        self.reconcile_calls: list[dict[str, object]] = []

    async def resolve_manual(self, **kwargs: object) -> int:
        """返回一个合成 PostgreSQL BIGINT 审计 ID。"""
        self.manual_calls.append(kwargs)
        return 42

    async def request_reconciliation(self, **kwargs: object):
        """返回稳定的原 ToolExecution UUID。"""
        self.reconcile_calls.append(kwargs)
        return kwargs["task_id"]


class _TransactionFactory:
    """为每次用例调用提供同一个合成事务对象。"""

    def __init__(self, transaction: _Transaction) -> None:
        """保存事务记录器。"""
        self.transaction = transaction

    @asynccontextmanager
    async def __call__(self):
        """模拟 repository factory 的自动提交上下文。"""
        yield self.transaction


@pytest.mark.asyncio
async def test_manual_resolution_use_case_validates_canonical_version_and_returns_new_cursor() -> (
    None
):
    """人工用例只接受枚举/规范游标，并把新审计 ID序列化为 canonical string。"""
    transaction = _Transaction()
    use_case = ManualResolutionUseCase(_TransactionFactory(transaction))
    user_id = uuid4()
    task_id = uuid4()
    result = await use_case.execute(
        user_id=user_id,
        task_id=task_id,
        task_version="7",
        resolution="confirmed_not_executed",
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert result == "42"
    assert transaction.manual_calls == [
        {
            "user_id": user_id,
            "task_id": task_id,
            "task_version": 7,
            "resolution": "confirmed_not_executed",
            "resolved_at": datetime(2030, 1, 1, tzinfo=UTC),
        }
    ]
    with pytest.raises(ValueError):
        await use_case.execute(
            user_id=user_id,
            task_id=task_id,
            task_version="01",
            resolution="confirmed_not_executed",
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(ValueError):
        await use_case.execute(
            user_id=user_id,
            task_id=task_id,
            task_version="7",
            resolution="free_text",
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_request_reconciliation_use_case_forwards_utc_time_without_write_decision() -> None:
    """用户重开端口只传递 task/user/time，不携带命令或写动作选择。"""
    transaction = _Transaction()
    task_id = uuid4()
    result = await RequestActionReconciliationUseCase(_TransactionFactory(transaction)).execute(
        user_id=uuid4(),
        task_id=task_id,
        now=datetime(2030, 1, 1, 8, tzinfo=UTC),
    )
    assert result == task_id
    assert len(transaction.reconcile_calls) == 1
    assert set(transaction.reconcile_calls[0]) == {"user_id", "task_id", "requested_at"}


class _ReadOnlyWorkflow:
    """验证 reconciliation Step 不会意外调用 execute。"""

    def __init__(self) -> None:
        """初始化调用计数。"""
        self.load_calls = 0
        self.reconcile_calls = 0
        self.execute_calls = 0

    async def load_graph_facts(self, **_: object) -> TrustedActionGraphFacts:
        """返回只含合成哈希的图事实。"""
        self.load_calls += 1
        return TrustedActionGraphFacts(payload_hash="a" * 64, decision="approved")

    async def reconcile(self, **_: object) -> None:
        """记录唯一允许的 provider 只读入口。"""
        self.reconcile_calls += 1

    async def execute(self, **_: object) -> None:
        """若被调用则测试失败，确保没有写路径穿透。"""
        self.execute_calls += 1
        raise AssertionError("reconciliation step must never call execute")


@pytest.mark.asyncio
async def test_reconcile_step_only_calls_read_only_workflow() -> None:
    """专用 Step 从 identifier-only 输入加载哈希后只调用 reconcile。"""
    workflow = _ReadOnlyWorkflow()
    task_id = uuid4()
    approval_id = uuid4()
    operation_id = uuid4()
    task = LeasedTask(
        task_id=task_id,
        kind="trusted_action",
        input_payload={
            "approval_id": str(approval_id),
            "operation_id": str(operation_id),
        },
        started_at=datetime(2030, 1, 1, tzinfo=UTC),
        lease_owner="reconcile:test",
    )
    await ReconcileActionsTaskStep(workflow).execute(task)  # type: ignore[arg-type]
    assert workflow.load_calls == 1
    assert workflow.reconcile_calls == 1
    assert workflow.execute_calls == 0


@pytest.mark.asyncio
async def test_reconciliation_lease_release_finishes_after_outer_cancellation() -> None:
    """取消 provider 核对时，shield 的内部释放事务必须先完成再传播取消。"""
    started = asyncio.Event()
    finish = asyncio.Event()
    calls: list[dict[str, object]] = []

    class _Transaction:
        """阻塞一次释放调用，模拟数据库提交尚未返回的窗口。"""

        async def release_reconciliation_lease(self, **kwargs: object) -> bool:
            """记录 owner/CAS 参数并等待测试显式允许提交。"""
            calls.append(kwargs)
            started.set()
            await finish.wait()
            return True

    @asynccontextmanager
    async def _transactions():
        """提供单一合成事务上下文。"""
        yield _Transaction()

    task = asyncio.create_task(
        _release_reconciliation_lease(
            transactions=_transactions,  # type: ignore[arg-type]
            task_id=uuid4(),
            lease_owner="reconcile:test",
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) == 1
    assert calls[0]["lease_owner"] == "reconcile:test"
    assert calls[0]["now"] == datetime(2030, 1, 1, tzinfo=UTC)
