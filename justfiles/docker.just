# Docker 命令只管理 M1 所需基础设施与运行诊断，不引入额外编排层。
[group('docker')]
infra-up:
    docker compose up -d postgres redis

[group('docker')]
infra-down:
    docker compose stop postgres redis

[group('docker')]
logs service='':
    #!/usr/bin/env bash
    set -euo pipefail

    service="${1-}"
    if [[ -z "${service}" ]]; then
      docker compose logs -f
    else
      # -- 将用户提供的 service 与 Compose 选项隔离，双引号确保它始终是单一参数。
      docker compose logs -f -- "${service}"
    fi

[group('docker')]
ps:
    docker compose ps

[group('docker')]
health:
    #!/usr/bin/env bash
    set -euo pipefail

    for service in api worker scheduler postgres redis; do
      container_id="$(docker compose ps -q "${service}")"
      [[ -n "${container_id}" ]] || { printf 'health failed: %s is not running\n' "${service}" >&2; exit 1; }
      # Docker 的模板语法会与 Just 配方插值冲突，故读取 JSON 后匹配固定健康字面量。
      docker inspect "${container_id}" | grep -q '"Status": "healthy"' \
        || { printf 'health failed: %s is not healthy\n' "${service}" >&2; exit 1; }
    done
    curl --fail --silent http://localhost:8000/api/v1/system/readiness >/dev/null

[group('docker')]
observability-up:
    docker compose --profile observability up -d prometheus grafana

[group('docker')]
observability-down:
    docker compose --profile observability stop prometheus grafana
