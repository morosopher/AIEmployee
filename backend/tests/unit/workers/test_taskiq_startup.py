"""验证真实 ``just`` 启动配置在干净进程中显式注册 Taskiq 任务。"""

import json
import shlex
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_TASK_NAMES = [
    "ai_employee.workers.execute_task:execute_task",
    "ai_employee.workers.schedules:dispatch_due_briefs",
    "ai_employee.workers.schedules:expire_approvals",
    "ai_employee.workers.schedules:expire_sessions",
    "ai_employee.workers.schedules:recover_task_retries",
    "ai_employee.workers.schedules:relay_outbox",
]
EXPECTED_SCHEDULE_IDS = [
    "due-daily-briefs",
    "expire-approvals",
    "expire-sessions",
    "outbox-relay",
    "recover-task-retries",
]


def _dry_run_recipe(recipe: str) -> list[str]:
    """返回根 ``just`` recipe 展开的真实命令 token。

    Args:
        recipe: 待检查的根 recipe 名称。

    Returns:
        经 shell 规则解析、可直接定位 Taskiq positional modules 的 token 列表。
    """
    completed = subprocess.run(
        ["just", "--dry-run", recipe],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = completed.stdout.strip() or completed.stderr.strip()
    return shlex.split(rendered)


def _run_clean_registration_probe(script: str, *arguments: str) -> list[str]:
    """在全新 Python 解释器中运行 Taskiq CLI 等价的模块加载探针。

    Args:
        script: 只负责导入 CLI 对象、加载 positional modules 并输出 JSON 的脚本。
        arguments: 从真实 ``just`` dry-run 命令提取的对象路径与模块路径。

    Returns:
        干净进程输出的稳定任务名或 schedule ID 列表。
    """
    completed = subprocess.run(
        [sys.executable, "-c", script, *arguments],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = json.loads(completed.stdout)
    assert isinstance(parsed, list)
    return [str(value) for value in parsed]


def test_worker_recipe_registers_all_tasks_in_a_clean_process() -> None:
    """Worker 必须由真实启动命令显式加载执行入口与五个固定 job。"""
    tokens = _dry_run_recipe("worker")
    broker_path = "ai_employee.infrastructure.queue.broker:broker"
    broker_index = tokens.index(broker_path)
    modules = tokens[broker_index + 1 :]

    assert modules == [
        "ai_employee.workers.execute_task",
        "ai_employee.workers.schedules",
    ]

    registered = _run_clean_registration_probe(
        """
import json
import sys
from taskiq.cli.utils import import_object, import_tasks

broker = import_object(sys.argv[1])
broker.is_worker_process = True
import_tasks(sys.argv[2:], ["**/tasks.py"], False)
print(json.dumps(sorted(broker.get_all_tasks())))
""",
        broker_path,
        *modules,
    )

    assert registered == EXPECTED_TASK_NAMES


def test_scheduler_recipe_exposes_five_stable_label_jobs_in_a_clean_process() -> None:
    """Scheduler 启动配置必须显式加载固定 job，并由 label source 返回稳定 ID。"""
    tokens = _dry_run_recipe("scheduler")
    scheduler_path = "ai_employee.infrastructure.queue.scheduler:scheduler"
    scheduler_index = tokens.index(scheduler_path)
    modules = tokens[scheduler_index + 1 :]

    assert modules == ["ai_employee.workers.schedules"]

    schedule_ids = _run_clean_registration_probe(
        """
import asyncio
import json
import sys
from taskiq.cli.utils import import_object, import_tasks

scheduler = import_object(sys.argv[1])
scheduler.broker.is_scheduler_process = True
import_tasks(sys.argv[2:], ["**/tasks.py"], False)

async def main():
    await scheduler.sources[0].startup()
    schedules = await scheduler.sources[0].get_schedules()
    print(json.dumps(sorted(task.schedule_id for task in schedules)))

asyncio.run(main())
""",
        scheduler_path,
        *modules,
    )

    assert schedule_ids == EXPECTED_SCHEDULE_IDS
