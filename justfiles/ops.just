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
      *) fail 'APP_ENV must be explicitly set to development or test' ;;
    esac

    restore_file="${1-}"
    [[ -n "${restore_file}" ]] || fail 'restore file must not be empty'

    printf 'APP_ENV=%s\nCompose service: postgres\nRestore target: %s\n' \
      "${app_env}" "${restore_file}" >&2
    printf 'Type the exact restore file path to confirm: ' >&2
    confirmation=''
    IFS= read -r confirmation || fail 'confirmation input is required'
    [[ "${confirmation}" == "${restore_file}" ]] || fail 'confirmation did not match restore target'

    # Task 19 才提供真实恢复脚本；此处只冻结安全调用协议，不提前实现恢复逻辑。
    scripts/restore-postgres.sh "${restore_file}"
