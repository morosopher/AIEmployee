# 生产镜像只安装锁定的运行依赖；迁移、API、Worker 与 Scheduler 共用同一不可变构建产物。
FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /uvx /bin/
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl postgresql-client rclone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/backend/.venv/bin:$PATH"

COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN uv sync --project ./backend --frozen --no-dev --no-install-project
COPY backend ./backend
# 运维可执行来自同一repository-root构建；运行时只能挂载数据和Secret。
COPY scripts/backup-postgres.sh scripts/restore-postgres.sh scripts/restore-calendar-aad-0018.sh \
    scripts/init-db-roles.sh scripts/convert-legacy-backup.sh scripts/scavenge-legacy-backups.sh \
    scripts/audit-calendar-aad-0019.sh scripts/run-postgres-operations.py \
    scripts/legacy-backup-operations.py ./scripts/
COPY scripts/check-process-health.sh ./scripts/check-process-health.sh
RUN uv sync --project ./backend --frozen --no-dev

# 固定one-off仍用uv选择锁定venv；非root运行时仅用/tmp临时缓存，禁止写入只读源码目录。
# 构建完成后再关闭持久缓存，保留上方依赖安装的构建缓存行为；不提供外部配置开关。
ENV UV_NO_CACHE=1
WORKDIR /app/backend
USER 65532:65532
