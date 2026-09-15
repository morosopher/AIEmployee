#!/usr/bin/env bash
# 验证生产编排、数据库权限和可观测性入口的最小部署契约，不连接外部账号或生产资源。
set -euo pipefail

# Task27D完整operations profile的真实Compose渲染必须保持三个独立身份边界。
# 检查只读取配置元数据，任何断言都不回显完整配置或Secret内容。
check_database_operations_profiles() (
  unset EXTERNAL_WRITES_ENABLED GOOGLE_WRITES_ENABLED MICROSOFT_WRITES_ENABLED
  unset RESTORE_STATE_DIR CALENDAR_AAD_RESTORE_IMAGE_ID
  docker compose --env-file /dev/null -f compose.yaml --profile '*' config --format json | python3 -c '
import json, sys
config = json.load(sys.stdin)
services = config["services"]
fixed_state = "/var/lib/ai-employee/restore-state"
for name, script in (("postgres-restore", "restore-postgres.sh"), ("calendar-aad-restore-0018", "restore-calendar-aad-0018.sh")):
    service = services[name]
    assert service["profiles"] == ["operations"], "restore profile boundary"
    assert set(service["depends_on"]) == {"postgres"}, "restore starts a general consumer"
    assert service["command"] == ["bash", "/app/scripts/" + script], "restore fixed executable"
    environment = service["environment"]
    assert environment["RESTORE_STATE_DIR"] == fixed_state, "fixed container state missing"
    assert "PGUSER" not in environment and "PGPASSWORD" not in environment, "broad credential environment"
    assert all(environment[key] == "false" for key in ("EXTERNAL_WRITES_ENABLED", "GOOGLE_WRITES_ENABLED", "MICROSOFT_WRITES_ENABLED")), "restore write switches"
    mounts = {item["target"]: item for item in service["volumes"]}
    assert set(mounts) == {"/backups", fixed_state}, "restore executable mount"
    assert mounts["/backups"]["read_only"] is True, "backup must be read only"
    assert not mounts[fixed_state].get("read_only", False), "state must be writable"
    assert mounts[fixed_state]["source"].endswith("/var/restore-state"), "development state default"
    assert {item["source"] for item in service["secrets"]} == {"postgres_bootstrap_password", "app_database_password", "retention_database_password", "backup_passphrase"}, "restore exact Secret boundary"
for name in ("api", "worker", "scheduler"):
    assert "postgres_bootstrap_password" not in {item["source"] for item in services[name]["secrets"]}, "ordinary consumer owner Secret"
backup = services["backup"]
assert backup["command"] == ["bash", "/app/scripts/backup-postgres.sh"], "backup fixed controller"
assert "PGUSER" not in backup["environment"] and "PGPASSWORD" not in backup["environment"], "backup broad owner credential"
assert {item["source"] for item in backup["secrets"]} == {"postgres_bootstrap_password", "app_database_password", "backup_passphrase"}, "backup exact Secret boundary"
for name in ("legacy-conversion-postgres", "legacy-backup-converter"):
    service = services[name]
    assert service["profiles"] == ["legacy-conversion"], "legacy profile"
    assert set(service["networks"]) == {"legacy_conversion"}, "legacy workspace network"
    assert not service.get("ports"), "legacy published port"
    assert service["labels"]["com.ai-employee.maintenance.kind"] == "legacy_conversion", "legacy labels"
    assert set(service["labels"]) == {"com.ai-employee.maintenance.kind", "com.ai-employee.maintenance.attempt", "com.ai-employee.maintenance.created_at"}, "legacy exact labels"
assert config["networks"]["legacy_conversion"]["internal"] is True, "legacy private network"
assert services["legacy-conversion-postgres"]["user"] == "0:0", "legacy volume bootstrap user"
assert services["legacy-conversion-postgres"]["volumes"][0]["source"] == "legacy_conversion_data", "legacy workspace volume"
assert config["secrets"]["legacy_database_password"]["file"].endswith("/.legacy-conversion-registry/unconfigured.secret"), "legacy generated Secret default"
print("database operations profile contracts ok")
'
)

# 验证开发覆盖层的实际合并结果：本机 OAuth 回调必须可达，私有 Secret 由宿主
# 用户身份读取，数据库密码只来自文件，镜像虚拟环境不能被宿主源码挂载覆盖。
check_development_runtime() (
  export LOCAL_UID=12345 LOCAL_GID=23456
  docker compose --env-file /dev/null -f compose.yaml -f compose.dev.yaml config --format json | python3 -c '
import json, sys
from urllib.parse import urlsplit

config = json.load(sys.stdin)
services = config["services"]
api_ports = services["api"].get("ports", [])
assert len(api_ports) == 1, "development API callback port missing"
assert api_ports[0]["host_ip"] == "127.0.0.1", "development API must remain loopback-only"
assert str(api_ports[0]["published"]) == "8000" and api_ports[0]["target"] == 8000, "development callback port mismatch"
web_ports = services["caddy"]["ports"]
assert len(web_ports) == 1 and web_ports[0]["host_ip"] == "127.0.0.1", "development frontend must replace public production ports"
assert str(web_ports[0]["published"]) == "5173" and web_ports[0]["target"] == 5173, "development frontend port mismatch"
for name in ("api", "worker", "scheduler"):
    service = services[name]
    assert service["user"] == "12345:23456", "development consumer cannot read owner-only Secret files"
    for field in ("DATABASE_URL", "CHECKPOINT_DATABASE_URL"):
        address = urlsplit(service["environment"][field])
        assert address.username == "ai_employee_app" and address.password is None, "development database credentials must come from Secret"
    assert "PGPASSWORD" in service["command"][-1] and "/run/secrets/app_database_password" in service["command"][-1], "development command does not load database Secret"
    mounts = {item["target"]: item for item in service["volumes"]}
    assert mounts["/app/backend/.venv"]["type"] == "volume", "host virtualenv shadows image dependencies"
    assert mounts["/app/backend/.venv"]["source"] == "backend_venv", "backend consumers must share image dependencies"
print("development runtime contracts ok")
'
  # 后端源挂载用独立卷保留镜像依赖；切换不可变镜像时必须创建新卷，不能复用旧依赖。
  python3 - <<'PY'
import json
import os
import subprocess

names = []
for project, tag in (("dev-a", "image-a"), ("dev-a", "image-b"), ("dev-b", "image-a")):
    environment = dict(os.environ, COMPOSE_PROJECT_NAME=project, APP_IMAGE_TAG=tag)
    result = subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "-f", "compose.yaml", "-f", "compose.dev.yaml", "config", "--format", "json"],
        env=environment, text=True, capture_output=True, check=True,
    )
    configuration = json.loads(result.stdout)
    names.append(configuration["volumes"]["backend_venv"]["name"])
assert len(set(names)) == 3, "development backend dependencies survive an image or project change"
print("development dependency volume isolation ok")
PY
)

# 四个真实 API 启动命令均必须关闭原始 request-target 日志；注释中的 flag 不算证据。
for api_entry in compose.yaml compose.dev.yaml justfiles/dev.just; do
  if ! grep -Eq -- '^[[:space:]]*(command:|uv run).*uvicorn.*--no-access-log' "${api_entry}"; then
    printf 'OAuth access-log boundary missing: %s\n' "${api_entry}" >&2
    exit 1
  fi
done
# E2E 由受管 Python 生命周期生成 argv；验证实际返回的启动命令，不能继续在不再
# 承载 Uvicorn 的 shell wrapper 中搜索旧文本。四入口真实 canary 另验证日志和持久审计。
uv run --project backend python - <<'PY'
import sys

sys.path.insert(0, "backend")
from tests.integration.e2e_backend import service_command

assert service_command("api") == [
    sys.executable, "-m", "uvicorn", "ai_employee.main:app", "--host", "127.0.0.1",
    "--port", "8000", "--no-access-log",
]
PY

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
check_development_runtime
check_database_operations_profiles

# 根 .env 只参与 Compose 插值，不能假定镜像内 Settings 会读取宿主文件。此矩阵实际
# 展开官方 Compose，再用每个进程拿到的环境构造 Settings；所有 Secret 只来自本轮
# 临时合成文件，配置或凭据均不回显，也不启动容器或访问供应商。
uv run --project backend python - <<'PY'
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from ai_employee.api.deps import get_connections_use_case
from ai_employee.config import Settings
from ai_employee.integrations.google.oauth import GoogleOAuthAdapter
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter

defaults = {
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_REDIRECT_URI": "http://localhost:8000/api/v1/connections/google/callback",
    "MODEL_BASE_URL": "",
    "MODEL_NAME": "",
    "MODEL_SUPPORTS_JSON_SCHEMA": "true",
    "MODEL_INPUT_COST_PER_MILLION_USD": "0",
    "MODEL_OUTPUT_COST_PER_MILLION_USD": "0",
    "MODEL_REDACTION_PATTERNS": "[]",
}
overrides = {
    "GOOGLE_CLIENT_ID": "synthetic-compose-google-client",
    "GOOGLE_REDIRECT_URI": "https://app.example.test/api/v1/connections/google/callback",
    "MODEL_BASE_URL": "https://model.example.test/v1",
    "MODEL_NAME": "synthetic-compose-model",
    "MODEL_SUPPORTS_JSON_SCHEMA": "false",
    "MODEL_INPUT_COST_PER_MILLION_USD": "1.25",
    "MODEL_OUTPUT_COST_PER_MILLION_USD": "2.5",
    "MODEL_REDACTION_PATTERNS": '["SYNTHETIC_PATTERN_[0-9]+"]',
}


def reject_io(*args: object, **kwargs: object) -> None:
    """配置组合只能绑定依赖；若开始数据库或时钟操作，立即拒绝该测试。"""
    raise AssertionError("configuration construction must not perform I/O")


with TemporaryDirectory(prefix="ai-employee-compose-config-") as directory:
    root = Path(directory)
    for filename in ("compose.yaml", "compose.dev.yaml"):
        shutil.copyfile(filename, root / filename)
    master = root / "synthetic-master"
    master.write_text(base64.urlsafe_b64encode(bytes(range(32))).decode(), encoding="ascii")
    provider_secret = root / "synthetic-provider"
    provider_secret.write_text("synthetic-compose-provider-secret", encoding="ascii")
    os.chmod(master, 0o600)
    os.chmod(provider_secret, 0o600)
    # 只清除本合同输入和 Compose 自身选择器，防止宿主环境覆盖合成根 .env；不读取真实值。
    child_environment = {
        key: value for key, value in os.environ.items()
        if key not in set(defaults) | {
            "APP_IMAGE_TAG", "APP_DOMAIN", "MICROSOFT_CLIENT_ID", "MICROSOFT_REDIRECT_URI",
            "EXTERNAL_WRITES_ENABLED", "GOOGLE_WRITES_ENABLED", "MICROSOFT_WRITES_ENABLED",
            "WRITE_TEST_ACCOUNT_ALLOWLIST", "COMPOSE_FILE", "COMPOSE_PROFILES",
        }
    }
    for mode, expected in (("defaults", defaults), ("overrides", overrides)):
        fixture = {
            "APP_IMAGE_TAG": "synthetic-config-v1",
            "APP_DOMAIN": "app.example.test",
        }
        if mode == "overrides":
            fixture.update(overrides)
            fixture.update({
                "MICROSOFT_CLIENT_ID": "synthetic-compose-microsoft-client",
                "MICROSOFT_REDIRECT_URI": "https://app.example.test/api/v1/connections/microsoft/callback",
            })
        (root / ".env").write_text(
            "".join(f"{key}={value}\n" for key, value in fixture.items()), encoding="utf-8"
        )
        for development in (False, True):
            command = ["docker", "compose", "--env-file", str(root / ".env"), "-f", str(root / "compose.yaml")]
            if development:
                command.extend(["-f", str(root / "compose.dev.yaml")])
            command.extend(["config", "--format", "json"])
            rendered = subprocess.run(command, env=child_environment, capture_output=True, check=False)
            assert rendered.returncode == 0, "synthetic Compose rendering failed"
            services = json.loads(rendered.stdout)["services"]
            for service in ("api", "worker", "scheduler"):
                environment = services[service]["environment"]
                label = f"{'development' if development else 'production'}/{mode}/{service}"
                for key, value in expected.items():
                    assert environment.get(key) == value, f"{label}: {key} missing or changed"
                assert all(environment[key] == "false" for key in (
                    "EXTERNAL_WRITES_ENABLED", "GOOGLE_WRITES_ENABLED", "MICROSOFT_WRITES_ENABLED"
                )), f"{label}: default write gate changed"
                with patch.dict(os.environ, environment, clear=True):
                    settings = Settings(
                        _env_file=None,
                        app_master_key_file=master,
                        google_client_secret_file=provider_secret,
                        microsoft_client_secret_file=provider_secret,
                    )
                assert settings.google_client_id == expected["GOOGLE_CLIENT_ID"]
                assert settings.google_redirect_uri == expected["GOOGLE_REDIRECT_URI"]
                assert settings.model_base_url == expected["MODEL_BASE_URL"]
                assert settings.model_name == expected["MODEL_NAME"]
                assert settings.model_supports_json_schema is (mode == "defaults")
                assert settings.model_redaction_patterns == json.loads(expected["MODEL_REDACTION_PATTERNS"])
                assert settings.model_input_cost_per_million_usd == float(expected["MODEL_INPUT_COST_PER_MILLION_USD"])
                assert settings.model_output_cost_per_million_usd == float(expected["MODEL_OUTPUT_COST_PER_MILLION_USD"])
                if mode == "overrides":
                    state = SimpleNamespace(
                        auth_settings=settings, auth_session_factory=reject_io,
                        connections_store_factory=reject_io, auth_clock=reject_io,
                    )
                    use_case = get_connections_use_case(SimpleNamespace(app=SimpleNamespace(state=state)))
                    assert isinstance(use_case._adapters["google"], GoogleOAuthAdapter)
                    assert isinstance(use_case._adapters["microsoft"], MicrosoftOAuthAdapter)
print("Google/model root-env configuration matrix: 12 service cases passed")
PY

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
