# 备份与恢复委托给可审计脚本；恢复操作保留交互确认和生产环境保护。
[group('operations')]
backup:
    scripts/backup-postgres.sh

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

    # 恢复脚本会校验加密完整性并在生产环境再次检查 ALLOW_PRODUCTION_RESTORE。
    scripts/restore-postgres.sh "${restore_file}"
