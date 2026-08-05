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
COPY scripts/init-db-roles.sh ./scripts/init-db-roles.sh
RUN uv sync --project ./backend --frozen --no-dev

WORKDIR /app/backend
USER 65532:65532
