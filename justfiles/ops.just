# 备份与恢复委托给可审计脚本；恢复操作保留交互确认和生产环境保护。
[group('operations')]
backup:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -n "${BACKUP_ARTIFACT_BASENAME-}" ]]; then
      exec python3 scripts/run-calendar-aad-0019.py backup
    fi
    exec docker compose --profile operations run --rm backup

# 0019 入口不接受 scope/账户/期限参数；宿主校验停服与同一不可变镜像，CLI 持固定数据库 lease。
[group('operations')]
calendar-aad-preflight-0019:
    python3 scripts/run-calendar-aad-0019.py preflight

[group('operations')]
calendar-aad-migrate-0019:
    python3 scripts/run-calendar-aad-0019.py migrate

[group('operations')]
calendar-aad-resync-0019:
    python3 scripts/run-calendar-aad-0019.py resync

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

    # basename 只能作为 Compose 环境参数传递，绝不能拼接进 /bin/sh -ec 的固定程序文本。
    restore_basename="$(basename -- "${restore_file}")"

    # 恢复容器同时获得 PostgreSQL 内网和受限 Secret 文件，宿主机无需安装数据库客户端。
    docker compose --profile operations run --rm --no-deps \
      -e APP_ENV="${app_env}" -e ALLOW_PRODUCTION_RESTORE="${ALLOW_PRODUCTION_RESTORE-}" \
      -e "RESTORE_FILE=/backups/${restore_basename}" \
      --entrypoint /bin/sh backup -ec \
      'export PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec bash /app/scripts/restore-postgres.sh "$RESTORE_FILE"'
