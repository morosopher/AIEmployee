# Trusted Daily Brief MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the M1 single-user AI Employee MVP that connects read-only Gmail and Google Calendar data, generates a traceable daily office brief, and exposes every durable task step through a recoverable Vue/FastAPI experience.

**Architecture:** Use a modular monolith in one repository with separate FastAPI API, Taskiq Worker, and Scheduler processes. PostgreSQL is authoritative for identity, source data, tasks, audit events, Outbox records, and LangGraph checkpoints; Redis Streams carries task IDs and Redis Pub/Sub carries low-latency notifications.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL, Redis, Taskiq, LangGraph 1.x, Vue 3, TypeScript, Vite, Pinia, Playwright, Docker Compose, Caddy, uv, pnpm, just.

---

## Execution Rules

- Run every feature task test-first: create the failing test, observe the expected failure, add the smallest implementation, then rerun the focused and related suites.
- Keep PostgreSQL as the source of truth. Redis messages contain identifiers and transient notifications only.
- Do not request Google write scopes in M1.
- Do not introduce Qdrant, Outlook, file upload, real external write tools, long-term memory, a general Planner, or multiple agents.
- Commit after every numbered task with the exact commit message shown.
- Before each commit run the focused checks listed in that task. Before the final commit run `just ci`.

## File Map

### Root and developer workflow

- `.python-version` — pins Python 3.12.
- `.node-version` — pins Node 24 LTS.
- `.gitignore` — excludes secrets, local environments, build outputs, reports, and encrypted dumps.
- `.env.example` — documents every non-secret and secret-backed setting.
- `justfile` — imports grouped recipes and provides the default recipe list.
- `justfiles/dev.just` — bootstrap and foreground development processes.
- `justfiles/test.just` — formatting, lint, type, unit, integration, E2E, and CI recipes.
- `justfiles/db.just` — migration and guarded database reset recipes.
- `justfiles/docker.just` — Compose lifecycle, logs, status, and health recipes.
- `justfiles/ops.just` — backup and restore recipes.
- `compose.yaml` and `compose.dev.yaml` — production topology and development overrides.
- `Caddyfile` — TLS, SPA serving, API proxy, and SSE-safe proxy settings.
- `.github/workflows/ci.yml` — runs the same checks as `just ci`.

### Backend application

- `backend/pyproject.toml` — Python dependencies, uv groups, pytest, Ruff, and mypy configuration.
- `backend/alembic.ini` and `backend/migrations/` — async database migrations.
- `backend/src/ai_employee/config.py` — validated process configuration.
- `backend/src/ai_employee/main.py` — FastAPI application factory and lifespan.
- `backend/src/ai_employee/api/` — dependencies, RFC 9457 errors, REST routers, and SSE.
- `backend/src/ai_employee/domain/` — pure identity, task, email, calendar, and brief types and rules.
- `backend/src/ai_employee/application/ports/` — model, Gmail, Calendar, encryption, clock, ID, queue, and event interfaces.
- `backend/src/ai_employee/application/use_cases/` — login, task, approval, sync, conversation, brief, settings, and privacy use cases.
- `backend/src/ai_employee/agents/` — LangGraph runner, fake approval graph, and daily brief graph.
- `backend/src/ai_employee/integrations/google/` — OAuth, Gmail, and Calendar HTTP adapters.
- `backend/src/ai_employee/integrations/llm/openai_compatible.py` — configured cloud model adapter.
- `backend/src/ai_employee/infrastructure/db/` — Base, engine/session factory, ORM models, and repositories.
- `backend/src/ai_employee/infrastructure/security/` — passwords, sessions, CSRF, and AEAD encryption.
- `backend/src/ai_employee/infrastructure/queue/` — Taskiq broker, scheduler, and enqueue adapter.
- `backend/src/ai_employee/infrastructure/observability/` — structured logging, redaction, metrics, and tracing.
- `backend/src/ai_employee/workers/` — task execution, Outbox relay, due-schedule scan, retention cleanup, and privacy deletion handlers.
- `backend/src/ai_employee/prompts/daily_brief_v1.md` — versioned model instructions.

### Frontend application

- `frontend/src/api/` — cookie-aware REST client, Problem Details, and API/SSE types.
- `frontend/src/router/index.ts` — login, chat, brief, tasks, and settings routes.
- `frontend/src/stores/auth.ts` — current user and CSRF state.
- `frontend/src/stores/tasks.ts` — task snapshots and ordered durable events.
- `frontend/src/composables/useTaskEvents.ts` — EventSource lifecycle and reconnect state.
- `frontend/src/components/` — MarkdownMessage, TaskTimeline, ApprovalCard, SourceLink, BriefView, and shell components.
- `frontend/src/pages/` — LoginPage, ChatPage, TodayBriefPage, TasksPage, ConnectionsPage, and SettingsPage.

### Tests and operations

- `backend/tests/unit/` — pure domain and use-case tests.
- `backend/tests/integration/` — PostgreSQL, Redis, Taskiq, LangGraph, OAuth, sync, and SSE tests.
- `backend/tests/contract/fixtures/` — sanitized Gmail and Calendar JSON.
- `frontend/src/feature-name.spec.ts` — colocated component and store tests.
- `frontend/e2e/` — Playwright end-to-end tests.
- `scripts/backup-postgres.sh` and `scripts/restore-postgres.sh` — encrypted backup and guarded restore.
- `scripts/run-e2e-backend.sh` — isolated test database/Redis reset plus API and Worker lifecycle.
- `scripts/init-db-roles.sh` — idempotent application/retention database privilege setup.
- `ops/observability/` — optional Prometheus and Grafana provisioning.
- `docs/operations.md` — deployment, recovery, token rotation, and soak-test runbook.

### Task 1: Scaffold the repository and unified just workflow

**Files:**
- Create: `.python-version`
- Create: `.node-version`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `justfile`
- Create: `justfiles/dev.just`
- Create: `justfiles/test.just`
- Create: `justfiles/db.just`
- Create: `justfiles/docker.just`
- Create: `justfiles/ops.just`
- Create: `backend/pyproject.toml`
- Create: `backend/uv.lock`
- Create: `frontend/package.json`
- Create: `frontend/pnpm-workspace.yaml`
- Create: `scripts/test-tooling.sh`

- [ ] **Step 1: Write the failing tooling contract**

Create `scripts/test-tooling.sh`:

~~~bash
#!/usr/bin/env bash
set -euo pipefail

required_recipes=(
  doctor bootstrap dev infra-up infra-down web api worker scheduler
  test test-backend test-frontend test-integration test-e2e
  lint format typecheck check ci db-upgrade db-revision db-reset
  logs ps health backup restore
)

summary="$(just --summary)"
for recipe in "${required_recipes[@]}"; do
  grep -Eq "(^| )${recipe}( |$)" <<<"${summary}"
done
~~~

After the recipe-name check, the script must create an isolated `mktemp -d` sandbox, copy the root
justfile and imported modules into it, and install fake `docker`, `uv`, and
`scripts/restore-postgres.sh` commands that only append their arguments to a sandbox log. A trap
must remove the sandbox. The behavior contract must verify all of the following without touching
the real repository scripts, Docker daemon, network, or databases:

- revision names, log service names, and restore paths containing spaces, quotes, and semicolons
  arrive as one unchanged argument and cannot create an injection sentinel;
- `db-reset` and `restore` reject missing or unknown `APP_ENV` values and allow only explicit
  `development` or `test`;
- `db-reset` rejects missing/unsafe database configuration, identifiers longer than PostgreSQL's
  63-byte limit, system database names, and test-environment database names without `_test`;
- the raw `DATABASE_URL` must begin with the exact lowercase `postgresql://` or
  `postgresql+asyncpg://` prefix, with no leading whitespace or control character, and must contain
  no TAB, LF, or CR anywhere because `urlsplit` silently removes them while SQLAlchemy preserves
  them; the parsed URL must then use the configured username, a loopback host, an explicit `5432`
  port, no query or fragment, and a database path exactly matching `POSTGRES_DB`, all before a fake
  destructive call;
- successful `db-reset` output includes the verified endpoint, database name, and user without a
  password or complete URL, while fake `uv` proves `PGHOST`, `PGHOSTADDR`, `PGPORT`, `PGDATABASE`,
  `PGUSER`, `PGSERVICE`, and `PGSERVICEFILE` are absent from the migration process;
- `just --show db-reset` and `just --show restore` retain `[confirm]`, while `just --yes` still
  cannot bypass the recipe's own exact target confirmation;
- `doctor` checks the `python3` runtime used by the local URL guard;
- `logs` preserves the no-service form and uses `--` plus one quoted service argument when present.

- [ ] **Step 2: Run the tooling contract and observe the expected failure**

Run: `bash scripts/test-tooling.sh`

Expected: FAIL because the root justfile does not exist.

After the root justfile exists, the same contract must also fail against the original unsafe
template-interpolation recipes and pass only after the sandbox behavior checks are implemented.

- [ ] **Step 3: Add the root imports and recipe groups**

Create `justfile`:

~~~just
set dotenv-load
set positional-arguments

import 'justfiles/dev.just'
import 'justfiles/test.just'
import 'justfiles/db.just'
import 'justfiles/docker.just'
import 'justfiles/ops.just'

default:
    @just --list --list-heading 'Available recipes:\n'
~~~

Create `justfiles/dev.just`:

~~~just
[group('development')]
doctor:
    @command -v uv
    @command -v pnpm
    @command -v docker
    @command -v python3
    @docker compose version

[group('development')]
bootstrap:
    uv sync --project backend --all-groups
    pnpm --dir frontend install

[group('development')]
dev:
    docker compose -f compose.yaml -f compose.dev.yaml up --build

[group('development')]
web:
    pnpm --dir frontend dev

[group('development')]
api:
    uv run --project backend uvicorn ai_employee.main:app --reload --host 0.0.0.0 --port 8000

[group('development')]
worker:
    uv run --project backend taskiq worker --ack-type when_executed ai_employee.infrastructure.queue.broker:broker

[group('development')]
scheduler:
    uv run --project backend taskiq scheduler ai_employee.infrastructure.queue.scheduler:scheduler
~~~

Create `justfiles/test.just`:

~~~just
[group('quality')]
test: test-backend test-frontend

[group('quality')]
test-backend:
    uv run --project backend pytest backend/tests/unit -q

[group('quality')]
test-frontend:
    pnpm --dir frontend test:unit --run

[group('quality')]
test-integration:
    uv run --project backend pytest backend/tests/integration backend/tests/contract -q

[group('quality')]
test-e2e:
    pnpm --dir frontend test:e2e

[group('quality')]
lint:
    uv run --project backend ruff check backend
    pnpm --dir frontend lint

[group('quality')]
format:
    uv run --project backend ruff format backend
    pnpm --dir frontend format

[group('quality')]
typecheck:
    uv run --project backend mypy backend/src
    pnpm --dir frontend type-check

[group('quality')]
check: lint typecheck test

[group('quality')]
ci: check test-integration
    pnpm --dir frontend build
    pnpm --dir frontend test:e2e
~~~

Create `justfiles/db.just`:

The destructive recipes below are intentionally fail-closed: they require an explicit development
or test environment, enforce ASCII PostgreSQL identifiers of at most 63 UTF-8 bytes, require test
database names to end in `_test`, and parse `DATABASE_URL` locally with `urllib.parse`. Before
parsing, the raw value must begin with the exact lowercase `postgresql://` or
`postgresql+asyncpg://` prefix so mixed-case schemes and leading characters silently stripped by
`urlsplit` are rejected before any destructive call. It must also contain no TAB, LF, or CR at any
position because `urlsplit` silently removes those characters while SQLAlchemy preserves them.
Only the configured user on `localhost`, `127.0.0.1`, or `::1` with an explicit `5432` port, no
query or fragment, and an exact database path is accepted. The validated endpoint, database name,
and user are shown before exact confirmation without printing the password or complete URL.
Migration commands remove libpq target/service override variables so Alembic consumes only
`DATABASE_URL`; a second exact confirmation remains mandatory even when `just --yes` bypasses the
outer `[confirm]` prompt.

~~~just
[group('database')]
db-upgrade:
    # 清除可覆盖连接目标或 libpq service 的环境变量，保证迁移只消费应用显式提供的 DATABASE_URL。
    env -u PGHOST -u PGHOSTADDR -u PGPORT -u PGDATABASE -u PGUSER -u PGSERVICE -u PGSERVICEFILE uv run --project backend alembic -c backend/alembic.ini upgrade head

[group('database')]
db-revision name:
    uv run --project backend alembic -c backend/alembic.ini revision --autogenerate -m "$1"

[confirm]
[group('database')]
db-reset:
    #!/usr/bin/env bash
    set -euo pipefail

    # just 的 [confirm] 只能防止误触；脚本仍需独立验证环境、精确目标和人工输入。
    fail() {
      printf 'db-reset refused: %s\n' "$1" >&2
      exit 1
    }

    app_env="${APP_ENV-}"
    case "${app_env}" in
      development|test) ;;
      *) fail 'APP_ENV must be explicitly set to development or test' ;;
    esac

    database_name="${POSTGRES_DB-}"
    database_user="${POSTGRES_USER-}"
    database_url="${DATABASE_URL-}"
    [[ -n "${database_name}" ]] || fail 'POSTGRES_DB is required'
    [[ -n "${database_user}" ]] || fail 'POSTGRES_USER is required'
    [[ -n "${database_url}" ]] || fail 'DATABASE_URL is required'

    # 使用 Python 标准库解析 URL，避免 Bash 字符串切片漏掉 username、host 或 port。
    # 敏感 URL 通过临时环境变量传入，不作为参数或错误消息输出；脚本只打印静态拒绝原因。
    AI_EMPLOYEE_DB_RESET_ENV="${app_env}" \
    AI_EMPLOYEE_DB_RESET_NAME="${database_name}" \
    AI_EMPLOYEE_DB_RESET_USER="${database_user}" \
    AI_EMPLOYEE_DB_RESET_URL="${database_url}" \
    python3 - <<'PY'
    import os
    import re
    import sys
    from typing import NoReturn
    from urllib.parse import urlsplit


    def reject(reason: str) -> NoReturn:
        """以不泄露 URL、密码或完整标识符的方式终止数据库目标校验。"""

        print(f"db-reset refused: {reason}", file=sys.stderr)
        raise SystemExit(1)


    app_env = os.environ["AI_EMPLOYEE_DB_RESET_ENV"]
    database_name = os.environ["AI_EMPLOYEE_DB_RESET_NAME"]
    database_user = os.environ["AI_EMPLOYEE_DB_RESET_USER"]
    database_url = os.environ["AI_EMPLOYEE_DB_RESET_URL"]


    def validate_identifier(label: str, value: str) -> None:
        """验证 PostgreSQL 标识符的 ASCII 语法和 63 字节上限。"""

        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
            reject(f"{label} must match [A-Za-z_][A-Za-z0-9_]*")
        if len(value.encode("utf-8")) > 63:
            reject(f"{label} must be at most 63 UTF-8 bytes")


    validate_identifier("POSTGRES_DB", database_name)
    validate_identifier("POSTGRES_USER", database_user)
    if database_name.casefold() in {"postgres", "template0", "template1"}:
        reject("system databases cannot be reset")
    if app_env == "test" and not database_name.endswith("_test"):
        reject("test database name must end with _test")

    # urlsplit 会规范化 scheme 大小写，并从任意位置静默删除 TAB、LF、CR；先校验原始
    # 字符串，确保安全 guard 与后续 SQLAlchemy 对 DATABASE_URL 的接受范围完全一致。
    if not database_url.startswith(("postgresql://", "postgresql+asyncpg://")):
        reject("DATABASE_URL must begin with an exact lowercase PostgreSQL scheme")
    if any(character in database_url for character in "\t\n\r"):
        reject("DATABASE_URL must not contain TAB, LF, or CR characters")

    try:
        parsed_url = urlsplit(database_url)
        url_scheme = parsed_url.scheme
        url_username = parsed_url.username
        url_hostname = parsed_url.hostname
        url_port = parsed_url.port
    except ValueError:
        # urlsplit 会对非法 IPv6 或 port 抛出 ValueError；不要把原始 URL 带入错误。
        reject("DATABASE_URL is malformed")

    if url_scheme not in {"postgresql", "postgresql+asyncpg"}:
        reject("DATABASE_URL must use a PostgreSQL scheme")
    if url_username != database_user:
        reject("DATABASE_URL username must exactly match POSTGRES_USER")
    if url_hostname is None or url_hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        reject("DATABASE_URL host must be localhost, 127.0.0.1, or ::1")
    if url_port != 5432:
        reject("DATABASE_URL port must be explicitly set to 5432")
    # SQLAlchemy 与底层驱动可能把 query 解释为 authority/path 的覆盖项；破坏性操作不接受任何 query。
    if "?" in database_url:
        reject("DATABASE_URL must not contain a query")
    if "#" in database_url:
        reject("DATABASE_URL must not contain a fragment")
    if parsed_url.path != f"/{database_name}":
        reject("DATABASE_URL database must exactly match POSTGRES_DB")

    # 仅输出完成全部校验后的规范化端点，不回显密码、query、fragment 或完整 URL。
    normalized_host = f"[{url_hostname.lower()}]" if ":" in url_hostname else url_hostname.lower()
    print(f"Verified endpoint: {normalized_host}:{url_port}", file=sys.stderr)
    PY

    printf 'APP_ENV=%s\nCompose service: postgres\nDatabase target: %s\nDatabase user: %s\n' \
      "${app_env}" "${database_name}" "${database_user}" >&2
    printf 'Type the exact database name to confirm: ' >&2
    confirmation=''
    IFS= read -r confirmation || fail 'confirmation input is required'
    [[ "${confirmation}" == "${database_name}" ]] || fail 'confirmation did not match database target'

    # 只有全部验证与精确确认完成后才允许 Fake/真实命令接触目标；set -e 保证迁移严格后置。
    docker compose exec -T postgres dropdb --if-exists -U "${database_user}" "${database_name}"
    docker compose exec -T postgres createdb -U "${database_user}" "${database_name}"
    just db-upgrade
~~~

Create `justfiles/docker.just`:

~~~just
[group('docker')]
infra-up:
    docker compose up -d postgres redis

[group('docker')]
infra-down:
    docker compose stop postgres redis

[group('docker')]
logs service='':
    #!/usr/bin/env bash
    set -euo pipefail

    service="${1-}"
    if [[ -z "${service}" ]]; then
      docker compose logs -f
    else
      # -- 将用户提供的 service 与 Compose 选项隔离，双引号确保它始终是单一参数。
      docker compose logs -f -- "${service}"
    fi

[group('docker')]
ps:
    docker compose ps

[group('docker')]
health:
    curl --fail --silent http://localhost:8000/api/v1/system/health
~~~

Create `justfiles/ops.just`:

~~~just
[group('operations')]
backup:
    scripts/backup-postgres.sh

[confirm]
[group('operations')]
restore file:
    #!/usr/bin/env bash
    set -euo pipefail

    fail() {
      printf 'restore refused: %s\n' "$1" >&2
      exit 1
    }

    app_env="${APP_ENV-}"
    case "${app_env}" in
      development|test) ;;
      *) fail 'APP_ENV must be explicitly set to development or test' ;;
    esac

    restore_file="${1-}"
    [[ -n "${restore_file}" ]] || fail 'restore file must not be empty'

    printf 'APP_ENV=%s\nCompose service: postgres\nRestore target: %s\n' \
      "${app_env}" "${restore_file}" >&2
    printf 'Type the exact restore file path to confirm: ' >&2
    confirmation=''
    IFS= read -r confirmation || fail 'confirmation input is required'
    [[ "${confirmation}" == "${restore_file}" ]] || fail 'confirmation did not match restore target'

    # Task 19 才提供真实恢复脚本；此处只冻结安全调用协议，不提前实现恢复逻辑。
    scripts/restore-postgres.sh "${restore_file}"
~~~

- [ ] **Step 4: Add reproducible package manifests**

Create `.python-version`:

~~~text
3.12
~~~

Create `.node-version`:

~~~text
24
~~~

Create `.gitignore`:

~~~gitignore
.env
.env.*
!.env.example
.venv/
backend/.venv/
**/__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
frontend/node_modules/
frontend/dist/
frontend/coverage/
frontend/playwright-report/
frontend/test-results/
secrets/
*.pem
*.key
*.dump
*.enc
~~~

Create `backend/pyproject.toml`:

Security note: the minimum versions of `cryptography` and `pytest` are raised to address GHSA-537c-gmf6-5ccf and PYSEC-2026-1845.

~~~toml
[project]
name = "ai-employee"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "alembic>=1.16,<2",
  "argon2-cffi>=25,<26",
  "asyncpg>=0.30,<1",
  "beautifulsoup4>=4.13,<5",
  "cryptography>=48.0.1,<49",
  "fastapi>=0.118,<1",
  "httpx>=0.28,<1",
  "langgraph>=1.0,<2",
  "langgraph-checkpoint-postgres>=2,<4",
  "prometheus-client>=0.22,<1",
  "pydantic-settings>=2.10,<3",
  "redis>=6,<7",
  "sse-starlette>=3,<4",
  "sqlalchemy[asyncio]>=2.0,<3",
  "structlog>=25,<26",
  "taskiq>=0.11,<1",
  "taskiq-redis>=1,<2",
  "uvicorn[standard]>=0.35,<1"
]

[dependency-groups]
dev = [
  "mypy>=1.17,<2",
  "pytest>=9.0.3,<10",
  "pytest-asyncio>=1.1,<2",
  "respx>=0.22,<1",
  "ruff>=0.12,<1"
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ai_employee"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.mypy]
python_version = "3.12"
strict = true
plugins = ["pydantic.mypy"]
~~~

Create `frontend/package.json`:

~~~json
{
  "name": "ai-employee-web",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc -b && vite build",
    "test:unit": "vitest",
    "test:e2e": "playwright test",
    "lint": "eslint .",
    "format": "prettier --write src",
    "type-check": "vue-tsc -b"
  }
}
~~~

Install frontend packages:

~~~bash
pnpm --dir frontend add vue pinia vue-router markdown-it dompurify
pnpm --dir frontend add -D @eslint/js @playwright/test @types/markdown-it @vitejs/plugin-vue @vitest/coverage-v8 @vue/test-utils eslint eslint-plugin-vue globals jsdom prettier typescript typescript-eslint vite vitest vue-eslint-parser vue-tsc
~~~

Use pnpm 11.18.0 for the lockfile. Keep the generated manifest's compatible direct ranges for
TypeScript (`^5.9.3`), Vite (`^7.3.6`), and Vue Router (`^4.6.4`) so the lint/build toolchain has
no unresolved peer dependencies.

Create `frontend/pnpm-workspace.yaml` with an explicit project-level build allowlist. Only `esbuild`
may run its install script; all other dependency build scripts remain denied by default.

Create `.env.example`:

~~~dotenv
APP_ENV=development
APP_TEST_MODE=false
APP_BASE_URL=http://localhost:5173
API_BASE_URL=http://localhost:8000
DATABASE_URL=postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee
CHECKPOINT_DATABASE_URL=postgresql://ai_employee:ai_employee@localhost:5432/ai_employee
TEST_DATABASE_URL=postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee_test
TEST_REDIS_URL=redis://localhost:6379/15
POSTGRES_USER=ai_employee
POSTGRES_PASSWORD=ai_employee
POSTGRES_DB=ai_employee
REDIS_URL=redis://localhost:6379/0
TASK_TIMEOUT_SECONDS=900
TASK_STEP_TIMEOUT_SECONDS=300
SESSION_COOKIE_NAME=ai_employee_session
SESSION_TTL_SECONDS=604800
APP_MASTER_KEY_FILE=/run/secrets/app_master_key
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET_FILE=/run/secrets/google_client_secret
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/connections/google/callback
MODEL_BASE_URL=
MODEL_API_KEY_FILE=/run/secrets/model_api_key
MODEL_NAME=
MODEL_SUPPORTS_JSON_SCHEMA=true
MODEL_INPUT_COST_PER_MILLION_USD=0
MODEL_OUTPUT_COST_PER_MILLION_USD=0
MODEL_REDACTION_PATTERNS=[]
WORK_EMAIL_DOMAINS=[]
DEFAULT_TIMEZONE=Asia/Shanghai
DEFAULT_BRIEF_TIME=08:00
~~~

- [ ] **Step 5: Install dependencies and rerun the tooling contract**

Run:

~~~bash
chmod +x scripts/test-tooling.sh
uv sync --project backend --all-groups
pnpm --dir frontend install
bash scripts/test-tooling.sh
~~~

Expected: PASS; every required recipe is present and all sandbox behavior/security checks pass.

- [ ] **Step 6: Commit the scaffold**

~~~bash
git add .python-version .node-version .gitignore .env.example justfile justfiles backend/pyproject.toml backend/uv.lock frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml scripts/test-tooling.sh docs/superpowers/plans/2026-07-30-trusted-daily-brief-mvp.md
git commit -m "chore: scaffold project tooling"
~~~

### Task 2: Create the FastAPI application, configuration, and system endpoints

**Files:**
- Create: `backend/src/ai_employee/__init__.py`
- Create: `backend/src/ai_employee/config.py`
- Create: `backend/src/ai_employee/main.py`
- Create: `backend/src/ai_employee/api/__init__.py`
- Create: `backend/src/ai_employee/api/routers/__init__.py`
- Create: `backend/src/ai_employee/api/routers/system.py`
- Create: `backend/tests/unit/api/test_system.py`

- [ ] **Step 1: Write failing health and readiness tests**

Create `backend/tests/unit/api/test_system.py`:

~~~python
import pytest
from fastapi.testclient import TestClient

from ai_employee.config import Settings
from ai_employee.main import create_app


def test_health_returns_process_status() -> None:
    client = TestClient(create_app())
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}


def test_readiness_reports_dependency_probe_result() -> None:
    app = create_app(readiness_probe=lambda: {"postgres": True, "redis": False})
    client = TestClient(app)
    response = client.get("/api/v1/system/readiness")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "dependencies": {"postgres": True, "redis": False},
    }


def test_production_rejects_test_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_TEST_MODE", "true")
    with pytest.raises(ValueError, match="APP_TEST_MODE"):
        Settings()
~~~

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `uv run --project backend pytest backend/tests/unit/api/test_system.py -q`

Expected: FAIL with ModuleNotFoundError for `ai_employee.main`.

- [ ] **Step 3: Implement validated settings**

Create `backend/src/ai_employee/config.py`:

~~~python
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_test_mode: bool = False
    app_base_url: str = "http://localhost:5173"
    database_url: str = "postgresql+asyncpg://ai_employee:ai_employee@localhost:5432/ai_employee"
    checkpoint_database_url: str = "postgresql://ai_employee:ai_employee@localhost:5432/ai_employee"
    redis_url: str = "redis://localhost:6379/0"
    task_timeout_seconds: int = Field(default=900, ge=1)
    task_step_timeout_seconds: int = Field(default=300, ge=1)
    session_cookie_name: str = "ai_employee_session"
    session_ttl_seconds: int = 604800
    app_master_key_file: Path = Path("/run/secrets/app_master_key")
    google_client_id: str = ""
    google_client_secret_file: Path = Path("/run/secrets/google_client_secret")
    google_redirect_uri: str = ""
    model_base_url: str = ""
    model_api_key_file: Path = Path("/run/secrets/model_api_key")
    model_name: str = ""
    model_supports_json_schema: bool = True
    model_input_cost_per_million_usd: float = Field(default=0, ge=0)
    model_output_cost_per_million_usd: float = Field(default=0, ge=0)
    model_redaction_patterns: list[str] = Field(default_factory=list)
    work_email_domains: list[str] = Field(default_factory=list)
    default_timezone: str = "Asia/Shanghai"
    default_brief_time: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @field_validator("default_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def validate_cross_field_settings(self) -> "Settings":
        if self.app_env == "production" and self.app_test_mode:
            raise ValueError("APP_TEST_MODE cannot be enabled in production")
        if self.task_step_timeout_seconds > self.task_timeout_seconds:
            raise ValueError("TASK_STEP_TIMEOUT_SECONDS cannot exceed TASK_TIMEOUT_SECONDS")
        return self

    def read_secret_file(self, path: Path) -> SecretStr:
        return SecretStr(path.read_text(encoding="utf-8").strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
~~~

- [ ] **Step 4: Implement the application factory and system router**

Create `backend/src/ai_employee/api/routers/system.py`:

~~~python
from collections.abc import Callable

from fastapi import APIRouter, Response, status


def build_system_router(readiness_probe: Callable[[], dict[str, bool]]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/system", tags=["system"])

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "api"}

    @router.get("/readiness")
    def readiness(response: Response) -> dict[str, object]:
        dependencies = readiness_probe()
        ready = all(dependencies.values())
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if ready else "not_ready",
            "dependencies": dependencies,
        }

    return router
~~~

Create `backend/src/ai_employee/main.py`:

~~~python
from collections.abc import Callable

from fastapi import FastAPI

from ai_employee.api.routers.system import build_system_router


def create_app(
    readiness_probe: Callable[[], dict[str, bool]] | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Employee API", version="0.1.0")
    probe = readiness_probe or (lambda: {"postgres": True, "redis": True})
    app.include_router(build_system_router(probe))
    return app


app = create_app()
~~~

Create the empty package marker files listed above.

- [ ] **Step 5: Run focused and static checks**

Run:

~~~bash
uv run --project backend pytest backend/tests/unit/api/test_system.py -q
uv run --project backend ruff format backend
uv run --project backend ruff check backend
uv run --project backend mypy backend/src
~~~

Expected: 3 tests pass and static checks exit 0.

- [ ] **Step 6: Commit the API skeleton**

~~~bash
git add backend/src backend/tests/unit/api
git commit -m "feat: add API application skeleton"
~~~

### Task 3: Add async SQLAlchemy and Alembic identity persistence

**Files:**
- Create: `backend/src/ai_employee/infrastructure/db/base.py`
- Create: `backend/src/ai_employee/infrastructure/db/session.py`
- Create: `backend/src/ai_employee/infrastructure/db/models/identity.py`
- Create: `backend/src/ai_employee/infrastructure/db/models/__init__.py`
- Create: `backend/alembic.ini`
- Create: `backend/migrations/env.py`
- Create: `backend/migrations/script.py.mako`
- Create: `backend/migrations/versions/20260730_0001_identity.py`
- Create: `backend/tests/integration/conftest.py`
- Create: `backend/tests/integration/db/test_identity_models.py`

- [ ] **Step 1: Write the failing identity persistence test**

Create `backend/tests/integration/db/test_identity_models.py`:

~~~python
from datetime import time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.session import build_session_factory


@pytest.mark.asyncio
async def test_user_round_trip(database_url: str) -> None:
    session_factory = build_session_factory(database_url)
    async with session_factory.begin() as session:
        session.add(
            UserModel(
                email="owner@example.com",
                display_name="Owner",
                timezone="Asia/Shanghai",
                locale="zh-CN",
                brief_time=time(8, 0),
            )
        )

    async with session_factory() as session:
        saved = await session.scalar(select(UserModel))
        assert saved is not None
        assert saved.email == "owner@example.com"
        assert ZoneInfo(saved.timezone).key == "Asia/Shanghai"
~~~

- [ ] **Step 2: Run the test and verify the missing persistence layer**

Run: `uv run --project backend pytest backend/tests/integration/db/test_identity_models.py -q`

Expected: FAIL because database modules and the `database_url` fixture do not exist.

- [ ] **Step 3: Implement the async Base, engine, and session factory**

Create `backend/src/ai_employee/infrastructure/db/base.py`:

~~~python
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
~~~

Create `backend/src/ai_employee/infrastructure/db/session.py`:

~~~python
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def build_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def build_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(build_engine(database_url), expire_on_commit=False)
~~~

- [ ] **Step 4: Add identity ORM models and the first migration**

Create `backend/src/ai_employee/infrastructure/db/models/identity.py`:

~~~python
from datetime import datetime, time
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, Time
from sqlalchemy.orm import Mapped, mapped_column

from ai_employee.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")
    brief_time: Mapped[time] = mapped_column(Time, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)


class UserSessionModel(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user_expires", "user_id", "expires_at"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    csrf_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
~~~

Create migration `20260730_0001_identity.py` with revision `20260730_0001` and SQLAlchemy operations matching both models exactly. Configure `migrations/env.py` with `async_engine_from_config`, `connection.run_sync`, and `Base.metadata` as `target_metadata`.

- [ ] **Step 5: Add integration fixtures and run migrations**

Create `backend/tests/integration/conftest.py` to read `TEST_DATABASE_URL`, run `alembic upgrade head` once per session, truncate application tables between tests, and expose `database_url`.

Run:

~~~bash
just infra-up
uv run --project backend alembic -c backend/alembic.ini upgrade head
uv run --project backend pytest backend/tests/integration/db/test_identity_models.py -q
~~~

Expected: 1 test passes.

- [ ] **Step 6: Commit database foundations**

~~~bash
git add backend/alembic.ini backend/migrations backend/src/ai_employee/infrastructure backend/tests/integration
git commit -m "feat: add async database foundations"
~~~

### Task 4: Implement single-admin authentication, sessions, and CSRF

**Files:**
- Modify: `justfiles/db.just`
- Modify: `scripts/test-tooling.sh`
- Create: `backend/src/ai_employee/domain/identity.py`
- Create: `backend/src/ai_employee/infrastructure/security/passwords.py`
- Create: `backend/src/ai_employee/infrastructure/security/tokens.py`
- Create: `backend/src/ai_employee/application/use_cases/auth.py`
- Create: `backend/src/ai_employee/api/deps.py`
- Create: `backend/src/ai_employee/api/routers/auth.py`
- Create: `backend/src/ai_employee/cli/create_admin.py`
- Create: `backend/tests/unit/security/test_passwords.py`
- Create: `backend/tests/integration/api/test_auth.py`
- Create: `backend/tests/integration/cli/test_create_admin.py`

- [ ] **Step 1: Write failing password and login tests**

Create `backend/tests/unit/security/test_passwords.py`:

~~~python
from ai_employee.infrastructure.security.passwords import PasswordHasher


def test_password_hash_is_salted_and_verifiable() -> None:
    hasher = PasswordHasher()
    first = hasher.hash("correct horse battery staple")
    second = hasher.hash("correct horse battery staple")
    assert first != second
    assert hasher.verify(first, "correct horse battery staple")
    assert not hasher.verify(first, "wrong")
~~~

Create `backend/tests/integration/api/test_auth.py` with tests that seed one active user and assert:

- `POST /api/v1/auth/login` sets session and CSRF cookies;
- `GET /api/v1/auth/me` returns that user;
- `POST /api/v1/auth/logout` without `X-CSRF-Token` returns 403;
- logout with the matching header revokes the session and clears both cookies;
- `GET /api/v1/auth/sessions` lists active sessions without token hashes;
- `DELETE /api/v1/auth/sessions/{session_id}` revokes an owned session and cannot revoke another user's session.

Create a CLI test that invokes admin creation with an email and password file, verifies one active
Argon2id-hashed administrator exists, verifies a second invocation refuses to overwrite it, and
verifies `--if-absent` is an idempotent no-op only for the same normalized email.

- [ ] **Step 2: Run the tests and verify they fail**

Run:

~~~bash
uv run --project backend pytest backend/tests/unit/security/test_passwords.py backend/tests/integration/api/test_auth.py backend/tests/integration/cli/test_create_admin.py -q
~~~

Expected: FAIL because password, auth use case, CLI, dependencies, and routes are missing.

- [ ] **Step 3: Implement password and token primitives**

Create `backend/src/ai_employee/infrastructure/security/passwords.py`:

~~~python
from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2.exceptions import VerifyMismatchError


class PasswordHasher:
    def __init__(self) -> None:
        self._hasher = Argon2PasswordHasher()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except VerifyMismatchError:
            return False
~~~

Create `backend/src/ai_employee/infrastructure/security/tokens.py`:

~~~python
import hashlib
import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()
~~~

- [ ] **Step 4: Implement login, current-user, logout, and CSRF enforcement**

The auth use case must:

- normalize email and look up the active user;
- verify the Argon2id hash;
- create raw session and CSRF tokens;
- persist only SHA-256 hashes;
- use UTC timestamps and the configured TTL;
- reject expired or revoked sessions;
- update `last_seen_at` at most once every five minutes.

The router must set:

- session cookie: HttpOnly, Secure outside development, SameSite=Lax, Path=/;
- CSRF cookie: readable by the frontend, Secure outside development, SameSite=Strict, Path=/.

Unsafe authenticated methods require the `X-CSRF-Token` header to equal the CSRF cookie and stored hash.

Add session-list and session-revoke endpoints. Revoking the current session also clears its cookies.

Add `just create-admin email password_file`, backed by
`python -m ai_employee.cli.create_admin`. The command reads the password only from the explicit
file, refuses an empty password, never prints it, and refuses to replace an existing administrator.
Extend `scripts/test-tooling.sh` to assert the recipe is listed.
`--if-absent` may return success for an already-created administrator with the same email, but it
must never change the existing password hash.
New administrators use configured default timezone/brief time, locale `zh-CN`, and retention
defaults 30/180/365 once those columns are introduced by Task 16.

- [ ] **Step 5: Register the router and rerun checks**

Run:

~~~bash
uv run --project backend pytest backend/tests/unit/security/test_passwords.py backend/tests/integration/api/test_auth.py backend/tests/integration/cli/test_create_admin.py -q
uv run --project backend ruff check backend
uv run --project backend mypy backend/src
~~~

Expected: focused tests pass and static checks exit 0.

- [ ] **Step 6: Commit authentication**

~~~bash
git add justfiles/db.just scripts/test-tooling.sh backend/src/ai_employee/domain/identity.py backend/src/ai_employee/infrastructure/security backend/src/ai_employee/application/use_cases/auth.py backend/src/ai_employee/api backend/src/ai_employee/cli/create_admin.py backend/tests
git commit -m "feat: add single-admin authentication"
~~~

### Task 5: Define domain errors and the pure task, step, and approval state machines

**Files:**
- Create: `backend/src/ai_employee/domain/errors.py`
- Create: `backend/src/ai_employee/domain/tasks.py`
- Create: `backend/tests/unit/domain/test_errors.py`
- Create: `backend/tests/unit/domain/test_task_state_machine.py`
- Create: `backend/tests/unit/domain/test_approval_policy.py`

- [ ] **Step 1: Write failing transition tests**

Create `backend/tests/unit/domain/test_errors.py` to assert a
`TransientProviderError(error_code="provider_rate_limited", message="Try again later", retry_after=30)`
preserves both machine-readable fields and that every public domain error exposes a stable
`error_code`.

Create `backend/tests/unit/domain/test_task_state_machine.py`:

~~~python
import pytest

from ai_employee.domain.tasks import InvalidTaskTransition, TaskStatus, transition_task


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.CREATED, TaskStatus.QUEUED),
        (TaskStatus.QUEUED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL),
        (TaskStatus.RUNNING, TaskStatus.RETRY_SCHEDULED),
        (TaskStatus.RUNNING, TaskStatus.SUCCEEDED),
        (TaskStatus.RUNNING, TaskStatus.FAILED),
        (TaskStatus.WAITING_APPROVAL, TaskStatus.QUEUED),
        (TaskStatus.RETRY_SCHEDULED, TaskStatus.QUEUED),
    ],
)
def test_allowed_task_transitions(current: TaskStatus, target: TaskStatus) -> None:
    assert transition_task(current, target) is target


def test_terminal_task_cannot_restart() -> None:
    with pytest.raises(InvalidTaskTransition):
        transition_task(TaskStatus.SUCCEEDED, TaskStatus.RUNNING)
~~~

Create `backend/tests/unit/domain/test_approval_policy.py`:

~~~python
from ai_employee.domain.tasks import ApprovalProposal, ApprovalStatus


def test_approval_hash_changes_when_payload_changes() -> None:
    first = ApprovalProposal.create("calendar.create", {"title": "A"})
    second = ApprovalProposal.create("calendar.create", {"title": "B"})
    assert first.payload_hash != second.payload_hash
    assert first.status is ApprovalStatus.PENDING


def test_payload_is_canonical_before_hashing() -> None:
    first = ApprovalProposal.create("email.send", {"to": "a@example.com", "subject": "Hi"})
    second = ApprovalProposal.create("email.send", {"subject": "Hi", "to": "a@example.com"})
    assert first.payload_hash == second.payload_hash
~~~

- [ ] **Step 2: Run the tests and verify the missing domain**

Run: `uv run --project backend pytest backend/tests/unit/domain/test_errors.py backend/tests/unit/domain/test_task_state_machine.py backend/tests/unit/domain/test_approval_policy.py -q`

Expected: FAIL because `ai_employee.domain.errors` and `ai_employee.domain.tasks` do not exist.

- [ ] **Step 3: Implement immutable task and approval types**

Create `backend/src/ai_employee/domain/errors.py`:

~~~python
from typing import Any


class DomainError(Exception):
    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.metadata = metadata or {}


class UserActionRequiredError(DomainError):
    pass


class TransientProviderError(DomainError):
    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        retry_after: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error_code=error_code, message=message, metadata=metadata)
        self.retry_after = retry_after


class PermanentProviderError(DomainError):
    pass


class ModelOutputError(DomainError):
    pass


class StateConflictError(DomainError):
    pass


class InternalInvariantError(DomainError):
    pass
~~~

Provider/debug metadata must not contain source content.

Create `backend/src/ai_employee/domain/tasks.py`:

~~~python
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ai_employee.domain.errors import StateConflictError


class TaskStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class InvalidTaskTransition(StateConflictError):
    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        super().__init__(
            error_code="invalid_task_transition",
            message=f"{current} cannot transition to {target}",
        )


ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.RETRY_SCHEDULED,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_APPROVAL: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.RETRY_SCHEDULED: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


def transition_task(current: TaskStatus, target: TaskStatus) -> TaskStatus:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTaskTransition(current, target)
    return target


@dataclass(frozen=True)
class ApprovalProposal:
    action: str
    payload: dict[str, Any]
    payload_hash: str
    status: ApprovalStatus

    @classmethod
    def create(cls, action: str, payload: dict[str, Any]) -> "ApprovalProposal":
        canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(action, payload, payload_hash, ApprovalStatus.PENDING)
~~~

- [ ] **Step 4: Run focused domain tests**

Run: `uv run --project backend pytest backend/tests/unit/domain -q`

Expected: all task and approval tests pass.

- [ ] **Step 5: Commit the task domain**

~~~bash
git add backend/src/ai_employee/domain/errors.py backend/src/ai_employee/domain/tasks.py backend/tests/unit/domain
git commit -m "feat: define trusted task state machine"
~~~

### Task 6: Persist tasks, steps, audit events, approvals, tool executions, and Outbox records

**Files:**
- Create: `backend/src/ai_employee/infrastructure/db/models/tasks.py`
- Modify: `backend/src/ai_employee/infrastructure/db/models/__init__.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/tasks.py`
- Create: `backend/src/ai_employee/application/use_cases/tasks.py`
- Create: `backend/migrations/versions/20260730_0002_tasks.py`
- Create: `backend/tests/integration/tasks/test_task_repository.py`

- [ ] **Step 1: Write the failing transactional creation test**

Create `backend/tests/integration/tasks/test_task_repository.py`. Call:

~~~python
created = await use_case.execute(
    user_id=user.id,
    kind="daily_brief",
    input_payload={"local_date": "2026-07-30"},
    idempotency_key="brief:user:2026-07-30:scheduled",
)
~~~

Assert in a new session that:

- exactly one TaskRun row exists with status CREATED;
- exactly one AuditEvent exists with event type `task.created`;
- exactly one unpublished OutboxEvent exists with topic `task.execute` and deterministic initial deduplication key;
- a second call with the same user and idempotency key returns the first task ID and creates no extra rows.

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `uv run --project backend pytest backend/tests/integration/tasks/test_task_repository.py -q`

Expected: FAIL because task ORM models and repository are absent.

- [ ] **Step 3: Add the task persistence schema**

Implement these exact model fields in `models/tasks.py`:

- TaskRunModel: UUID ID, user_id, nullable retry_of_task_id self-reference, kind, status, idempotency_key, input_payload JSONB, result_payload JSONB, error_code, graph_thread_id, current_step, attempt_count, lease_owner, lease_expires_at, scheduled_for, started_at, finished_at, created_at, updated_at. Add a unique constraint on `(user_id, idempotency_key)`.
- TaskStepModel: UUID ID, task_id, sequence, name, kind, status, input_summary JSONB, output_summary JSONB, error_code, started_at, finished_at. Add a unique constraint on `(task_id, sequence)`.
- ApprovalRequestModel: UUID ID, task_id, step_id, version, action, payload JSONB, payload_hash, preview_markdown, status, expires_at, decided_at, decided_by_user_id.
- ToolExecutionModel: UUID ID, task_id, step_id, tool_name, idempotency_key, request_payload_hash, provider_request_id, status, result_summary JSONB, error_code. Make idempotency_key unique.
- AuditEventModel: BIGINT identity ID, user_id, nullable task_id, event_type, actor_type, actor_id, metadata JSONB, created_at. Index `(task_id, id)`.
- OutboxEventModel: UUID ID, topic, aggregate_id, deduplication_key, payload JSONB, available_at, attempt_count, published_at, last_error, created_at. Make `deduplication_key` unique.

Create migration `20260730_0002_tasks.py` with revision `20260730_0002` and down revision `20260730_0001`.

- [ ] **Step 4: Implement idempotent task creation in one transaction**

`TaskRepository.create_with_outbox` must:

1. query by user_id and idempotency_key;
2. return the existing row when present;
3. otherwise insert TaskRunModel;
4. insert AuditEventModel with kind and status metadata;
5. insert OutboxEventModel with the task ID and `task.execute:{task_id}:initial` deduplication key;
6. flush once and let the caller transaction commit.

`CreateTaskUseCase.execute` must wrap the repository call in `async_sessionmaker.begin()`.

- [ ] **Step 5: Run migration and integration tests**

Run:

~~~bash
uv run --project backend alembic -c backend/alembic.ini upgrade head
uv run --project backend pytest backend/tests/integration/tasks/test_task_repository.py -q
~~~

Expected: idempotency, audit, and Outbox assertions pass.

- [ ] **Step 6: Commit task persistence**

~~~bash
git add backend/src/ai_employee/infrastructure/db backend/src/ai_employee/application/use_cases/tasks.py backend/migrations/versions/20260730_0002_tasks.py backend/tests/integration/tasks
git commit -m "feat: persist tasks audit and outbox"
~~~

### Task 7: Add Taskiq dispatch, execution leases, retries, and due-schedule scanning

**Files:**
- Modify: `backend/src/ai_employee/config.py`
- Create: `backend/src/ai_employee/infrastructure/queue/broker.py`
- Create: `backend/src/ai_employee/infrastructure/queue/scheduler.py`
- Create: `backend/src/ai_employee/infrastructure/queue/enqueue.py`
- Create: `backend/src/ai_employee/workers/execute_task.py`
- Create: `backend/src/ai_employee/workers/outbox.py`
- Create: `backend/src/ai_employee/workers/schedules.py`
- Create: `backend/tests/unit/workers/test_execution_lease.py`
- Create: `backend/tests/unit/workers/test_due_schedule.py`
- Create: `backend/tests/integration/workers/test_outbox_dispatch.py`

- [ ] **Step 1: Write failing lease and dispatch tests**

The lease test must prove:

- acquiring a lease changes QUEUED to RUNNING;
- a second worker cannot acquire an unexpired lease;
- an expired lease can be replaced;
- a terminal task cannot be leased.
- a step that exceeds `task_step_timeout_seconds` fails with a stable timeout code;
- the whole runner cannot exceed `task_timeout_seconds`, including retries.

The Outbox test must insert one unpublished record, run `relay_once`, and assert:

- the queue adapter receives the exact task ID once;
- CREATED transitions to QUEUED before the external enqueue call;
- claiming moves `available_at` forward by a short delivery lease so concurrent relays skip it;
- `published_at` is set only after successful enqueue;
- enqueue failure leaves the task QUEUED and `published_at` null, increments `attempt_count`, and records `last_error`.

The due-schedule test must prove user-local dates drive idempotency, a repeated fall-back hour
creates one daily task, and a configured time skipped by a spring-forward transition runs at the
first valid local minute after the gap.

- [ ] **Step 2: Run the tests and observe the missing worker layer**

Run:

~~~bash
uv run --project backend pytest backend/tests/unit/workers/test_execution_lease.py backend/tests/unit/workers/test_due_schedule.py backend/tests/integration/workers/test_outbox_dispatch.py -q
~~~

Expected: FAIL because queue and worker modules do not exist.

- [ ] **Step 3: Configure Taskiq Redis Streams**

Create `backend/src/ai_employee/infrastructure/queue/broker.py`:

~~~python
from taskiq_redis import RedisStreamBroker

from ai_employee.config import get_settings


settings = get_settings()
broker = RedisStreamBroker(
    url=settings.redis_url,
    queue_name="ai_employee_tasks",
    consumer_group_name="ai_employee_workers",
)
~~~

The enqueue adapter calls `execute_task.kiq(str(task_id))`. It does not use Redis as a result backend.
Transient retries are scheduled by a PostgreSQL Outbox record with a delayed `available_at`, not by
SmartRetryMiddleware, so the schedule remains recoverable after Redis loss.

- [ ] **Step 4: Implement lease acquisition and the task entrypoint**

Use one conditional UPDATE that matches:

- the task ID;
- status QUEUED or RUNNING with an expired lease;
- lease expiry null or earlier than the current UTC time.

Set `lease_owner`, `lease_expires_at`, status RUNNING, `started_at` when absent, and increment `attempt_count`. Check affected row count to determine ownership.

Create the Taskiq entrypoint:

~~~python
from uuid import UUID

from ai_employee.infrastructure.queue.broker import broker


@broker.task(retry_on_error=False)
async def execute_task(task_id: str) -> None:
    runner = build_task_runner()
    await runner.run(UUID(task_id))
~~~

The runner renews the lease between graph nodes and clears it on every terminal transition. Wrap
each node in `asyncio.timeout(task_step_timeout_seconds)` and the complete run in
`asyncio.timeout(task_timeout_seconds)`; task kinds may lower these budgets but cannot silently
remove them.

Catch the Task 5 error taxonomy at the execution boundary: a `TransientProviderError` transitions
to `RETRY_SCHEDULED` in the same PostgreSQL transaction that writes its audit event and a delayed
`task.execute` Outbox record. The runner uses durable `attempt_count` for the three-retry limit;
user-action, permanent, model-final, state, and unknown errors transition to the appropriate
non-retrying terminal state after writing their safe error code. The entrypoint acknowledges every
already-persisted result and never bypasses Outbox by asking Taskiq to retry directly.

- [ ] **Step 5: Implement Outbox relay and fixed scheduler jobs**

`relay_once(limit=100)` must use `SELECT FOR UPDATE SKIP LOCKED`, ordered by `available_at`.
For each claimed record, transition CREATED to QUEUED, move `available_at` forward by a
60-second delivery lease, and commit before calling Redis. After successful enqueue, set
`published_at` in a new short transaction. For a delayed retry event, the same confirmation transaction
also sets its PostgreSQL Redis-loss recovery deadline; before that confirmation the unpublished Outbox
is itself the recovery fact and the retry scanner must not create a duplicate. If enqueue fails, retain QUEUED, record the error, and
set `available_at` to the retry backoff. If the relay crashes after claiming, the row becomes due
again after 60 seconds. A duplicate delivery is safe because the Worker leases from PostgreSQL and
terminal tasks exit without work.

Register these first four fixed scheduler jobs:

- relay Outbox every minute;
- recover confirmed delayed retries after Redis loss every minute;
- dispatch due briefs every minute;
- clean expired sessions hourly.

`dispatch_due_briefs` queries active users whose local brief time is due and calls CreateTaskUseCase with a deterministic daily idempotency key. User schedules remain in PostgreSQL.
Compare timezone-aware instants rather than naive wall-clock equality. Record the user-local date
in the idempotency key so DST fall-back cannot duplicate; when a wall time does not exist, choose
the first valid instant after the gap.

Create the scheduler object with Taskiq's label source:

~~~python
from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from ai_employee.infrastructure.queue.broker import broker


scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)
~~~

Attach schedule IDs `outbox-relay`, `recover-task-retries`, `due-daily-briefs`, and `expire-sessions` to these four
jobs. The labels schedule system scans only; user-specific times remain database rows. Task 8 adds
approval expiry after the approval model exists, Task 13 adds the ten-minute Google
incremental-sync dispatcher after both adapters exist, and Task 18 adds diagnostics and daily
retention after their implementations exist.

After each task-creation transaction commits, run the same claim/transition/enqueue path
immediately. On enqueue failure, return the QUEUED task to the API and let the minute relay retry
the unpublished Outbox row.

- [ ] **Step 6: Run worker checks**

Run:

~~~bash
uv run --project backend pytest backend/tests/unit/workers backend/tests/integration/workers -q
uv run --project backend ruff check backend
uv run --project backend mypy backend/src
~~~

Expected: lease and Outbox tests pass; static checks exit 0.

- [ ] **Step 7: Commit background execution**

~~~bash
git add backend/src/ai_employee/config.py backend/src/ai_employee/infrastructure/queue backend/src/ai_employee/workers backend/tests/unit/workers backend/tests/integration/workers
git commit -m "feat: add durable background execution"
~~~

### Task 8: Integrate LangGraph PostgreSQL checkpoints and fake approval flow

**Files:**
- Create: `backend/src/ai_employee/agents/runner.py`
- Create: `backend/src/ai_employee/agents/fake_write/state.py`
- Create: `backend/src/ai_employee/agents/fake_write/graph.py`
- Create: `backend/src/ai_employee/application/use_cases/approvals.py`
- Modify: `backend/src/ai_employee/workers/schedules.py`
- Create: `backend/tests/integration/agents/test_checkpoint_resume.py`
- Create: `backend/tests/integration/agents/test_fake_approval.py`

- [ ] **Step 1: Write failing checkpoint and approval tests**

Build a graph with `prepare`, `approval`, and `execute` nodes and assert:

- first invocation returns an interrupt;
- TaskRun becomes WAITING_APPROVAL;
- ApprovalRequest is persisted with a payload hash;
- resuming with rejection reaches SUCCEEDED without calling the fake tool;
- replaying from the saved checkpoint does not rerun `prepare`;
- changing the payload hash between display and decision returns 409 and leaves the graph paused.
- the minute expiry scan marks an overdue request EXPIRED, fails the task with `approval_expired`, writes `approval.expired`, and never calls the fake tool.

- [ ] **Step 2: Run the tests and verify the graph layer is missing**

Run: `uv run --project backend pytest backend/tests/integration/agents -q`

Expected: FAIL because graph runner and approval use case are absent.

- [ ] **Step 3: Build the fake approval graph**

Define `FakeWriteState` with:

~~~python
from typing import TypedDict


class FakeWriteState(TypedDict):
    task_id: str
    proposal_payload: dict[str, object]
    approval_decision: str | None
    tool_called: bool
    messages: list[str]
~~~

The approval node calls `interrupt` with action, payload hash, preview Markdown, and version. The execute node invokes the fake tool only when the decision is approved.

Compile with AsyncPostgresSaver and set `configurable.thread_id` to TaskRun ID.

- [ ] **Step 4: Implement ApprovalDecisionUseCase**

The use case must:

- lock the pending ApprovalRequest;
- verify user ownership, pending status, expiry, version, and payload hash;
- update approved or rejected status;
- append `approval.resolved` AuditEvent;
- transition WAITING_APPROVAL to QUEUED;
- create an OutboxEvent that resumes the same graph thread with `Command(resume=decision)`.

Add `ExpireApprovalsUseCase` and a labeled minute task with
`schedule_id="expire-approvals"`. It locks overdue pending requests in bounded batches, marks
each EXPIRED, transitions its task to FAILED with `approval_expired`, and appends a content-free
`approval.expired` event. Retrying the task uses Task 9's new-TaskRun retry path and therefore
creates a fresh proposal/hash.

- [ ] **Step 5: Run checkpoint and approval tests**

Run:

~~~bash
uv run --project backend pytest backend/tests/integration/agents -q
uv run --project backend pytest backend/tests/unit/domain/test_approval_policy.py -q
~~~

Expected: interrupt, rejection, expiry, resume, replay, and stale-hash scenarios pass.

- [ ] **Step 6: Commit LangGraph durability**

~~~bash
git add backend/src/ai_employee/agents backend/src/ai_employee/application/use_cases/approvals.py backend/src/ai_employee/workers/schedules.py backend/tests/integration/agents
git commit -m "feat: add checkpointed approval workflow"
~~~

### Task 9: Expose task REST APIs and replayable SSE events

**Files:**
- Create: `backend/src/ai_employee/api/errors.py`
- Create: `backend/src/ai_employee/api/routers/tasks.py`
- Create: `backend/src/ai_employee/api/routers/approvals.py`
- Create: `backend/src/ai_employee/api/sse.py`
- Create: `backend/src/ai_employee/infrastructure/events/publisher.py`
- Create: `backend/tests/integration/api/test_tasks.py`
- Create: `backend/tests/integration/api/test_task_sse.py`

- [ ] **Step 1: Write failing task API and SSE tests**

Cover:

- `POST /api/v1/tasks` returns 202 with task ID and the current QUEUED status after Outbox claim;
- repeated `Idempotency-Key` returns the same task ID;
- `GET /api/v1/tasks/{id}` returns task and ordered steps;
- cancel transitions a cancellable task;
- retrying a failed task creates one new TaskRun with `retry_of_task_id`, while repeated retry requests with the same Idempotency-Key return that same new task;
- `Last-Event-ID` replays only later AuditEvent rows;
- a `Last-Event-ID` older than the retained range emits a current `task.snapshot` before live events;
- a persisted event with a dropped Pub/Sub notification is found on the next heartbeat poll;
- heartbeat events are not stored;
- a task owned by another user returns 404.

- [ ] **Step 2: Run the API tests and verify they fail**

Run: `uv run --project backend pytest backend/tests/integration/api/test_tasks.py backend/tests/integration/api/test_task_sse.py -q`

Expected: FAIL because routes and SSE implementation are missing.

- [ ] **Step 3: Implement RFC 9457 errors and task routes**

Problem responses must contain:

~~~json
{
  "type": "https://ai-employee.local/problems/task-conflict",
  "title": "Task state conflict",
  "status": 409,
  "detail": "The task cannot be changed from its current state.",
  "error_code": "task_state_conflict",
  "trace_id": "request-trace-id"
}
~~~

Register create, snapshot, cancel, retry, and approval-decision endpoints. Retry never reopens the
terminal TaskRun; it creates a new task with copied input, a caller-supplied Idempotency-Key, and
`retry_of_task_id` pointing to the failed task. Apply authenticated user scoping and CSRF to
unsafe methods.

Use `POST /api/v1/tasks/{task_id}/cancel` and
`POST /api/v1/tasks/{task_id}/retry`; both return the resulting task snapshot.

- [ ] **Step 4: Implement replay-first SSE**

Use FastAPI `EventSourceResponse` and `ServerSentEvent`. The generator must:

1. subscribe to the task Pub/Sub channel;
2. query AuditEvent rows with ID greater than Last-Event-ID;
3. emit each durable event with both SSE ID and envelope `sequence` equal to AuditEvent ID;
4. fetch notified event rows from PostgreSQL;
5. on every 15-second tick query PostgreSQL for missed rows before emitting a heartbeat;
6. stop on client cancellation;
7. deduplicate by last emitted ID.

If `Last-Event-ID` predates the task's oldest retained AuditEvent, read the authenticated task
snapshot and current maximum event ID in one transaction, emit `task.snapshot` with that maximum
as its SSE ID, then continue only with newer live events. Do not invent missing step transitions.

- [ ] **Step 5: Run API and SSE checks**

Run:

~~~bash
uv run --project backend pytest backend/tests/integration/api/test_tasks.py backend/tests/integration/api/test_task_sse.py -q
uv run --project backend ruff check backend
~~~

Expected: API, isolation, replay, and heartbeat tests pass.

- [ ] **Step 6: Commit task APIs**

~~~bash
git add backend/src/ai_employee/api backend/src/ai_employee/infrastructure/events backend/tests/integration/api
git commit -m "feat: expose task APIs and replayable SSE"
~~~

### Task 10: Build the Vue shell, authentication, task store, and execution timeline

**Files:**
- Create: `frontend/index.html`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tsconfig.app.json`
- Create: `frontend/tsconfig.node.json`
- Create: `frontend/eslint.config.js`
- Create: `frontend/.prettierrc.json`
- Create: `frontend/src/env.d.ts`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/router/index.ts`
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/stores/auth.ts`
- Create: `frontend/src/stores/tasks.ts`
- Create: `frontend/src/composables/useTaskEvents.ts`
- Create: `frontend/src/components/TaskTimeline.vue`
- Create: `frontend/src/pages/LoginPage.vue`
- Create: `frontend/src/pages/TasksPage.vue`
- Create: `frontend/src/stores/tasks.spec.ts`
- Create: `frontend/src/composables/useTaskEvents.spec.ts`
- Create: `frontend/src/components/TaskTimeline.spec.ts`

- [ ] **Step 1: Write failing task reducer and EventSource lifecycle tests**

The task store test must apply events out of order and assert it:

- ignores duplicate sequence IDs;
- sorts durable steps by sequence;
- updates status from `task.status_changed`;
- replaces stale local state from a `task.snapshot` replay-gap event;
- records connection state separately from task state.

The composable test mocks EventSource and asserts `close` is called on unmount.
TaskTimeline tests assert a retry action creates and follows the returned replacement task rather
than mutating the failed task locally.

- [ ] **Step 2: Run frontend unit tests and verify failure**

Run: `pnpm --dir frontend test:unit --run`

Expected: FAIL because the Vue app, store, and composable do not exist.

- [ ] **Step 3: Implement typed API and event models**

Configure Vite with the Vue plugin, Vitest's jsdom environment, and an `@` alias to
`frontend/src`. Make `tsconfig.json` reference `tsconfig.app.json` and
`tsconfig.node.json`; include `src/**/*.vue`, tests, and Vite/Vitest types. Configure ESLint's
flat config from `@eslint/js`, `typescript-eslint`, and `eslint-plugin-vue`, ignoring only
generated build, coverage, and Playwright-report directories. Prettier uses two spaces, no
semicolons, and single quotes.

Create these central types:

~~~typescript
export type TaskStatus =
  | 'created'
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'retry_scheduled'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export interface TaskEvent {
  id: string
  task_id: string
  sequence: string
  event: string
  occurred_at: string
  step_id: string | null
  payload: Record<string, unknown>
}

export interface TaskSnapshot {
  id: string
  kind: string
  status: TaskStatus
  event_cursor: string
  steps: Array<{
    id: string
    sequence: number
    name: string
    status: string
    output_summary: Record<string, unknown> | null
  }>
}
~~~

The API client uses `credentials: 'include'`, attaches `X-CSRF-Token` to unsafe methods, and throws a typed ProblemError.

- [ ] **Step 4: Implement EventSource cleanup and ordered task state**

`useTaskEvents` opens `/api/v1/tasks/{id}/events`, uses the snapshot `event_cursor` as its reconnect cursor, updates a connection-state ref, parses TaskEvent data, passes events to Pinia, and calls `source.close()` inside `onUnmounted`. Switching tasks records the opened task ID so closing A marks only A disconnected before opening B.

`TaskTimeline` renders step name, state, duration, error code, and expandable summaries without raw HTML.
TasksPage exposes cancel only for cancellable states and retry only for failed tasks; after retry it
subscribes to the returned task ID and retains a link to the original task.

- [ ] **Step 5: Add login and protected routing**

LoginPage posts credentials, auth store loads `/auth/me`, and router guards:

- redirect unauthenticated users to `/login`;
- redirect authenticated users away from `/login`;
- preserve the intended destination in a query parameter.

- [ ] **Step 6: Run frontend checks**

Run:

~~~bash
pnpm --dir frontend test:unit --run
pnpm --dir frontend type-check
pnpm --dir frontend lint
pnpm --dir frontend build
~~~

Expected: unit tests and build pass.

- [ ] **Step 7: Commit the first end-to-end UI slice**

~~~bash
git add frontend
git commit -m "feat: add task timeline web shell"
~~~

### Task 11: Add encrypted Google OAuth connections and source-data schema

**Files:**
- Create: `backend/src/ai_employee/domain/connections.py`
- Create: `backend/src/ai_employee/infrastructure/security/encryption.py`
- Create: `backend/src/ai_employee/infrastructure/db/models/sources.py`
- Create: `backend/src/ai_employee/integrations/google/oauth.py`
- Create: `backend/src/ai_employee/application/use_cases/connections.py`
- Create: `backend/src/ai_employee/api/routers/connections.py`
- Create: `backend/migrations/versions/20260730_0003_google_sources.py`
- Create: `backend/tests/unit/security/test_encryption.py`
- Create: `backend/tests/integration/google/test_oauth_flow.py`

- [ ] **Step 1: Write failing AEAD and OAuth tests**

The encryption tests must prove:

- encrypting the same plaintext twice produces different nonces and ciphertext;
- decrypt returns the original bytes when AAD matches;
- decrypt fails when user ID or owning record ID in AAD changes.

The OAuth integration test must:

- authenticate, include CSRF, and request `/api/v1/connections/google/start`;
- assert state and PKCE challenge are present;
- simulate the callback with a mocked token endpoint;
- assert refresh token, access token, and expiry are stored only in `encrypted_credentials` rows as encrypted bytes, with no plaintext token column;
- assert the connection scopes equal Gmail readonly, Calendar readonly, OpenID, and email;
- assert a reused or expired state is rejected.

- [ ] **Step 2: Run the tests and verify they fail**

Run:

~~~bash
uv run --project backend pytest backend/tests/unit/security/test_encryption.py backend/tests/integration/google/test_oauth_flow.py -q
~~~

Expected: FAIL because encryption, source models, and OAuth routes are missing.

- [ ] **Step 3: Implement record-bound AEAD encryption**

Create `backend/src/ai_employee/infrastructure/security/encryption.py`:

~~~python
import base64
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: bytes
    nonce: bytes
    key_version: int


class AeadCipher:
    def __init__(self, key: bytes, key_version: int = 1) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._aead = AESGCM(key)
        self._key_version = key_version

    @classmethod
    def from_file(cls, path: Path) -> "AeadCipher":
        encoded = path.read_text(encoding="utf-8").strip().encode("ascii")
        return cls(base64.urlsafe_b64decode(encoded))

    def encrypt(self, plaintext: bytes, aad: bytes) -> EncryptedValue:
        nonce = os.urandom(12)
        ciphertext = self._aead.encrypt(nonce, plaintext, aad)
        return EncryptedValue(ciphertext, nonce, self._key_version)

    def decrypt(self, value: EncryptedValue, aad: bytes) -> bytes:
        return self._aead.decrypt(value.nonce, value.ciphertext, aad)
~~~

Use AAD formatted as `user_id:record_id:secret_kind`: OAuth attempts use their attempt ID and
`pkce_verifier`; stored access/refresh tokens use the connection ID and credential kind.

- [ ] **Step 4: Add Google source models and migration**

`domain/connections.py` defines ConnectionStatus values `connecting`, `connected`,
`degraded`, `expired`, and `disconnected`. `models/sources.py` must define:

- OAuthAttemptModel: user_id, state_hash, encrypted PKCE verifier, nonce, key_version, expires_at, consumed_at. Make `state_hash` unique.
- OAuthConnectionModel: user_id, provider, provider_account_id, account_email, scopes ARRAY or JSONB, status, last_error_code, created_at, updated_at. Unique `(user_id, provider, provider_account_id)`.
- EncryptedCredentialModel: user_id, connection_id, credential_kind (`access_token` or `refresh_token`), ciphertext, nonce, key_version, token_expires_at, created_at, updated_at. Unique `(connection_id, credential_kind)`; no plaintext token column.
- SyncCursorModel: connection_id, resource_kind, cursor, last_success_at, last_attempt_at, last_error_code. Unique `(connection_id, resource_kind)`.
- EmailThreadModel: user_id, connection_id, provider_thread_id, subject, participants JSONB, latest_message_at, provider_url, created_at, updated_at. Unique `(connection_id, provider_thread_id)`.
- EmailMessageModel: user_id, thread_id, provider_message_id, received_at, sender JSONB, recipients JSONB, subject, snippet, body_ciphertext, body_nonce, body_key_version, labels JSONB, headers JSONB, provider_url, created_at, updated_at. Unique `(thread_id, provider_message_id)`.
- EmailAnalysisModel: user_id, thread_id, category, urgency, needs_reply, deadline_at, confidence, reason_codes JSONB, model_name, prompt_version, input_hash, created_at.
- CalendarEventModel: user_id, connection_id, provider_event_id, calendar_id, title, description_ciphertext, description_nonce, description_key_version, location_ciphertext, location_nonce, location_key_version, starts_at, ends_at, all_day, transparency, status, timezone, recurring_event_id, etag, provider_url, created_at, updated_at. Unique `(connection_id, provider_event_id)`.

Create migration `20260730_0003_google_sources.py` with revision `20260730_0003` and down revision `20260730_0002`.

- [ ] **Step 5: Implement state, PKCE, callback, refresh, and disconnect**

Use these exact Google scopes:

~~~python
GOOGLE_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]
~~~

The start use case:

- generates a 32-byte state and 64-byte PKCE verifier;
- stores SHA-256 state hash and encrypted verifier for 10 minutes;
- returns the Google authorization URL with `access_type=offline`, `prompt=consent`, and S256 challenge.

The callback:

- atomically consumes the state;
- exchanges code using httpx;
- fetches OpenID user info;
- encrypts tokens;
- upserts the connection;
- creates Gmail and Calendar sync cursors.

Disconnect revokes the token when possible, deletes the connection's `EncryptedCredentialModel` rows, marks the connection disconnected, and stops future sync.

Register:

- `GET /api/v1/connections`;
- `POST /api/v1/connections/google/start`;
- `GET /api/v1/connections/google/callback`;
- `DELETE /api/v1/connections/{connection_id}`;
- `POST /api/v1/connections/{connection_id}/sync`.

Start, disconnect, and sync require CSRF and user ownership. Manual sync creates one idempotent Gmail task
and one idempotent Calendar task from the request's `Idempotency-Key` and returns both task IDs.

- [ ] **Step 6: Run migration and OAuth tests**

Run:

~~~bash
uv run --project backend alembic -c backend/alembic.ini upgrade head
uv run --project backend pytest backend/tests/unit/security/test_encryption.py backend/tests/integration/google/test_oauth_flow.py -q
~~~

Expected: AEAD, PKCE, state consumption, encrypted token, and scope assertions pass.

- [ ] **Step 7: Commit Google connection foundations**

~~~bash
git add backend/src/ai_employee/domain/connections.py backend/src/ai_employee/infrastructure/security/encryption.py backend/src/ai_employee/infrastructure/db/models/sources.py backend/src/ai_employee/integrations/google/oauth.py backend/src/ai_employee/application/use_cases/connections.py backend/src/ai_employee/api/routers/connections.py backend/migrations/versions/20260730_0003_google_sources.py backend/tests
git commit -m "feat: add encrypted Google connections"
~~~

### Task 12: Implement Gmail incremental sync and normalized thread storage

**Files:**
- Create: `backend/src/ai_employee/application/ports/gmail.py`
- Create: `backend/src/ai_employee/integrations/google/gmail.py`
- Create: `backend/src/ai_employee/application/use_cases/sync_gmail.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/email.py`
- Create: `backend/src/ai_employee/workers/sync_gmail.py`
- Create: `backend/tests/contract/fixtures/gmail_initial.json`
- Create: `backend/tests/contract/fixtures/gmail_history.json`
- Create: `backend/tests/contract/test_gmail_adapter.py`
- Create: `backend/tests/integration/google/test_gmail_sync.py`

- [ ] **Step 1: Write failing adapter and sync tests**

Contract tests must assert:

- initial list requests `newer_than:7d`;
- message fetch excludes attachment downloads;
- multipart messages prefer `text/plain` and fall back to sanitized `text/html`;
- quoted history, signatures, scripts, tracking pixels, and empty blocks are removed;
- history response maps added messages and label changes.
- one 401 refreshes and retries exactly once; a second 401 becomes user-action-required.

Integration tests must assert:

- initial sync upserts thread and message rows;
- rerunning the same response creates no duplicates;
- a valid history cursor advances only after all writes commit;
- Gmail 404 on history cursor triggers a seven-day resync;
- message body is encrypted and raw MIME is absent from storage.
- refreshed access-token ciphertext and expiry commit atomically, while an omitted rotated refresh token preserves the existing encrypted refresh credential.

- [ ] **Step 2: Run Gmail tests and verify failure**

Run:

~~~bash
uv run --project backend pytest backend/tests/contract/test_gmail_adapter.py backend/tests/integration/google/test_gmail_sync.py -q
~~~

Expected: FAIL because Gmail port, adapter, repository, and sync use case are missing.

- [ ] **Step 3: Define normalized Gmail port types**

Create immutable types:

~~~python
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    thread_id: str
    history_id: str
    received_at: datetime
    sender: dict[str, str]
    recipients: list[dict[str, str]]
    subject: str
    snippet: str
    normalized_body: str
    labels: tuple[str, ...]
    headers: dict[str, str]
    provider_url: str


@dataclass(frozen=True)
class GmailSyncPage:
    messages: tuple[GmailMessage, ...]
    next_page_token: str | None
    latest_history_id: str
~~~

The Gmail port exposes initial pages, history pages, and refresh-aware request execution.

- [ ] **Step 4: Implement the Gmail REST adapter**

Use httpx against:

- `GET https://gmail.googleapis.com/gmail/v1/users/me/messages`
- `GET https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}`
- `GET https://gmail.googleapis.com/gmail/v1/users/me/history`

Use the standard-library `email` package for MIME structure and Beautiful Soup only for HTML
text extraction after removing script, style, tracking, quoted-history, and signature nodes.
Send bearer access token from the decrypted connection. On 401, refresh once and retry once. On
refresh, atomically replace encrypted access-token bytes and expiry and replace the refresh
credential only when Google returns a new one. On a second 401, mark the connection expired and
raise `UserActionRequiredError`. On 429 or 5xx, raise a typed transient provider error carrying
`Retry-After`.

- [ ] **Step 5: Implement transactional sync**

For each page:

- upsert thread by `(connection_id, provider_thread_id)`;
- upsert message by `(thread_id, provider_message_id)`;
- encrypt normalized body with AAD containing user, connection, message ID, and `body`;
- save headers and labels needed for deterministic classification;
- update thread latest timestamp and participants;
- advance cursor only in the transaction that commits the final page.

Publish a `source.gmail.synced` AuditEvent with counts and cursor, never message body.

- [ ] **Step 6: Run Gmail checks**

Run:

~~~bash
uv run --project backend pytest backend/tests/contract/test_gmail_adapter.py backend/tests/integration/google/test_gmail_sync.py -q
uv run --project backend ruff check backend
~~~

Expected: contract, idempotency, encryption, and expired-cursor tests pass.

- [ ] **Step 7: Commit Gmail sync**

~~~bash
git add backend/src/ai_employee/application/ports/gmail.py backend/src/ai_employee/integrations/google/gmail.py backend/src/ai_employee/application/use_cases/sync_gmail.py backend/src/ai_employee/infrastructure/db/repositories/email.py backend/src/ai_employee/workers/sync_gmail.py backend/tests/contract backend/tests/integration/google/test_gmail_sync.py
git commit -m "feat: add Gmail incremental sync"
~~~

### Task 13: Implement Google Calendar incremental sync

**Files:**
- Create: `backend/src/ai_employee/application/ports/calendar.py`
- Create: `backend/src/ai_employee/integrations/google/calendar.py`
- Create: `backend/src/ai_employee/integrations/google/fake.py`
- Create: `backend/src/ai_employee/application/use_cases/sync_calendar.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/calendar.py`
- Create: `backend/src/ai_employee/workers/sync_calendar.py`
- Modify: `backend/src/ai_employee/workers/schedules.py`
- Create: `backend/tests/contract/fixtures/calendar_initial.json`
- Create: `backend/tests/contract/fixtures/calendar_incremental.json`
- Create: `backend/tests/contract/test_calendar_adapter.py`
- Create: `backend/tests/integration/google/test_calendar_sync.py`
- Create: `backend/tests/integration/google/test_test_mode_adapters.py`
- Create: `backend/tests/integration/workers/test_google_sync_schedule.py`

- [ ] **Step 1: Write failing Calendar tests**

Contract tests must assert:

- initial sync requests `singleEvents=true`, `showDeleted=true`, user local midnight, and seven-day horizon;
- timed and all-day events normalize correctly;
- free, busy, cancelled, recurring, and updated events retain required fields.

Integration tests must assert:

- event upsert uses `(connection_id, provider_event_id)`;
- descriptions and locations are encrypted;
- `nextSyncToken` advances after commit;
- HTTP 410 clears the cursor and performs a window resync;
- cancelled events remain as tombstones so stale rows are not shown.

The scheduler test must assert that connected users receive one Gmail and one Calendar
incremental task per ten-minute bucket, with deterministic idempotency keys, and that
disconnected or degraded connections are not silently treated as healthy.

The test-mode adapter test must assert `APP_TEST_MODE=true` selects fake OAuth, Gmail, and
Calendar ports backed by sanitized fixtures, while normal development selects the real httpx
adapters.

- [ ] **Step 2: Run Calendar tests and verify failure**

Run:

~~~bash
uv run --project backend pytest backend/tests/contract/test_calendar_adapter.py backend/tests/integration/google/test_calendar_sync.py -q
~~~

Expected: FAIL because Calendar port, adapter, repository, and sync use case are missing.

- [ ] **Step 3: Define normalized Calendar types**

Create:

~~~python
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    calendar_id: str
    title: str
    description: str
    location: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    transparency: str
    status: str
    timezone: str
    recurring_event_id: str | None
    etag: str
    provider_url: str


@dataclass(frozen=True)
class CalendarSyncPage:
    events: tuple[CalendarEvent, ...]
    next_page_token: str | None
    next_sync_token: str | None
~~~

- [ ] **Step 4: Implement Calendar REST adapter and transactional sync**

Use `GET https://www.googleapis.com/calendar/v3/calendars/primary/events`.

Use the same refresh-once and typed retry policy as Gmail. Convert date-only events into local midnight boundaries with `all_day=True`; preserve the source timezone for display.

Upsert description and location with distinct AAD values containing user, connection, event ID,
and field kind; advance the cursor only after the final page commits. Emit
`source.calendar.synced` AuditEvent with counts and cutoff.

Add a labeled `dispatch_google_incremental_syncs` task with a 600-second interval and
`schedule_id="google-incremental-sync"`. It queries connected Google accounts from PostgreSQL
and creates Gmail and Calendar tasks with
`sync:google:{connection_id}:{resource_kind}:{utc_ten_minute_bucket}` idempotency keys. Provider
failures update cursor/connection state; they do not disable future scans.

`integrations/google/fake.py` must implement the same OAuth, Gmail, and Calendar ports, return
only fixture data, make no network calls, and expose deterministic connect/reconnect states for
Playwright. Dependency wiring may select it only when `APP_TEST_MODE=true`.

- [ ] **Step 5: Run Calendar and scheduler checks**

Run:

~~~bash
uv run --project backend pytest backend/tests/contract/test_calendar_adapter.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/google/test_test_mode_adapters.py backend/tests/integration/workers/test_google_sync_schedule.py -q
uv run --project backend mypy backend/src
~~~

Expected: timed, all-day, recurring, cancelled, encryption, 410 fallback, and ten-minute
incremental scheduling tests pass.

- [ ] **Step 6: Commit Calendar sync**

~~~bash
git add backend/src/ai_employee/application/ports/calendar.py backend/src/ai_employee/integrations/google/calendar.py backend/src/ai_employee/integrations/google/fake.py backend/src/ai_employee/application/use_cases/sync_calendar.py backend/src/ai_employee/infrastructure/db/repositories/calendar.py backend/src/ai_employee/workers/sync_calendar.py backend/src/ai_employee/workers/schedules.py backend/tests/contract backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/google/test_test_mode_adapters.py backend/tests/integration/workers/test_google_sync_schedule.py
git commit -m "feat: add Calendar incremental sync"
~~~

### Task 14: Add deterministic email analysis and calendar conflict rules

**Files:**
- Create: `backend/src/ai_employee/domain/email.py`
- Create: `backend/src/ai_employee/domain/calendar.py`
- Create: `backend/src/ai_employee/domain/model_redaction.py`
- Create: `backend/tests/unit/domain/test_email_rules.py`
- Create: `backend/tests/unit/domain/test_calendar_conflicts.py`
- Create: `backend/tests/unit/domain/test_model_redaction.py`

- [ ] **Step 1: Write failing email rule tests**

Cover:

- Gmail SPAM label returns category SPAM without model input;
- `List-Unsubscribe` or bulk precedence returns NOTIFICATION;
- known work sender domain returns WORK when configured;
- ambiguous mail returns `None` so the model can decide;
- urgency remains an independent dimension;
- deterministic results include reason codes.

- [ ] **Step 2: Write failing conflict tests**

Cover:

- overlapping busy timed events conflict;
- adjacent events do not conflict;
- cancelled or transparent events are ignored;
- all-day events are displayed but do not conflict in M1;
- events crossing UTC midnight are compared after timezone normalization.

The model-redaction test must mask bearer tokens, API keys, six-digit one-time codes, and user-configured regular expressions while leaving ordinary names and meeting times intact.

- [ ] **Step 3: Run tests and verify failure**

Run: `uv run --project backend pytest backend/tests/unit/domain/test_email_rules.py backend/tests/unit/domain/test_calendar_conflicts.py backend/tests/unit/domain/test_model_redaction.py -q`

Expected: FAIL because email and calendar domain rules are missing.

- [ ] **Step 4: Implement the pure rule functions**

Define:

~~~python
from enum import StrEnum


class EmailCategory(StrEnum):
    WORK = "work"
    NOTIFICATION = "notification"
    SPAM = "spam"
    OTHER = "other"


class EmailUrgency(StrEnum):
    URGENT = "urgent"
    NORMAL = "normal"
~~~

`classify_by_rules` returns category plus reason codes or `None`. It never invents urgency from category.

`find_conflicts` sorts busy timed events by start time and returns unique overlap pairs. It excludes cancelled, transparent, and all-day events.

`redact_for_model` applies built-in secret patterns and configured patterns before any cloud-model request. It returns redacted text plus reason codes so the task timeline can report local masking without revealing values.

- [ ] **Step 5: Run pure domain tests**

Run: `uv run --project backend pytest backend/tests/unit/domain/test_email_rules.py backend/tests/unit/domain/test_calendar_conflicts.py backend/tests/unit/domain/test_model_redaction.py -q`

Expected: all deterministic analysis tests pass.

- [ ] **Step 6: Commit analysis rules**

~~~bash
git add backend/src/ai_employee/domain/email.py backend/src/ai_employee/domain/calendar.py backend/src/ai_employee/domain/model_redaction.py backend/tests/unit/domain
git commit -m "feat: add deterministic mail and calendar rules"
~~~

### Task 15: Implement the model gateway and daily brief LangGraph

**Files:**
- Create: `backend/src/ai_employee/domain/briefs.py`
- Create: `backend/src/ai_employee/application/ports/model.py`
- Create: `backend/src/ai_employee/integrations/llm/openai_compatible.py`
- Create: `backend/src/ai_employee/integrations/llm/fake.py`
- Create: `backend/src/ai_employee/agents/daily_brief/state.py`
- Create: `backend/src/ai_employee/agents/daily_brief/nodes.py`
- Create: `backend/src/ai_employee/agents/daily_brief/graph.py`
- Create: `backend/src/ai_employee/prompts/daily_brief_v1.md`
- Create: `backend/src/ai_employee/prompts/conversation_intent_v1.md`
- Create: `backend/tests/unit/briefs/test_schema.py`
- Create: `backend/tests/integration/briefs/test_daily_brief_graph.py`

- [ ] **Step 1: Write failing schema and graph tests**

The schema test must reject:

- a brief item without a source reference;
- urgency outside urgent or normal;
- an unknown section;
- a suggested action that implies automatic execution.
- a conversation intent outside `generate_daily_brief`, `show_latest_brief`, or `explain_capabilities`.

The graph test uses a FakeModel and asserts:

- deterministic email categories bypass model classification;
- spam bodies never enter model messages and contribute only an aggregate count;
- each thread contributes at most one summary input, while notifications are folded into an aggregate section;
- ambiguous threads receive structured classifications;
- conflict detection runs without model input;
- invalid model JSON receives exactly one repair attempt;
- a second invalid result produces a partial brief;
- every rendered important item carries a source reference.
- `APP_TEST_MODE=true` selects a deterministic no-network model adapter suitable for browser tests.

- [ ] **Step 2: Run brief tests and verify failure**

Run:

~~~bash
uv run --project backend pytest backend/tests/unit/briefs/test_schema.py backend/tests/integration/briefs/test_daily_brief_graph.py -q
~~~

Expected: FAIL because brief schemas, gateway, prompt, and graph are missing.

- [ ] **Step 3: Define structured brief and model port types**

Define Pydantic models:

- EmailJudgement: thread_id, category, urgency, needs_reply, deadline_at, confidence, reason_codes.
- BriefSourceRef: source_type, source_id, provider_url.
- BriefItem: section, title, body_markdown, priority, source_refs, suggested_action_kind.
- DailyBriefContent: local_date, source_cutoff, completeness, headline, items, warnings.
- ConversationIntent: intent, confidence, reason_code.

Allowed sections are `attention`, `needs_reply`, `mail_summary`, `notifications`, `schedule`, `conflicts`, and `suggested_actions`.
Allowed completeness values are `complete` and `partial`; a total failure stores no DailyBrief
and terminates the TaskRun with a typed error. One usable source or deterministic section is enough
for `partial`; zero usable sources is a total failure.

The ModelGateway exposes one async method that receives model name, prompt version, messages, and a Pydantic response type, then returns the validated model plus input/output token counts, latency, and optional `estimated_cost_microusd`. Calculate cost only from the explicitly configured per-million-token rates; zero rates produce a null estimate rather than an invented price.

- [ ] **Step 4: Implement the OpenAI-compatible adapter**

POST to `{MODEL_BASE_URL}/chat/completions` with:

- configured model;
- system and user messages;
- temperature 0;
- structured JSON response format when `MODEL_SUPPORTS_JSON_SCHEMA=true`;
- request timeout 120 seconds.

When `MODEL_SUPPORTS_JSON_SCHEMA=true`, send the schema response format. When false, send an explicit JSON-only instruction and still validate with the requested Pydantic type. Convert 429, 5xx, timeouts, invalid JSON, and schema errors into typed model errors. Never log message content or API key.

`integrations/llm/fake.py` returns deterministic structured output from source IDs and supports
explicit test scenarios for complete, partial, and invalid-twice responses. It is selected only
when `APP_TEST_MODE=true` and never reads a model API key.

- [ ] **Step 5: Build small checkpointed graph nodes**

Use nodes:

1. `load_sources`;
2. `apply_deterministic_rules`;
3. `classify_ambiguous_threads`;
4. `detect_calendar_conflicts`;
5. `compose_structured_brief`;
6. `validate_and_render`.

Each node writes TaskStep start/completion events. Keep provider calls in separate nodes so checkpoint resume does not rerun successful work.

The prompt file must instruct the model to:

- use only supplied facts;
- preserve names, times, and deadlines;
- omit unsupported claims;
- output Chinese by default;
- follow the user locale supplied in graph state, with `zh-CN` as the default;
- treat category and urgency separately;
- cite source IDs supplied in the input;
- propose actions without executing them.

`conversation_intent_v1.md` is a narrow classifier, not a Planner. Deterministic Chinese/English
allow rules for “summarize today's mail”, “generate/refresh today's brief”, and “show today's
brief”, plus deny rules for unsupported tool verbs, run before the model. Only remaining ambiguous
text is redacted locally and classified into the three allowed intents. Requests to send mail,
modify calendars, search the web, upload files, use memory, or plan arbitrary work map to
`explain_capabilities`; they never create a real tool call.

- [ ] **Step 6: Run model and graph tests**

Run:

~~~bash
uv run --project backend pytest backend/tests/unit/briefs/test_schema.py backend/tests/integration/briefs/test_daily_brief_graph.py -q
uv run --project backend ruff check backend
uv run --project backend mypy backend/src
~~~

Expected: schema, repair-once, partial result, checkpoint, and source-reference tests pass.

- [ ] **Step 7: Commit the daily brief graph**

~~~bash
git add backend/src/ai_employee/domain/briefs.py backend/src/ai_employee/application/ports/model.py backend/src/ai_employee/integrations/llm backend/src/ai_employee/agents/daily_brief backend/src/ai_employee/prompts backend/tests/unit/briefs backend/tests/integration/briefs
git commit -m "feat: add structured daily brief graph"
~~~

### Task 16: Persist briefs and conversations, expose user settings, and schedule generation

**Files:**
- Modify: `backend/src/ai_employee/infrastructure/db/models/identity.py`
- Modify: `backend/src/ai_employee/infrastructure/db/models/__init__.py`
- Create: `backend/src/ai_employee/infrastructure/db/models/briefs.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/briefs.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/conversations.py`
- Create: `backend/src/ai_employee/domain/settings.py`
- Create: `backend/src/ai_employee/application/use_cases/briefs.py`
- Create: `backend/src/ai_employee/application/use_cases/conversations.py`
- Create: `backend/src/ai_employee/application/use_cases/settings.py`
- Create: `backend/src/ai_employee/workers/generate_brief.py`
- Create: `backend/src/ai_employee/workers/conversation.py`
- Modify: `backend/src/ai_employee/workers/execute_task.py`
- Create: `backend/src/ai_employee/api/routers/briefs.py`
- Create: `backend/src/ai_employee/api/routers/conversations.py`
- Create: `backend/src/ai_employee/api/routers/settings.py`
- Create: `backend/migrations/versions/20260730_0004_daily_briefs.py`
- Create: `backend/tests/integration/briefs/test_brief_persistence.py`
- Create: `backend/tests/integration/db/test_user_settings_constraints.py`
- Create: `backend/tests/integration/api/test_briefs.py`
- Create: `backend/tests/integration/api/test_conversations.py`
- Create: `backend/tests/integration/api/test_settings.py`

- [ ] **Step 1: Write failing persistence, settings, and API tests**

Cover:

- scheduled creation uses `(user_id, local_date, schedule_kind)` idempotency;
- manual refresh creates the next version instead of overwriting;
- a complete and partial brief both persist structured content, Markdown, source cutoff, warnings, and task ID;
- every cloud or fake model call persists one LLMInvocation with hashes/tokens/cost/latency but no prompt content;
- every email judgement persists category, urgency, needs-reply, reasons, model/prompt version, and input hash;
- each BriefItem persists ordered source references;
- `GET /api/v1/briefs/today` returns the latest successful or partial version;
- `GET /api/v1/briefs?local_date=2026-07-30` returns every version newest first;
- `GET /api/v1/briefs/{brief_id}` returns one owned historical version and returns 404 for another user;
- `POST /api/v1/briefs/generate` returns 202 and a task ID;
- posting a conversation message stores the user message, creates a TaskRun, and later stores the final assistant message;
- “总结今天邮件” and equivalent supported phrases route to `generate_daily_brief`, while “查看今日简报” routes to the latest persisted brief;
- unsupported write/planning requests persist a concise M1 capability response and invoke no provider or tool;
- `DELETE /api/v1/conversations/{conversation_id}` requires CSRF, deletes only the owned conversation and messages, and leaves a content-free `conversation.deleted` audit event;
- another user cannot read the conversation or brief;
- `GET /api/v1/settings` returns timezone, locale, brief time, and all three retention periods;
- `PATCH /api/v1/settings` accepts partial updates, requires CSRF, and affects the next due-schedule scan without restarting the scheduler;
- invalid IANA timezones, non-`HH:MM` brief times, invalid locale tags, and retention values outside 1–3650 days return 422;
- direct database writes outside the same retention bounds fail their named check constraints.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

~~~bash
uv run --project backend pytest backend/tests/integration/briefs/test_brief_persistence.py backend/tests/integration/db/test_user_settings_constraints.py backend/tests/integration/api/test_briefs.py backend/tests/integration/api/test_conversations.py backend/tests/integration/api/test_settings.py -q
~~~

Expected: FAIL because brief tables, settings fields, repositories, use cases, and routes are missing.

- [ ] **Step 3: Add conversation, brief, item, invocation, and settings persistence**

Create:

- ConversationModel: user_id, title, created_at, updated_at.
- MessageModel: user_id, conversation_id, role, content_markdown, task_id, created_at.
- DailyBriefModel: user_id, local_date, version, task_id, completeness, source_cutoff, headline, structured_content JSONB, markdown, warnings JSONB, created_at. Unique `(user_id, local_date, version)`.
- DailyBriefItemModel: brief_id, position, section, priority, title, body_markdown, source_refs JSONB, suggested_action_kind.
- LLMInvocationModel: user_id, task_id, step_id, provider, model_name, prompt_version, input_hash, output_schema, input_tokens, output_tokens, estimated_cost_microusd, latency_ms, status, error_code, created_at.

Extend UserModel with these exact non-null settings:

~~~python
email_body_retention_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
source_metadata_retention_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=180)
workspace_history_retention_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=365)
~~~

Create migration `20260730_0004_daily_briefs.py` with revision `20260730_0004` and down revision `20260730_0003`. Add the three columns with server defaults 30, 180, and 365. Add `ck_users_email_body_retention_days`, `ck_users_source_metadata_retention_days`, and `ck_users_workspace_history_retention_days`, each independently enforcing `BETWEEN 1 AND 3650`. Create the conversation, message, brief, brief item, and invocation tables in the same migration.

- [ ] **Step 4: Implement validated user settings**

Define one settings view and one partial update command with these JSON fields:

~~~text
timezone
locale
brief_time
email_body_retention_days
source_metadata_retention_days
workspace_history_retention_days
updated_at
~~~

`UpdateUserSettingsUseCase` must:

1. load only the authenticated user's row;
2. validate timezone with `zoneinfo.ZoneInfo`;
3. parse brief time strictly as 24-hour `HH:MM`;
4. accept a normalized BCP 47-style locale tag of 2–16 characters;
5. constrain every retention period to 1–3650 days;
6. update only supplied fields in one transaction;
7. append `settings.updated` with changed field names but no previous or new values.

The due-schedule scanner continues to read timezone and brief time from PostgreSQL on every minute scan, so a saved change takes effect without process restart.

- [ ] **Step 5: Implement generation and persistence use cases**

`GenerateDailyBriefUseCase` must:

1. derive the user-local date and source cutoff;
2. treat a missing or older-than-15-minute successful cursor as stale, run the corresponding sync step before selection, and preserve a partial path when that sync fails;
3. select email threads received during the local day and events overlapping that day;
4. invoke the daily brief graph with the user's locale;
5. calculate the next manual version or version 1 for scheduled generation;
6. persist EmailAnalysis judgements, DailyBrief, and ordered items in one transaction;
7. append `brief.ready` AuditEvent;
8. store task result payload with brief ID and completeness.

Persist each ModelGateway metadata result as LLMInvocation in the surrounding task transaction;
store only input hash, schema name, token/cost/latency metadata, status, and safe error code.
Deterministic email judgements use `model_name="deterministic"` and a versioned ruleset name.

`CreateConversationMessageUseCase` persists the user message and a `conversation.respond`
TaskRun in one transaction and uses a stable client request ID for idempotency.
`workers/conversation.py` applies deterministic intent rules, falls back to the Task 15
three-value classifier, invokes `GenerateDailyBriefUseCase` or loads the latest brief only for
supported intents, and always persists one final assistant message before succeeding. Unsupported
requests return the explicit M1 capability boundary without invoking Google or the fake write
tool. Register `conversation.respond` and `daily_brief` in the task dispatcher.
`DeleteConversationUseCase` scopes by user ID, deletes the conversation and its messages in
one transaction, and records only the deleted conversation ID in `conversation.deleted`.

- [ ] **Step 6: Implement brief, conversation, and settings routes**

Register:

- `GET /api/v1/briefs/today`
- `GET /api/v1/briefs?local_date={local_date}`
- `GET /api/v1/briefs/{brief_id}`
- `POST /api/v1/briefs/generate`
- `GET /api/v1/conversations`
- `POST /api/v1/conversations`
- `GET /api/v1/conversations/{conversation_id}`
- `POST /api/v1/conversations/{conversation_id}/messages`
- `DELETE /api/v1/conversations/{conversation_id}`
- `GET /api/v1/settings`
- `PATCH /api/v1/settings`

Use 202 for generation and message task creation and 204 for conversation deletion. Include task IDs so the frontend can subscribe immediately. Scope every read, update, and delete by the authenticated user, and apply CSRF to settings and conversation deletion.

- [ ] **Step 7: Run migrations and focused tests**

Run:

~~~bash
uv run --project backend alembic -c backend/alembic.ini upgrade head
uv run --project backend pytest backend/tests/integration/briefs backend/tests/integration/db/test_user_settings_constraints.py backend/tests/integration/api/test_briefs.py backend/tests/integration/api/test_conversations.py backend/tests/integration/api/test_settings.py -q
uv run --project backend ruff check backend
uv run --project backend mypy backend/src
~~~

Expected: settings validation, version history, source references, idempotency, scheduling updates, and user isolation tests pass.

- [ ] **Step 8: Commit brief persistence, settings, and APIs**

~~~bash
git add backend/src/ai_employee/infrastructure/db backend/src/ai_employee/domain/settings.py backend/src/ai_employee/application/use_cases/briefs.py backend/src/ai_employee/application/use_cases/conversations.py backend/src/ai_employee/application/use_cases/settings.py backend/src/ai_employee/workers/generate_brief.py backend/src/ai_employee/workers/conversation.py backend/src/ai_employee/workers/execute_task.py backend/src/ai_employee/api/routers/briefs.py backend/src/ai_employee/api/routers/conversations.py backend/src/ai_employee/api/routers/settings.py backend/migrations/versions/20260730_0004_daily_briefs.py backend/tests
git commit -m "feat: persist briefs and user settings"
~~~

### Task 17: Complete the ChatGPT-style brief, chat, connection, settings, and approval UI

**Files:**
- Modify: `justfiles/test.just`
- Modify: `scripts/test-tooling.sh`
- Create: `scripts/run-e2e-backend.sh`
- Create: `frontend/src/components/AppShell.vue`
- Create: `frontend/src/components/MarkdownMessage.vue`
- Create: `frontend/src/components/SourceLink.vue`
- Create: `frontend/src/components/BriefView.vue`
- Create: `frontend/src/components/ApprovalCard.vue`
- Create: `frontend/src/pages/ChatPage.vue`
- Create: `frontend/src/pages/TodayBriefPage.vue`
- Create: `frontend/src/pages/ConnectionsPage.vue`
- Create: `frontend/src/pages/SettingsPage.vue`
- Create: `frontend/src/api/auth.ts`
- Create: `frontend/src/api/briefs.ts`
- Create: `frontend/src/api/conversations.ts`
- Create: `frontend/src/api/connections.ts`
- Create: `frontend/src/api/settings.ts`
- Create: `frontend/src/components/BriefView.spec.ts`
- Create: `frontend/src/components/ApprovalCard.spec.ts`
- Create: `frontend/src/pages/TodayBriefPage.spec.ts`
- Create: `frontend/src/pages/SettingsPage.spec.ts`
- Create: `frontend/playwright.config.ts`
- Create: `frontend/e2e/daily-brief.spec.ts`

- [ ] **Step 1: Write failing component, page, and browser tests**

Component and page tests must assert:

- MarkdownMessage sanitizes scripts, event handlers, and unsafe links;
- BriefView renders completeness, data cutoff, warnings, sections, and source links;
- BriefView turns a suggested action into a new task proposal and never calls an external write API directly;
- ApprovalCard submits version and payload hash, disables duplicate decisions, and displays 409 conflicts;
- partial briefs show a persistent warning rather than a success-only badge;
- TodayBriefPage selects the newest version by default, lists every version for the local date, and loads the selected historical brief without overwriting the newest one;
- SettingsPage loads and saves timezone, locale, brief time, and the three named retention periods;
- SettingsPage lists active sessions without secrets, revokes a selected session, and redirects to login when the current session is revoked;
- failed settings validation stays inline and does not discard the user's form values;
- conversation history confirms deletion, removes the deleted conversation from navigation, and cannot delete another user's conversation;
- the shell has no enabled file-upload entry in M1.

The Playwright test must:

- log in;
- connect a fake Google provider in test mode;
- trigger a manual brief;
- observe the task timeline update through SSE;
- refresh the page mid-task and recover the same timeline;
- generate a second brief version and switch between both versions;
- open a Gmail and Calendar source link;
- update timezone and brief time, reload, and observe the saved values;
- revoke a non-current session;
- delete one conversation and verify it is absent after reload;
- approve, reject, and expire separate fake write tasks.

- [ ] **Step 2: Run frontend tests and verify failure**

Run:

~~~bash
pnpm --dir frontend test:unit --run
pnpm --dir frontend test:e2e
~~~

Expected: FAIL because final pages, API modules, and components are missing.

- [ ] **Step 3: Configure the isolated browser-test backend**

Configure Playwright with `baseURL=http://127.0.0.1:5173`, trace-on-first-retry, screenshot on
failure, one Chromium project, and web servers for the combined test backend plus Vite frontend.
The backend command receives `APP_ENV=test` and `APP_TEST_MODE=true`; no live Google or model
credentials are present. CI uses one Playwright worker; local runs may use the default parallelism.

Add `just e2e-backend`, backed by `scripts/run-e2e-backend.sh`. The script requires
`TEST_DATABASE_URL`, `TEST_REDIS_URL`, `E2E_ADMIN_EMAIL`, and an explicit
`E2E_ADMIN_PASSWORD_FILE`; exports the isolated database and Redis values as `DATABASE_URL` and
`REDIS_URL`; refuses to continue unless the parsed database name ends in `_test` and Redis uses
database 15; resets only those test stores; runs migrations; calls the admin CLI with
`--if-absent`; starts
one Taskiq Worker in the background and FastAPI in the foreground with `APP_ENV=test` and
`APP_TEST_MODE=true`; and traps EXIT/INT/TERM to stop the Worker. Extend the tooling contract to
list this recipe. Playwright's backend web server uses this command.

- [ ] **Step 4: Implement the application shell and pages**

Desktop layout:

- left navigation: new chat, conversations, today brief, task history, settings, and connections;
- center: route content, message composer, brief content, source chips, and settings forms;
- right: collapsible TaskTimeline and ApprovalCard.

Mobile layout moves navigation and execution timeline into drawers.

ConnectionsPage displays disconnected, connecting, connected, degraded, and expired states with start, reconnect, sync-now, and disconnect actions.

- [ ] **Step 5: Implement safe Markdown and sources**

Configure markdown-it with raw HTML disabled. Pass rendered output through DOMPurify. Allow only `http` and `https` links, set external links to `rel="noopener noreferrer"`, and render source references through SourceLink rather than model-provided HTML.

- [ ] **Step 6: Implement chat and versioned brief data flow**

ChatPage:

- persists conversation before first message;
- sends a generated `client_request_id`;
- appends the user message immediately;
- subscribes to returned task ID;
- replaces transient assistant text with the persisted final message.

Conversation history calls `DELETE /api/v1/conversations/{conversation_id}` after explicit confirmation, then routes to a new conversation if the open conversation was removed. It never removes local state before the server confirms success.

TodayBriefPage:

- loads the latest brief and the version summaries for its local date;
- displays source cutoff and completeness;
- triggers manual refresh and subscribes to the task;
- reloads both the latest brief and version list on `brief.ready`;
- uses `GET /api/v1/briefs/{brief_id}` when the user selects a historical version;
- visibly labels a historical selection and offers one action to return to latest.

Suggested-action buttons submit a new task proposal through the existing task API and open its
timeline. In M1 the only executable write path remains the fake approval tool.

- [ ] **Step 7: Implement settings and session management**

`api/settings.ts` exposes typed `getSettings` and `updateSettings` calls using snake_case JSON. SettingsPage uses IANA timezone options from `Intl.supportedValuesOf("timeZone")` when available and preserves a text fallback for older browsers.

`api/auth.ts` exposes `listSessions` and `revokeSession`. The page shows created, last-seen, and expiry timestamps, marks the current session, confirms revocation, and clears the auth store when the current session is revoked.

Retention controls use the exact API fields:

~~~typescript
interface UserSettings {
  timezone: string
  locale: string
  brief_time: string
  email_body_retention_days: number
  source_metadata_retention_days: number
  workspace_history_retention_days: number
  updated_at: string
}
~~~

- [ ] **Step 8: Run frontend tests and build**

Run:

~~~bash
pnpm --dir frontend test:unit --run
pnpm --dir frontend type-check
pnpm --dir frontend lint
pnpm --dir frontend build
pnpm --dir frontend test:e2e
bash scripts/test-tooling.sh
~~~

Expected: components, history switching, settings persistence, session revocation, reconnection flow, fake approval, and production build pass.

- [ ] **Step 9: Commit the complete M1 UI**

~~~bash
git add frontend justfiles/test.just scripts/test-tooling.sh scripts/run-e2e-backend.sh
git commit -m "feat: complete daily brief user experience"
~~~

### Task 18: Add typed errors, redacted observability, privacy deletion, and retention cleanup

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `.env.example`
- Modify: `backend/src/ai_employee/config.py`
- Modify: `backend/src/ai_employee/main.py`
- Modify: `backend/src/ai_employee/api/routers/system.py`
- Modify: `backend/src/ai_employee/infrastructure/queue/scheduler.py`
- Modify: `backend/src/ai_employee/workers/execute_task.py`
- Modify: `backend/src/ai_employee/workers/schedules.py`
- Modify: `backend/src/ai_employee/domain/errors.py`
- Create: `backend/src/ai_employee/application/use_cases/privacy.py`
- Create: `backend/src/ai_employee/api/routers/privacy.py`
- Create: `backend/src/ai_employee/infrastructure/observability/logging.py`
- Create: `backend/src/ai_employee/infrastructure/observability/redaction.py`
- Create: `backend/src/ai_employee/infrastructure/observability/metrics.py`
- Create: `backend/src/ai_employee/infrastructure/observability/tracing.py`
- Create: `backend/src/ai_employee/workers/privacy.py`
- Create: `backend/src/ai_employee/workers/diagnostics.py`
- Create: `backend/src/ai_employee/workers/retention.py`
- Create: `scripts/init-db-roles.sh`
- Create: `backend/tests/unit/observability/test_redaction.py`
- Create: `backend/tests/integration/observability/test_metrics.py`
- Create: `backend/tests/integration/observability/test_tracing.py`
- Create: `backend/tests/integration/retention/test_retention_cleanup.py`
- Create: `backend/tests/integration/diagnostics/test_overdue_brief.py`
- Create: `backend/tests/integration/api/test_privacy.py`
- Modify: `frontend/src/components/AppShell.vue`
- Create: `frontend/src/components/AppShell.spec.ts`
- Modify: `frontend/src/pages/SettingsPage.vue`
- Modify: `frontend/src/pages/SettingsPage.spec.ts`
- Create: `frontend/src/api/privacy.ts`
- Create: `frontend/src/api/system.ts`

- [ ] **Step 1: Write failing error, observability, retention, and privacy tests**

Redaction tests must remove or replace:

- Authorization values;
- cookies;
- OAuth access and refresh tokens;
- model API keys;
- complete email body fields;
- values matching configured secret patterns.

Metrics tests must assert counters and histograms for task outcome, duration, retry count, queue wait, stuck tasks, sync freshness, provider error, model schema repairs, model tokens, estimated model cost, model latency, API errors, SSE connections, Worker/Scheduler heartbeat age, and PostgreSQL/Redis health. Labels may contain operation, provider, status, and error code, but never raw user IDs or message content.

Tracing tests must use an in-memory exporter and assert FastAPI and SQLAlchemy spans are created, health and metrics routes are excluded, and request headers, cookies, and bodies are absent from span attributes.

Retention tests must assert:

- normalized email body is cleared after the user's `email_body_retention_days` while sender, subject, labels, and provider IDs remain;
- email metadata/analysis and ended Calendar event snapshots expire after the user's `source_metadata_retention_days`;
- future Calendar events are retained;
- conversations, briefs, tasks, and audit content expire after the user's `workspace_history_retention_days`;
- active OAuth credentials are not deleted;
- disconnected credentials are deleted immediately;
- durable task events and results exist only in PostgreSQL; any application-owned Redis cache or coordination key has a positive TTL no greater than 86,400 seconds;
- cleanup uses bounded batches and writes one content-free retention AuditEvent per run.
- the ordinary application database role cannot update or delete AuditEvent rows, while the dedicated retention role can delete only records selected by the retention use case.

Privacy API tests must assert:

- `POST /api/v1/privacy/source-cache-deletions` requires CSRF, returns 202 with a task ID, removes source rows, analyses, and source-derived briefs, resets sync cursors for a clean resync, and retains credentials and conversations;
- `POST /api/v1/privacy/all-data-deletions` requires the exact confirmation `DELETE ALL DATA`, returns 202, revokes sessions, removes source, conversation, brief, task, and credential content, and leaves one content-free deletion audit event;
- a second user’s rows are untouched;
- retry after an injected mid-batch crash completes idempotently and still leaves exactly one deletion audit;
- the deletion audit metadata contains only operation, opaque request ID, completion time, and trace ID, never email, subject, sender, body, token, or prompt text.

Overdue-brief tests must assert:

- no alert exists before the configured brief time plus 15 minutes;
- after 15 minutes without a successful or partial brief, `GET /api/v1/system/alerts` returns a critical alert and the scheduler creates exactly one `brief.overdue_diagnostic` task for that user-local date;
- a successful or partial brief suppresses the alert, while a failed brief does not;
- timezone and daylight-saving boundaries use the user's local date and configured brief time.

- [ ] **Step 2: Write failing settings privacy controls and scheduler tests**

Add frontend tests that mock the privacy API and assert SettingsPage:

- asks for the exact confirmation phrase before all-data deletion;
- disables both destructive buttons while a deletion task is pending;
- shows the task timeline link for source-cache deletion;
- keeps the task pending after HTTP 202, then routes to LoginPage only when deletion revokes the current session; a pre-deletion task failure remains visible.

Add an AppShell test that shows the overdue brief as a persistent red banner with a link to the
diagnostic task, and removes it after the alerts endpoint no longer returns it.

Add a backend scheduler test that imports all task modules and finds exactly these schedule IDs:
`outbox-relay`, `recover-task-retries`, `due-daily-briefs`, `expire-sessions`, `expire-approvals`,
`google-incremental-sync`, `brief-overdue-diagnostics`, and `retention-cleanup`. Assert
`retention-cleanup` uses cron `30 2 * * *`.

- [ ] **Step 3: Run the new tests and verify failure**

Run:

~~~bash
uv run --project backend pytest backend/tests/unit/observability backend/tests/integration/observability backend/tests/integration/retention backend/tests/integration/diagnostics backend/tests/integration/api/test_privacy.py -q
pnpm --dir frontend test:unit --run src/pages/SettingsPage.spec.ts src/components/AppShell.spec.ts
~~~

Expected: FAIL because error mapping, telemetry, privacy routes/workers, retention fields, and destructive controls are missing.

- [ ] **Step 4: Add telemetry dependencies and complete error-to-API mapping**

Extend `backend/pyproject.toml` with these runtime dependencies, then run `uv lock --project backend`. Add these non-secret configuration keys to `.env.example` and load them through `config.py`:

~~~dotenv
OTEL_ENABLED=false
OTEL_SERVICE_NAME=ai-employee
OTEL_EXPORTER_OTLP_ENDPOINT=
METRICS_ENABLED=true
WORKER_METRICS_PORT=9101
SCHEDULER_METRICS_PORT=9102
RETENTION_DATABASE_URL_FILE=/run/secrets/retention_database_url
~~~

~~~toml
"opentelemetry-api>=1.36,<2",
"opentelemetry-sdk>=1.36,<2",
"opentelemetry-exporter-otlp-proto-http>=1.36,<2",
"opentelemetry-instrumentation-fastapi>=0.57b0,<1",
"opentelemetry-instrumentation-sqlalchemy>=0.57b0,<1",
"opentelemetry-instrumentation-httpx>=0.57b0,<1"
~~~

Use the stable domain error classes introduced in Task 5. Add any missing safe metadata fields without
renaming their constructors or `error_code` contract, then map them to RFC 9457 statuses. Unknown
errors return a generic 500 with trace ID; stack traces stay in redacted internal logs.

- [ ] **Step 5: Configure JSON logging, metrics, and OpenTelemetry tracing**

Every log record must include timestamp, level, event name, trace ID, task ID when available, step ID when available, and provider/error code when applicable. Use an HMAC or SHA-256 digest of the user ID for any diagnostic dimension. Never include user email, message body, token, cookie, Authorization header, or prompt content.

Expose API metrics at `/metrics`, start internal-only Prometheus HTTP listeners on Worker port 9101 and Scheduler port 9102, and keep every metrics endpoint off the public Caddy site. Define the metric names and labels in one module:

~~~text
ai_employee_tasks_total{kind,status}
ai_employee_task_duration_seconds{kind,status}
ai_employee_task_retries_total{kind}
ai_employee_queue_wait_seconds{kind}
ai_employee_stuck_tasks{kind}
ai_employee_sync_age_seconds{provider,resource}
ai_employee_provider_errors_total{provider,error_code}
ai_employee_model_schema_repairs_total{model,outcome}
ai_employee_model_tokens_total{model,direction}
ai_employee_model_cost_usd_total{model}
ai_employee_model_latency_seconds{model}
ai_employee_api_errors_total{route,error_code}
ai_employee_sse_connections{state}
ai_employee_process_heartbeat_age_seconds{process}
ai_employee_dependency_health{dependency}
~~~

Configure `tracing.py` with an OTLP HTTP exporter only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; otherwise install a no-op/in-memory-safe provider. Instrument FastAPI with `FastAPIInstrumentor.instrument_app` while excluding `/health/*` and `/metrics`, instrument an async SQLAlchemy engine through `SQLAlchemyInstrumentor().instrument(engine=async_engine.sync_engine)`, and instrument outbound provider calls with `HTTPXClientInstrumentor`. Do not capture request headers or bodies. Initialize tracing once in API, Worker, and Scheduler bootstrap code.

- [ ] **Step 6: Implement privacy deletion use cases and routes**

`RequestSourceCacheDeletionUseCase` and `RequestAllDataDeletionUseCase` create idempotent TaskRuns with kinds `privacy.clear_source_cache` and `privacy.delete_all_data`. The routes are:

- `POST /api/v1/privacy/source-cache-deletions` with an `Idempotency-Key`;
- `POST /api/v1/privacy/all-data-deletions` with JSON `{"confirmation":"DELETE ALL DATA"}` and an `Idempotency-Key`.

Both routes require an authenticated session and CSRF and return HTTP 202 with the task ID. Cleanup workers use the dedicated retention DSN. The source-cache worker deletes the user’s EmailMessage, EmailThread, EmailAnalysis, CalendarEvent, and source-derived DailyBrief rows in bounded transactions, resets Gmail/Calendar cursor values and freshness timestamps, and retains OAuthConnection rows and conversations. The all-data worker makes one bounded best-effort provider revocation attempt, then deletes local credentials regardless of provider availability, followed by source rows, conversations/messages, briefs/items, Outbox rows, sessions, and every user TaskRun including its own in bounded dependency-order batches. It replaces email with `deleted-{user_id}@invalid.local`, display name with `Deleted User`, clears the password hash, resets settings to defaults, and sets `is_active=false` so foreign-key history remains valid without personal identity. Its final transaction writes exactly one `privacy.deletion_completed` AuditEvent with `task_id = null` and content-free metadata, then raises the internal `AllDataDeletionCompleted` sentinel. `execute_task.py` catches only that sentinel, returns successfully, and must not attempt to update the now-deleted TaskRun. After HTTP 202, the browser keeps the pending state until its session is revoked; the resulting 401/404 is treated as deletion completion rather than as a generic failure.

Use the opaque deletion request ID to check for an existing completion audit before insertion, so a
crash/retry cannot create a second retained deletion event.

- [ ] **Step 7: Implement overdue-brief diagnostics**

Add `GET /api/v1/system/alerts` as an authenticated, user-scoped derived view. It compares the
current instant with the user's local date, timezone, and brief time, and returns a critical
`daily_brief_overdue` alert only when the grace period has exceeded 15 minutes and no successful
or partial DailyBrief exists.

Register a labeled minute scan with `schedule_id="brief-overdue-diagnostics"`. The scan creates
`brief.overdue_diagnostic` with idempotency key
`diagnostic:daily-brief:{user_id}:{local_date}`. Its worker records Gmail/Calendar freshness,
recent failed generation task IDs, and actionable error codes without copying message or event
content. AppShell polls the alerts endpoint on load and after terminal task events and renders the
red banner.

- [ ] **Step 8: Implement configurable retention cleanup and register its schedule**

`retention.py` opens its write session from the dedicated DSN in `RETENTION_DATABASE_URL_FILE`,
then reads each active user’s three retention fields and runs bounded, committed batches:

1. clear `body_ciphertext`, `body_nonce`, and `body_key_version` together when `received_at + email_body_retention_days < now`;
2. delete EmailAnalysis rows, EmailMessage metadata when `received_at + source_metadata_retention_days < now`, now-empty EmailThread rows, and CalendarEvent snapshots only when `ends_at + source_metadata_retention_days < now`;
3. delete conversations/messages, brief items/briefs, completed task graphs (steps, approvals, tool executions, LLM invocations, published Outbox records, and TaskRun), and AuditEvent rows older than `workspace_history_retention_days`, preserving nonterminal work and the current run’s content-free retention event.

Never delete an active OAuth credential. Delete credentials marked disconnected immediately and reset their sync cursor. Register the fixed Taskiq label below after importing the retention module:

~~~python
@broker.task(
    schedule=[{"cron": "30 2 * * *", "schedule_id": "retention-cleanup"}],
)
async def retention_cleanup() -> None:
    use_case = build_retention_cleanup_use_case()
    await use_case.run_for_all_active_users()
~~~

The scheduler must import all labeled task modules before constructing `LabelScheduleSource`, and the job must be idempotent if two scheduler instances overlap.

`scripts/init-db-roles.sh` creates the application and retention roles from explicit password
files, applies the audit-table privilege boundary idempotently, and is invoked by the integration
fixture before the permission assertion.

Keep Redis Pub/Sub events ephemeral and do not configure a Redis result backend. Centralize any
application-owned cache or coordination write behind a helper that requires `ttl_seconds` in the
range 1–86,400; replayable events and final results remain in PostgreSQL.

- [ ] **Step 9: Wire routes, telemetry, workers, and frontend controls**

Register `privacy.py` and the extended system router in the API, initialize redacted logging/metrics/tracing in the application lifespan, dispatch privacy and diagnostic task kinds from `execute_task.py`, and expose privacy/system APIs through the frontend. Add a Privacy section to SettingsPage with explicit confirmation, pending-task state, task timeline link, and logout when the completed all-data deletion revokes the session.

- [ ] **Step 10: Run focused and regression checks**

Run:

~~~bash
uv run --project backend pytest backend/tests/unit/observability backend/tests/integration/observability backend/tests/integration/retention backend/tests/integration/diagnostics backend/tests/integration/api/test_privacy.py -q
pnpm --dir frontend test:unit --run
uv run --project backend pytest backend/tests/unit backend/tests/integration/api -q
uv run --project backend ruff check backend
uv run --project backend mypy backend/src
~~~

Expected: redaction, metrics, tracing, overdue diagnostics, privacy isolation/deletion, dynamic retention, scheduler registration, and existing API tests pass.

- [ ] **Step 11: Commit operational visibility, privacy, and cleanup**

~~~bash
git add backend/pyproject.toml backend/uv.lock .env.example backend/src/ai_employee/config.py backend/src/ai_employee/main.py backend/src/ai_employee/api/routers/system.py backend/src/ai_employee/infrastructure/queue/scheduler.py backend/src/ai_employee/workers backend/src/ai_employee/domain/errors.py backend/src/ai_employee/application/use_cases/privacy.py backend/src/ai_employee/api/routers/privacy.py backend/src/ai_employee/infrastructure/observability backend/tests scripts/init-db-roles.sh frontend/src/api/privacy.ts frontend/src/api/system.ts frontend/src/components/AppShell.vue frontend/src/components/AppShell.spec.ts frontend/src/pages/SettingsPage.vue frontend/src/pages/SettingsPage.spec.ts
git commit -m "feat: add observability privacy and retention controls"
~~~

### Task 19: Add production containers, Caddy, CI, backup, and restore

**Files:**
- Create: `backend/Dockerfile`
- Create: `backend/.dockerignore`
- Create: `frontend/Dockerfile`
- Create: `frontend/.dockerignore`
- Create: `compose.yaml`
- Create: `compose.dev.yaml`
- Create: `Caddyfile`
- Create: `.github/workflows/ci.yml`
- Create: `scripts/backup-postgres.sh`
- Create: `scripts/restore-postgres.sh`
- Modify: `scripts/init-db-roles.sh`
- Create: `scripts/test-deployment.sh`
- Create: `ops/observability/prometheus.yml`
- Create: `ops/observability/grafana/provisioning/datasources/prometheus.yml`
- Create: `docs/operations.md`
- Modify: `.env.example`
- Modify: `scripts/test-tooling.sh`
- Modify: `justfiles/dev.just`
- Modify: `justfiles/docker.just`
- Modify: `justfiles/ops.just`

- [ ] **Step 1: Write the failing deployment contract**

Create `scripts/test-deployment.sh`:

~~~bash
#!/usr/bin/env bash
set -euo pipefail

docker compose -f compose.yaml config >/dev/null
docker compose -f compose.yaml --profile observability config >/dev/null
bash -n scripts/backup-postgres.sh
bash -n scripts/restore-postgres.sh
bash -n scripts/init-db-roles.sh
grep -q 'ai_employee_retention' scripts/init-db-roles.sh
grep -q 'flush_interval -1' Caddyfile
grep -q 'appendonly yes' compose.yaml
grep -q '/run/secrets/app_master_key' compose.yaml
just --list | grep -q 'observability-up'
just --list | grep -q 'observability-down'
~~~

- [ ] **Step 2: Run the deployment contract and verify failure**

Run: `bash scripts/test-deployment.sh`

Expected: FAIL because Compose, Caddy, and scripts do not exist.

- [ ] **Step 3: Create production images and Compose services**

`compose.yaml` must define:

- caddy;
- api;
- worker;
- scheduler;
- postgres;
- redis.

Under the optional `observability` profile, also define:

- Prometheus, scraping the internal API, Worker, and Scheduler metrics endpoints;
- Grafana, provisioned with Prometheus as its default data source and no directly published host port.

Add `just observability-up` and `just observability-down` recipes that wrap the profile without
changing the default `just dev` service set.

Update `just health` to require healthy Compose state for API, Worker, Scheduler, PostgreSQL, and
Redis, then call API readiness; any missing or stale process exits nonzero.

Requirements:

- only Caddy publishes host ports;
- API, Worker, and Scheduler share the backend image;
- migrations run as a one-shot service before API startup, then rerun `init-db-roles.sh` so grants apply to newly created tables;
- PostgreSQL and Redis have health checks and persistent volumes;
- API, Worker, and Scheduler health checks verify their HTTP readiness/metrics listener and recent process heartbeat;
- Redis starts with AOF enabled;
- secrets mount as files under `/run/secrets`;
- production secret files cover the PostgreSQL bootstrap password, retention DSN, app master key, Google client secret, model API key, backup passphrase, and Grafana administrator password; none are committed to Git;
- PostgreSQL initialization creates separate application and retention roles; the application role has no UPDATE/DELETE privilege on `audit_events`, and the retention DSN is mounted only into Worker/Scheduler containers, never API, and injected only into retention/privacy use cases;
- Worker command uses Taskiq `--ack-type when_executed`;
- restart policy is `unless-stopped`;
- JSON log rotation is configured;
- Prometheus and Grafana start only with `--profile observability`, use persistent volumes, and contain no application secrets;
- production images use an explicit immutable `APP_IMAGE_TAG`, never an implicit `latest`;
- migrations run as a separate forward-compatible release step before the new API/Worker/Scheduler instances.

`frontend/Dockerfile` must be multi-stage: build the Vue app with Node 24 and pnpm, then use Caddy as the final image and copy `dist/` to `/srv`. The Compose `caddy` service uses this final image, so production does not run a separate Node process.

Both Docker build contexts exclude VCS metadata, local environments, node_modules, test reports,
coverage, local secrets, and `.env` files.

`compose.dev.yaml` adds bind mounts, Vite, reload, development cookies, and local secret files.

`init-db-roles.sh` reads explicit application and retention password files, creates or alters
`ai_employee_app` and `ai_employee_retention`, grants the application role normal table access
except UPDATE/DELETE on `audit_events`, and grants the retention role only the table operations
used by retention/privacy cleanup. It tolerates a pre-migration bootstrap run, is idempotent when
the migration service reapplies grants, and must never print either password.

- [ ] **Step 4: Configure Caddy for SPA, API, and SSE**

Use:

~~~caddy
{$APP_DOMAIN} {
    encode zstd gzip

    handle /api/* {
        reverse_proxy api:8000 {
            flush_interval -1
        }
    }

    handle /metrics {
        respond 404
    }

    handle {
        root * /srv
        try_files {path} /index.html
        file_server
    }
}
~~~

Production deployment sets `APP_DOMAIN` to the real host name.

Add a Grafana reverse-proxy path such as `/ops/grafana/*` through Caddy, retain Grafana's own
login, configure its root URL for subpath serving, and leave `/metrics` inaccessible from the
public site.

- [ ] **Step 5: Implement encrypted backup and guarded restore**

`backup-postgres.sh` must:

- resolve an explicit backup directory;
- create it when absent;
- run `pg_dump --format=custom`;
- encrypt the dump with OpenSSL AES-256-CBC, PBKDF2, and a passphrase file;
- write a SHA-256 checksum;
- keep 7 daily and 4 weekly files;
- copy the encrypted artifact and checksum to a configured off-host `rclone` remote;
- refuse a production backup when the off-host remote is missing;
- never print the passphrase.

`restore-postgres.sh` must:

- require one explicit existing file;
- refuse production unless `ALLOW_PRODUCTION_RESTORE=yes`;
- verify checksum;
- decrypt to a `mktemp` directory;
- run `pg_restore --clean --if-exists`;
- remove the temporary directory with a trap.

`docs/operations.md` defines a monthly restore drill, immutable image rollback, and a
forward-compatible migration rule: application images may roll back, but production migrations
are never destructively downgraded. It also requires encrypted-at-rest VPS volumes and documents
TLS, secret rotation, Google token revocation, and model-key rotation.

- [ ] **Step 6: Add CI that calls project commands**

The workflow must:

- install just, uv, Node, pnpm, and Playwright browsers;
- start PostgreSQL and Redis services;
- create test secret files;
- set `E2E_ADMIN_EMAIL`, `E2E_ADMIN_PASSWORD_FILE`, `TEST_DATABASE_URL`, and isolated Redis database 15;
- create application and retention database roles before integration tests;
- run `just ci`;
- upload Playwright traces and coverage on failure;
- never use live Google or model credentials.

- [ ] **Step 7: Run deployment validation**

Run:

~~~bash
chmod +x scripts/init-db-roles.sh scripts/backup-postgres.sh scripts/restore-postgres.sh scripts/test-deployment.sh
bash scripts/test-deployment.sh
docker compose -f compose.yaml build
docker compose -f compose.yaml --profile observability config
docker compose -f compose.yaml up -d postgres redis
docker compose -f compose.yaml ps
~~~

Expected: deployment contract passes, images build, and infrastructure is healthy.

- [ ] **Step 8: Commit deployment and operations**

~~~bash
git add backend/Dockerfile backend/.dockerignore frontend/Dockerfile frontend/.dockerignore compose.yaml compose.dev.yaml Caddyfile .github scripts ops/observability docs/operations.md .env.example justfiles
git commit -m "ops: add production deployment and recovery"
~~~

### Task 20: Prove fault recovery, complete acceptance tests, and freeze the M1 release

**Files:**
- Modify: `backend/src/ai_employee/main.py`
- Create: `backend/tests/integration/faults/test_duplicate_delivery.py`
- Create: `backend/tests/integration/faults/test_worker_recovery.py`
- Create: `backend/tests/integration/faults/test_redis_loss.py`
- Create: `backend/tests/integration/faults/test_provider_failures.py`
- Create: `frontend/e2e/reconnect.spec.ts`
- Create: `frontend/e2e/provider-errors.spec.ts`
- Create: `backend/src/ai_employee/api/routers/test_support.py`
- Create: `backend/tests/unit/api/test_test_support.py`
- Create: `backend/tests/evals/daily_brief_cases.json`
- Create: `backend/tests/evals/test_daily_brief_regression.py`
- Create: `docs/acceptance-checklist.md`
- Create: `README.md`

- [ ] **Step 1: Write failure-injection tests**

Cover:

- duplicate Taskiq delivery results in one task result and one tool execution;
- a worker that loses its lease cannot commit a terminal result;
- an expired lease allows another worker to resume from the latest LangGraph checkpoint;
- flushing Redis leaves PostgreSQL state intact and Outbox republishes pending work;
- Gmail 429 respects Retry-After;
- Calendar 5xx retries with exponential delay;
- revoked OAuth produces user-action-required state;
- two invalid model responses produce a partial brief, not fabricated success;
- SSE disconnect and browser refresh recover durable events from PostgreSQL;
- the test-support router is absent unless `APP_ENV=test` and `APP_TEST_MODE=true`;
- a test-only failure scenario expires from Redis after 600 seconds.

- [ ] **Step 2: Run fault tests and verify failures before final fixes**

Run:

~~~bash
uv run --project backend pytest backend/tests/integration/faults backend/tests/unit/api/test_test_support.py -q
pnpm --dir frontend test:e2e e2e/reconnect.spec.ts e2e/provider-errors.spec.ts
~~~

Expected: each newly introduced scenario fails for a specific missing guard or recovery path.

- [ ] **Step 3: Add only the recovery guards required by the tests**

Implement:

- lease-owner compare-and-set on completion;
- a recovery scan that creates a fresh unpublished OutboxEvent for stale QUEUED tasks or RUNNING tasks with expired leases, even when an earlier Outbox row was marked published; use `task.execute:{task_id}:recovery:{utc_five_minute_bucket}` to prevent scan duplicates;
- retry-after parsing and capped jittered delays;
- OAuth connection degraded status and reconnect action;
- final snapshot reconciliation after SSE terminal event;
- partial-brief persistence after model repair failure;
- an authenticated, CSRF-protected test-only scenario endpoint backed by a per-user Redis key with TTL 600 seconds; fake adapters consume it atomically once, and the router is never registered outside test mode.

`frontend/e2e/provider-errors.spec.ts` uses that endpoint to verify revoked OAuth shows a reconnect
action and a single-source failure produces a partial warning with missing source, last-success
time, and repair action.

- [ ] **Step 4: Add the prompt regression dataset**

Create at least 50 synthetic or sanitized cases across:

- work, notification, spam, and other;
- urgent and normal;
- needs reply and no reply;
- explicit and absent deadlines;
- conflicting and non-conflicting schedules;
- Chinese and English messages;
- malformed or adversarial HTML.

The regression test stores the approved baseline and fails when:

- a deterministic rule changes unexpectedly;
- an urgent labelled case becomes normal;
- a source reference disappears;
- a summary introduces a fact not present in the fixture.

- [ ] **Step 5: Run the complete local acceptance suite**

Run:

~~~bash
just ci
bash scripts/test-deployment.sh
docker compose -f compose.yaml up -d
just health
~~~

Expected:

- backend unit, integration, contract, fault, and evaluation tests pass;
- frontend unit, type, lint, build, and E2E tests pass;
- deployment contract passes;
- `just health` reports API, PostgreSQL, Redis, Worker, and Scheduler ready.

- [ ] **Step 6: Execute the manual acceptance checklist**

`docs/acceptance-checklist.md` must require evidence for:

- read-only Google scopes;
- scheduled and manual brief generation;
- source links;
- partial source failure;
- overdue brief red alert and idempotent diagnostic task;
- page refresh and SSE reconnect;
- fake approval approve, reject, expiry, and stale hash;
- source-cache deletion, individual conversation deletion, session revocation, and all-data deletion audit;
- configured retention cutoffs and Redis temporary-key TTL;
- log scan with no tokens or complete body;
- encrypted backup restore;
- 200-email and 20-event brief completed within five minutes;
- start of the 14-day no-duplicate and no-silent-miss soak test.

- [ ] **Step 7: Update README and commit release readiness**

README must contain quick start, first-admin creation, `just` commands, architecture summary, OAuth setup, test modes, deployment link, and explicit M1 exclusions.

~~~bash
git add backend/src/ai_employee/main.py backend/src/ai_employee/api/routers/test_support.py backend/tests frontend/e2e docs/acceptance-checklist.md README.md
git commit -m "test: complete M1 acceptance coverage"
~~~

## Spec Coverage Review

| Specification requirement | Implementing tasks |
|---|---|
| Tooling, just, uv, pnpm | 1 |
| FastAPI shell and system health | 2 |
| PostgreSQL, Alembic, user isolation base | 3 |
| Single-admin creation/login, secure Cookie, CSRF, session list/revoke | 4 |
| Stable domain errors and trusted task/approval state model | 5 |
| Task, step, audit, approval, tool execution, Outbox | 6 |
| Taskiq, Redis Streams, lease, retry, timeout budgets, core scheduler | 7 |
| LangGraph checkpoint and human interrupt | 8 |
| REST, Problem Details, SSE replay | 9 |
| PostgreSQL-only durable events/results and bounded Redis temporary TTL | 7, 9, 18 |
| Vue auth, task store, execution timeline | 10 |
| Encrypted credential table, OAuth, disconnect, and read-only Google scopes | 11 |
| Gmail incremental sync and cursor recovery | 12 |
| Calendar incremental sync, cursor recovery, and ten-minute Google sync schedule | 13 |
| Category/urgency separation and conflict rules | 14 |
| Model gateway, structured output, prompt version, brief graph | 15 |
| Conversations and deletion, brief versions, settings, scheduled/manual generation, APIs | 16 |
| ChatGPT-style UI, historical versions, settings, session revoke, sources, approvals | 17 |
| Error taxonomy, redaction, metrics, tracing, overdue diagnostics, privacy deletion, configurable retention | 18 |
| Docker Compose, Caddy, DB roles, optional observability profile, CI, off-host backup and restore | 19 |
| Fault recovery, evals, performance, 14-day soak gate | 20 |

## Execution Order and Checkpoints

- Tasks 1–10 produce a working fake-tool vertical slice. Stop and review task replay, approval, and browser recovery before adding Google.
- Tasks 11–16 produce the real read-only daily brief backend. Stop and review real source selection, privacy, and brief correctness.
- Tasks 17–20 complete user experience, operations, and release evidence.
- If a checkpoint reveals a design-level contradiction, update the approved design spec before changing this plan.
