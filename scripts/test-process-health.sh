#!/usr/bin/env bash
# 用合成 Prometheus 文本验证 Worker/Scheduler 健康探针，不启动网络服务或读取真实指标。
set -euo pipefail

probe='scripts/check-process-health.sh'
sandbox="$(mktemp -d "${TMPDIR:-/tmp}/ai-employee-process-health.XXXXXX")"
trap 'rm -rf -- "$sandbox"' EXIT
mkdir -p "$sandbox/bin"

cat >"$sandbox/bin/curl" <<'FAKE_CURL'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "${PROCESS_HEALTH_FIXTURE:?PROCESS_HEALTH_FIXTURE is required}"
FAKE_CURL
chmod +x "$sandbox/bin/curl"

run_probe() {
  local fixture="$1"
  local process="$2"
  local port="$3"
  PATH="$sandbox/bin:$PATH" PROCESS_HEALTH_FIXTURE="$fixture" \
    bash "$probe" "$process" "$port"
}

# prometheus_client 会把刚刷新的极小心跳年龄输出为科学计数法；它仍是合法且新鲜的数值。
run_probe 'ai_employee_process_heartbeat_age_seconds{process="worker"} 3.0249993869801983e-06' worker 9101
run_probe 'ai_employee_process_heartbeat_age_seconds{process="scheduler"} 59.9' scheduler 9102

# 六十秒及以上、错误进程标签或非数值都不能被健康检查误判为可用。
! run_probe 'ai_employee_process_heartbeat_age_seconds{process="worker"} 60.0' worker 9101 >/dev/null 2>&1
! run_probe 'ai_employee_process_heartbeat_age_seconds{process="scheduler"} 1.0' worker 9101 >/dev/null 2>&1
! run_probe 'ai_employee_process_heartbeat_age_seconds{process="worker"} NaN' worker 9101 >/dev/null 2>&1

# 参数只接受固定进程名和合法 TCP 端口，避免把健康检查变成任意请求入口。
! run_probe 'ai_employee_process_heartbeat_age_seconds{process="worker"} 1.0' api 9101 >/dev/null 2>&1
! run_probe 'ai_employee_process_heartbeat_age_seconds{process="worker"} 1.0' worker '9101/path' >/dev/null 2>&1

printf '%s\n' 'process health probe contract ok'
