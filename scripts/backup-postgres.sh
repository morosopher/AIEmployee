#!/usr/bin/env bash
# 创建加密 PostgreSQL 自定义格式备份，并按日/周窗口保留后复制到异地 remote。
set -euo pipefail

fail() {
  printf 'backup refused: %s\n' "$1" >&2
  exit 1
}

# 普通备份与0019 hook 共用同一 dump/encrypt producer。使用子 shell 的 EXIT trap，
# 使调用者 source 此文件时也能清理自身明文，而不覆盖外层 lease/暂存目录生命周期。
create_encrypted_backup() (
  set -euo pipefail
  umask 077
  local artifact="$1"
  local plain_dump
  : "${BACKUP_PASSPHRASE_FILE:?BACKUP_PASSPHRASE_FILE is required}"
  [[ -r "${BACKUP_PASSPHRASE_FILE}" ]] || fail 'backup passphrase file is unreadable'
  plain_dump="$(mktemp "$(dirname -- "${artifact}")/.ai-employee-backup.XXXXXX.dump")"
  trap 'rm -f -- "${plain_dump}"' EXIT
  # guard CLI 对整个进程组发 TERM；先回收自己的 pg_dump/openssl，再触发 EXIT 删除明文。
  trap 'wait; exit 143' TERM
  # pg_dump 继续从 PGPASSFILE/PG* 读取连接信息，密码不进入 argv 或进程标题。
  pg_dump --format=custom --file="${plain_dump}"
  openssl enc -aes-256-cbc -pbkdf2 -salt \
    -pass "file:${BACKUP_PASSPHRASE_FILE}" \
    -in "${plain_dump}" -out "${artifact}"
)

# 保留既有日/周保留与 remote 行为；版本化 manifest 和整组发布由后续备份任务负责。
finish_existing_backup() {
  local artifact="$1"
  local checksum="${artifact}.sha256"
  local backup_dir
  backup_dir="$(dirname -- "${artifact}")"
  local -a candidates
  local -A kept_weeks=()
  local index entry mtime candidate week remote
  # 先保留最近七份日备；较旧条目按 ISO 周保留四份，其他条目及其校验和一并删除。
  mapfile -t candidates < <(find "${backup_dir}" -maxdepth 1 -type f -name 'ai_employee-*.dump.enc' \
    -printf '%T@ %p\n' | sort -rn)
  for index in "${!candidates[@]}"; do
    entry="${candidates[${index}]}"
    mtime="${entry%% *}"
    candidate="${entry#* }"
    if (( index < 7 )); then
      continue
    fi
    week="$(date -u -d "@${mtime%.*}" +%G-%V)"
    if (( ${#kept_weeks[@]} < 4 )) && [[ -z "${kept_weeks[${week}]+x}" ]]; then
      kept_weeks["${week}"]=1
      continue
    fi
    rm -f -- "${candidate}" "${candidate}.sha256"
  done
  remote="${BACKUP_RCLONE_REMOTE-}"
  if [[ "${APP_ENV-}" == production && -z "${remote}" ]]; then
    fail 'BACKUP_RCLONE_REMOTE is required in production'
  fi
  if [[ -n "${remote}" ]]; then
    command -v rclone >/dev/null || fail 'rclone is required when BACKUP_RCLONE_REMOTE is set'
    rclone copy "${artifact}" "${remote}"
    rclone copy "${checksum}" "${remote}"
  fi
  printf 'encrypted backup created: %s\n' "$(basename "${artifact}")"
}

backup_main() {
  # 显式 rollout basename 必须先取得数据库 lease，再由 guard 检查任何已有产物。
  # 内部 producer 由 Python 固定 source 入口调用；不存在 skip-guard 参数或环境开关。
  if [[ -n "${BACKUP_ARTIFACT_BASENAME-}" ]]; then
    exec uv run --no-sync python -m ai_employee.cli.calendar_aad_backup_0019
  fi
  : "${BACKUP_DIR:?BACKUP_DIR is required}"
  [[ "${BACKUP_DIR}" != / && -n "${BACKUP_DIR}" ]] || fail 'BACKUP_DIR must be an explicit non-root directory'
  local backup_dir timestamp artifact
  mkdir -p -- "${BACKUP_DIR}"
  backup_dir="$(cd -- "${BACKUP_DIR}" && pwd -P)"
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  artifact="${backup_dir}/ai_employee-${timestamp}.dump.enc"
  create_encrypted_backup "${artifact}"
  sha256sum "${artifact}" >"${artifact}.sha256"
  finish_existing_backup "${artifact}"
}

# 仅直接执行走公开入口；被固定内部 producer source 时只定义上述共享函数。
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  backup_main "$@"
fi
