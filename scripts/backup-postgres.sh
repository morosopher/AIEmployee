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
  local snapshot="${2-}"
  local plain_dump
  : "${BACKUP_PASSPHRASE_FILE:?BACKUP_PASSPHRASE_FILE is required}"
  [[ -r "${BACKUP_PASSPHRASE_FILE}" ]] || fail 'backup passphrase file is unreadable'
  plain_dump="$(mktemp "$(dirname -- "${artifact}")/.ai-employee-backup.XXXXXX.dump")"
  trap 'rm -f -- "${plain_dump}"' EXIT
  # guard CLI 对整个进程组发 TERM；先回收自己的 pg_dump/openssl，再触发 EXIT 删除明文。
  trap 'wait; exit 143' TERM
  # pg_dump 继续从 PGPASSFILE/PG* 读取连接信息，密码不进入 argv 或进程标题。
  local -a snapshot_arguments=()
  if [[ -n "${snapshot}" ]]; then
    [[ "${snapshot}" =~ ^[0-9A-F]{8}-[0-9A-F]{8}-[0-9]+$ ]] || fail 'invalid backup snapshot'
    snapshot_arguments=("--snapshot=${snapshot}")
  fi
  pg_dump --format=custom --file="${plain_dump}" "${snapshot_arguments[@]}"
  openssl enc -aes-256-cbc -pbkdf2 -salt \
    -pass "file:${BACKUP_PASSPHRASE_FILE}" \
    -in "${plain_dump}" -out "${artifact}"
)

# 完整manifest/锁/remote/保留由同一Python父流程负责，不能从shell绕过admission。
backup_main() {
  [[ "$#" -eq 0 ]] || fail 'backup_arguments_invalid'
  exec /app/backend/.venv/bin/python -m ai_employee.cli.postgres_backup
}

# 仅直接执行走公开入口；被固定内部 producer source 时只定义上述共享函数。
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  backup_main "$@"
fi
