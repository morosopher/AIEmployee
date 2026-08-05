# 备份与恢复委托给可审计脚本；恢复操作保留交互确认和生产环境保护。
[group('operations')]
backup:
    docker compose --profile operations run --rm backup

[confirm]
[group('operations')]
restore file:
    #!/usr/bin/env bash
    set -euo pipefail

    fail() {
      printf 'restore refused: %s\n' "$1" >&2
      exit 1
    }

    app_env="${APP_ENV-}"
    case "${app_env}" in
      development|test) ;;
      production) [[ "${ALLOW_PRODUCTION_RESTORE-}" == yes ]] || fail 'ALLOW_PRODUCTION_RESTORE=yes is required in production' ;;
      *) fail 'APP_ENV must be explicitly set to development, test, or production' ;;
    esac

    restore_file="${1-}"
    [[ -n "${restore_file}" ]] || fail 'restore file must not be empty'

    printf 'APP_ENV=%s\nCompose service: postgres\nRestore target: %s\n' \
      "${app_env}" "${restore_file}" >&2
    printf 'Type the exact restore file path to confirm: ' >&2
    confirmation=''
    IFS= read -r confirmation || fail 'confirmation input is required'
    [[ "${confirmation}" == "${restore_file}" ]] || fail 'confirmation did not match restore target'

    # 恢复容器同时获得 PostgreSQL 内网和受限 Secret 文件，宿主机无需安装数据库客户端。
    docker compose --profile operations run --rm --no-deps \
      -e APP_ENV="${app_env}" -e ALLOW_PRODUCTION_RESTORE="${ALLOW_PRODUCTION_RESTORE-}" \
      --entrypoint /bin/sh backup -ec 'export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec bash /app/scripts/restore-postgres.sh "/backups/'"$(basename -- "${restore_file}")"'"'
