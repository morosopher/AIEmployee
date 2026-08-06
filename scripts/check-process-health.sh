#!/usr/bin/env bash
# 校验 Worker/Scheduler 内部 Prometheus 心跳；仅访问固定 localhost 端点，不输出指标正文。
set -euo pipefail

process="${1-}"
port="${2-}"

case "$process" in
  worker|scheduler) ;;
  *) printf '%s\n' 'process health failed: unsupported process' >&2; exit 1 ;;
esac

# 端口来自部署配置但仍做 fail-closed 校验，防止路径、选项或超范围值进入 curl URL。
[[ "$port" =~ ^[0-9]{1,5}$ ]] || {
  printf '%s\n' 'process health failed: invalid port' >&2
  exit 1
}
port_number=$((10#$port))
((port_number >= 1 && port_number <= 65535)) || {
  printf '%s\n' 'process health failed: invalid port' >&2
  exit 1
}

metric="ai_employee_process_heartbeat_age_seconds{process=\"${process}\"}"
curl --fail --silent --show-error --connect-timeout 1 --max-time 3 \
  "http://localhost:${port_number}/metrics" |
  awk -v expected="$metric" '
    $1 == expected {
      seen += 1
      value = $2
      if (value ~ /^[0-9]+([.][0-9]+)?([eE][+-]?[0-9]+)?$/ && value + 0 >= 0 && value + 0 < 60) {
        fresh = 1
      }
    }
    END { exit (seen == 1 && fresh == 1) ? 0 : 1 }
  '
