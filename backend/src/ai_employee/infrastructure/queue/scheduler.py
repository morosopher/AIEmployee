"""构造只读取固定任务 label 的 Taskiq Scheduler。"""

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from ai_employee.infrastructure.queue.broker import broker
from ai_employee.workers import schedules as fixed_schedules

# 用户时区与简报时间始终保存在 PostgreSQL；LabelScheduleSource 只触发固定扫描入口。
# 显式引用模块确保 scheduler 进程仅注册 Task 7 已批准的三个固定 label job。
_FIXED_SCHEDULES_REGISTERED = fixed_schedules
scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)
