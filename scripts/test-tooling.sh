#!/usr/bin/env bash
set -euo pipefail

# 四个真实 API 启动命令均必须关闭原始 request-target 日志；注释中的 flag 不算证据。
for api_entry in compose.yaml compose.dev.yaml justfiles/dev.just scripts/run-e2e-backend.sh; do
  if ! grep -Eq -- '^[[:space:]]*(command:|uv run).*uvicorn.*--no-access-log' "${api_entry}"; then
    printf 'OAuth access-log boundary missing: %s\n' "${api_entry}" >&2
    exit 1
  fi
done

# 独立指标 listener 只允许每个 Worker 容器/进程组拥有一个 Taskiq 子进程；开发入口必须
# 与生产及 E2E 保持同一约束，横向并发由多个容器提供。
grep -Fq 'taskiq worker --workers 1 --ack-type when_executed' justfiles/dev.just

# 该列表是仓库统一命令入口的最小契约，任何 recipe 缺失都应让脚手架检查立即失败。
required_recipes=(
  doctor bootstrap dev infra-up infra-down web api worker scheduler
  test test-backend test-frontend test-integration test-e2e e2e-backend
  lint format typecheck check ci db-upgrade db-revision db-reset
  create-admin logs ps health backup restore observability-up observability-down
  calendar-aad-preflight-0019 calendar-aad-migrate-0019 calendar-aad-resync-0019
)

# 只读取 just 的摘要，避免测试依赖面向人的分组标题、颜色或详细帮助格式。
summary="$(just --summary)"
for recipe in "${required_recipes[@]}"; do
  # 使用空格或行边界匹配，防止短 recipe 被较长名称的子串误判为存在。
  grep -Eq "(^| )${recipe}( |$)" <<<"${summary}"
done

# 标准 integration 入口必须交给 Python orchestrator 建立两个独立 pytest session；禁止
# 回退为会混合 cluster-wide roles-present/absent 状态的单一 pytest 命令。
integration_definition="$(just --show test-integration)"
grep -Fq 'scripts/run_integration_tests.py' <<<"${integration_definition}"
! grep -Fq 'pytest backend/tests/integration backend/tests/contract backend/tests/evals' \
  <<<"${integration_definition}"

# 所有普通数据库生命周期入口必须路由 typed CLI；recipe 不得保留 shell Alembic、
# direct drop/create 或任何 obsolete-0019 repair/stamp/downgrade/revision rewrite。
db_upgrade_definition="$(just --show db-upgrade)"
db_reset_definition="$(just --show db-reset)"
grep -Fq 'ai_employee.cli.database_maintenance migrate' <<<"${db_upgrade_definition}"
grep -Fq 'ai_employee.cli.database_maintenance db-reset' <<<"${db_reset_definition}"
for forbidden_lifecycle_text in \
  'alembic -c backend/alembic.ini upgrade head' \
  'dropdb ' \
  'createdb ' \
  ' repair' \
  ' stamp' \
  ' downgrade' \
  'revision rewrite'; do
  ! grep -Fq -- "${forbidden_lifecycle_text}" <<<"${db_upgrade_definition}"
  ! grep -Fq -- "${forbidden_lifecycle_text}" <<<"${db_reset_definition}"
done
! grep -Eiq 'GRANT[[:space:]]+ALL|ALL[[:space:]]+(TABLES|SEQUENCES)|ALTER[[:space:]]+DEFAULT[[:space:]]+PRIVILEGES' \
  scripts/init-db-roles.sh
! grep -Fq 'init-db-roles.sh' <<<"$(
  awk '/^  migration:/{capture=1} capture && /^  api:/{exit} capture{print}' compose.yaml
)"
! grep -Eiq 'obsolete[-_ ]?0019|repair[-_ ]?0019' justfiles/db.just scripts/run-e2e-backend.sh

# 后续行为测试全部在临时目录运行；Fake 命令只记录参数，绝不连接真实 Docker、数据库或恢复脚本。
sandbox_dir="$(mktemp -d "${TMPDIR:-/tmp}/ai-employee-tooling.XXXXXX")"
cleanup() {
  rm -rf -- "${sandbox_dir}"
}
trap cleanup EXIT

cp justfile "${sandbox_dir}/justfile"
cp -R justfiles "${sandbox_dir}/justfiles"
mkdir -p "${sandbox_dir}/fake-bin" "${sandbox_dir}/scripts"
mkdir -p "${sandbox_dir}/just-temp"
cp scripts/run-calendar-aad-0019.py "${sandbox_dir}/scripts/"

command_log="${sandbox_dir}/fake-commands.log"
uv_environment_log="${sandbox_dir}/fake-uv-environment.log"
typed_database_name_log="${sandbox_dir}/fake-typed-database-name.log"
output_file="${sandbox_dir}/command-output.log"
stdout_file="${sandbox_dir}/command-stdout.log"
stderr_file="${sandbox_dir}/command-stderr.log"
export TOOLING_TEST_LOG="${command_log}"
export TOOLING_TEST_UV_ENV_LOG="${uv_environment_log}"
export TOOLING_TEST_TYPED_DATABASE_NAME_LOG="${typed_database_name_log}"
export TOOLING_TEST_COMPOSE_CONFIG="${sandbox_dir}/rollout-config.json"
export TOOLING_TEST_COMPOSE_PS="${sandbox_dir}/rollout-ps.json"
export TOOLING_TEST_RENDERED="${sandbox_dir}/rollout-rendered.json"
export TOOLING_TEST_SCREEN_RENDERED="${sandbox_dir}/rollout-screen-rendered.json"
export TOOLING_TEST_SCREEN_SEEN="${sandbox_dir}/rollout-screen-seen"
export TOOLING_TEST_IMAGE_ID="sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

# Fake 命令把每个参数作为独立的制表符字段记录，能同时验证引用边界和参数原样性。
cat >"${sandbox_dir}/fake-bin/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
set -euo pipefail
{
  printf 'docker'
  for argument in "$@"; do
    printf '\t%s' "$argument"
  done
  printf '\n'
} >>"${TOOLING_TEST_LOG}"
if [[ "$*" == 'compose config --format json' ]]; then
  cat "${TOOLING_TEST_COMPOSE_CONFIG}"
elif [[ "$*" == 'compose ps --all --format json' ]]; then
  if [[ -f "${TOOLING_TEST_SCREEN_SEEN}" && "${TOOLING_TEST_SERVICE_AFTER_SCREEN-}" == yes ]]; then
    printf '[{"Service":"api","State":"running"}]\n'
  else
    cat "${TOOLING_TEST_COMPOSE_PS}"
  fi
elif [[ "$*" == image\ inspect* ]]; then
  [[ "${TOOLING_TEST_INSPECT_FAIL-}" != yes ]] || exit 1
  if [[ "${TOOLING_TEST_IMAGE_MISMATCH-}" == yes && "${@: -1}" == synthetic-migration:* ]]; then
    printf 'sha256:%064d\n' 0
  elif [[ -f "${TOOLING_TEST_SCREEN_SEEN}" && "${TOOLING_TEST_IMAGE_AFTER_SCREEN-}" == yes ]]; then
    printf 'sha256:%064d\n' 0
  else
    printf '%s\n' "${TOOLING_TEST_IMAGE_ID}"
  fi
elif [[ "${2-}" == --project-directory && "$*" == *'--profile operations run'* ]]; then
  # 捕获真实宿主渲染文件；这里不执行内部 shell 或访问任何 Secret/数据库。
  [[ "$(stat -c %a "$5")" == 600 ]] || exit 1
  if [[ "${@: -1}" == *ai_employee.cli.calendar_aad_revision_0019 ]]; then
    cp "$5" "${TOOLING_TEST_SCREEN_RENDERED}"
    : >"${TOOLING_TEST_SCREEN_SEEN}"
    if [[ "${TOOLING_TEST_SCREEN_FAIL-}" == yes ]]; then
      printf 'calendar_aad_revision_screen_failed\n' >&2
      exit 1
    fi
    printf '%s\n' "${TOOLING_TEST_SCREEN_REVISION}"
    exit 0
  fi
  cp "$5" "${TOOLING_TEST_RENDERED}"
  if [[ "${TOOLING_TEST_BOUND_IMAGE-}" != '' && "${TOOLING_TEST_BOUND_IMAGE}" != "${TOOLING_TEST_IMAGE_ID}" ]]; then
    printf 'calendar_aad_artifact_invalid\n' >&2
    exit 1
  fi
  printf 'calendar_aad_oneoff_passed\n'
fi
FAKE_DOCKER

cat >"${sandbox_dir}/fake-bin/uv" <<'FAKE_UV'
#!/usr/bin/env bash
set -euo pipefail

# typed lifecycle 只能从 passwordless DATABASE_URL 解析目标；这里只记录 libpq override
# 是否存在，并记录已经确认不含密码的 lifecycle URL。
if [[ "$*" == *'ai_employee.cli.database_maintenance migrate'* \
   || "$*" == *'ai_employee.cli.database_maintenance db-reset'* ]]; then
  printf 'uv-upgrade-environment' >>"${TOOLING_TEST_UV_ENV_LOG}"
  for variable_name in PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGSERVICE PGSERVICEFILE; do
    if [[ -v "${variable_name}" ]]; then
      printf '\t%s=set' "${variable_name}" >>"${TOOLING_TEST_UV_ENV_LOG}"
    else
      printf '\t%s=unset' "${variable_name}" >>"${TOOLING_TEST_UV_ENV_LOG}"
    fi
  done
  printf '\tDATABASE_URL=%s' "${DATABASE_URL-}" >>"${TOOLING_TEST_UV_ENV_LOG}"
  printf '\n' >>"${TOOLING_TEST_UV_ENV_LOG}"
  AI_EMPLOYEE_TOOLING_DATABASE_URL="${DATABASE_URL-}" python3 - <<'PY'
import os
from urllib.parse import unquote, urlsplit

value = os.environ["AI_EMPLOYEE_TOOLING_DATABASE_URL"]
database_name = unquote(
    urlsplit(value).path.removeprefix("/"),
    encoding="utf-8",
    errors="strict",
)
with open(os.environ["TOOLING_TEST_TYPED_DATABASE_NAME_LOG"], "a", encoding="utf-8") as log:
    log.write(database_name + "\n")
PY
fi

{
  printf 'uv'
  for argument in "$@"; do
    printf '\t%s' "$argument"
  done
  printf '\n'
} >>"${TOOLING_TEST_LOG}"
FAKE_UV

cat >"${sandbox_dir}/scripts/restore-postgres.sh" <<'FAKE_RESTORE'
#!/usr/bin/env bash
set -euo pipefail
{
  printf 'restore'
  for argument in "$@"; do
    printf '\t%s' "$argument"
  done
  printf '\n'
} >>"${TOOLING_TEST_LOG}"
FAKE_RESTORE

chmod +x "${sandbox_dir}/fake-bin/docker" \
  "${sandbox_dir}/fake-bin/uv" \
  "${sandbox_dir}/scripts/restore-postgres.sh"

run_just_capture() {
  local destination="$1"
  shift
  (
    cd "${sandbox_dir}"
    JUST_TEMPDIR="${sandbox_dir}/just-temp" PATH="${sandbox_dir}/fake-bin:${PATH}" "$@"
  ) >"${destination}" 2>&1
}

run_just_with_input_capture() {
  local input="$1"
  local destination="$2"
  shift 2
  printf '%s\n' "${input}" | (
    cd "${sandbox_dir}"
    JUST_TEMPDIR="${sandbox_dir}/just-temp" PATH="${sandbox_dir}/fake-bin:${PATH}" "$@"
  ) >"${destination}" 2>&1
}

run_just_with_input_split() {
  local input="$1"
  local stdout_destination="$2"
  local stderr_destination="$3"
  shift 3
  printf '%s\n' "${input}" | (
    cd "${sandbox_dir}"
    JUST_TEMPDIR="${sandbox_dir}/just-temp" PATH="${sandbox_dir}/fake-bin:${PATH}" "$@"
  ) >"${stdout_destination}" 2>"${stderr_destination}"
}

clear_command_log() {
  : >"${command_log}"
}

assert_contains() {
  local expected="$1"
  local file="$2"
  if ! grep -F -- "${expected}" "${file}" >/dev/null; then
    printf 'tooling behavior contract failed: missing %q in %s\n' "${expected}" "${file}" >&2
    sed -n '1,160p' "${file}" >&2 || true
    exit 1
  fi
}

assert_not_contains() {
  local unexpected="$1"
  local file="$2"
  if grep -F -- "${unexpected}" "${file}" >/dev/null; then
    printf 'tooling behavior contract failed: unexpected %q in %s\n' "${unexpected}" "${file}" >&2
    sed -n '1,160p' "${file}" >&2 || true
    exit 1
  fi
}

assert_exact_line() {
  local expected="$1"
  local file="$2"
  if ! grep -Fx -- "${expected}" "${file}" >/dev/null; then
    printf 'tooling behavior contract failed: missing exact line %q in %s\n' "${expected}" "${file}" >&2
    sed -n '1,160p' "${file}" >&2 || true
    exit 1
  fi
}

assert_line_count() {
  local expected_count="$1"
  local file="$2"
  local actual_count
  actual_count="$(wc -l <"${file}")"
  if [[ "${actual_count}" -ne "${expected_count}" ]]; then
    printf 'tooling behavior contract failed: expected %s lines in %s, got %s\n' \
      "${expected_count}" "${file}" "${actual_count}" >&2
    sed -n '1,160p' "${file}" >&2 || true
    exit 1
  fi
}

assert_no_fake_calls() {
  if [[ -s "${command_log}" ]]; then
    printf 'tooling behavior contract failed: fake external command was called\n' >&2
    sed -n '1,160p' "${command_log}" >&2
    exit 1
  fi
}

set_valid_database_environment() {
  export APP_ENV=development
  export POSTGRES_DB=ai_employee_test
  export POSTGRES_USER=ai_employee
  export DATABASE_URL='postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test'
  export POSTGRES_BOOTSTRAP_PASSWORD_FILE="${sandbox_dir}/owner-password"
  export APP_DATABASE_PASSWORD_FILE="${sandbox_dir}/app-password"
  export RETENTION_DATABASE_PASSWORD_FILE="${sandbox_dir}/retention-password"
}

expect_db_upgrade_failure() {
  local label="$1"
  local rejected_url="${DATABASE_URL-}"
  local unexpectedly_allowed=false
  clear_command_log
  : >"${uv_environment_log}"
  : >"${typed_database_name_log}"
  if run_just_capture "${output_file}" just --yes db-upgrade; then
    unexpectedly_allowed=true
    printf 'tooling behavior contract failed: db-upgrade unexpectedly allowed %s\n' \
      "${label}" >&2
  fi
  if [[ -n "${rejected_url}" && "$(<"${output_file}")" == *"${rejected_url}"* ]]; then
    printf 'tooling behavior contract failed: db-upgrade output leaked rejected DATABASE_URL\n' >&2
    exit 1
  fi
  assert_no_fake_calls
  [[ ! -s "${uv_environment_log}" ]]
  [[ ! -s "${typed_database_name_log}" ]]
  [[ "${unexpectedly_allowed}" == false ]] || exit 1
}

expect_db_reset_failure() {
  local label="$1"
  local expected_message="${2-}"
  local rejected_url="${DATABASE_URL-}"
  local unexpectedly_allowed=false
  clear_command_log
  if run_just_with_input_capture "${POSTGRES_DB-}" "${output_file}" just --yes db-reset; then
    unexpectedly_allowed=true
    printf 'tooling behavior contract failed: db-reset unexpectedly allowed %s\n' "${label}" >&2
  fi
  # 即使 recipe 意外成功，也先验证错误输出没有回显完整 DSN，再核对破坏性 Fake 调用为零。
  if [[ -n "${rejected_url}" && "$(<"${output_file}")" == *"${rejected_url}"* ]]; then
    printf 'tooling behavior contract failed: db-reset output leaked rejected DATABASE_URL\n' >&2
    exit 1
  fi
  assert_no_fake_calls
  [[ "${unexpectedly_allowed}" == false ]] || exit 1
  if [[ -n "${expected_message}" ]]; then
    assert_contains "${expected_message}" "${output_file}"
  fi
}

expect_restore_failure() {
  local label="$1"
  local restore_target="$2"
  local confirmation="${3-${restore_target}}"
  clear_command_log
  if run_just_with_input_capture "${confirmation}" "${output_file}" just --yes restore "${restore_target}"; then
    printf 'tooling behavior contract failed: restore unexpectedly allowed %s\n' "${label}" >&2
    exit 1
  fi
  assert_no_fake_calls
}

# 恶意 revision 参数必须作为单一原样参数传给 uv，不能被旧模板插值展开成第二条命令。
clear_command_log
revision_sentinel="${sandbox_dir}/revision-injection-sentinel"
revision_payload="revision \"; : > \"${revision_sentinel}\"; #"
if ! run_just_capture "${output_file}" just --yes db-revision "${revision_payload}"; then
  printf 'tooling behavior contract failed: safe db-revision invocation failed\n' >&2
  exit 1
fi
if [[ -e "${revision_sentinel}" ]]; then
  printf 'tooling behavior contract failed: db-revision payload escaped into shell\n' >&2
  exit 1
fi
assert_contains "${revision_payload}" "${command_log}"
assert_exact_line $'uv\trun\t--project\tbackend\talembic\t-c\tbackend/alembic.ini\trevision\t--autogenerate\t-m\t'"${revision_payload}" "${command_log}"
assert_line_count 1 "${command_log}"

# create-admin 的邮箱与密码文件路径必须各自作为单一参数传给 Python 模块，不能发生 shell 逃逸。
clear_command_log
admin_sentinel="${sandbox_dir}/admin-injection-sentinel"
admin_email="owner+\"; : > \"${admin_sentinel}\"; #@example.com"
admin_password_file="${sandbox_dir}/password file; quoted.txt"
if ! run_just_capture "${output_file}" just --yes create-admin "${admin_email}" "${admin_password_file}"; then
  printf 'tooling behavior contract failed: safe create-admin invocation failed\n' >&2
  exit 1
fi
if [[ -e "${admin_sentinel}" ]]; then
  printf 'tooling behavior contract failed: create-admin payload escaped into shell\n' >&2
  exit 1
fi
assert_exact_line $'uv\trun\t--project\tbackend\tpython\t-m\tai_employee.cli.create_admin\t--email\t'"${admin_email}"$'\t--password-file\t'"${admin_password_file}" "${command_log}"
assert_line_count 1 "${command_log}"

# just 只保留一个稳定入口，并把进程边界、suite lock 与 cleanup 交给 typed orchestrator。
clear_command_log
if ! run_just_capture "${output_file}" just --yes test-integration; then
  printf 'tooling behavior contract failed: test-integration orchestrator entry failed\n' >&2
  exit 1
fi
assert_exact_line \
  $'uv\trun\t--project\tbackend\tpython\tscripts/run_integration_tests.py' \
  "${command_log}"
assert_line_count 1 "${command_log}"

# 普通 db-upgrade 必须从现有 URL 只保留 endpoint，清除 libpq override，并调用 typed migrate。
set_valid_database_environment
export PGHOST=production.example PGHOSTADDR=203.0.113.10 PGPORT=6543
export PGDATABASE=production_database PGUSER=production_user
export PGSERVICE=production_service PGSERVICEFILE=/synthetic/production-service.conf
clear_command_log
: >"${uv_environment_log}"
if ! run_just_capture "${output_file}" just --yes db-upgrade; then
  printf 'tooling behavior contract failed: typed db-upgrade failed\n' >&2
  exit 1
fi
assert_exact_line $'uv\trun\t--project\tbackend\tpython\t-m\tai_employee.cli.database_maintenance\tmigrate\t--owner-password-file\t'"${POSTGRES_BOOTSTRAP_PASSWORD_FILE}" "${command_log}"
assert_line_count 1 "${command_log}"
assert_exact_line $'uv-upgrade-environment\tPGHOST=unset\tPGHOSTADDR=unset\tPGPORT=unset\tPGDATABASE=unset\tPGUSER=unset\tPGSERVICE=unset\tPGSERVICEFILE=unset\tDATABASE_URL=postgresql+psycopg://ai_employee@localhost:5432/ai_employee_test' "${uv_environment_log}"
assert_line_count 1 "${uv_environment_log}"

# 普通 migrate 的 wrapper 必须按 URL 语义取得 exact decoded PostgreSQL 数据库名，再把
# passwordless lifecycle URL 规范编码；quoted UTF-8、组合字符和编码后的分隔符都不能
# 被 owner role 的 ASCII identifier 规则误拒绝，也不能改变数据库 identity。
valid_db_upgrade_cases=(
  $'\u62a5\u4ef7\u5e93_test|\u62a5\u4ef7\u5e93_test|%E6%8A%A5%E4%BB%B7%E5%BA%93_test'
  $'\u62a5\u4ef7%22\u5e93_test|\u62a5\u4ef7"\u5e93_test|%E6%8A%A5%E4%BB%B7%22%E5%BA%93_test'
  $'Cafe%CC%81%2Fquote%3Fprice%23_test|Cafe\u0301/quote?price#_test|Cafe%CC%81%2Fquote%3Fprice%23_test'
  'literal%25percent_test|literal%percent_test|literal%25percent_test'
  'literal%252F_test|literal%2F_test|literal%252F_test'
)
for valid_db_upgrade_case in "${valid_db_upgrade_cases[@]}"; do
  source_database_path="${valid_db_upgrade_case%%|*}"
  remaining_case="${valid_db_upgrade_case#*|}"
  exact_database_name="${remaining_case%%|*}"
  canonical_database_path="${remaining_case#*|}"
  set_valid_database_environment
  export DATABASE_URL="postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/${source_database_path}"
  clear_command_log
  : >"${uv_environment_log}"
  : >"${typed_database_name_log}"
  if ! run_just_capture "${output_file}" just --yes db-upgrade; then
    printf 'tooling behavior contract failed: db-upgrade rejected exact UTF-8 database %q\n' \
      "${exact_database_name}" >&2
    sed -n '1,80p' "${output_file}" >&2 || true
    exit 1
  fi
  assert_exact_line $'uv\trun\t--project\tbackend\tpython\t-m\tai_employee.cli.database_maintenance\tmigrate\t--owner-password-file\t'"${POSTGRES_BOOTSTRAP_PASSWORD_FILE}" "${command_log}"
  assert_line_count 1 "${command_log}"
  assert_exact_line $'uv-upgrade-environment\tPGHOST=unset\tPGHOSTADDR=unset\tPGPORT=unset\tPGDATABASE=unset\tPGUSER=unset\tPGSERVICE=unset\tPGSERVICEFILE=unset\tDATABASE_URL=postgresql+psycopg://ai_employee@localhost:5432/'"${canonical_database_path}" "${uv_environment_log}"
  assert_line_count 1 "${uv_environment_log}"
  assert_exact_line "${exact_database_name}" "${typed_database_name_log}"
  assert_line_count 1 "${typed_database_name_log}"
  assert_not_contains 'synthetic-password-marker' "${uv_environment_log}"
done

# wrapper 的放宽只适用于 decoded database name；endpoint、owner role、query/fragment 以及
# decoded UTF-8/NUL/63-byte 门禁仍必须在 fake typed migrate 首次调用前 fail closed。
invalid_db_upgrade_urls=(
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/'
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/%00'
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/%FF'
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/ai_employee_test?host=elsewhere'
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/ai_employee_test#fragment'
  'postgresql+asyncpg://:synthetic-password-marker@localhost:5432/ai_employee_test'
  'postgresql+asyncpg://bad-user:synthetic-password-marker@localhost:5432/ai_employee_test'
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@:5432/ai_employee_test'
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost/ai_employee_test'
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:0/ai_employee_test'
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/bad%'
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/bad%4'
  'postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/bad%GG'
  'postgresql+asyncpg://ai_employee%:synthetic-password-marker@localhost:5432/ai_employee_test'
  'postgresql+asyncpg://ai%4_employee:synthetic-password-marker@localhost:5432/ai_employee_test'
  'postgresql+asyncpg://ai_employee%gG:synthetic-password-marker@localhost:5432/ai_employee_test'
)
for invalid_db_upgrade_url in "${invalid_db_upgrade_urls[@]}"; do
  set_valid_database_environment
  export DATABASE_URL="${invalid_db_upgrade_url}"
  expect_db_upgrade_failure "invalid endpoint or decoded name"
done

set_valid_database_environment
oversized_decoded_database_path="$(printf '%%C3%%A9%.0s' {1..32})"
export DATABASE_URL="postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/${oversized_decoded_database_path}"
expect_db_upgrade_failure 'database name over 63 decoded UTF-8 bytes'

set_valid_database_environment
oversized_upgrade_owner="$(printf 'a%.0s' {1..64})"
export DATABASE_URL="postgresql+asyncpg://${oversized_upgrade_owner}:synthetic-password-marker@localhost:5432/ai_employee_test"
expect_db_upgrade_failure 'owner role over 63 bytes'

# service 参数同样不能逃逸；有参数时还必须经过 -- 与固定命令选项隔离。
clear_command_log
logs_sentinel="${sandbox_dir}/logs-injection-sentinel"
logs_payload="service; : > \"${logs_sentinel}\"; #"
if ! run_just_capture "${output_file}" just --yes logs "${logs_payload}"; then
  printf 'tooling behavior contract failed: safe logs invocation failed\n' >&2
  exit 1
fi
if [[ -e "${logs_sentinel}" ]]; then
  printf 'tooling behavior contract failed: logs payload escaped into shell\n' >&2
  exit 1
fi
assert_contains "${logs_payload}" "${command_log}"
assert_exact_line $'docker\tcompose\tlogs\t-f\t--\t'"${logs_payload}" "${command_log}"
assert_line_count 1 "${command_log}"

# restore file 必须经过自有精确确认；容器映射只取 basename，但仍要作为独立环境参数
# 传给 Compose，不能插入固定的 /bin/sh -ec 程序文本。
set_valid_database_environment
clear_command_log
restore_sentinel="${sandbox_dir}/restore-injection-sentinel"
restore_payload="${sandbox_dir}/backup \"; : > restore-injection-sentinel; #.dump"
if ! run_just_with_input_capture "${restore_payload}" "${output_file}" just --yes restore "${restore_payload}"; then
  printf 'tooling behavior contract failed: safe restore invocation failed\n' >&2
  exit 1
fi
if [[ -e "${restore_sentinel}" ]]; then
  printf 'tooling behavior contract failed: restore payload escaped into shell\n' >&2
  exit 1
fi
restore_basename="$(basename -- "${restore_payload}")"
assert_exact_line $'docker\tcompose\t--profile\toperations\trun\t--rm\t--no-deps\t-e\tAPP_ENV=development\t-e\tALLOW_PRODUCTION_RESTORE=\t-e\tRESTORE_FILE=/backups/'"${restore_basename}"$'\t--entrypoint\t/bin/sh\tbackup\t-ec\texport PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec bash /app/scripts/restore-postgres.sh "$RESTORE_FILE"' "${command_log}"
assert_line_count 1 "${command_log}"

# 生产、缺失、拼错或未知环境一律 fail-closed；测试绕过 just 自身确认，但 recipe 仍应拒绝。
set_valid_database_environment
export APP_ENV=production
expect_db_reset_failure production
expect_restore_failure production "${sandbox_dir}/synthetic.dump"

unset APP_ENV
expect_db_reset_failure unset
expect_restore_failure unset "${sandbox_dir}/synthetic.dump"

for invalid_environment in prod staging; do
  export APP_ENV="${invalid_environment}"
  expect_db_reset_failure "${invalid_environment}"
  expect_restore_failure "${invalid_environment}" "${sandbox_dir}/synthetic.dump"
done

# 关键配置缺失、URL 数据库不一致或数据库标识符不安全时，任何 fake 外部命令都不能先执行。
set_valid_database_environment
for missing_variable in \
  POSTGRES_DB POSTGRES_USER DATABASE_URL POSTGRES_BOOTSTRAP_PASSWORD_FILE \
  APP_DATABASE_PASSWORD_FILE RETENTION_DATABASE_PASSWORD_FILE; do
  set_valid_database_environment
  unset "${missing_variable}"
  expect_db_reset_failure "missing ${missing_variable}"
done

set_valid_database_environment
export DATABASE_URL='postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/another_database'
expect_db_reset_failure database-url-mismatch

for system_database in postgres template0 template1; do
  set_valid_database_environment
  export APP_ENV=development
  export POSTGRES_DB="${system_database}"
  export DATABASE_URL="postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/${system_database}"
  expect_db_reset_failure "system database ${system_database}" 'system databases cannot be reset'
done

# quoted/Unicode 数据库名属于 PostgreSQL 的合法 UTF-8 标识符；guard 必须逐字保留，且
# 只有 exact 小写系统库名称被拒绝，不能 normalization、case-fold 或套用 role 的 ASCII 语法。
valid_database_cases=(
  $'test|\u62a5\u4ef7"\u5e93_test|%E6%8A%A5%E4%BB%B7%22%E5%BA%93_test'
  'test|Postgres_test|Postgres_test'
  'development|Postgres|Postgres'
)
for valid_database_case in "${valid_database_cases[@]}"; do
  valid_environment="${valid_database_case%%|*}"
  remaining_database_case="${valid_database_case#*|}"
  valid_database="${remaining_database_case%%|*}"
  canonical_database_path="${remaining_database_case#*|}"
  set_valid_database_environment
  export APP_ENV="${valid_environment}"
  export POSTGRES_DB="${valid_database}"
  export DATABASE_URL="postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/${valid_database}"
  clear_command_log
  : >"${uv_environment_log}"
  if ! run_just_with_input_split \
    "${valid_database}" "${stdout_file}" "${stderr_file}" just --yes db-reset; then
    printf 'tooling behavior contract failed: db-reset rejected exact UTF-8 database %q\n' \
      "${valid_database}" >&2
    exit 1
  fi
  assert_exact_line $'uv\trun\t--project\tbackend\tpython\t-m\tai_employee.cli.database_maintenance\tdb-reset\t--owner-password-file\t'"${POSTGRES_BOOTSTRAP_PASSWORD_FILE}"$'\t--app-password-file\t'"${APP_DATABASE_PASSWORD_FILE}"$'\t--retention-password-file\t'"${RETENTION_DATABASE_PASSWORD_FILE}"$'\t--app-env\t'"${valid_environment}"$'\t--confirmed-database-name\t'"${valid_database}" "${command_log}"
  assert_line_count 1 "${command_log}"
  assert_exact_line $'uv-upgrade-environment\tPGHOST=unset\tPGHOSTADDR=unset\tPGPORT=unset\tPGDATABASE=unset\tPGUSER=unset\tPGSERVICE=unset\tPGSERVICEFILE=unset\tDATABASE_URL=postgresql+psycopg://ai_employee@localhost:5432/'"${canonical_database_path}" "${uv_environment_log}"
  assert_line_count 1 "${uv_environment_log}"
done

# 字面 percent 与 percent-encoding 不能形成两套 reset identity：合法 `%25` 与 `%252F`
# 均只解码一次，输出再规范编码，typed CLI 最终仍看到人工确认的原始名称。
valid_reset_percent_cases=(
  'literal%_test|literal%25_test'
  'literal%2F_test|literal%252F_test'
  'literal%41_test|literal%2541_test'
)
for valid_reset_percent_case in "${valid_reset_percent_cases[@]}"; do
  exact_percent_database="${valid_reset_percent_case%%|*}"
  encoded_percent_database_path="${valid_reset_percent_case#*|}"
  set_valid_database_environment
  export APP_ENV=test
  export POSTGRES_DB="${exact_percent_database}"
  export DATABASE_URL="postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/${encoded_percent_database_path}"
  clear_command_log
  : >"${uv_environment_log}"
  : >"${typed_database_name_log}"
  if ! run_just_with_input_split \
    "${POSTGRES_DB}" "${stdout_file}" "${stderr_file}" just --yes db-reset; then
    printf 'tooling behavior contract failed: db-reset rejected exact percent database %q\n' \
      "${POSTGRES_DB}" >&2
    exit 1
  fi
  assert_exact_line $'uv-upgrade-environment\tPGHOST=unset\tPGHOSTADDR=unset\tPGPORT=unset\tPGDATABASE=unset\tPGUSER=unset\tPGSERVICE=unset\tPGSERVICEFILE=unset\tDATABASE_URL=postgresql+psycopg://ai_employee@localhost:5432/'"${encoded_percent_database_path}" "${uv_environment_log}"
  assert_exact_line "${POSTGRES_DB}" "${typed_database_name_log}"
done

set_valid_database_environment
export APP_ENV=test
export POSTGRES_DB='literal%41_test'
export DATABASE_URL='postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/literal%41_test'
expect_db_reset_failure 'percent-encoding decoded to a different database identity'

for malformed_percent_database in 'bad%' 'bad%4' 'bad%GG'; do
  set_valid_database_environment
  export APP_ENV=development
  export POSTGRES_DB="${malformed_percent_database}"
  export DATABASE_URL="postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/${malformed_percent_database}"
  expect_db_reset_failure "malformed percent database ${malformed_percent_database}"
done

# 这些名称在 PostgreSQL 中本身合法；test 环境仍应由现有 exact ``_test`` 后缀门禁拒绝。
unsafe_databases=(
  '' bad-name '1bad' 'bad name' 'bad;name' 'bad"name'
)
for unsafe_database in "${unsafe_databases[@]}"; do
  set_valid_database_environment
  export APP_ENV=test
  export POSTGRES_DB="${unsafe_database}"
  export DATABASE_URL="postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/${unsafe_database}"
  expect_db_reset_failure "unsafe database ${unsafe_database}"
done

set_valid_database_environment
export POSTGRES_USER='bad;user'
expect_db_reset_failure unsafe-user

set_valid_database_environment
export DATABASE_URL='mysql://ai_employee:ai_employee@localhost:3306/ai_employee_test'
expect_db_reset_failure unsupported-database-url

# 破坏性命令必须校验 SQLAlchemy 最终消费的原始字符串：scheme 仅接受精确小写形式，
# 并拒绝 urlsplit 会静默剥离的前导空格或制表符，避免重建数据库后才由 Alembic 报错。
non_canonical_database_urls=(
  'POSTGRESQL+ASYNCPG://ai_employee:ai_employee@localhost:5432/ai_employee_test'
  'PostgreSQL://ai_employee:ai_employee@localhost:5432/ai_employee_test'
  ' postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test'
  $'\tpostgresql://ai_employee:ai_employee@localhost:5432/ai_employee_test'
)
for non_canonical_database_url in "${non_canonical_database_urls[@]}"; do
  set_valid_database_environment
  export DATABASE_URL="${non_canonical_database_url}"
  expect_db_reset_failure non-canonical-database-url-prefix
  assert_contains 'DATABASE_URL must begin with an exact lowercase PostgreSQL scheme' "${output_file}"
done

# urlsplit 会在 URL 任意位置静默删除 TAB、LF 和 CR，但 SQLAlchemy 保留原字符；用户名、密码、
# 主机、数据库 path、query 或 fragment 出现这些字符时必须在解析前拒绝，避免后置 guard
# 抢先返回其他错误并掩盖原始字符串不安全。
control_character_names=(tab line-feed carriage-return)
control_characters=($'\t' $'\n' $'\r')
for control_index in "${!control_character_names[@]}"; do
  control_name="${control_character_names[control_index]}"
  control_character="${control_characters[control_index]}"
  control_character_locations=(username password host database-path query fragment)
  control_character_urls=(
    "postgresql+asyncpg://ai_${control_character}employee:ai_employee@localhost:5432/ai_employee_test"
    "postgresql+asyncpg://ai_employee:ai_${control_character}employee@localhost:5432/ai_employee_test"
    "postgresql+asyncpg://ai_employee:ai_employee@local${control_character}host:5432/ai_employee_test"
    "postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee${control_character}_test"
    "postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test?application_name=ai_${control_character}employee"
    "postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test#synthetic-${control_character}fragment"
  )
  for location_index in "${!control_character_locations[@]}"; do
    set_valid_database_environment
    export DATABASE_URL="${control_character_urls[location_index]}"
    expect_db_reset_failure \
      "DATABASE_URL with internal ${control_name} in ${control_character_locations[location_index]}" \
      'DATABASE_URL must not contain TAB, LF, or CR characters'
  done
done

# URL 的连接端点和认证主体必须与本地 Compose 目标一致；只匹配 path 不能证明目标安全。
set_valid_database_environment
export DATABASE_URL='postgresql+asyncpg://ai_employee:synthetic-secret-marker@production.example:5432/ai_employee_test'
expect_db_reset_failure remote-database-host
assert_not_contains 'synthetic-secret-marker' "${output_file}"

set_valid_database_environment
export DATABASE_URL='postgresql+asyncpg://ai_employee:ai_employee@localhost:6543/ai_employee_test'
expect_db_reset_failure non-default-database-port

set_valid_database_environment
export DATABASE_URL='postgresql+asyncpg://different_user:ai_employee@localhost:5432/ai_employee_test'
expect_db_reset_failure database-url-user-mismatch

# SQLAlchemy/libpq 可让 query 参数覆盖 authority/path，因此 db-reset 必须拒绝任何 query。
query_override_urls=(
  'postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test?host=production.example:6543'
  'postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test?user=different_user'
  'postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test?database=another_database'
  'postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test?host=localhost&host=production.example'
  'postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test?'
)
for query_override_url in "${query_override_urls[@]}"; do
  set_valid_database_environment
  export DATABASE_URL="${query_override_url}"
  expect_db_reset_failure database-url-query
done

for fragment_url in \
  'postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test#synthetic-fragment' \
  'postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test#'; do
  set_valid_database_environment
  export DATABASE_URL="${fragment_url}"
  expect_db_reset_failure database-url-fragment
done

set_valid_database_environment
export DATABASE_URL='postgresql+asyncpg://ai_employee:ai_employee@localhost/ai_employee_test'
expect_db_reset_failure missing-database-port

# PostgreSQL 标识符按 UTF-8 字节限制为 63；长度边界必须在任何 destructive command 前拒绝。
oversized_database="$(printf 'a%063d' 0)"
set_valid_database_environment
export POSTGRES_DB="${oversized_database}"
export DATABASE_URL="postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/${oversized_database}"
expect_db_reset_failure oversized-database-identifier

oversized_user="$(printf 'a%063d' 0)"
set_valid_database_environment
export POSTGRES_USER="${oversized_user}"
export DATABASE_URL="postgresql+asyncpg://${oversized_user}:ai_employee@localhost:5432/ai_employee_test"
expect_db_reset_failure oversized-user-identifier

set_valid_database_environment
export APP_ENV=test
export POSTGRES_DB=ai_employee
export DATABASE_URL='postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee'
expect_db_reset_failure test-database-without-test-suffix

# 确认值必须精确匹配目标；空输入和错误输入都要在 destructive command 前拒绝。
set_valid_database_environment
export APP_ENV=development
clear_command_log
if run_just_with_input_capture wrong-database "${output_file}" just --yes db-reset; then
  printf 'tooling behavior contract failed: wrong db confirmation was accepted\n' >&2
  exit 1
fi
assert_no_fake_calls

clear_command_log
if run_just_with_input_capture '' "${output_file}" just --yes db-reset; then
  printf 'tooling behavior contract failed: empty db confirmation was accepted\n' >&2
  exit 1
fi
assert_no_fake_calls

expect_restore_failure wrong-file "${sandbox_dir}/synthetic.dump" wrong-file
expect_restore_failure empty-file-confirmation "${sandbox_dir}/synthetic.dump" ''
expect_restore_failure empty-file '' ''

# development/test 是唯一允许继续的环境；合法目标必须携带显式 5432 且不得包含 query。
for allowed_environment in development test; do
  set_valid_database_environment
  export APP_ENV="${allowed_environment}"
  export DATABASE_URL='postgresql+asyncpg://ai_employee:synthetic-password-marker@localhost:5432/ai_employee_test'
  export PGHOST=production.example
  export PGHOSTADDR=203.0.113.10
  export PGPORT=6543
  export PGDATABASE=production_database
  export PGUSER=production_user
  export PGSERVICE=production_service
  export PGSERVICEFILE=/synthetic/production-service.conf
  clear_command_log
  : >"${uv_environment_log}"
  if ! run_just_with_input_split "${POSTGRES_DB}" "${stdout_file}" "${stderr_file}" just --yes db-reset; then
    printf 'tooling behavior contract failed: db-reset rejected %s\n' "${allowed_environment}" >&2
    exit 1
  fi
  assert_contains "APP_ENV=${allowed_environment}" "${stderr_file}"
  assert_contains 'Compose service: postgres' "${stderr_file}"
  assert_contains 'Verified endpoint: localhost:5432' "${stderr_file}"
  assert_contains "Database target: ${POSTGRES_DB}" "${stderr_file}"
  assert_contains "Database user: ${POSTGRES_USER}" "${stderr_file}"
  assert_not_contains 'synthetic-password-marker' "${stderr_file}"
  assert_not_contains "${DATABASE_URL}" "${stderr_file}"
  assert_exact_line $'uv\trun\t--project\tbackend\tpython\t-m\tai_employee.cli.database_maintenance\tdb-reset\t--owner-password-file\t'"${POSTGRES_BOOTSTRAP_PASSWORD_FILE}"$'\t--app-password-file\t'"${APP_DATABASE_PASSWORD_FILE}"$'\t--retention-password-file\t'"${RETENTION_DATABASE_PASSWORD_FILE}"$'\t--app-env\t'"${allowed_environment}"$'\t--confirmed-database-name\tai_employee_test' "${command_log}"
  assert_line_count 1 "${command_log}"
  assert_exact_line $'uv-upgrade-environment\tPGHOST=unset\tPGHOSTADDR=unset\tPGPORT=unset\tPGDATABASE=unset\tPGUSER=unset\tPGSERVICE=unset\tPGSERVICEFILE=unset\tDATABASE_URL=postgresql+psycopg://ai_employee@localhost:5432/ai_employee_test' "${uv_environment_log}"
  assert_line_count 1 "${uv_environment_log}"

  restore_target="${sandbox_dir}/safe backup; \"copy\".dump"
  clear_command_log
  if ! run_just_with_input_split "${restore_target}" "${stdout_file}" "${stderr_file}" just --yes restore "${restore_target}"; then
    printf 'tooling behavior contract failed: restore rejected %s\n' "${allowed_environment}" >&2
    exit 1
  fi
  assert_contains "APP_ENV=${allowed_environment}" "${stderr_file}"
  assert_contains 'Compose service: postgres' "${stderr_file}"
  assert_contains "Restore target: ${restore_target}" "${stderr_file}"
  restore_target_basename="$(basename -- "${restore_target}")"
  assert_exact_line $'docker\tcompose\t--profile\toperations\trun\t--rm\t--no-deps\t-e\tAPP_ENV='"${allowed_environment}"$'\t-e\tALLOW_PRODUCTION_RESTORE=\t-e\tRESTORE_FILE=/backups/'"${restore_target_basename}"$'\t--entrypoint\t/bin/sh\tbackup\t-ec\texport PGPASSWORD="$(cat /run/secrets/app_database_password)"; exec bash /app/scripts/restore-postgres.sh "$RESTORE_FILE"' "${command_log}"
  assert_line_count 1 "${command_log}"
done

# logs 无参数必须保留全部日志行为；有参数时参数必须是单一字段且位于 -- 之后。
clear_command_log
if ! run_just_capture "${output_file}" just --yes logs; then
  printf 'tooling behavior contract failed: logs without service failed\n' >&2
  exit 1
fi
assert_exact_line $'docker\tcompose\tlogs\t-f' "${command_log}"
assert_line_count 1 "${command_log}"
assert_not_contains $'docker\tcompose\tlogs\t-f\t--' "${command_log}"

service_name='mail service; "quoted"'
clear_command_log
if ! run_just_capture "${output_file}" just --yes logs "${service_name}"; then
  printf 'tooling behavior contract failed: logs service failed\n' >&2
  exit 1
fi
assert_exact_line $'docker\tcompose\tlogs\t-f\t--\t'"${service_name}" "${command_log}"
assert_line_count 1 "${command_log}"

# [confirm] 是额外的 just 层保护，不能因改成脚本式 recipe 而丢失；检查完整展开结果，
# 不依赖注释或相邻行的偶然布局。
assert_recipe_has_confirm() {
  local recipe="$1"
  local recipe_header="$2"
  local definition
  if ! definition="$(
    cd "${sandbox_dir}"
    JUST_TEMPDIR="${sandbox_dir}/just-temp" just --show "${recipe}"
  )"; then
    printf 'tooling behavior contract failed: just --show %s failed\n' "${recipe}" >&2
    exit 1
  fi
  grep -Fqx -- '[confirm]' <<<"${definition}" || {
    printf 'tooling behavior contract failed: %s is missing [confirm]\n' "${recipe}" >&2
    exit 1
  }
  grep -Fqx -- "${recipe_header}" <<<"${definition}" || {
    printf 'tooling behavior contract failed: %s definition is missing %s\n' \
      "${recipe}" "${recipe_header}" >&2
    exit 1
  }
}

assert_recipe_has_confirm db-reset 'db-reset:'
assert_recipe_has_confirm restore 'restore file:'

# db-reset 直接使用 Python 标准库执行安全 guard，因此 doctor 必须显式暴露该本地依赖。
doctor_definition="$(
  cd "${sandbox_dir}"
  JUST_TEMPDIR="${sandbox_dir}/just-temp" just --show doctor
)"
if ! grep -Fq -- '@command -v python3' <<<"${doctor_definition}"; then
  printf 'tooling behavior contract failed: doctor does not check python3\n' >&2
  exit 1
fi

# 0019 宿主入口必须执行实际 Docker metadata 查询；Fake 仅提供合成 JSON 和内容 ID。
set_valid_rollout_environment() {
  export APP_IMAGE_TAG=synthetic-0019
  export BACKUP_ARTIFACT_BASENAME=calendar-aad-0019-synthetic
  export BACKUP_DIR="${sandbox_dir}/backup output..0019"
  mkdir -p "${BACKUP_DIR}"
  unset CALENDAR_AAD_IMMUTABLE_IMAGE_ID CALENDAR_AAD_ROLLOUT_STATE TOOLING_TEST_INSPECT_FAIL TOOLING_TEST_IMAGE_MISMATCH TOOLING_TEST_BOUND_IMAGE
  unset TOOLING_TEST_SCREEN_FAIL TOOLING_TEST_SERVICE_AFTER_SCREEN TOOLING_TEST_IMAGE_AFTER_SCREEN
  export TOOLING_TEST_SCREEN_REVISION=20260809_0018
  rm -f -- "${TOOLING_TEST_SCREEN_SEEN}"
  printf '[]\n' >"${TOOLING_TEST_COMPOSE_PS}"
  python3 - <<'PY'
import json
import os

environment = {flag: "false" for flag in ("EXTERNAL_WRITES_ENABLED", "GOOGLE_WRITES_ENABLED", "MICROSOFT_WRITES_ENABLED")}
environment["DATABASE_URL"] = "postgresql+asyncpg://synthetic_app@postgres:5432/synthetic_test"
services = {
    name: {"image": f"synthetic-{name}:synthetic-0019", "environment": dict(environment),
           "secrets": [{"source": "postgres_bootstrap_password" if name == "migration" else "app_database_password"}],
           "volumes": [], "build": {"context": "."}}
    for name in ("worker", "migration", "api", "scheduler", "backup")
}
services["migration"]["environment"]["DATABASE_URL"] = "postgresql+psycopg://synthetic_owner@postgres:5432/synthetic_test"
with open(os.environ["TOOLING_TEST_COMPOSE_CONFIG"], "w", encoding="utf-8") as stream:
    json.dump({"name": "synthetic-rollout", "services": services}, stream)
PY
}

expect_rollout_failure() {
  local expected_code="$1"
  local recipe="${2:-calendar-aad-preflight-0019}"
  clear_command_log
  if run_just_capture "${output_file}" just --yes "${recipe}"; then
    printf 'tooling behavior contract failed: rollout accepted %s\n' "${expected_code}" >&2
    exit 1
  fi
  assert_contains "${expected_code}" "${output_file}"
  assert_not_contains $'\trun\t--rm\t--no-deps\t--pull\tnever' "${command_log}"
}

for rollout_recipe in calendar-aad-preflight-0019 calendar-aad-migrate-0019 calendar-aad-resync-0019 backup; do
  set_valid_rollout_environment
  if [[ "${rollout_recipe}" == calendar-aad-resync-0019 ]]; then
    export TOOLING_TEST_SCREEN_REVISION=20260809_0019
  fi
  clear_command_log
  run_just_capture "${output_file}" just --yes "${rollout_recipe}" || {
    printf 'tooling behavior contract failed: valid rollout recipe %s failed\n' "${rollout_recipe}" >&2
    cat "${output_file}" >&2
    exit 1
  }
  assert_contains $'docker\tcompose\tconfig\t--format\tjson' "${command_log}"
  assert_contains $'docker\tcompose\tps\t--all\t--format\tjson' "${command_log}"
  assert_contains $'docker\timage\tinspect\t--format\t{{.Id}}\tsynthetic-worker:synthetic-0019' "${command_log}"
  assert_contains $'docker\timage\tinspect\t--format\t{{.Id}}\tsynthetic-migration:synthetic-0019' "${command_log}"
  assert_contains $'\t--no-deps\t--pull\tnever\t--entrypoint\t/bin/sh\t' "${command_log}"
  if [[ "${rollout_recipe}" == calendar-aad-migrate-0019 ]]; then
    assert_contains 'export PGPASSWORD="$(cat /run/secrets/postgres_bootstrap_password)"; exec uv run --no-sync python -m ai_employee.cli.calendar_aad_migrate_0019' "${command_log}"
  else
    assert_contains 'export PGPASSWORD="$(cat /run/secrets/app_database_password)";' "${command_log}"
  fi
  TOOLING_TEST_RECIPE="${rollout_recipe}" python3 - <<'PY'
import json
import os

recipe = os.environ["TOOLING_TEST_RECIPE"]
name = "migration" if recipe == "calendar-aad-migrate-0019" else "backup" if recipe == "backup" else "worker"
with open(os.environ["TOOLING_TEST_RENDERED"], encoding="utf-8") as stream:
    selected = json.load(stream)["services"][name]
assert selected["image"] == os.environ["TOOLING_TEST_IMAGE_ID"]
assert selected["pull_policy"] == "never" and "build" not in selected
assert selected["environment"]["CALENDAR_AAD_IMMUTABLE_IMAGE_ID"] == selected["image"]
assert selected["environment"]["BACKUP_ARTIFACT_BASENAME"] == os.environ["BACKUP_ARTIFACT_BASENAME"]
assert selected["environment"]["BACKUP_DIR"] == "/backups"
assert selected["environment"]["CALENDAR_AAD_ROLLOUT_STATE"] == f'/backups/{os.environ["BACKUP_ARTIFACT_BASENAME"]}.calendar-aad-preflight.json'
assert all(selected["environment"][flag] == "false" for flag in ("EXTERNAL_WRITES_ENABLED", "GOOGLE_WRITES_ENABLED", "MICROSOFT_WRITES_ENABLED"))
assert selected["volumes"] == [{"type": "bind", "source": os.environ["BACKUP_DIR"], "target": "/backups",
                                  "read_only": recipe in ("calendar-aad-migrate-0019", "calendar-aad-resync-0019")}]
assert selected["secrets"] == [{"source": "postgres_bootstrap_password" if name == "migration" else "app_database_password"}]
with open(os.environ["TOOLING_TEST_SCREEN_RENDERED"], encoding="utf-8") as stream:
    screen = json.load(stream)["services"]["migration"]
assert screen["image"] == selected["image"] and screen["pull_policy"] == "never"
assert "build" not in screen and screen["volumes"] == []
assert screen["secrets"] == [{"source": "postgres_bootstrap_password"}]
assert all(key not in screen["environment"] for key in ("CALENDAR_AAD_ROLLOUT_STATE", "BACKUP_DIR", "BACKUP_ARTIFACT_BASENAME"))
with open(os.environ["TOOLING_TEST_LOG"], encoding="utf-8") as stream:
    commands = [line.rstrip("\n").split("\t") for line in stream]
screen_index = next(index for index, fields in enumerate(commands) if fields[-1].endswith("ai_employee.cli.calendar_aad_revision_0019"))
formal_index = max(index for index, fields in enumerate(commands) if "run" in fields)
assert screen_index < formal_index
between = commands[screen_index + 1:formal_index]
assert ["docker", "compose", "ps", "--all", "--format", "json"] in between
assert any(fields[:3] == ["docker", "image", "inspect"] and fields[-1].startswith("synthetic-worker:") for fields in between)
assert any(fields[:3] == ["docker", "image", "inspect"] and fields[-1].startswith("synthetic-migration:") for fields in between)
PY
done

# 这些失败发生在只读 screen 之后，必须有且只有 screen one-off，不能进入正式命令。
for screen_failure in unavailable wrong_revision service_changed image_changed; do
  set_valid_rollout_environment
  case "${screen_failure}" in
    unavailable) export TOOLING_TEST_SCREEN_FAIL=yes; expected_screen_code=calendar_aad_revision_screen_failed ;;
    wrong_revision) export TOOLING_TEST_SCREEN_REVISION=20260809_0019; expected_screen_code=calendar_aad_revision_mismatch ;;
    service_changed) export TOOLING_TEST_SERVICE_AFTER_SCREEN=yes; expected_screen_code=calendar_aad_services_running ;;
    image_changed) export TOOLING_TEST_IMAGE_AFTER_SCREEN=yes; expected_screen_code=calendar_aad_image_mismatch ;;
  esac
  clear_command_log
  if run_just_capture "${output_file}" just --yes calendar-aad-preflight-0019; then
    printf 'tooling behavior contract failed: screen boundary accepted %s\n' "${screen_failure}" >&2
    exit 1
  fi
  assert_contains "${expected_screen_code}" "${output_file}"
  assert_contains 'ai_employee.cli.calendar_aad_revision_0019' "${command_log}"
  assert_not_contains 'ai_employee.cli.calendar_aad_preflight_0019' "${command_log}"
done

# basename 沿用唯一 canonical 边界；内部两个点不是路径穿越，不得另造不兼容规则。
set_valid_rollout_environment
export BACKUP_ARTIFACT_BASENAME=synthetic..0019
run_just_capture "${output_file}" just --yes calendar-aad-preflight-0019 || {
  printf 'tooling behavior contract failed: canonical dotted basename rejected\n' >&2
  exit 1
}

for bad_tag in '' latest; do
  set_valid_rollout_environment
  export APP_IMAGE_TAG="${bad_tag}"
  expect_rollout_failure calendar_aad_image_invalid
done
set_valid_rollout_environment
export TOOLING_TEST_INSPECT_FAIL=yes
expect_rollout_failure calendar_aad_docker_query_failed
set_valid_rollout_environment
export TOOLING_TEST_IMAGE_MISMATCH=yes
expect_rollout_failure calendar_aad_image_mismatch
set_valid_rollout_environment
export CALENDAR_AAD_IMMUTABLE_IMAGE_ID="${TOOLING_TEST_IMAGE_ID}"
expect_rollout_failure calendar_aad_image_override_forbidden
for unsafe_basename in '' ../synthetic 'synthetic/scope' 'synthetic scope'; do
  set_valid_rollout_environment
  export BACKUP_ARTIFACT_BASENAME="${unsafe_basename}"
  expect_rollout_failure calendar_aad_basename_invalid
done
for unsafe_directory in / /tmp/.. relative-path; do
  set_valid_rollout_environment
  export BACKUP_DIR="${unsafe_directory}"
  expect_rollout_failure calendar_aad_backup_directory_invalid
done
# 只创建 sandbox 自有链接；拒绝发生在 Docker 边界前，绝不挂载或写入真实根目录。
set_valid_rollout_environment
ln -s / "${sandbox_dir}/root-directory-link"
export BACKUP_DIR="${sandbox_dir}/root-directory-link"
expect_rollout_failure calendar_aad_backup_directory_invalid
for service in caddy api worker scheduler migration; do
  for state in running paused restarting; do
    set_valid_rollout_environment
    printf '[{"Service":"%s","State":"%s"}]\n' "${service}" "${state}" >"${TOOLING_TEST_COMPOSE_PS}"
    expect_rollout_failure calendar_aad_services_running
  done
done
for unsafe_config in write code_mount latest_image; do
  set_valid_rollout_environment
  TOOLING_TEST_UNSAFE_CONFIG="${unsafe_config}" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["TOOLING_TEST_COMPOSE_CONFIG"])
config = json.loads(path.read_text())
worker = config["services"]["worker"]
case = os.environ["TOOLING_TEST_UNSAFE_CONFIG"]
if case == "write":
    worker["environment"]["GOOGLE_WRITES_ENABLED"] = "true"
elif case == "code_mount":
    worker["volumes"] = [{"type": "bind", "source": "/synthetic/source", "target": "/app"}]
else:
    worker["image"] = "synthetic:latest"
path.write_text(json.dumps(config))
PY
  case "${unsafe_config}" in
    write) expect_rollout_failure calendar_aad_write_switch_enabled ;;
    code_mount) expect_rollout_failure calendar_aad_image_mount_invalid ;;
    latest_image) expect_rollout_failure calendar_aad_image_invalid ;;
  esac
done

# 当同一个 tag 移动到另一 ID，内部 CLI 必须实际收到新 ID，不能重用 operator 缓存。
set_valid_rollout_environment
export TOOLING_TEST_BOUND_IMAGE="sha256:0000000000000000000000000000000000000000000000000000000000000000"
export TOOLING_TEST_SCREEN_REVISION=20260809_0019
if run_just_capture "${output_file}" just --yes calendar-aad-resync-0019; then
  printf 'tooling behavior contract failed: moved image was accepted\n' >&2
  exit 1
fi
assert_contains calendar_aad_artifact_invalid "${output_file}"
for recipe in calendar-aad-preflight-0019 calendar-aad-migrate-0019 calendar-aad-resync-0019; do
  set_valid_rollout_environment
  clear_command_log
  if run_just_capture "${output_file}" just --yes "${recipe}" synthetic-extra-scope; then
    printf 'tooling behavior contract failed: extra rollout scope argument accepted\n' >&2
    exit 1
  fi
  assert_no_fake_calls
done

# 普通备份继续沿既有 service；只有显式 basename 的封闭窗口经过窄 guard composition。
unset BACKUP_ARTIFACT_BASENAME
clear_command_log
run_just_capture "${output_file}" just --yes backup
assert_exact_line $'docker\tcompose\t--profile\toperations\trun\t--rm\tbackup' "${command_log}"
grep -Fq 'ai_employee.cli.calendar_aad_backup_0019' scripts/backup-postgres.sh

printf 'tooling recipe behavior contract ok\n'
