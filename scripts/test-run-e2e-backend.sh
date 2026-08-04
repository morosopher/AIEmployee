#!/usr/bin/env bash
set -euo pipefail

script='scripts/run-e2e-backend.sh'
grep -Fq 'api_pid=' "$script"
grep -Fq 'wait "$api_pid"' "$script"
! grep -Fq 'exec uv run --project backend uvicorn' "$script"
