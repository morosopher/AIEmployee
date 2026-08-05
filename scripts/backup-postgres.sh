#!/usr/bin/env bash
# 创建加密 PostgreSQL 自定义格式备份，并按日/周窗口保留后复制到异地 remote。
set -euo pipefail

fail() {
  printf 'backup refused: %s\n' "$1" >&2
  exit 1
}

: "${BACKUP_DIR:?BACKUP_DIR is required}"
: "${BACKUP_PASSPHRASE_FILE:?BACKUP_PASSPHRASE_FILE is required}"
[[ -r "${BACKUP_PASSPHRASE_FILE}" ]] || fail 'backup passphrase file is unreadable'
[[ "${BACKUP_DIR}" != / && -n "${BACKUP_DIR}" ]] || fail 'BACKUP_DIR must be an explicit non-root directory'

# 路径由运维显式提供；创建后解析物理路径避免清理逻辑跨越符号链接或误删宽泛目录。
mkdir -p -- "${BACKUP_DIR}"
backup_dir="$(cd -- "${BACKUP_DIR}" && pwd -P)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
artifact="${backup_dir}/ai_employee-${timestamp}.dump.enc"
checksum="${artifact}.sha256"
plain_dump="$(mktemp "${backup_dir}/.ai-employee-backup.XXXXXX.dump")"

cleanup() {
  rm -f -- "${plain_dump}"
}
trap cleanup EXIT

# pg_dump 从 PGPASSFILE/PG* 读取连接信息，避免 DSN 或密码落入 argv、日志和进程标题。
pg_dump --format=custom --file="${plain_dump}"
openssl enc -aes-256-cbc -pbkdf2 -salt \
  -pass "file:${BACKUP_PASSPHRASE_FILE}" \
  -in "${plain_dump}" -out "${artifact}"
sha256sum "${artifact}" >"${checksum}"

# 先保留最近七份日备；较旧条目按 ISO 周保留四份，其他条目及其校验和一并删除。
mapfile -t candidates < <(find "${backup_dir}" -maxdepth 1 -type f -name 'ai_employee-*.dump.enc' \
  -printf '%T@ %p\n' | sort -rn)
declare -A kept_weeks=()
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
