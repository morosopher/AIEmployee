"""构造只触发 PostgreSQL 固定扫描任务的 Taskiq Scheduler。"""

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from ai_employee.infrastructure.queue.broker import broker

# 用户时区、简报时间和延迟重试都以 PostgreSQL 为事实来源；LabelScheduleSource 只触发
# 固定扫描入口。Taskiq CLI 会先把 broker 标记为 scheduler process，再加载 ``just scheduler``
# 显式列出的固定任务模块；此处不能提前 import，否则模块缓存会让 label job 在标记生效前完成装饰。
scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)
