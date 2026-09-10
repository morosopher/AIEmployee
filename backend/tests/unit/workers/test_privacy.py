"""验证隐私删除最终串行化边界的稳定失败契约。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from ai_employee.application.use_cases.privacy import PrivacyDeletionBinding
from ai_employee.domain.errors import StateConflictError
from ai_employee.workers import privacy as privacy_module
from ai_employee.workers.privacy import PrivacyDeletionWorker


class _MissingTaskSession:
    """只允许执行一次 Task 根行锁查询，并记录任何越界数据库动作。"""

    def __init__(self) -> None:
        self.scalar_calls = 0

    async def scalars(self, statement: object) -> _MissingTaskSession:
        """空 Task 集合表示绑定根不存在；不能继续锁用户或读取审计。"""
        del statement
        self.scalar_calls += 1
        if self.scalar_calls != 1:
            raise AssertionError("missing task must stop before user or audit lookup")
        return self

    def all(self) -> list[object]:
        """返回真实缺少目标 Task 的空查询投影。"""
        return []

    async def execute(self, statement: object) -> None:
        """不存在用户时不得执行匿名化 UPDATE。"""
        del statement
        raise AssertionError("missing user must not execute mutations")

    def add(self, item: object) -> None:
        """不存在用户时不得构造待提交完成审计。"""
        del item
        raise AssertionError("missing user must not add an audit event")


class _MissingUserSessionFactory:
    """为 Worker 提供单个可追踪的合成事务。"""

    def __init__(self) -> None:
        self.session = _MissingTaskSession()

    def begin(self) -> _MissingUserSessionFactory:
        """返回自身作为异步事务 context manager。"""
        return self

    async def __aenter__(self) -> _MissingTaskSession:
        """进入合成事务。"""
        return self.session

    async def __aexit__(self, *args: object) -> None:
        """退出合成事务；不模拟额外提交异常。"""
        del args


@pytest.mark.asyncio
async def test_finalize_deleted_user_rejects_missing_lock_root_before_time_or_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2最终事务先锁Task；缺失绑定根必须在取时、审计读取与写入前拒绝。"""

    class _ForbiddenDatetime:
        """证明 missing-user 分支不应读取当前时间。"""

        @classmethod
        def now(cls, timezone: object) -> None:
            """任何调用都表示 Worker 在确认串行化根之前取时。"""
            del cls, timezone
            raise AssertionError("current time was read before the user lock was confirmed")

    monkeypatch.setattr(privacy_module, "datetime", _ForbiddenDatetime)
    session_factory = _MissingUserSessionFactory()
    worker = PrivacyDeletionWorker(session_factory)  # type: ignore[arg-type]

    with pytest.raises(StateConflictError) as caught:
        await worker._finalize_deleted_user(
            binding=PrivacyDeletionBinding(
                uuid4(), uuid4(), "synthetic-missing-task-request", "synthetic-owner"
            ),
        )

    assert caught.value.error_code == "privacy_deletion_unavailable"
    assert caught.value.message == "Privacy deletion lease is unavailable"
    assert caught.value.metadata == {}
    assert session_factory.session.scalar_calls == 1
