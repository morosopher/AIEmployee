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
    curl --fail --silent http://localhost:8000/api/v1/system/health
