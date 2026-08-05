#!/usr/bin/env bash
# 从单个经校验的加密备份恢复 PostgreSQL；生产恢复必须显式二次授权。
set -euo pipefail

fail() {
  printf 'restore refused: %s\n' "$1" >&2
  exit 1
}

[[ "$#" -eq 1 ]] || fail 'exactly one encrypted backup file is required'
artifact="$1"
[[ -f "${artifact}" ]] || fail 'backup file does not exist'
[[ "${artifact}" == *.dump.enc ]] || fail 'backup file must end in .dump.enc'
: "${BACKUP_PASSPHRASE_FILE:?BACKUP_PASSPHRASE_FILE is required}"
[[ -r "${BACKUP_PASSPHRASE_FILE}" ]] || fail 'backup passphrase file is unreadable'
if [[ "${APP_ENV-}" == production && "${ALLOW_PRODUCTION_RESTORE-}" != yes ]]; then
  fail 'ALLOW_PRODUCTION_RESTORE=yes is required in production'
fi

checksum="${artifact}.sha256"
[[ -f "${checksum}" ]] || fail 'backup checksum file does not exist'
artifact_dir="$(cd -- "$(dirname -- "${artifact}")" && pwd -P)"
artifact_name="$(basename -- "${artifact}")"
(cd "${artifact_dir}" && sha256sum -c "${artifact_name}.sha256") >/dev/null

temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/ai-employee-restore.XXXXXX")"
cleanup() {
  rm -rf -- "${temporary_dir}"
}
trap cleanup EXIT
plain_dump="${temporary_dir}/restore.dump"

openssl enc -d -aes-256-cbc -pbkdf2 -salt \
  -pass "file:${BACKUP_PASSPHRASE_FILE}" \
  -in "${artifact}" -out "${plain_dump}"
# pg_restore 的连接信息仅来自 PGPASSFILE/PG*；禁止把凭据或 DSN 拼入命令参数。
pg_restore --clean --if-exists --no-owner --no-privileges "${plain_dump}"
printf 'restore completed from: %s\n' "${artifact_name}"
