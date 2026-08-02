"""验证可信任务创建用例的提交后投递契约。"""

from typing import cast

import pytest

from ai_employee.application.use_cases.tasks import (
    CreateTaskUseCase,
    TaskRepositoryFactory,
)


def test_create_task_use_case_requires_post_commit_dispatcher() -> None:
    """构造用例时不得省略提交后 dispatcher，避免任务永久停留在 CREATED。"""
    repositories = cast(TaskRepositoryFactory, object())

    with pytest.raises(TypeError):
        CreateTaskUseCase(repositories)
