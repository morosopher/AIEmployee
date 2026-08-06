#!/usr/bin/env bash
# 验证生产编排、数据库权限和可观测性入口的最小部署契约，不连接外部账号或生产资源。
set -euo pipefail

[[ "${APP_IMAGE_TAG:-}" != latest ]] || { printf '%s\n' 'APP_IMAGE_TAG must not be latest' >&2; exit 1; }

# 验收仅解析配置，不拉取或运行镜像；提供合成不可变标签和本地域名以通过生产必填变量校验。
export APP_IMAGE_TAG="${APP_IMAGE_TAG:-ci-immutable-test}"
export APP_DOMAIN="${APP_DOMAIN:-localhost}"

# Compose 先展开普通服务，再展开可选可观测性 profile；解析失败表示镜像、secret 或依赖关系无效。
docker compose -f compose.yaml config >/dev/null
docker compose -f compose.yaml --profile observability config >/dev/null

# Shell 脚本必须在执行任何备份、恢复或授权操作前通过语法检查。
bash -n scripts/backup-postgres.sh
bash -n scripts/restore-postgres.sh
bash -n scripts/init-db-roles.sh
bash -n scripts/check-process-health.sh
bash scripts/test-process-health.sh

# 这些断言守住 M1 的保留角色、SSE 流式转发、Redis 持久化和文件 Secret 边界。
grep -q 'ai_employee_retention' scripts/init-db-roles.sh
grep -q 'flush_interval -1' Caddyfile
# Compose 使用数组传递 Redis 参数；检查相邻的 flag/value，而不是依赖 shell 命令字符串格式。
grep -Eq '"--appendonly"[[:space:]]*,[[:space:]]*"yes"' compose.yaml
grep -q '/run/secrets/app_master_key' compose.yaml
# Prometheus listener 由 Worker 子进程持有；单容器只能启动一个 Taskiq 子进程，横向扩容
# 应通过增加容器副本完成，否则多个子进程会争抢同一个内部指标端口并持续崩溃。
grep -Fq 'taskiq worker --workers 1 --ack-type when_executed' compose.yaml
grep -Fq '["CMD", "bash", "/app/scripts/check-process-health.sh", "worker", "9101"]' compose.yaml
grep -Fq '["CMD", "bash", "/app/scripts/check-process-health.sh", "scheduler", "9102"]' compose.yaml
grep -Fq 'COPY scripts/check-process-health.sh ./scripts/check-process-health.sh' backend/Dockerfile
just --list | grep -q 'observability-up'
just --list | grep -q 'observability-down'
