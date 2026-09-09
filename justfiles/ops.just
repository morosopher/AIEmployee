# 备份与恢复委托给可审计脚本；恢复操作保留交互确认和生产环境保护。
[group('operations')]
backup:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -v BACKUP_ARTIFACT_BASENAME ]]; then
      exec python3 scripts/run-calendar-aad-0019.py backup
    fi
    exec uv run --project backend python scripts/run-postgres-operations.py backup

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

# 宿主先验证三件套/本地image/停服；production再要求精确文件路径二次确认。
[confirm]
[group('operations')]
restore file approve_call_ordinal='' approve_reopen_ordinal='':
    #!/usr/bin/env bash
    set -euo pipefail
    case "${APP_ENV-}" in
      development|test) ;;
      production) [[ "${ALLOW_PRODUCTION_RESTORE-}" == yes ]] || { printf '%s\n' 'restore refused: production confirmation required' >&2; exit 1; } ;;
      *) printf '%s\n' 'restore refused: explicit environment required' >&2; exit 1 ;;
    esac
    [[ -n "$1" ]] || { printf '%s\n' 'restore refused: empty file' >&2; exit 1; }
    # 保留原有全环境精确路径确认；production的第二次确认由纯验证后的host入口执行。
    printf 'APP_ENV=%s\nCompose service: postgres\nRestore target: %s\n' "$APP_ENV" "$1" >&2
    printf 'Type the exact restore file path to confirm: ' >&2
    IFS= read -r confirmation || exit 1
    [[ "$confirmation" == "$1" ]] || { printf '%s\n' 'restore refused: confirmation mismatch' >&2; exit 1; }
    arguments=(restore "$1")
    [[ -z "${2-}" ]] || arguments+=(--approve-call-ordinal "$2")
    [[ -z "${3-}" ]] || arguments+=(--approve-reopen-ordinal "$3")
    exec uv run --project backend python scripts/run-postgres-operations.py "${arguments[@]}"

# sealed入口额外绑定原preflight与pre-migration；复用controller，不复用普通validator。
[confirm]
[group('operations')]
calendar-aad-restore-0018 file approve_call_ordinal='' approve_reopen_ordinal='':
    #!/usr/bin/env bash
    set -euo pipefail
    case "${APP_ENV-}" in
      development|test) ;;
      production) [[ "${ALLOW_PRODUCTION_RESTORE-}" == yes ]] || { printf '%s\n' 'restore refused: production confirmation required' >&2; exit 1; } ;;
      *) printf '%s\n' 'restore refused: explicit environment required' >&2; exit 1 ;;
    esac
    [[ -n "$1" ]] || { printf '%s\n' 'restore refused: empty file' >&2; exit 1; }
    printf 'APP_ENV=%s\nCompose service: postgres\nRestore target: %s\n' "$APP_ENV" "$1" >&2
    printf 'Type the exact restore file path to confirm: ' >&2
    IFS= read -r confirmation || exit 1
    [[ "$confirmation" == "$1" ]] || { printf '%s\n' 'restore refused: confirmation mismatch' >&2; exit 1; }
    arguments=(sealed "$1")
    [[ -z "${2-}" ]] || arguments+=(--approve-call-ordinal "$2")
    [[ -z "${3-}" ]] || arguments+=(--approve-reopen-ordinal "$3")
    exec uv run --project backend python scripts/run-postgres-operations.py "${arguments[@]}"

[group('operations')]
calendar-aad-audit phase artifact:
    uv run --project backend python scripts/run-postgres-operations.py audit "$1" "$2"

[group('operations')]
restore-legacy-to-isolated file output_basename:
    bash scripts/convert-legacy-backup.sh "$1" "$2"

[group('operations')]
legacy-backup-scavenge:
    bash scripts/scavenge-legacy-backups.sh
