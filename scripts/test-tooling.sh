#!/usr/bin/env bash
set -euo pipefail

# 该列表是仓库统一命令入口的最小契约，任何 recipe 缺失都应让脚手架检查立即失败。
required_recipes=(
  doctor bootstrap dev infra-up infra-down web api worker scheduler
  test test-backend test-frontend test-integration test-e2e e2e-backend
  lint format typecheck check ci db-upgrade db-revision db-reset
  create-admin logs ps health backup restore observability-up observability-down
)

# 只读取 just 的摘要，避免测试依赖面向人的分组标题、颜色或详细帮助格式。
summary="$(just --summary)"
for recipe in "${required_recipes[@]}"; do
  # 使用空格或行边界匹配，防止短 recipe 被较长名称的子串误判为存在。
  grep -Eq "(^| )${recipe}( |$)" <<<"${summary}"
done

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

command_log="${sandbox_dir}/fake-commands.log"
uv_environment_log="${sandbox_dir}/fake-uv-environment.log"
output_file="${sandbox_dir}/command-output.log"
stdout_file="${sandbox_dir}/command-stdout.log"
stderr_file="${sandbox_dir}/command-stderr.log"
export TOOLING_TEST_LOG="${command_log}"
export TOOLING_TEST_UV_ENV_LOG="${uv_environment_log}"

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
FAKE_DOCKER

cat >"${sandbox_dir}/fake-bin/uv" <<'FAKE_UV'
#!/usr/bin/env bash
set -euo pipefail

# 迁移进程只能从 DATABASE_URL 解析连接目标；这里只记录 libpq 覆盖变量是否存在，绝不记录值。
if [[ "$*" == *'alembic -c backend/alembic.ini upgrade head'* ]]; then
  printf 'uv-upgrade-environment' >>"${TOOLING_TEST_UV_ENV_LOG}"
  for variable_name in PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGSERVICE PGSERVICEFILE; do
    if [[ -v "${variable_name}" ]]; then
      printf '\t%s=set' "${variable_name}" >>"${TOOLING_TEST_UV_ENV_LOG}"
    else
      printf '\t%s=unset' "${variable_name}" >>"${TOOLING_TEST_UV_ENV_LOG}"
    fi
  done
  printf '\n' >>"${TOOLING_TEST_UV_ENV_LOG}"
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

# restore file 也必须以一个原样参数到达假恢复脚本，并且经过自有精确确认。
set_valid_database_environment
clear_command_log
restore_sentinel="${sandbox_dir}/restore-injection-sentinel"
restore_payload="backup \"; : > \"${restore_sentinel}\"; #"
if ! run_just_with_input_capture "${restore_payload}" "${output_file}" just --yes restore "${restore_payload}"; then
  printf 'tooling behavior contract failed: safe restore invocation failed\n' >&2
  exit 1
fi
if [[ -e "${restore_sentinel}" ]]; then
  printf 'tooling behavior contract failed: restore payload escaped into shell\n' >&2
  exit 1
fi
assert_contains "${restore_payload}" "${command_log}"
assert_exact_line $'restore\t'"${restore_payload}" "${command_log}"
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
for missing_variable in POSTGRES_DB POSTGRES_USER DATABASE_URL; do
  set_valid_database_environment
  unset "${missing_variable}"
  expect_db_reset_failure "missing ${missing_variable}"
done

set_valid_database_environment
export DATABASE_URL='postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/another_database'
expect_db_reset_failure database-url-mismatch

unsafe_databases=(
  '' postgres template0 template1 bad-name '1bad' 'bad name' 'bad;name' 'bad"name'
)
for unsafe_database in "${unsafe_databases[@]}"; do
  set_valid_database_environment
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
  assert_exact_line $'docker\tcompose\texec\t-T\tpostgres\tdropdb\t--if-exists\t-U\tai_employee\tai_employee_test' "${command_log}"
  assert_exact_line $'docker\tcompose\texec\t-T\tpostgres\tcreatedb\t-U\tai_employee\tai_employee_test' "${command_log}"
  assert_exact_line $'uv\trun\t--project\tbackend\talembic\t-c\tbackend/alembic.ini\tupgrade\thead' "${command_log}"
  assert_line_count 3 "${command_log}"
  assert_exact_line $'uv-upgrade-environment\tPGHOST=unset\tPGHOSTADDR=unset\tPGPORT=unset\tPGDATABASE=unset\tPGUSER=unset\tPGSERVICE=unset\tPGSERVICEFILE=unset' "${uv_environment_log}"
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
  assert_exact_line $'restore\t'"${restore_target}" "${command_log}"
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

printf 'tooling recipe behavior contract ok\n'
