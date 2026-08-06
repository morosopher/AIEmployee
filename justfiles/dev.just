# 开发命令只编排既定工具，不在 FastAPI 请求进程中承载后台长任务。
[group('development')]
doctor:
    @command -v uv
    @command -v pnpm
    @command -v docker
    @command -v python3
    @docker compose version

[group('development')]
bootstrap:
    uv sync --project backend --all-groups
    pnpm --dir frontend install

[group('development')]
dev:
    # 可观测性 profile 保持显式启动，避免默认开发环境增加 Prometheus/Grafana 资源消耗。
    docker compose -f compose.yaml -f compose.dev.yaml up --build

[group('development')]
web:
    pnpm --dir frontend dev

[group('development')]
api:
    uv run --project backend uvicorn ai_employee.main:app --reload --host 0.0.0.0 --port 8000

[group('development')]
worker:
    # 指标 listener 由单个 Taskiq 子进程独占；需要并发时启动多个 Worker 实例。
    uv run --project backend taskiq worker --workers 1 --ack-type when_executed ai_employee.infrastructure.queue.broker:broker ai_employee.workers.execute_task ai_employee.workers.schedules

[group('development')]
scheduler:
    uv run --project backend taskiq scheduler --update-interval 1 ai_employee.infrastructure.queue.scheduler:scheduler ai_employee.workers.schedules
