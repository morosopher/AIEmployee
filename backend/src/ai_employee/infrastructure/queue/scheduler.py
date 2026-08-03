"""构造同时消费延迟重投与固定扫描任务的 Taskiq Scheduler。"""

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from ai_employee.infrastructure.queue.broker import broker, retry_schedule_source

# 用户时区与简报时间始终保存在 PostgreSQL；LabelScheduleSource 只触发固定扫描入口，
# retry_schedule_source 则是 SmartRetry 的唯一延迟投递事实。Taskiq CLI 会先把 broker 标记为
# scheduler process，再加载 ``just scheduler`` 显式列出的固定任务模块；此处不能提前 import，
# 否则模块缓存会让 label job 在标记生效前完成装饰。
scheduler = TaskiqScheduler(
    broker=broker,
    sources=[retry_schedule_source, LabelScheduleSource(broker)],
)
