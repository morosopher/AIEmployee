#!/usr/bin/env bash
# 验证生产编排、数据库权限和可观测性入口的最小部署契约，不连接外部账号或生产资源。
set -euo pipefail

# 四个真实 API 启动命令均必须关闭原始 request-target 日志；注释中的 flag 不算证据。
for api_entry in compose.yaml compose.dev.yaml justfiles/dev.just scripts/run-e2e-backend.sh; do
  if ! grep -Eq -- '^[[:space:]]*(command:|uv run).*uvicorn.*--no-access-log' "${api_entry}"; then
    printf 'OAuth access-log boundary missing: %s\n' "${api_entry}" >&2
    exit 1
  fi
done

[[ "${APP_IMAGE_TAG:-}" != latest ]] || { printf '%s\n' 'APP_IMAGE_TAG must not be latest' >&2; exit 1; }

# 验收仅解析配置，不拉取或运行镜像；提供合成不可变标签和本地域名以通过生产必填变量校验。
export APP_IMAGE_TAG="${APP_IMAGE_TAG:-ci-immutable-test}"
export APP_DOMAIN="${APP_DOMAIN:-localhost}"

# 原始 Compose lifecycle 契约在任何渲染/容器调用前先做静态拒绝；migration 只能调用
# typed migrate，且必须等待 role-bootstrap 成功，不能追加 shell grant repair。
role_bootstrap_section="$(
  awk '/^  role-bootstrap:/{capture=1} capture && /^  migration:/{exit} capture{print}' compose.yaml
)"
migration_section="$(
  awk '/^  migration:/{capture=1} capture && /^  api:/{exit} capture{print}' compose.yaml
)"
grep -Fq 'ai_employee.cli.database_maintenance role-bootstrap' <<<"${role_bootstrap_section}"
grep -Fq 'ai_employee.cli.database_maintenance migrate' <<<"${migration_section}"
grep -Fq 'condition: service_completed_successfully' <<<"${migration_section}"
! grep -Fq 'init-db-roles.sh' <<<"${migration_section}"
! grep -Fq 'alembic ' <<<"${migration_section}"
! grep -Eq 'repair|stamp|downgrade|revision rewrite' <<<"${migration_section}"

assert_file_secret_contract() {
  local configuration_label="$1"
  local secret_name="$2"
  local expected_file="$3"
  local secret_config="$4"
  local expected_suffix file_count file_line file_path path_matches
  file_count="$(
    grep -Ec '^[[:space:]]+file:[[:space:]]+.+$' <<<"${secret_config}" || true
  )"
  [[ "${file_count}" -eq 1 ]] || {
    printf '%s\n' \
      "expected ${configuration_label} ${secret_name} to define exactly one local secret file" >&2
    exit 1
  }
  file_line="$(grep -E '^[[:space:]]+file:[[:space:]]+.+$' <<<"${secret_config}")"
  file_path="${file_line#*:}"
  file_path="${file_path#"${file_path%%[![:space:]]*}"}"
  file_path="${file_path%"${file_path##*[![:space:]]}"}"
  expected_suffix="${expected_file#./}"
  path_matches=false
  if [[ "${file_path}" == "${expected_file}" ]]; then
    path_matches=true
  elif [[ "${file_path}" == /* && "${file_path}" == */"${expected_suffix}" ]]; then
    path_matches=true
  fi
  [[ "${path_matches}" == true ]] || {
    printf '%s\n' \
      "expected ${configuration_label} ${secret_name} to use its configured local secret file" >&2
    exit 1
  }
  if grep -Eq '^[[:space:]]+external:[[:space:]]+true$' <<<"${secret_config}"; then
    printf '%s\n' \
      "expected ${configuration_label} ${secret_name} not to remain external" >&2
    exit 1
  fi
}

run_secret_path_self_test() {
  local synthetic_absolute_secret_config
  synthetic_absolute_secret_config=$'  microsoft_client_secret:\n    external: false\n    file: /synthetic/worktree/secrets/development/microsoft_client_secret'
  assert_file_secret_contract \
    compose-absolute-path microsoft_client_secret \
    ./secrets/development/microsoft_client_secret "${synthetic_absolute_secret_config}"
}

# 合成自测先于任何 Compose 调用执行，也可在没有容器 CLI 时独立验证官方绝对路径兼容性。
run_secret_path_self_test
if [[ "${1:-}" == --self-test-secret-path ]]; then
  printf '%s\n' 'secret path contract ok'
  exit 0
fi

# 合成渲染使用空 env 文件，并在子 Shell 中清除目标变量，避免开发机的 .env 污染默认值证据。
write_configuration_variables=(
  EXTERNAL_WRITES_ENABLED
  GOOGLE_WRITES_ENABLED
  MICROSOFT_WRITES_ENABLED
  WRITE_TEST_ACCOUNT_ALLOWLIST
  MICROSOFT_CLIENT_ID
  MICROSOFT_REDIRECT_URI
)

render_default_compose() (
  unset "${write_configuration_variables[@]}"
  docker compose --env-file /dev/null "$@" config
)

# 显式覆盖只使用合成非秘密值；渲染结果保存在内存中，断言失败时不得回显 allowlist。
render_overridden_compose() (
  export EXTERNAL_WRITES_ENABLED=true
  export GOOGLE_WRITES_ENABLED=true
  export MICROSOFT_WRITES_ENABLED=true
  export WRITE_TEST_ACCOUNT_ALLOWLIST='["google::synthetic-deployment-account"]'
  export MICROSOFT_CLIENT_ID=synthetic-microsoft-client-id
  export MICROSOFT_REDIRECT_URI=https://synthetic.invalid/microsoft/callback
  docker compose --env-file /dev/null "$@" config
)

# 同时展开生产、开发覆盖层和可选可观测性 profile；解析失败表示配置合并或依赖契约无效。
production_config="$(render_default_compose -f compose.yaml)"
development_config="$(render_default_compose -f compose.yaml -f compose.dev.yaml)"
# Compose 会裁掉未启用 profile 的服务及其专属 Secret；完整 file-secret 契约需显式启用
# 所有 profile，默认服务/开关断言仍使用上面的默认渲染，避免改变生产启动语义。
development_all_profiles_config="$(
  render_default_compose -f compose.yaml -f compose.dev.yaml --profile '*'
)"
overridden_production_config="$(render_overridden_compose -f compose.yaml)"
overridden_development_config="$(
  render_overridden_compose -f compose.yaml -f compose.dev.yaml
)"
render_default_compose -f compose.yaml --profile observability >/dev/null

# 只检查渲染后的非秘密开关、Secret 名称与挂载路径，不读取或输出任何 Secret 文件内容。
rendered_service_config() {
  local service="$1"
  local rendered_config="$2"
  awk -v service="${service}" '
    $0 == "  " service ":" { capturing = 1; print; next }
    capturing && $0 ~ /^  [[:alnum:]_-]+:$/ { exit }
    capturing && $0 ~ /^[^ ]/ { exit }
    capturing { print }
  ' <<<"${rendered_config}"
}

rendered_secret_config() {
  local secret_name="$1"
  local rendered_config="$2"
  awk -v secret_name="${secret_name}" '
    $0 == "  " secret_name ":" { capturing = 1; print; next }
    capturing && $0 ~ /^  [[:alnum:]_-]+:$/ { exit }
    capturing && $0 ~ /^[^ ]/ { exit }
    capturing { print }
  ' <<<"${rendered_config}"
}

assert_service_contract() {
  local configuration_label="$1"
  local service="$2"
  local service_config="$3"
  local contract_label="$4"
  local contract="$5"
  local actual_count
  actual_count="$(grep -Fc -- "${contract}" <<<"${service_config}" || true)"
  [[ "${actual_count}" -eq 1 ]] || {
    printf 'expected %s %s %s contract exactly once, got %s\n' \
      "${configuration_label}" "${service}" "${contract_label}" "${actual_count}" >&2
    exit 1
  }
}

assert_backend_configuration() {
  local configuration_label="$1"
  local rendered_config="$2"
  local expected_switch_value="$3"
  local expected_allowlist_value="$4"
  local expected_client_id="$5"
  local expected_redirect_uri="$6"
  local backend_service normalized_service_config

  for backend_service in api worker scheduler; do
    normalized_service_config="$(
      rendered_service_config "${backend_service}" "${rendered_config}" | tr -d "\"'"
    )"
    assert_service_contract \
      "${configuration_label}" "${backend_service}" "${normalized_service_config}" \
      external-write-switch "EXTERNAL_WRITES_ENABLED: ${expected_switch_value}"
    assert_service_contract \
      "${configuration_label}" "${backend_service}" "${normalized_service_config}" \
      google-write-switch "GOOGLE_WRITES_ENABLED: ${expected_switch_value}"
    assert_service_contract \
      "${configuration_label}" "${backend_service}" "${normalized_service_config}" \
      microsoft-write-switch "MICROSOFT_WRITES_ENABLED: ${expected_switch_value}"
    assert_service_contract \
      "${configuration_label}" "${backend_service}" "${normalized_service_config}" \
      write-account-allowlist "WRITE_TEST_ACCOUNT_ALLOWLIST: ${expected_allowlist_value}"
    assert_service_contract \
      "${configuration_label}" "${backend_service}" "${normalized_service_config}" \
      microsoft-client-secret-path \
      'MICROSOFT_CLIENT_SECRET_FILE: /run/secrets/microsoft_client_secret'
    assert_service_contract \
      "${configuration_label}" "${backend_service}" "${normalized_service_config}" \
      microsoft-redirect-uri "MICROSOFT_REDIRECT_URI: ${expected_redirect_uri}"

    if [[ -n "${expected_client_id}" ]]; then
      assert_service_contract \
        "${configuration_label}" "${backend_service}" "${normalized_service_config}" \
        microsoft-client-id "MICROSOFT_CLIENT_ID: ${expected_client_id}"
    else
      grep -Eq '^[[:space:]]+MICROSOFT_CLIENT_ID:[[:space:]]*$' \
        <<<"${normalized_service_config}" || {
        printf '%s\n' \
          "expected ${configuration_label} ${backend_service} microsoft-client-id to be empty" >&2
        exit 1
      }
    fi
    grep -Eq '^[[:space:]]+- (source: )?microsoft_client_secret$' \
      <<<"${normalized_service_config}" || {
      printf '%s\n' \
        "expected ${configuration_label} ${backend_service} to mount microsoft_client_secret" >&2
      exit 1
    }
  done
}

# 默认配置保持 fail closed；显式宿主值必须分别穿透生产 Compose 与开发覆盖层。
default_redirect_uri=http://localhost:8000/api/v1/connections/microsoft/callback
assert_backend_configuration \
  production-defaults "${production_config}" false '[]' '' "${default_redirect_uri}"
assert_backend_configuration \
  development-defaults "${development_config}" false '[]' '' "${default_redirect_uri}"
assert_backend_configuration \
  production-overrides "${overridden_production_config}" true \
  '[google::synthetic-deployment-account]' synthetic-microsoft-client-id \
  https://synthetic.invalid/microsoft/callback
assert_backend_configuration \
  development-overrides "${overridden_development_config}" true \
  '[google::synthetic-deployment-account]' synthetic-microsoft-client-id \
  https://synthetic.invalid/microsoft/callback

assert_development_file_secret() {
  local secret_name="$1"
  local expected_file="$2"
  local secret_config
  secret_config="$(
    rendered_secret_config "${secret_name}" "${development_all_profiles_config}" | tr -d "\"'"
  )"
  assert_file_secret_contract development "${secret_name}" "${expected_file}" "${secret_config}"
}

# 开发覆盖层的全部 file secret 必须解除生产 external 标记，否则标准 Compose 会拒绝合并模型。
development_file_secrets=(
  'microsoft_client_secret=./secrets/development/microsoft_client_secret'
  'google_client_secret=./secrets/development/google_client_secret'
  'model_api_key=./secrets/development/model_api_key'
  'app_master_key=./secrets/development/app_master_key'
  'app_database_password=./secrets/development/app_database_password'
  'postgres_bootstrap_password=./secrets/development/postgres_bootstrap_password'
  'retention_database_password=./secrets/development/retention_database_password'
  'retention_database_url=./secrets/development/retention_database_url'
  'backup_passphrase=./secrets/development/backup_passphrase'
  'grafana_admin_password=./secrets/development/grafana_admin_password'
)
for development_file_secret in "${development_file_secrets[@]}"; do
  assert_development_file_secret \
    "${development_file_secret%%=*}" "${development_file_secret#*=}"
done

# Shell 脚本必须在执行任何备份、恢复或授权操作前通过语法检查。
bash -n scripts/backup-postgres.sh
bash -n scripts/restore-postgres.sh
bash -n scripts/init-db-roles.sh
bash -n scripts/check-process-health.sh
bash scripts/test-process-health.sh

# 这些断言守住 typed role-bootstrap wrapper、SSE 流式转发、Redis 持久化和文件 Secret 边界。
grep -Fq -- '--retention-password-file "$RETENTION_DATABASE_PASSWORD_FILE"' scripts/init-db-roles.sh
grep -q 'flush_interval -1' Caddyfile
# Compose 使用数组传递 Redis 参数；检查相邻的 flag/value，而不是依赖 shell 命令字符串格式。
grep -Eq '"--appendonly"[[:space:]]*,[[:space:]]*"yes"' compose.yaml
grep -q '/run/secrets/app_master_key' compose.yaml
# Prometheus listener 由 Worker 子进程持有；单容器只能启动一个 Taskiq 子进程，横向扩容
# 应通过增加容器副本完成，否则多个子进程会争抢同一个内部指标端口并持续崩溃。
grep -Fq 'taskiq worker --workers 1 --ack-type when_executed' compose.yaml
grep -Fq '["CMD", "bash", "/app/scripts/check-process-health.sh", "worker", "9101"]' compose.yaml
grep -Fq '["CMD", "bash", "/app/scripts/check-process-health.sh", "scheduler", "9102"]' compose.yaml
grep -Fq 'COPY scripts/check-process-health.sh ./scripts/check-process-health.sh' backend/Dockerfile
just --list | grep -q 'observability-up'
just --list | grep -q 'observability-down'
