# Executable Mail and Calendar Assistant M2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the trusted M1 task center into an M2 assistant that can incrementally connect Google and Microsoft mail/calendar data, prepare editable local actions, and execute one precisely approved external write with durable reconciliation.

**Architecture:** Preserve the modular monolith and the existing PostgreSQL-backed `TaskRun → ApprovalRequest → ToolExecution → Audit/Outbox/SSE` chain. Add a mail/calendar-only typed command union, encrypted draft/proposal storage, provider-neutral read/write ports, and isolated Google/Microsoft adapters; LangGraph coordinates pause/resume while application/domain code owns validation, state transitions, idempotency, and reconciliation.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL, Redis, Taskiq, LangGraph 1.x, httpx, PyJWT, Babel, Python tzdata, Vue 3, TypeScript, Vite, Pinia, Vitest, Playwright, Docker Compose, Caddy, uv, pnpm, just.

---

## Execution Rules

- The approved source of truth is `docs/superpowers/specs/2026-08-06-executable-mail-calendar-assistant-m2-design.md`. Stop if implementation evidence contradicts a command, scope, approval, retry, or reconciliation invariant in that specification.
- Execute every behavior change red-green-refactor: add the focused failing test, run it and read the expected failure, implement the smallest complete behavior, rerun focused and adjacent tests, then commit.
- Keep legacy `fake.write` records and M1 read paths compatible. A real M2 command must never fall back from encrypted payload columns to `ApprovalRequest.payload`.
- Keep all real writes disabled by default. Automated tests use Fake providers and sanitized HTTP fixtures; only the release task may describe a separately authorized test-account run.
- Never place addresses, subjects, bodies, event titles, descriptions, locations, attendee lists, tokens, raw provider responses, or decrypted commands in logs, audit metadata, metrics labels, SSE payloads, URLs, fixtures copied from real accounts, or commit messages.
- Use the exact task commit shown after its focused checks pass. Do not combine two numbered tasks, and do not combine two provider write adapters in one commit.
- Run `just check` before the final release-evidence task and `just ci` plus the M2 fault/release contracts before claiming the milestone is releasable.
- Every test helper or uppercase fixture symbol shown below must be defined in that task's test module or
  its explicit `conftest.py`; no snippet relies on an undeclared production global. Reuse these fixed
  synthetic identities unless a contract fixture requires a provider string:

~~~text
USER_ID                    00000000-0000-0000-0000-000000000101
OTHER_USER_ID              00000000-0000-0000-0000-000000000102
GOOGLE_CONNECTION_ID       00000000-0000-0000-0000-000000000201
MICROSOFT_CONNECTION_ID    00000000-0000-0000-0000-000000000202
DRAFT_ID                   00000000-0000-0000-0000-000000000301
PROPOSAL_ID                00000000-0000-0000-0000-000000000302
TASK_ID                    00000000-0000-0000-0000-000000000401
STEP_ID                    00000000-0000-0000-0000-000000000402
APPROVAL_ID                00000000-0000-0000-0000-000000000403
TOOL_EXECUTION_ID          00000000-0000-0000-0000-000000000404
EVENT_ID                   00000000-0000-0000-0000-000000000501
SNAPSHOT_ID                00000000-0000-0000-0000-000000000502
OPERATION_ID               00000000-0000-0000-0000-000000000601
NOW                         2030-01-01T00:00:00Z
SOURCE_MESSAGE_ID           synthetic-message-1
SOURCE_THREAD_ID            synthetic-thread-1
OTHER_APPROVAL_ID           00000000-0000-0000-0000-000000000405
VERSION_ID                  00000000-0000-0000-0000-000000000303
REQUEST_ID                  synthetic-request-1
FROZEN_DRAFT_VERSION        3
~~~

  `TASK_VERSION` always means the canonical decimal string for the current persisted audit-event
  cursor loaded by the fixture, and `PREVIOUS_AUDIT_EVENT_ID` is the canonical string for an actual
  earlier cursor from the same task. Addresses use
  `.example.test`, provider IDs use `synthetic-*`, and helper factories must construct the complete
  strict schema rather than bypass validation. Generic `CONNECTION_ID` and `SOURCE_CONNECTION_ID`
  alias `GOOGLE_CONNECTION_ID`; `STALE_CONNECTION_ID` aliases `MICROSOFT_CONNECTION_ID` in partial
  completeness examples. Retention tests define `RETAIN_AFTER` from the row's actual deadline, and
  hash constants are complete 64-character synthetic hex strings.

## File Map

### Milestone, configuration, and operations

- `AGENTS.md`, `backend/AGENTS.md`, `frontend/AGENTS.md` — replace the M1-only collaboration boundary with the approved M2 scope while preserving all trust and privacy rules.
- `README.md`, `docs/acceptance-checklist.md`, `docs/operations.md` — document M2 user capabilities, release evidence, kill switches, reconciliation, Microsoft consent, and incident response.
- `.env.example`, `compose.yaml`, `compose.dev.yaml`, `backend/src/ai_employee/config.py` — define fail-closed global/provider write switches, Microsoft OAuth settings, and the dedicated test-account allowlist.
- `scripts/test-m2-release.sh`, `scripts/verify-m2-sensitive-output.py`, `docs/releases/2026-08-06-m2-release-evidence.md` — automate the non-provider release gate and record completed automated/manual release evidence without an unowned template.

### Backend domain and application

- `backend/src/ai_employee/domain/mail_actions.py` — immutable mail modes, normalized addresses, recipient rules, and `MailSendCommand`.
- `backend/src/ai_employee/domain/calendar_actions.py` — immutable create/update/restore commands and notification rules.
- `backend/src/ai_employee/domain/actions.py` — proposal/tool lifecycle, risk, write-outcome, and safe retry/reconciliation decisions.
- `backend/src/ai_employee/domain/calendar_availability.py` — deterministic working-hours, buffer, DST, completeness, and candidate-time algorithm.
- `backend/src/ai_employee/domain/tasks.py`, `backend/src/ai_employee/domain/connections.py`, `backend/src/ai_employee/domain/settings.py` — extend existing task, approval, capability, and settings invariants.
- `backend/src/ai_employee/application/commands.py` — strict Pydantic command schemas, domain mapping, canonical JSON, and stable SHA-256 hashing.
- `backend/src/ai_employee/application/ports/oauth.py`, `mail.py`, `calendar.py`, `trusted_actions.py` — provider-neutral OAuth, sync, write, and reconciliation contracts.
- `backend/src/ai_employee/application/use_cases/connections.py`, `sync_mail.py`, `sync_calendar.py` — provider-neutral connection capability and incremental-sync orchestration.
- `backend/src/ai_employee/application/use_cases/mail_drafts.py`, `calendar_proposals.py` — editable local content, versions, generation, conflict suggestions, restore proposals, and submission.
- `backend/src/ai_employee/application/use_cases/trusted_actions.py`, `action_views.py` — encrypted freezing, approval invalidation, claim, execution, reconciliation, manual resolution, and unified action snapshots.
- `backend/src/ai_employee/agents/trusted_actions/` — checkpoint state containing identifiers/hashes only and the fixed approval/execution graph.
- `backend/src/ai_employee/prompts/mail_draft_v1.md` — versioned body-only drafting prompt.

### Backend persistence, integrations, and workers

- `backend/migrations/versions/20260806_0011_m2_connections_sources.py` — connection capabilities, provider calendars, scoped cursors, normalized provider identity, source fields, and working settings.
- `backend/migrations/versions/20260806_0012_m2_trusted_actions.py` — mail drafts/versions, calendar proposals/snapshots, encrypted approvals, and extended tool execution facts.
- `backend/migrations/versions/20260806_0013_m2_provider_neutral_cursors.py` — rename the legacy `gmail` cursor resource to provider-neutral `mail` after the shared sync code is ready.
- `backend/src/ai_employee/infrastructure/db/models/actions.py` — M2 action-content ORM models.
- `backend/src/ai_employee/infrastructure/db/models/identity.py`, `sources.py`, `tasks.py` — settings, provider, approval, and tool-execution extensions.
- `backend/src/ai_employee/infrastructure/db/repositories/mail_drafts.py`, `calendar_proposals.py`, `trusted_actions.py`, `action_views.py` — user-scoped transactional adapters.
- `backend/src/ai_employee/infrastructure/db/repositories/connections.py`, `email.py`, `calendar.py` — capabilities, provider-neutral source storage, and per-scope cursor CAS.
- `backend/src/ai_employee/infrastructure/security/action_payloads.py` — canonical JSON AEAD helpers and record-bound AAD.
- `backend/src/ai_employee/integrations/google/` — progressive OAuth, directory/sync refactors, Gmail send/reconcile, and Calendar write/reconcile.
- `backend/src/ai_employee/integrations/microsoft/` — OIDC/OAuth, Graph mail/calendar delta, mail/calendar writes, reconciliation, and IANA/Windows timezone mapping.
- `backend/src/ai_employee/integrations/registry.py` — explicit provider adapter selection; no dynamic tool registration.
- `backend/src/ai_employee/workers/sync_mail.py`, `sync_calendar.py`, `generate_mail_draft.py`, `trusted_actions.py`, `reconcile_actions.py` — durable provider-neutral work.
- `backend/src/ai_employee/workers/execute_task.py`, `schedules.py`, `retention.py`, `privacy.py` — register new task kinds and lifecycle scans without running long work in the API process.

### Backend API and observability

- `backend/src/ai_employee/api/routers/mail.py`, `calendar.py`, `actions.py` — draft, proposal, unified action, reconcile, and manual-resolution endpoints.
- `backend/src/ai_employee/api/routers/connections.py`, `approvals.py`, `settings.py`, `privacy.py`, `tasks.py` — capability, encrypted approval, settings, deletion, and new task-state contracts.
- `backend/src/ai_employee/api/deps.py`, `backend/src/ai_employee/main.py` — compose narrow use cases and provider registries.
- `backend/src/ai_employee/api/sse.py` — map new durable action events to content-free SSE payloads.
- `backend/src/ai_employee/infrastructure/observability/metrics.py`, `redaction.py` — fixed low-cardinality M2 metrics and sensitive-field filtering.

### Frontend

- `frontend/src/api/types.ts`, `connections.ts`, `mail.ts`, `calendar.ts`, `actions.ts`, `approvals.ts`, `settings.ts` — validate all new REST/SSE contracts at the browser boundary.
- `frontend/src/stores/actions.ts`, `frontend/src/stores/tasks.ts` — server-authoritative action projections and new task statuses/events.
- `frontend/src/pages/ActionsPage.vue`, `MailDraftPage.vue`, `CalendarProposalPage.vue` — unified action center and focused editors.
- `frontend/src/components/MailApprovalPreview.vue`, `CalendarApprovalPreview.vue`, `ApprovalCard.vue`, `NeedsAttentionPanel.vue` — typed approval and unknown-result interaction.
- `frontend/src/pages/ConnectionsPage.vue`, `SettingsPage.vue`, `frontend/src/components/AppShell.vue`, `frontend/src/router/index.ts` — capability controls, defaults, working hours, navigation, and responsive access.

### Tests and release evidence

- `backend/tests/unit/domain/` — command, address, lifecycle, capability, availability, and retry rules.
- `backend/tests/unit/application/` — command hashing, model input, submission, invalidation, and action use cases with Fakes.
- `backend/tests/integration/m2/` — migrations, encryption/AAD, version conflicts, claim races, checkpoints, revocation, reconciliation, deletion, and retention.
- `backend/tests/contract/google/`, `backend/tests/contract/microsoft/` — sanitized OAuth, delta, mail write, calendar write, timeout, and reconciliation fixtures.
- `frontend/src/**/*.spec.ts` — action store, editors, structured approval, needs-attention, connection, settings, and accessibility behavior.
- `frontend/e2e/m2-actions.spec.ts`, `backend/tests/integration/faults/test_m2_write_recovery.py` — complete Fake-provider flows and crash-point evidence.

### Task 1: Adopt the M2 milestone boundary and fail-closed write configuration

**Files:**
- Modify: `AGENTS.md`
- Modify: `backend/AGENTS.md`
- Modify: `frontend/AGENTS.md`
- Modify: `README.md`
- Modify: `docs/acceptance-checklist.md`
- Modify: `.env.example`
- Modify: `compose.yaml`
- Modify: `compose.dev.yaml`
- Modify: `backend/src/ai_employee/config.py`
- Test: `backend/tests/unit/test_config.py`
- Test: `scripts/test-deployment.sh`

- [ ] **Step 1: Write the failing configuration contract**

Create `backend/tests/unit/test_config.py` with an isolated environment check:

~~~python
"""验证 M2 外部写入配置始终 fail closed。"""

import pytest

from ai_employee.config import Settings


def test_external_write_switches_default_off() -> None:
    """缺少显式开关时，全局及两个供应商都不得执行真实写入。"""
    settings = Settings(_env_file=None)

    assert settings.external_writes_enabled is False
    assert settings.google_writes_enabled is False
    assert settings.microsoft_writes_enabled is False
    assert settings.write_test_account_allowlist == []
    assert settings.provider_writes_enabled("google") is False
    assert settings.provider_writes_enabled("microsoft") is False
    assert settings.write_account_allowed("google::synthetic-account") is False


def test_provider_switch_cannot_bypass_global_switch() -> None:
    """单独打开供应商开关不能绕过全局停机开关。"""
    settings = Settings(
        _env_file=None,
        external_writes_enabled=False,
        google_writes_enabled=True,
        microsoft_writes_enabled=True,
    )

    assert settings.provider_writes_enabled("google") is False
    assert settings.provider_writes_enabled("microsoft") is False


def test_non_production_real_writes_require_an_exact_account_allowlist() -> None:
    """专用验证环境不能因空 allowlist 把真实写能力开放给任意连接。"""
    with pytest.raises(ValueError, match="WRITE_TEST_ACCOUNT_ALLOWLIST"):
        Settings(
            _env_file=None,
            app_env="staging",
            external_writes_enabled=True,
            google_writes_enabled=True,
            write_test_account_allowlist=[],
        )


def test_allowlist_uses_stable_provider_identity_not_account_email() -> None:
    """认领边界按规范化供应商身份精确匹配，避免邮箱变更或别名绕过。"""
    settings = Settings(
        _env_file=None,
        app_env="staging",
        external_writes_enabled=True,
        google_writes_enabled=True,
        write_test_account_allowlist=["google::synthetic-account"],
    )

    assert settings.write_account_allowed("google::synthetic-account") is True
    assert settings.write_account_allowed("google::different-account") is False
~~~

- [ ] **Step 2: Run the contract and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/test_config.py -q`

Expected: FAIL because the M2 write/OAuth settings and write-gate helpers do not exist.

- [ ] **Step 3: Add validated switches and Microsoft OAuth configuration**

Extend `Settings` in `backend/src/ai_employee/config.py`:

~~~python
    external_writes_enabled: bool = False
    google_writes_enabled: bool = False
    microsoft_writes_enabled: bool = False
    write_test_account_allowlist: list[str] = Field(default_factory=list)
    microsoft_client_id: str = ""
    microsoft_client_secret_file: Path = Path("/run/secrets/microsoft_client_secret")
    microsoft_redirect_uri: str = ""

    def provider_writes_enabled(self, provider: str) -> bool:
        """返回全局和固定供应商开关的逻辑与，未知供应商一律关闭。"""
        provider_switch = {
            "google": self.google_writes_enabled,
            "microsoft": self.microsoft_writes_enabled,
        }.get(provider, False)
        return self.external_writes_enabled and provider_switch

    def write_account_allowed(self, provider_identity_key: str) -> bool:
        """限制非生产真实验证只访问显式允许的稳定供应商身份。"""
        allowed = frozenset(self.write_test_account_allowlist)
        if allowed:
            return provider_identity_key in allowed
        return self.app_env == "production"
~~~

Extend the existing cross-field validator so `APP_ENV != "production"` plus an enabled global
and provider write switch requires a non-empty `WRITE_TEST_ACCOUNT_ALLOWLIST`. Each entry is the
canonical connection identity `provider:encoded_tenant:encoded_account`, retaining the logical
three-part provider/tenant/stable-account semantics. Google uses an empty tenant segment. Microsoft
keeps the stored `provider_account_id=<tenant>:<graph_user_id>` composite but emits only the Graph ID
in the encoded account segment after verifying the embedded tenant matches. RFC 3986 unreserved
characters remain literal; every other opaque UTF-8 byte uses uppercase `%HH`. Each decoded raw
component is limited to 255 Unicode characters, each encoded segment to 3060 ASCII characters, and
the existing Microsoft stored composite remains limited to 255 characters. A raw `:` in the Graph
ID is rejected because that stored composite would be ambiguous. Validation must reject malformed,
non-canonical, overlong, whitespace/control, or invalid UTF-8 entries without echoing the value.
The key is derived from the stable provider account ID, never from the account email, and the full
key is never emitted to logs, metrics, audit, SSE, API responses, or committed evidence. Production
may leave the list empty, in which case the approved three normal write gates still apply; a
non-empty production list is an additional restriction and is enforced identically.

Add these exact non-secret defaults to `.env.example` and both Compose service environments:

~~~dotenv
EXTERNAL_WRITES_ENABLED=false
GOOGLE_WRITES_ENABLED=false
MICROSOFT_WRITES_ENABLED=false
WRITE_TEST_ACCOUNT_ALLOWLIST=[]
MICROSOFT_CLIENT_ID=
MICROSOFT_CLIENT_SECRET_FILE=/run/secrets/microsoft_client_secret
MICROSOFT_REDIRECT_URI=http://localhost:8000/api/v1/connections/microsoft/callback
~~~

Mount `microsoft_client_secret` beside the existing Google/model secrets, but do not create or commit a secret value.

- [ ] **Step 4: Update milestone and acceptance language**

Change the three `AGENTS.md` files, README, and acceptance checklist so they state that M2 is approved and limited to the four typed actions `mail.send`, `calendar.create`, `calendar.update`, and `calendar.restore`. Preserve explicit exclusions for attachments, HTML mail, forwarding, event deletion/cancellation, recurrence writes, contacts, generic tools, multiple users, and product multi-agent behavior. Add release evidence fields for `just ci`, scope audit, sensitive-output scan, dedicated Google/Microsoft account E2E, unique ToolExecution audit, backup/restore, and post-call crash recovery.

- [ ] **Step 5: Run focused checks**

Run: `uv run --project backend pytest backend/tests/unit/test_config.py -q`

Expected: PASS.

Run: `bash scripts/test-deployment.sh`

Expected: PASS with both new write switches rendered as `false` in the synthetic deployment configuration.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 6: Commit**

~~~bash
git add AGENTS.md backend/AGENTS.md frontend/AGENTS.md README.md docs/acceptance-checklist.md .env.example compose.yaml compose.dev.yaml backend/src/ai_employee/config.py backend/tests/unit/test_config.py scripts/test-deployment.sh
git commit -m "docs: adopt M2 execution boundary"
~~~

### Task 2: Define the immutable typed command union and stable hashes

**Files:**
- Create: `backend/src/ai_employee/domain/mail_actions.py`
- Create: `backend/src/ai_employee/domain/calendar_actions.py`
- Create: `backend/src/ai_employee/application/commands.py`
- Test: `backend/tests/unit/domain/test_mail_actions.py`
- Test: `backend/tests/unit/domain/test_calendar_actions.py`
- Test: `backend/tests/unit/application/test_trusted_commands.py`

- [ ] **Step 1: Write failing command and hash tests**

Create tests that prove normalization, exact reply binding, limits, and cross-process JSON stability:

~~~python
from uuid import UUID

import pytest

from ai_employee.application.commands import parse_trusted_command, trusted_command_hash
from ai_employee.domain.mail_actions import MailSendCommand


def test_mail_command_hash_is_independent_of_object_key_order() -> None:
    payload = {
        "schema_version": "mail_send.v1",
        "action": "mail.send",
        "operation_id": "00000000-0000-0000-0000-000000000001",
        "connection_id": "00000000-0000-0000-0000-000000000002",
        "draft_id": "00000000-0000-0000-0000-000000000003",
        "draft_version": 4,
        "message_date": "2030-01-01T00:00:00Z",
        "mode": "new",
        "source_thread_id": None,
        "source_message_id": None,
        "to": ["owner@Example.test", "owner@example.test"],
        "cc": [],
        "bcc": [],
        "subject": "Synthetic subject",
        "body_text": "Synthetic body",
        "thread_headers": None,
    }

    command = parse_trusted_command(payload)

    assert isinstance(command, MailSendCommand)
    assert command.operation_id == UUID("00000000-0000-0000-0000-000000000001")
    assert command.to == ("owner@example.test",)
    assert trusted_command_hash(payload) == trusted_command_hash(dict(reversed(payload.items())))


def test_reply_requires_frozen_source_message_and_thread() -> None:
    payload = {
        "schema_version": "mail_send.v1",
        "action": "mail.send",
        "operation_id": "00000000-0000-0000-0000-000000000001",
        "connection_id": "00000000-0000-0000-0000-000000000002",
        "draft_id": "00000000-0000-0000-0000-000000000003",
        "draft_version": 1,
        "message_date": "2030-01-01T00:00:00Z",
        "mode": "reply",
        "source_thread_id": None,
        "source_message_id": None,
        "to": ["person@example.test"],
        "cc": [],
        "bcc": [],
        "subject": "Re: Synthetic",
        "body_text": "Reply",
        "thread_headers": {
            "in_reply_to": "<source-message@example.test>",
            "references": ["<source-message@example.test>"],
        },
    }

    with pytest.raises(ValueError, match="reply source binding"):
        parse_trusted_command(payload)
~~~

Calendar tests must instantiate each of `calendar_create.v1`, `calendar_update.v1`, and `calendar_restore.v1`, reject recurrence/conference/attachments as extra fields, reject `ends_at <= starts_at`, reject mixed date/datetime all-day representations, prove all-day end dates are exclusive, reject more than 50 attendees, require `base_etag` plus `before_snapshot_id` for update/restore, and prove `calendar_client_event_id()` is stable and contains only lowercase `0-9a-v` characters.

- [ ] **Step 2: Run the tests and observe the expected import failure**

Run: `uv run --project backend pytest backend/tests/unit/domain/test_mail_actions.py backend/tests/unit/domain/test_calendar_actions.py backend/tests/unit/application/test_trusted_commands.py -q`

Expected: FAIL because the three new modules do not exist.

- [ ] **Step 3: Implement immutable domain commands**

Create `backend/src/ai_employee/domain/mail_actions.py` with `MailMode`, address normalization that case-folds only the domain, recipient de-duplication, an exact reply-header value, and this frozen command:

~~~python
@dataclass(frozen=True, slots=True)
class ReplyThreadHeaders:
    """只承载应用生成的回复引用，不接受任意 MIME Header 名称。"""

    in_reply_to: str
    references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MailSendCommand:
    """表示一次已冻结、不可变且绑定本地草稿版本的邮件发送。"""

    schema_version: str
    action: str
    operation_id: UUID
    connection_id: UUID
    draft_id: UUID
    draft_version: int
    message_date: datetime
    mode: MailMode
    source_thread_id: str | None
    source_message_id: str | None
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body_text: str
    thread_headers: ReplyThreadHeaders | None
~~~

Create `backend/src/ai_employee/domain/calendar_actions.py` with `NotificationPolicy` and complete frozen `CalendarCreateCommand`, `CalendarUpdateCommand`, and `CalendarRestoreCommand` values. Each command carries `schema_version`, fixed `action`, `operation_id`, exact connection/calendar, title/description/location, start/end or all-day dates, IANA timezone, attendees, and notification policy. Create additionally carries the frozen `client_event_id`; update/restore additionally carry `provider_event_id`, `base_etag`, `before_snapshot_id`, and deterministic `changed_fields`. Timed commands require aware instants; all-day commands require ISO dates with an exclusive end date, and the two representations may never be mixed.

Define `calendar_client_event_id(operation_id)` in this provider-neutral domain module: encode the 16
UUID bytes with lowercase base32hex, remove padding, and prefix `a`. The result is stable and satisfies
Google's documented event-ID alphabet/length while remaining an opaque correlation value for other
providers; adapters may use or ignore it but may not regenerate a different value after approval.

Export the concrete calendar-only union in the same module so later adapters never invent their own
command set:

~~~python
type CalendarCommand = (
    CalendarCreateCommand | CalendarUpdateCommand | CalendarRestoreCommand
)
~~~

- [ ] **Step 4: Implement strict Pydantic schemas, mapping, and hashing**

Create `backend/src/ai_employee/application/commands.py` with `extra="forbid"`, discriminated action literals, tuple normalization validators, and explicit domain mapping:

~~~python
TrustedCommandSchema = Annotated[
    MailSendCommandSchema
    | CalendarCreateCommandSchema
    | CalendarUpdateCommandSchema
    | CalendarRestoreCommandSchema,
    Field(discriminator="action"),
]
_COMMAND_ADAPTER = TypeAdapter(TrustedCommandSchema)

type TrustedCommand = MailSendCommand | CalendarCommand


def parse_trusted_command(payload: Mapping[str, object]) -> TrustedCommand:
    """严格验证边界 JSON，再映射为不依赖 Pydantic 的冻结领域值。"""
    schema = _COMMAND_ADAPTER.validate_python(dict(payload))
    return schema.to_domain()


def canonical_command_json(payload: Mapping[str, object]) -> bytes:
    """生成 UTF-8、排序键、无多余空白且拒绝 NaN 的规范 JSON。"""
    validated = _COMMAND_ADAPTER.validate_python(dict(payload))
    return json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def trusted_command_hash(payload: Mapping[str, object]) -> str:
    """对包含 action 与 schema_version 的完整规范命令计算 SHA-256。"""
    return sha256(canonical_command_json(payload)).hexdigest()
~~~

The mail schema enforces an aware RFC 3339 `message_date` frozen at submission time, 255 Unicode characters for subject, 100,000 for body, one to 50 unique recipients across To/CC/BCC, immutable reply source fields, and no user-supplied From. Define a strict `ReplyThreadHeadersSchema` with only `in_reply_to` and `references`; new mail requires `thread_headers=None`, while reply modes require the exact normalized value produced from synchronized source facts. Calendar schemas reject recurrence, conference, attachment, and provider-extension fields by construction. Export `CalendarCommand` and `TrustedCommand` explicitly and add type-check tests that every adapter signature imports those aliases rather than recreating a broader union.

- [ ] **Step 5: Run focused tests and type checking**

Run: `uv run --project backend pytest backend/tests/unit/domain/test_mail_actions.py backend/tests/unit/domain/test_calendar_actions.py backend/tests/unit/application/test_trusted_commands.py -q`

Expected: PASS.

Run: `uv run --project backend mypy backend/src/ai_employee/domain/mail_actions.py backend/src/ai_employee/domain/calendar_actions.py backend/src/ai_employee/application/commands.py`

Expected: PASS with no untyped command fields.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/domain/mail_actions.py backend/src/ai_employee/domain/calendar_actions.py backend/src/ai_employee/application/commands.py backend/tests/unit/domain/test_mail_actions.py backend/tests/unit/domain/test_calendar_actions.py backend/tests/unit/application/test_trusted_commands.py
git commit -m "feat: define M2 trusted commands"
~~~

### Task 3: Extend task, proposal, capability, and write-outcome state rules

**Files:**
- Create: `backend/src/ai_employee/domain/actions.py`
- Modify: `backend/src/ai_employee/domain/tasks.py`
- Modify: `backend/src/ai_employee/domain/connections.py`
- Test: `backend/tests/unit/domain/test_action_state_machine.py`
- Test: `backend/tests/unit/domain/test_task_state_machine.py`
- Test: `backend/tests/unit/domain/test_connection_capabilities.py`

- [ ] **Step 1: Write failing lifecycle tests**

~~~python
import pytest

from ai_employee.domain.actions import (
    CalendarProposalStatus,
    ExecutionDirective,
    MailDraftStatus,
    ProviderWriteOutcomeKind,
    directive_for_outcome,
    transition_mail_draft,
)
from ai_employee.domain.tasks import TaskStatus, transition_task


def test_unknown_write_outcome_can_only_reconcile() -> None:
    assert directive_for_outcome(
        ProviderWriteOutcomeKind.UNKNOWN,
        retryable=False,
    ) is ExecutionDirective.RECONCILE


def test_permanent_confirmed_rejection_is_not_retried() -> None:
    assert directive_for_outcome(
        ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
        retryable=False,
    ) is ExecutionDirective.FAIL


def test_needs_attention_cannot_be_retried_as_a_write() -> None:
    with pytest.raises(Exception, match="cannot transition"):
        transition_task(TaskStatus.NEEDS_ATTENTION, TaskStatus.QUEUED)


def test_edit_after_rejection_returns_to_editing_but_old_version_stays_terminal() -> None:
    assert transition_mail_draft(
        MailDraftStatus.AWAITING_APPROVAL,
        MailDraftStatus.EDITING,
        reason="approval_rejected",
    ) is MailDraftStatus.EDITING


def test_calendar_etag_conflict_has_a_distinct_stale_state() -> None:
    assert CalendarProposalStatus.STALE.value == "stale"
~~~

Capability tests must prove `mail.send` requires `mail.read`, `calendar.write` requires `calendar.read`, closing a read capability while its write capability is enabled is rejected, and unknown capability strings never become enabled.

- [ ] **Step 2: Run the tests and observe the expected failures**

Run: `uv run --project backend pytest backend/tests/unit/domain/test_action_state_machine.py backend/tests/unit/domain/test_task_state_machine.py backend/tests/unit/domain/test_connection_capabilities.py -q`

Expected: FAIL because M2 states and capability types do not exist.

- [ ] **Step 3: Add pure M2 action and outcome rules**

Create `backend/src/ai_employee/domain/actions.py` with these stable values:

~~~python
class MailDraftStatus(StrEnum):
    EDITING = "editing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    SENT = "sent"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


class CalendarProposalStatus(StrEnum):
    EDITING = "editing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    APPLIED = "applied"
    STALE = "stale"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


class ToolExecutionStatus(StrEnum):
    CLAIMED = "claimed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    CONFIRMED_FAILED = "confirmed_failed"
    RETRYABLE_FAILED = "retryable_failed"
    RECONCILING = "reconciling"
    NEEDS_ATTENTION = "needs_attention"


class ProviderWriteOutcomeKind(StrEnum):
    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"
    UNKNOWN = "unknown"


class ExecutionDirective(StrEnum):
    COMPLETE = "complete"
    FAIL = "fail"
    RETRY_WRITE = "retry_write"
    RECONCILE = "reconcile"


def directive_for_outcome(
    outcome: ProviderWriteOutcomeKind,
    *,
    retryable: bool,
) -> ExecutionDirective:
    """只有“明确未应用”且错误类别允许重试时才再次调用写接口。"""
    if outcome is ProviderWriteOutcomeKind.CONFIRMED_APPLIED:
        return ExecutionDirective.COMPLETE
    if outcome is ProviderWriteOutcomeKind.UNKNOWN:
        return ExecutionDirective.RECONCILE
    return ExecutionDirective.RETRY_WRITE if retryable else ExecutionDirective.FAIL
~~~

Add separate immutable transition maps for mail drafts and calendar proposals so a mail draft can never
become `applied` and a calendar proposal can never become `sent`. Mail allows
`editing → awaiting_approval/cancelled`, `awaiting_approval → executing/editing/cancelled`,
`executing → sent/editing/needs_attention`, and manual convergence from `needs_attention` to
`sent/editing`. Calendar allows the equivalent flow with `applied`, plus
`executing/needs_attention → stale` for a confirmed ETag conflict and `stale → editing/cancelled`.
Require a reason for every return to `editing`, and forbid every transition out of `sent`, `applied`,
or `cancelled`.

- [ ] **Step 4: Extend existing task, approval, and capability enums**

In `domain/tasks.py`, add `RECONCILING` and `NEEDS_ATTENTION`, add `ApprovalStatus.INVALIDATED`, and
extend the immutable transition map with exactly `running → reconciling`,
`reconciling → succeeded/failed/needs_attention`, and `needs_attention → reconciling`. Change
`transition_task` to accept `manual_resolution: bool = False`; permit direct
`needs_attention → succeeded/failed` only when it is true. Do not add
`needs_attention → queued/running`, because neither user confirmation nor reconciliation may enqueue
another provider write.

In `domain/connections.py`, add:

~~~python
class ConnectionCapability(StrEnum):
    MAIL_READ = "mail.read"
    MAIL_SEND = "mail.send"
    CALENDAR_READ = "calendar.read"
    CALENDAR_WRITE = "calendar.write"


class CapabilityStatus(StrEnum):
    DISABLED = "disabled"
    AUTHORIZING = "authorizing"
    ENABLED = "enabled"
    DEGRADED = "degraded"
    ACTION_REQUIRED = "action_required"
    REVOKED = "revoked"


CAPABILITY_DEPENDENCIES: Final[Mapping[ConnectionCapability, frozenset[ConnectionCapability]]] = {
    ConnectionCapability.MAIL_SEND: frozenset({ConnectionCapability.MAIL_READ}),
    ConnectionCapability.CALENDAR_WRITE: frozenset({ConnectionCapability.CALENDAR_READ}),
}
~~~

Provide pure `validate_capability_enable()` and `validate_capability_disable()` functions; they return the dependency closure or raise `StateConflictError` with `connection_capability_dependency_conflict`.

- [ ] **Step 5: Run domain suites**

Run: `uv run --project backend pytest backend/tests/unit/domain/test_action_state_machine.py backend/tests/unit/domain/test_task_state_machine.py backend/tests/unit/domain/test_connection_capabilities.py backend/tests/unit/domain/test_approval_policy.py -q`

Expected: PASS, including all existing M1 transitions.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/domain/actions.py backend/src/ai_employee/domain/tasks.py backend/src/ai_employee/domain/connections.py backend/tests/unit/domain/test_action_state_machine.py backend/tests/unit/domain/test_task_state_machine.py backend/tests/unit/domain/test_connection_capabilities.py
git commit -m "feat: extend trusted action state rules"
~~~

### Task 4: Migrate connection capabilities, provider calendars, scoped cursors, and work settings

**Files:**
- Create: `backend/migrations/versions/20260806_0011_m2_connections_sources.py`
- Modify: `backend/src/ai_employee/infrastructure/db/models/identity.py`
- Modify: `backend/src/ai_employee/infrastructure/db/models/sources.py`
- Modify: `backend/src/ai_employee/infrastructure/db/models/__init__.py`
- Modify: `backend/tests/integration/conftest.py`
- Modify: `backend/tests/integration/db/test_migrations.py`
- Create: `backend/tests/integration/m2/test_connection_source_schema.py`

- [ ] **Step 1: Write the failing migration/schema assertions**

Extend `test_migrations.py` and add an integration test that asserts the new tables, direct non-null `user_id` ownership, scoped cursor uniqueness, Google backfill, and safe defaults:

~~~python
async def test_m2_connection_and_source_defaults(database_url: str) -> None:
    sessions = build_session_factory(database_url)
    async with sessions() as session:
        capability_count = await session.scalar(
            select(func.count()).select_from(ConnectionCapabilityModel)
        )
        user = UserModel(
            email="m2-schema@example.test",
            display_name="M2 Schema",
            password_hash=None,
            timezone="UTC",
            locale="zh-CN",
            brief_time=time(8),
            is_active=True,
        )
        session.add(user)
        await session.flush()

        assert capability_count == 0
        assert user.meeting_buffer_minutes == 10
        assert user.working_hours["monday"] == [["09:00", "18:00"]]
    await sessions.dispose()
~~~

The migration metadata test must expect `connection_capabilities` and `provider_calendars`, inspect a unique constraint named `uq_sync_cursors_connection_resource_scope` on `(connection_id, resource_kind, scope_key)`, and verify the user/connection composite ownership guards are deferrable.

- [ ] **Step 2: Run migration tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/integration/db/test_migrations.py backend/tests/integration/m2/test_connection_source_schema.py -q`

Expected: FAIL because revision `20260806_0011`, the two tables, columns, and ORM models are absent.

- [ ] **Step 3: Create the forward migration**

Create revision `20260806_0011` with `down_revision = "20260804_0010"`. Its `upgrade()` must:

~~~python
op.add_column("oauth_attempts", sa.Column("provider", sa.String(32), server_default="google", nullable=False))
op.add_column("oauth_attempts", sa.Column("requested_capabilities", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False))
op.add_column("oauth_attempts", sa.Column("oidc_nonce_hash", sa.LargeBinary(32), nullable=True))
op.add_column("oauth_connections", sa.Column("provider_tenant_id", sa.String(255), server_default="", nullable=False))
op.add_column("oauth_connections", sa.Column("account_type", sa.String(32), server_default="google", nullable=False))
op.create_unique_constraint(
    "uq_oauth_connections_id_user_id",
    "oauth_connections",
    ["id", "user_id"],
)
op.create_table(
    "connection_capabilities",
    sa.Column("user_id", sa.Uuid(), nullable=False),
    sa.Column("connection_id", sa.Uuid(), nullable=False),
    sa.Column("capability", sa.String(32), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("actual_scopes", postgresql.JSONB(), nullable=False),
    sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_error_code", sa.String(100), nullable=True),
    sa.Column("id", sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
    sa.ForeignKeyConstraint(
        ["connection_id", "user_id"],
        ["oauth_connections.id", "oauth_connections.user_id"],
        name="fk_connection_capabilities_connection_user",
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    ),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("user_id", "connection_id", "capability", name="uq_connection_capabilities_user_connection_capability"),
)
op.create_table(
    "provider_calendars",
    sa.Column("user_id", sa.Uuid(), nullable=False),
    sa.Column("connection_id", sa.Uuid(), nullable=False),
    sa.Column("provider_calendar_id", sa.String(512), nullable=False),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("timezone", sa.String(64), nullable=False),
    sa.Column("is_primary", sa.Boolean(), nullable=False),
    sa.Column("access_role", sa.String(32), nullable=False),
    sa.Column("can_write", sa.Boolean(), nullable=False),
    sa.Column("provider_url", sa.Text(), nullable=True),
    sa.Column("id", sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
    sa.ForeignKeyConstraint(
        ["connection_id", "user_id"],
        ["oauth_connections.id", "oauth_connections.user_id"],
        name="fk_provider_calendars_connection_user",
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    ),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("connection_id", "provider_calendar_id", name="uq_provider_calendars_connection_provider_calendar"),
)
~~~

Then add `scope_key` to `sync_cursors`, backfill Gmail rows to `mailbox` and Calendar rows to `primary`, make it non-null, replace the old two-column constraint with `uq_sync_cursors_connection_resource_scope`, and add source fields: `EmailMessageModel.internet_message_id`, `provider_conversation_id`, `sent_at`, `mailbox_scope_key`; `CalendarEventModel.organizer`, `attendees`, `access_role`, and `can_edit`.

In the same migration, insert all four capability rows for every existing Google connection. Set
`mail.read` and `calendar.read` to `enabled` only when the saved scope set contains their exact
approved read scope, otherwise `disabled`; set both write capabilities to `disabled` unconditionally.
Copy the connection's normalized saved scopes into `actual_scopes`, use the owning `user_id`, and make
the data migration idempotent with the unique capability key. A configuration switch must never
backfill a write capability as enabled.

Add nullable default mail/calendar connection IDs, `default_calendar_id`, non-null JSONB `working_hours`, and `meeting_buffer_minutes` with a 0–120 check to `users`. Default connection columns use composite ownership foreign keys back to `(oauth_connections.id, oauth_connections.user_id)`. Backfill Monday–Friday to `09:00–18:00` and weekends to empty arrays.

- [ ] **Step 4: Mirror the migration in typed ORM models**

Add `ConnectionCapabilityModel` and `ProviderCalendarModel` to `sources.py`, update `SyncCursorModel.__table_args__`, and add all new fields with explicit `Mapped[...]` types. Update `APPLICATION_TABLES` so child M2 tables precede `oauth_connections` and `users` during test cleanup.

- [ ] **Step 5: Run migration and model checks**

Run: `uv run --project backend pytest backend/tests/integration/db/test_migrations.py backend/tests/integration/m2/test_connection_source_schema.py backend/tests/integration/tasks/test_task_schema_ownership.py -q`

Expected: PASS from an empty temporary database and the shared migrated test database.

Run: `uv run --project backend mypy backend/src/ai_employee/infrastructure/db/models`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/migrations/versions/20260806_0011_m2_connections_sources.py backend/src/ai_employee/infrastructure/db/models/identity.py backend/src/ai_employee/infrastructure/db/models/sources.py backend/src/ai_employee/infrastructure/db/models/__init__.py backend/tests/integration/conftest.py backend/tests/integration/db/test_migrations.py backend/tests/integration/m2/test_connection_source_schema.py
git commit -m "feat: add M2 connection source schema"
~~~

### Task 5: Migrate encrypted drafts, proposals, approvals, and tool executions

**Files:**
- Create: `backend/migrations/versions/20260806_0012_m2_trusted_actions.py`
- Create: `backend/src/ai_employee/infrastructure/db/models/actions.py`
- Modify: `backend/src/ai_employee/infrastructure/db/models/tasks.py`
- Modify: `backend/src/ai_employee/infrastructure/db/models/__init__.py`
- Modify: `backend/tests/integration/conftest.py`
- Modify: `backend/tests/integration/db/test_migrations.py`
- Create: `backend/tests/integration/m2/test_trusted_action_schema.py`

- [ ] **Step 1: Write failing table, nullability, and uniqueness tests**

~~~python
def test_m2_approval_model_has_encrypted_command_columns_and_keeps_json_marker() -> None:
    columns = ApprovalRequestModel.__table__.columns

    assert columns["payload"].nullable is False
    assert columns["payload_ciphertext"].nullable is True
    assert columns["payload_nonce"].type.length == 12
    assert columns["payload_key_version"].nullable is True
    assert columns["schema_version"].nullable is True
~~~

The same file must assert user-scoped draft/proposal creation-idempotency uniqueness, `(draft_id, version)` and `(proposal_id, version, snapshot_kind)` uniqueness, direct non-null user ownership, 12-byte nonce checks, the exact ToolExecution idempotency uniqueness, and all provider correlation columns including `provider_request_id`.

- [ ] **Step 2: Run migration tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/integration/db/test_migrations.py backend/tests/integration/m2/test_trusted_action_schema.py -q`

Expected: FAIL because the M2 content tables and encrypted approval columns do not exist.

- [ ] **Step 3: Create the trusted-action migration**

Create revision `20260806_0012` with `down_revision = "20260806_0011"`. Create:

- `mail_drafts`: direct `user_id`, exact `connection_id`, user-scoped `creation_idempotency_key` plus canonical `creation_payload_hash`, optional source thread/message, mode, current version, status, and `retain_until`; `(connection_id, user_id)` must reference the same owned connection and `(user_id, creation_idempotency_key)` must be unique.
- `mail_draft_versions`: direct `user_id`, draft/version unique key, To/CC/BCC JSONB, subject, body AEAD triple, prompt/model metadata, and `created_at`; `(draft_id, user_id)` must reference the same owned draft.
- `calendar_change_proposals`: direct `user_id`, user-scoped `creation_idempotency_key` plus canonical `creation_payload_hash`, connection/calendar, operation kind, target event, base ETag, current version, status, and retention; `(connection_id, user_id)` must reference the same owned connection and `(user_id, creation_idempotency_key)` must be unique.
- `calendar_change_snapshots`: direct `user_id`, proposal/version/kind unique key, content AEAD triple, canonical hash, and retention; `(proposal_id, user_id)` must reference the same owned proposal.

For every AEAD triple, add database checks that the three columns are either all null (after retention
or on a legacy approval) or all non-null, and that every non-null nonce has exactly 12 bytes. Add an
equivalent all-or-none check for `manual_resolution`, `manual_resolved_by_user_id`, and
`manual_resolved_at`; a manual result may never exist without its actor and timestamp.

Extend `approval_requests` with nullable legacy-compatible `schema_version`, `risk_level`, `payload_ciphertext`, `payload_nonce`, `payload_key_version`, `proposal_kind`, `proposal_id`, `proposal_version`, and `approved_execution_deadline_at`. Keep `payload` non-null JSONB.

Extend `tool_executions` with `operation_id`, `provider`, `provider_resource_id`, `provider_request_id`, `correlation_id`, `claimed_at`, `request_started_at`, `completed_at`, `write_attempt_count` defaulting to zero, `reconciliation_attempt_count` defaulting to zero, `last_reconciled_at`, `manual_resolution`, `manual_resolved_by_user_id`, and `manual_resolved_at`. Add non-negative checks for both counters and a unique constraint named `uq_tool_executions_operation` over `(task_id, operation_id)` while retaining the existing idempotency-key constraint.

- [ ] **Step 4: Add focused ORM models**

Create `models/actions.py` with one class per table. Use `LargeBinary(12)` plus the migration checks for nonces, `JSONB` only for non-body address metadata, `DateTime(timezone=True)` for all lifecycle times, and Chinese docstrings that explain immutable versions and retention. Extend `ApprovalRequestModel` and `ToolExecutionModel` without removing M1 fields, mirroring every database check in ORM metadata tests.

- [ ] **Step 5: Run migration, ownership, and metadata checks**

Run: `uv run --project backend pytest backend/tests/integration/db/test_migrations.py backend/tests/integration/m2/test_trusted_action_schema.py backend/tests/integration/tasks/test_task_schema_ownership.py -q`

Expected: PASS.

Run: `uv run --project backend ruff check backend/migrations/versions/20260806_0012_m2_trusted_actions.py backend/src/ai_employee/infrastructure/db/models/actions.py backend/src/ai_employee/infrastructure/db/models/tasks.py`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/migrations/versions/20260806_0012_m2_trusted_actions.py backend/src/ai_employee/infrastructure/db/models/actions.py backend/src/ai_employee/infrastructure/db/models/tasks.py backend/src/ai_employee/infrastructure/db/models/__init__.py backend/tests/integration/conftest.py backend/tests/integration/db/test_migrations.py backend/tests/integration/m2/test_trusted_action_schema.py
git commit -m "feat: add M2 trusted action schema"
~~~

### Task 6: Encrypt and persist versioned drafts, proposals, and commands with record-bound AAD

**Files:**
- Modify: `backend/src/ai_employee/application/ports/encryption.py`
- Create: `backend/src/ai_employee/infrastructure/security/action_payloads.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/mail_drafts.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/calendar_proposals.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py`
- Create: `backend/tests/unit/security/test_action_payloads.py`
- Create: `backend/tests/integration/m2/test_action_content_repositories.py`

- [ ] **Step 1: Write failing AAD and versioning tests**

~~~python
from cryptography.exceptions import InvalidTag
import pytest

from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher


def test_encrypted_command_cannot_move_between_approval_records() -> None:
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    value = cipher.encrypt_json(
        {"schema_version": "mail_send.v1", "action": "mail.send"},
        user_id=USER_ID,
        record_id=APPROVAL_ID,
        content_kind="approval_command",
        action="mail.send",
        schema_version="mail_send.v1",
    )

    with pytest.raises(InvalidTag):
        cipher.decrypt_json(
            value,
            user_id=USER_ID,
            record_id=OTHER_APPROVAL_ID,
            content_kind="approval_command",
            action="mail.send",
            schema_version="mail_send.v1",
        )
~~~

The integration test must replay matching creation keys to one row, reject a reused creation key with a different canonical request hash, concurrently save the same mail draft version, assert exactly one `(draft_id, version)` row, reject a stale expected version with `draft_version_conflict`, reject cross-user reads as not found, reject decrypting the same record under a different trusted action, and prove the database never contains the plaintext body or calendar snapshot bytes.

- [ ] **Step 2: Run the tests and observe the expected failures**

Run: `uv run --project backend pytest backend/tests/unit/security/test_action_payloads.py backend/tests/integration/m2/test_action_content_repositories.py -q`

Expected: FAIL because the cipher wrapper and three repositories do not exist.

- [ ] **Step 3: Extend the encryption port and add canonical JSON AEAD**

Add `decrypt()` to the existing `Encryption` protocol. Create `ActionPayloadCipher`, delegating to `AeadCipher` and constructing AAD exactly as follows:

~~~python
def action_payload_aad(
    *,
    user_id: UUID,
    record_id: UUID,
    content_kind: str,
    action: str,
    schema_version: str,
) -> bytes:
    """绑定用户、记录、内容类别、动作和 Schema，阻止跨记录或跨动作替换。"""
    return (
        f"{user_id}:{record_id}:{content_kind}:{action}:{schema_version}"
    ).encode("ascii")


class ActionPayloadCipher:
    """只处理标准 JSON；调用方永远拿不到未验证的 Python 对象。"""

    def encrypt_json(
        self,
        payload: Mapping[str, object],
        *,
        user_id: UUID,
        record_id: UUID,
        content_kind: str,
        action: str,
        schema_version: str,
    ) -> EncryptedValue:
        plaintext = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self._cipher.encrypt(
            plaintext,
            action_payload_aad(
                user_id=user_id,
                record_id=record_id,
                content_kind=content_kind,
                action=action,
                schema_version=schema_version,
            ),
        )
~~~

`decrypt_json()` must authenticate the same AAD, decode UTF-8, require a JSON object root, and return `dict[str, object]`; it must not catch `InvalidTag` or convert it into a retryable provider error. Approval commands pass their exact trusted `action`; local draft bodies and calendar snapshots pass fixed internal actions `mail.draft` and `calendar.snapshot`, so every encrypted record remains bound to both its storage role and semantic action.

- [ ] **Step 4: Implement transactional content repositories**

`SqlAlchemyMailDraftRepository` must expose `create`, `get_current`, `save_next_version`, `lock_for_submit`, and `cancel`; `create` reuses the same current-user row only when the existing creation key's canonical request hash matches, otherwise it raises `idempotency_key_payload_mismatch`. `save_next_version` updates the draft counter with `WHERE current_version = :expected_version` and raises `StateConflictError(error_code="draft_version_conflict", message="mail draft version changed")` when the row count is zero.

`SqlAlchemyCalendarProposalRepository` must expose the same hash-bound user-scoped creation-idempotency and optimistic version patterns plus `save_snapshot`, `load_snapshot`, `mark_stale`, and `create_restore_proposal`. Encrypt each snapshot with its own snapshot row ID and `calendar_snapshot.v1` AAD.

`SqlAlchemyTrustedActionRepository` must write a real command using only this plaintext marker:

~~~python
approval.payload = {
    "storage": "encrypted",
    "schema_version": schema_version,
}
approval.payload_ciphertext = encrypted.ciphertext
approval.payload_nonce = encrypted.nonce
approval.payload_key_version = encrypted.key_version
approval.payload_hash = trusted_command_hash(command_payload)
~~~

Its read path must branch explicitly: `schema_version is None` is the legacy `fake.write` path; every non-null M2 schema requires all three encrypted columns and never reads command fields from `payload`.

- [ ] **Step 5: Run focused repository and encryption checks**

Run: `uv run --project backend pytest backend/tests/unit/security/test_action_payloads.py backend/tests/integration/m2/test_action_content_repositories.py backend/tests/unit/security/test_encryption.py -q`

Expected: PASS.

Run: `uv run --project backend mypy backend/src/ai_employee/infrastructure/security/action_payloads.py backend/src/ai_employee/infrastructure/db/repositories/mail_drafts.py backend/src/ai_employee/infrastructure/db/repositories/calendar_proposals.py backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/application/ports/encryption.py backend/src/ai_employee/infrastructure/security/action_payloads.py backend/src/ai_employee/infrastructure/db/repositories/mail_drafts.py backend/src/ai_employee/infrastructure/db/repositories/calendar_proposals.py backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py backend/tests/unit/security/test_action_payloads.py backend/tests/integration/m2/test_action_content_repositories.py
git commit -m "feat: persist encrypted action content"
~~~

### Task 7: Refactor source synchronization behind provider-neutral ports and scoped cursors

**Files:**
- Create: `backend/migrations/versions/20260806_0013_m2_provider_neutral_cursors.py`
- Create: `backend/src/ai_employee/application/ports/mail.py`
- Modify: `backend/src/ai_employee/application/ports/gmail.py`
- Modify: `backend/src/ai_employee/application/ports/calendar.py`
- Create: `backend/src/ai_employee/application/use_cases/sync_mail.py`
- Modify: `backend/src/ai_employee/application/use_cases/sync_gmail.py`
- Modify: `backend/src/ai_employee/application/use_cases/sync_calendar.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/email.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/calendar.py`
- Create: `backend/src/ai_employee/integrations/registry.py`
- Create: `backend/src/ai_employee/workers/sync_mail.py`
- Modify: `backend/src/ai_employee/workers/sync_gmail.py`
- Modify: `backend/src/ai_employee/workers/sync_calendar.py`
- Modify: `backend/src/ai_employee/workers/execute_task.py`
- Modify: `backend/src/ai_employee/workers/schedules.py`
- Modify: `backend/tests/unit/test_layer_boundaries.py`
- Create: `backend/tests/unit/application/test_provider_neutral_sync.py`
- Modify: `backend/tests/integration/google/test_gmail_sync.py`
- Modify: `backend/tests/integration/google/test_calendar_sync.py`
- Modify: `backend/tests/integration/workers/test_google_sync_schedule.py`
- Modify: `backend/tests/integration/db/test_migrations.py`

- [ ] **Step 1: Write failing provider-neutral contracts**

~~~python
async def test_mail_sync_selects_adapter_from_connection_provider() -> None:
    registry = FakeReadAdapterRegistry()
    registry.mail_readers["microsoft"] = FakeMailReader(
        pages=(MailSyncPage(messages=(), next_page_token=None, next_cursor="delta-1"),)
    )

    result = await SyncMailUseCase(stores, registry).execute(
        user_id=USER_ID,
        connection_id=MICROSOFT_CONNECTION_ID,
        scope_key="inbox",
    )

    assert registry.requested == [("microsoft", MICROSOFT_CONNECTION_ID, "inbox")]
    assert result.next_cursor == "delta-1"
~~~

Add a Calendar test with two calendar IDs and two independent cursors; advancing one must not change the other. Extend the layer-boundary test so application/domain modules cannot import `integrations.google`, `integrations.microsoft`, or provider SDK types.

- [ ] **Step 2: Run the tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/application/test_provider_neutral_sync.py backend/tests/unit/test_layer_boundaries.py -q`

Expected: FAIL because only Gmail-named ports and connection-wide Calendar cursors exist.

- [ ] **Step 3: Define normalized read contracts**

Create `application/ports/mail.py` with immutable `MailScope`, `MailMessage`, `MailSyncPage`, and
`MailReader`. `MailScope` carries only opaque `scope_key`, safe display name, and optional normalized
well-known name; `MailReader.list_sync_scopes()` returns it, and the Google compatibility adapter
returns one stable `mailbox` scope. The normalized message must include `provider_message_id`, `provider_thread_id`, `provider_conversation_id`, `internet_message_id`, `mailbox_scope_key`, sender/recipients, subject, sanitized body, received/sent times, labels, normalized reply headers, and provider URL.

Define `MailCursorExpiredError(provider: str = "google", scope_key: str = "mailbox")` in the new port. Its stable error code is
provider-neutral, while its fields identify only the provider and opaque local scope key; it must not
contain a Delta URL or cursor value.

Keep `application/ports/gmail.py` as an explicit compatibility re-export during M2:

~~~python
from ai_employee.application.ports.mail import (
    MailCursorExpiredError,
    MailMessage as GmailMessage,
    MailReader as GmailReader,
    MailSyncPage as GmailSyncPage,
)

HistoryCursorExpiredError = MailCursorExpiredError

__all__ = ["GmailMessage", "GmailReader", "GmailSyncPage", "HistoryCursorExpiredError"]
~~~

Extend `application/ports/calendar.py` with `ProviderCalendar`, directory pages, organizer/attendee fields, `calendar_id` on every reader call, an exact read-only `get_current_event(calendar_id, provider_event_id)` method for proposal/restore preparation, and `CalendarCursorExpiredError(provider, scope_key)`. Keep its zero-argument compatibility construction mapped to `google/primary` until all M1 call sites and tests use the scoped form.

- [ ] **Step 4: Refactor use cases, repositories, workers, and schedules**

Create `SyncMailUseCase`; retain `SyncGmailUseCase = SyncMailUseCase` only for existing imports. Change repositories to validate `mail.read` or `calendar.read` capability and connection ownership instead of `provider == "google"`. Every cursor query must include `scope_key`.

Create migration `20260806_0013` with `down_revision = "20260806_0012"`; update existing `sync_cursors.resource_kind` values from `gmail` to `mail` and leave all other resource kinds and cursor strings unchanged. The migration test must prove the row count, `scope_key`, and cursor values are identical before and after the rename.

Create an explicit `ProviderAdapterRegistry` with fixed keys `google` and `microsoft`; its read methods return typed ports and raise `UnsupportedProviderError` for anything else. It is not a tool registry and cannot accept runtime registration.

Register new task kind `sync_mail`. `execute_task.py` must continue accepting persisted legacy `sync_gmail` tasks by routing them to the same mail step. Schedules query enabled read capabilities provider-specifically: Google continues to create one task per enabled mail/calendar scope; Microsoft mail is scheduled only through the connection's `mailbox` directory-discovery owner, which then runs real folders sequentially. Microsoft folder tasks are reserved for explicit recovery/maintenance; each folder cursor remains independent and authoritative.

- [ ] **Step 5: Run old and new synchronization suites**

Run: `uv run --project backend pytest backend/tests/unit/application/test_provider_neutral_sync.py backend/tests/integration/google/test_gmail_sync.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/workers/test_google_sync_schedule.py backend/tests/unit/test_layer_boundaries.py -q`

Expected: PASS, proving no M1 Google regression and independent scoped cursors.

- [ ] **Step 6: Commit**

~~~bash
git add backend/migrations/versions/20260806_0013_m2_provider_neutral_cursors.py backend/src/ai_employee/application/ports/mail.py backend/src/ai_employee/application/ports/gmail.py backend/src/ai_employee/application/ports/calendar.py backend/src/ai_employee/application/use_cases/sync_mail.py backend/src/ai_employee/application/use_cases/sync_gmail.py backend/src/ai_employee/application/use_cases/sync_calendar.py backend/src/ai_employee/infrastructure/db/repositories/email.py backend/src/ai_employee/infrastructure/db/repositories/calendar.py backend/src/ai_employee/integrations/registry.py backend/src/ai_employee/workers/sync_mail.py backend/src/ai_employee/workers/sync_gmail.py backend/src/ai_employee/workers/sync_calendar.py backend/src/ai_employee/workers/execute_task.py backend/src/ai_employee/workers/schedules.py backend/tests/unit/application/test_provider_neutral_sync.py backend/tests/integration/google/test_gmail_sync.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/workers/test_google_sync_schedule.py backend/tests/integration/db/test_migrations.py backend/tests/unit/test_layer_boundaries.py
git commit -m "refactor: add provider neutral source sync"
~~~

### Task 8: Generalize OAuth orchestration and expose connection capability APIs

**Files:**
- Create: `backend/src/ai_employee/application/ports/oauth.py`
- Modify: `backend/src/ai_employee/application/use_cases/connections.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/connections.py`
- Modify: `backend/src/ai_employee/api/routers/connections.py`
- Modify: `backend/src/ai_employee/api/deps.py`
- Modify: `backend/src/ai_employee/main.py`
- Create: `backend/tests/unit/application/test_connection_capability_use_cases.py`
- Create: `backend/tests/integration/api/test_connections.py`
- Create: `backend/tests/integration/m2/test_connection_capability_repository.py`

- [ ] **Step 1: Write failing capability API and repository tests**

~~~python
async def test_enable_mail_send_requests_dependency_union(authenticated_api_clients) -> None:
    response = await authenticated_api_clients.owner.post(
        f"/api/v1/connections/{CONNECTION_ID}/capabilities/mail.send/enable"
    )

    assert response.status_code == 200
    assert response.json()["requested_capabilities"] == ["mail.read", "mail.send"]
    assert response.json()["authorization_url"].startswith("https://provider.example.test/")


async def test_other_user_cannot_read_connection_capabilities(authenticated_api_clients) -> None:
    response = await authenticated_api_clients.other.get(
        f"/api/v1/connections/{CONNECTION_ID}/capabilities"
    )

    assert response.status_code == 404
~~~

Repository tests must prove the unique user/connection/capability key, actual-scope replacement, status transitions, and preservation of an existing encrypted refresh token when a callback contains no rotated refresh token.

API coverage must also prove `GET /connections/{id}/capabilities` returns the current user's typed
`provider_calendars` projection (ID, name, timezone, primary, access role, `can_write`, provider URL)
without cursors or raw provider JSON. This supplies the approved calendar/default selectors without
adding an undocumented second calendar-directory endpoint.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/application/test_connection_capability_use_cases.py backend/tests/integration/api/test_connections.py backend/tests/integration/m2/test_connection_capability_repository.py -q`

Expected: FAIL because OAuth is hard-coded to Google and capability endpoints do not exist.

- [ ] **Step 3: Define the OAuth adapter port**

Create `application/ports/oauth.py`:

~~~python
@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int
    granted_scopes: frozenset[str]
    id_token: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthAccount:
    provider_account_id: str
    account_email: str
    provider_tenant_id: str
    account_type: str


class OAuthRevocationStatus(StrEnum):
    REVOKED = "revoked"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class OAuthRevocationResult:
    status: OAuthRevocationStatus
    error_code: str | None = None


class OAuthProviderAdapter(Protocol):
    provider: str

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        raise NotImplementedError

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        raise NotImplementedError

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        raise NotImplementedError

    async def fetch_account(
        self, token: OAuthTokenSet, *, expected_nonce_hash: bytes | None
    ) -> OAuthAccount:
        raise NotImplementedError

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        raise NotImplementedError

    async def revoke(self, token: str) -> OAuthRevocationResult:
        raise NotImplementedError
~~~

Google returns `REVOKED` after its documented endpoint accepts the token. A provider with no narrow
endpoint returns `UNSUPPORTED` with a stable content-free code; transport failures remain classified
exceptions so Task 25 can distinguish an attempted failure from an unsupported operation.

- [ ] **Step 4: Implement provider-neutral use cases and persistence**

Refactor `GoogleConnectionsUseCase` into `ConnectionsUseCase` with an injected fixed adapter mapping. `start()` accepts a provider and non-empty read-capability set; `start_capability_enable()` takes the dependency closure plus all currently enabled capabilities. Persist provider, requested capabilities, PKCE verifier, and optional OIDC nonce hash in `oauth_attempts`.

The callback must consume state once, resolve the adapter recorded on the attempt, save normalized identity/tokens, and set each requested capability to `enabled` only if all required scopes are present; otherwise set `action_required` with `connection_scope_missing`. Preserve a prior refresh-token ciphertext when `OAuthTokenSet.refresh_token is None`.

Add typed API routes for the three specification endpoints. The capability read response joins the
connection's synchronized `ProviderCalendar` values with explicit schemas and user filtering; set
`Cache-Control: no-store` because it includes account/calendar names. `disable` may only change local
capability state at this task; cancellation of pending actions is added after trusted execution exists
in Task 25.

- [ ] **Step 5: Run API and repository checks**

Run: `uv run --project backend pytest backend/tests/unit/application/test_connection_capability_use_cases.py backend/tests/integration/api/test_connections.py backend/tests/integration/m2/test_connection_capability_repository.py -q`

Expected: PASS.

Run: `uv run --project backend mypy backend/src/ai_employee/application/ports/oauth.py backend/src/ai_employee/application/use_cases/connections.py backend/src/ai_employee/api/routers/connections.py`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/application/ports/oauth.py backend/src/ai_employee/application/use_cases/connections.py backend/src/ai_employee/infrastructure/db/repositories/connections.py backend/src/ai_employee/api/routers/connections.py backend/src/ai_employee/api/deps.py backend/src/ai_employee/main.py backend/tests/unit/application/test_connection_capability_use_cases.py backend/tests/integration/api/test_connections.py backend/tests/integration/m2/test_connection_capability_repository.py
git commit -m "feat: add connection capability orchestration"
~~~

### Task 9: Implement Google progressive scopes and capability verification

**Files:**
- Modify: `backend/src/ai_employee/integrations/google/oauth.py`
- Modify: `backend/src/ai_employee/application/use_cases/connections.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/connections.py`
- Modify: `backend/tests/integration/google/test_oauth_flow.py`
- Create: `backend/tests/contract/google/test_google_oauth_capabilities.py`

- [ ] **Step 1: Write failing Google scope contracts**

~~~python
def test_google_scope_union_is_minimal() -> None:
    adapter = GoogleOAuthAdapter(
        client_id="synthetic-client",
        client_secret="synthetic-secret",
        redirect_uri="https://app.example.test/api/v1/connections/google/callback",
    )

    assert adapter.scopes_for(frozenset({ConnectionCapability.MAIL_READ})) == frozenset(
        {"openid", "email", "https://www.googleapis.com/auth/gmail.readonly"}
    )
    assert "https://www.googleapis.com/auth/gmail.send" in adapter.scopes_for(
        frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND})
    )
    assert "https://www.googleapis.com/auth/calendar.events" not in adapter.scopes_for(
        frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND})
    )
~~~

Contract tests must assert `include_granted_scopes=true`, `access_type=offline`, S256 PKCE, callback use of returned scopes, missing-scope `action_required`, refresh-token preservation, one refresh retry, and best-effort revoke.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/integration/google/test_oauth_flow.py backend/tests/contract/google/test_google_oauth_capabilities.py -q`

Expected: FAIL because Google still requests the fixed M1 read-only set.

- [ ] **Step 3: Implement the exact capability-to-scope map**

~~~python
GOOGLE_BASE_SCOPES = frozenset({"openid", "email"})
GOOGLE_CAPABILITY_SCOPES = {
    ConnectionCapability.MAIL_READ: frozenset(
        {"https://www.googleapis.com/auth/gmail.readonly"}
    ),
    ConnectionCapability.MAIL_SEND: frozenset(
        {"https://www.googleapis.com/auth/gmail.send"}
    ),
    ConnectionCapability.CALENDAR_READ: frozenset(
        {"https://www.googleapis.com/auth/calendar.readonly"}
    ),
    ConnectionCapability.CALENDAR_WRITE: frozenset(
        {"https://www.googleapis.com/auth/calendar.events"}
    ),
}
~~~

Build the authorization URL from the requested union and include granted scopes. Normalize the token endpoint's optional `scope` string into `OAuthTokenSet.granted_scopes`; when absent, verify effective scopes through the Google token-info boundary rather than assuming the request succeeded.

- [ ] **Step 4: Backfill and verify existing Google read capabilities**

Add an idempotent repository verification/repair method for partially migrated development databases:
derive the two read states from existing connection scopes, insert only missing capability rows, and
keep both write rows disabled. Tests first prove revision `0011` already produced the correct rows;
the runtime method must not overwrite a user's later explicit capability state. Never enable a write
capability from configuration alone.

- [ ] **Step 5: Run Google OAuth and M1 regression suites**

Run: `uv run --project backend pytest backend/tests/integration/google/test_oauth_flow.py backend/tests/contract/google/test_google_oauth_capabilities.py backend/tests/integration/google/test_gmail_sync.py backend/tests/integration/google/test_calendar_sync.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/integrations/google/oauth.py backend/src/ai_employee/application/use_cases/connections.py backend/src/ai_employee/infrastructure/db/repositories/connections.py backend/tests/integration/google/test_oauth_flow.py backend/tests/contract/google/test_google_oauth_capabilities.py
git commit -m "feat: add Google progressive authorization"
~~~

### Task 10: Implement Microsoft delegated OIDC/OAuth for personal and work accounts

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/src/ai_employee/integrations/microsoft/__init__.py`
- Create: `backend/src/ai_employee/integrations/microsoft/oauth.py`
- Modify: `backend/src/ai_employee/api/routers/connections.py`
- Modify: `backend/src/ai_employee/api/deps.py`
- Modify: `backend/src/ai_employee/main.py`
- Create: `backend/tests/contract/microsoft/fixtures/openid_configuration.json`
- Create: `backend/tests/contract/microsoft/fixtures/jwks.json`
- Create: `backend/tests/contract/microsoft/test_microsoft_oauth.py`
- Create: `backend/tests/integration/microsoft/test_oauth_flow.py`

- [ ] **Step 1: Write failing Microsoft OAuth contracts**

The contract suite must cover the `common` v2 authorization endpoint, S256 PKCE, nonce validation, personal and work/school issuer forms, delegated scope unions, `offline_access`, refresh without a new refresh token, administrator-consent errors, state replay, 429/5xx classification, and disconnect behavior that never calls the broad `/me/revokeSignInSessions` or directory-level `oauth2PermissionGrants` APIs.

~~~python
def test_microsoft_mail_send_scope_depends_on_mail_read() -> None:
    adapter = MicrosoftOAuthAdapter(
        client_id="synthetic-client",
        client_secret="synthetic-secret",
        redirect_uri="https://app.example.test/api/v1/connections/microsoft/callback",
    )

    scopes = adapter.scopes_for(
        frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND})
    )

    assert scopes == frozenset(
        {
            "openid",
            "profile",
            "email",
            "User.Read",
            "offline_access",
            "Mail.Read",
            "Mail.Send",
        }
    )
~~~

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/contract/microsoft/test_microsoft_oauth.py backend/tests/integration/microsoft/test_oauth_flow.py -q`

Expected: FAIL because no Microsoft integration exists.

- [ ] **Step 3: Add and lock the JWT verification dependency**

Add `pyjwt[crypto]>=2.10,<3` to backend dependencies and regenerate `backend/uv.lock` with `uv lock --project backend`. Record in the task diff that PyJWT is MIT-licensed, actively maintained, and used only to validate Microsoft OIDC ID tokens against discovery/JWKS; do not add an unverified JWT parser.

- [ ] **Step 4: Implement Microsoft OIDC/OAuth**

Create `MicrosoftOAuthAdapter` using these fixed endpoints:

~~~python
MICROSOFT_AUTHORIZATION_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_DISCOVERY_URL = "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration"
MICROSOFT_GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
MICROSOFT_BASE_SCOPES = frozenset(
    {"openid", "profile", "email", "User.Read", "offline_access"}
)
MICROSOFT_CAPABILITY_SCOPES = {
    ConnectionCapability.MAIL_READ: frozenset({"Mail.Read"}),
    ConnectionCapability.MAIL_SEND: frozenset({"Mail.Send"}),
    ConnectionCapability.CALENDAR_READ: frozenset({"Calendars.Read"}),
    ConnectionCapability.CALENDAR_WRITE: frozenset({"Calendars.ReadWrite"}),
}
~~~

`User.Read` is the minimum delegated Microsoft Graph permission needed for the `/me` request that
returns the stable user ID and mailbox address for both personal and work/school accounts. It is
not a directory or application permission and must not be replaced with Contacts, `Mail.ReadWrite`,
or any broader permission. Fetch discovery and JWKS with bounded cache lifetime; validate signature,
audience, expiration, issuer template, and nonce hash before accepting identity. Use Graph `/me`
for the stable user ID and mailbox address, and the validated `tid`/issuer to derive
`provider_tenant_id` plus `personal` or `work_school` account type. Store a normalized provider
account key containing tenant and Graph user ID so the existing uniqueness constraint remains safe.

Map `AADSTS65001` or equivalent consent-required callback errors to `microsoft_admin_consent_required`; never persist raw error descriptions.

Microsoft documents broad sign-in-session and permission-grant revocation operations, not a narrow
RFC 7009-style endpoint that this delegated personal assistant can safely call with its approved
scopes. Implement `revoke()` as `OAuthRevocationResult(UNSUPPORTED, "microsoft_token_revoke_unsupported")`; disconnect still
deletes local tokens first and records the token-free maintenance fact from Task 25. Do not request
directory permissions or sign the user out of unrelated Microsoft sessions merely to emulate Google
token revocation.

- [ ] **Step 5: Register routes and run both account matrices**

Expose `/api/v1/connections/microsoft/start` and `/api/v1/connections/microsoft/callback` through the same provider-neutral use case. Run:

`uv run --project backend pytest backend/tests/contract/microsoft/test_microsoft_oauth.py backend/tests/integration/microsoft/test_oauth_flow.py backend/tests/integration/api/test_connections.py -q`

Expected: PASS for personal and work/school fixtures.

Run: `uv pip check --project backend`

Expected: PASS with the locked dependency set.

- [ ] **Step 6: Commit**

~~~bash
git add backend/pyproject.toml backend/uv.lock backend/src/ai_employee/integrations/microsoft backend/src/ai_employee/api/routers/connections.py backend/src/ai_employee/api/deps.py backend/src/ai_employee/main.py backend/tests/contract/microsoft backend/tests/integration/microsoft/test_oauth_flow.py
git commit -m "feat: add Microsoft delegated OAuth"
~~~

### Task 11: Add Microsoft mail-folder discovery and Delta synchronization

**Files:**
- Create: `backend/src/ai_employee/integrations/microsoft/mail.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Modify: `backend/src/ai_employee/application/ports/mail.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/email.py`
- Modify: `backend/src/ai_employee/workers/sync_mail.py`
- Create: `backend/tests/contract/microsoft/fixtures/mail_folders.json`
- Create: `backend/tests/contract/microsoft/fixtures/mail_delta_initial.json`
- Create: `backend/tests/contract/microsoft/fixtures/mail_delta_incremental.json`
- Create: `backend/tests/contract/microsoft/test_mail_adapter.py`
- Create: `backend/tests/integration/microsoft/test_mail_sync.py`

- [ ] **Step 1: Write failing Graph mail Delta tests**

~~~python
@pytest.mark.asyncio
@respx.mock
async def test_initial_mail_sync_excludes_deleted_and_junk_but_keeps_sent() -> None:
    respx.get("https://graph.microsoft.com/v1.0/me/mailFolders").mock(
        return_value=httpx.Response(200, json=mail_folders_fixture())
    )
    adapter = MicrosoftMailAdapter(access_token="synthetic-token")

    scopes = await adapter.list_sync_scopes()

    assert "sentitems" in {scope.well_known_name for scope in scopes}
    assert "deleteditems" not in {scope.well_known_name for scope in scopes}
    assert "junkemail" not in {scope.well_known_name for scope in scopes}
~~~

Add cases for initial seven-day filtering, pagination through `@odata.nextLink`, persisted `@odata.deltaLink`, tombstones with `@removed`, `Prefer: IdType="ImmutableId"` on every message read, folder-specific cursor expiry fallback, Sent coverage, 401 refresh once, 403 capability loss, 429 `Retry-After`, 5xx, and malformed fields. Fixtures must use `.example.test` addresses and synthetic bodies only.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/contract/microsoft/test_mail_adapter.py backend/tests/integration/microsoft/test_mail_sync.py -q`

Expected: FAIL because the Graph mail adapter and Microsoft registry entry do not exist.

- [ ] **Step 3: Implement folder discovery and Delta pages**

Use the normalized `MailScope` value introduced in Task 7 and implement:

~~~python
class MicrosoftMailAdapter:
    """读取非 Junk/Deleted 文件夹的 Graph Delta，并输出供应商无关邮件。"""

    async def list_sync_scopes(self) -> tuple[MailScope, ...]:
        payload = await self._get_json(
            "/me/mailFolders",
            params={"includeHiddenFolders": "false", "$select": "id,displayName,wellKnownName"},
        )
        scopes = tuple(self._normalize_scope(item) for item in self._items(payload))
        return tuple(
            scope
            for scope in scopes
            if scope.well_known_name not in {"deleteditems", "junkemail"}
        )

    def initial_pages(self, scope_key: str, *, since: datetime) -> AsyncIterator[MailSyncPage]:
        return self._delta_pages(
            f"/me/mailFolders/{quote(scope_key, safe='')}/messages/delta",
            params={
                "$filter": f"receivedDateTime ge {since.isoformat().replace('+00:00', 'Z')}",
                "$select": _MAIL_SELECT,
            },
        )
~~~

Follow absolute `@odata.nextLink` only when its scheme/host/path exactly matches the expected Graph collection or folder Delta path; preserve its opaque query unchanged and reject same-host wrong-path links before making a request. Persist the final opaque `deltaLink` but never log it. Send `Prefer: IdType="ImmutableId"` on folder, Delta, source-message, and Sent-item reads so a move within the mailbox does not silently change the stored Graph message identity. Normalize `id`, `conversationId`, `internetMessageId`, sender/recipients, body text, dates, categories, `webLink`, removal facts, and `lastModifiedDateTime` as optional `provider_updated_at` into `MailMessage`.

- [ ] **Step 4: Persist Microsoft messages through the shared repository**

Update the mail repository to use `connection_id + provider message/thread IDs`, store `mailbox_scope_key`, and advance only the locked `(connection_id, "mail", scope_key)` cursor after every page succeeds. A cursor-expired result must clear only that scope and rerun the bounded seven-day read.

The mailbox owner is the only periodic Microsoft mail entry point: discovery results are sorted by stable
`scope_key`, each folder is committed independently, and a partial folder failure leaves successful folder
cursors advanced while failed folders remain retryable. Brief refreshes use the same mailbox owner and must
not enqueue a second task for every stale folder. Add a 5 MiB per-response streaming limit and a fixed
collection/Delta chain wire/normalized byte budget in addition to the 100-page/10,000-item limits; budget
failure must occur before persistence/CAS and close the response. Add regressions for ownership, version
ordering, path validation, streaming budgets, and cursor non-advancement.

The mail identity migration is deliberately split into an online expand/contract pair: 0016 adds nullable
columns and fail-closed backfill while retaining legacy constraints; 0017 creates concurrent unique indexes,
composite ownership foreign keys, validated checks/NOT NULL, and only then removes legacy uniqueness. Test
fresh 0015-to-head and already-applied 0016 databases, downgrade row preservation, and rerunnable
valid/invalid concurrent-index paths. Deploy this pair in a fixed order: apply 0016 first because its nullable
expand remains compatible with old application instances; deploy a dual-write repository that runs on both
0016 and 0017; drain every old instance; then apply 0017. Before preflight or concurrent-index work, 0017 must
rerun the bounded autocommit catch-up so rows written with `connection_id IS NULL` by old instances during the
0016 window are filled before contract. The repository must also tolerate the valid intermediate state where
the new concurrent index exists but has not yet been attached with `USING INDEX`, while invalid or wrong-shape
catalog objects remain fail-closed. Provider network I/O stays outside database transactions throughout this
deployment sequence.

- [ ] **Step 5: Run Microsoft mail and provider-neutral regressions**

Run: `uv run --project backend pytest backend/tests/contract/microsoft/test_mail_adapter.py backend/tests/integration/microsoft/test_mail_sync.py backend/tests/unit/application/test_provider_neutral_sync.py backend/tests/integration/google/test_gmail_sync.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/integrations/microsoft/mail.py backend/src/ai_employee/integrations/registry.py backend/src/ai_employee/application/ports/mail.py backend/src/ai_employee/infrastructure/db/repositories/email.py backend/src/ai_employee/workers/sync_mail.py backend/tests/contract/microsoft/fixtures/mail_folders.json backend/tests/contract/microsoft/fixtures/mail_delta_initial.json backend/tests/contract/microsoft/fixtures/mail_delta_incremental.json backend/tests/contract/microsoft/test_mail_adapter.py backend/tests/integration/microsoft/test_mail_sync.py
git commit -m "feat: sync Microsoft mail delta"
~~~

### Task 12: Synchronize the Google calendar directory and each calendar independently

**Files:**
- Modify: `backend/src/ai_employee/integrations/google/calendar.py`
- Modify: `backend/src/ai_employee/application/ports/calendar.py`
- Modify: `backend/src/ai_employee/application/use_cases/sync_calendar.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/calendar.py`
- Modify: `backend/src/ai_employee/infrastructure/db/models/sources.py`
- Modify: `backend/src/ai_employee/workers/sync_calendar.py`
- Modify: `backend/src/ai_employee/integrations/google/fake.py`
- Create: `backend/migrations/versions/20260809_0018_calendar_event_calendar_identity.py`
- Create: `backend/tests/contract/fixtures/google_calendar_list.json`
- Modify: `backend/tests/contract/test_calendar_adapter.py`
- Modify: `backend/tests/integration/google/test_calendar_sync.py`
- Modify: `backend/tests/integration/google/test_test_mode_adapters.py`
- Modify: `backend/tests/integration/db/test_migrations.py`
- Modify: `backend/tests/integration/m2/test_connection_source_schema.py`

- [ ] **Step 1: Write failing directory and per-calendar tests**

~~~python
@pytest.mark.asyncio
@respx.mock
async def test_google_calendar_sync_uses_each_directory_calendar_id() -> None:
    respx.get("https://www.googleapis.com/calendar/v3/users/me/calendarList").mock(
        return_value=httpx.Response(200, json=calendar_list_fixture())
    )
    adapter = GoogleCalendarAdapter(access_token="synthetic", user_timezone="UTC")

    calendars = [item async for page in adapter.directory_pages() for item in page.calendars]

    assert [(item.calendar_id, item.can_write) for item in calendars] == [
        ("primary", True),
        ("readonly@example.test", False),
    ]
~~~

Integration coverage must prove separate `scope_key` rows, a CalendarList 410 reset that does not erase event cursors, an event sync-token 410 that resets only one calendar, past-one-day/future-30-day initial windows, directory write-role projection, attendees/organizer normalization, exact current-event GET normalization for restore preparation, and no duplicate events after queue replay.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/contract/test_calendar_adapter.py backend/tests/integration/google/test_calendar_sync.py -q`

Expected: FAIL because Google Calendar is fixed to `/primary/events` and has no directory pages.

- [ ] **Step 3: Implement CalendarList and calendar-specific event URLs**

Rename the concrete class to `GoogleCalendarAdapter`, replace the fixed event URL with URL-safe calendar IDs, and add:

~~~python
GOOGLE_CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"


async def directory_pages(
    self, cursor: str | None = None
) -> AsyncIterator[CalendarDirectoryPage]:
    """增量读取可见日历目录，并在最终页返回目录 nextSyncToken。"""
    parameters = {"showDeleted": "true"}
    if cursor is not None:
        parameters["syncToken"] = cursor
    async for payload in self._paged_get(GOOGLE_CALENDAR_LIST_URL, parameters):
        yield CalendarDirectoryPage(
            calendars=tuple(self._normalize_calendar(item) for item in self._items(payload)),
            next_page_token=self._optional_string(payload.get("nextPageToken")),
            next_cursor=self._optional_string(payload.get("nextSyncToken")),
        )


def _events_url(calendar_id: str) -> str:
    return f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"


# M1 imports and persisted Worker factories keep working while M2 call sites adopt the explicit name.
CalendarAdapter = GoogleCalendarAdapter
__all__ = ["GoogleCalendarAdapter", "CalendarAdapter"]
~~~

Map only documented `accessRole in {"owner", "writer"}` to `can_write=True`; preserve calendar timezone, primary flag, and provider URL. Unknown roles fail closed as read-only rather than being guessed writable.

- [ ] **Step 4: Update directory persistence and bounded event windows**

The Calendar sync use case must first sync the directory under scope key `directory`, then sync enabled calendar rows under their provider calendar IDs. The Google initial event request uses the user's IANA timezone local midnight minus one day through plus 30 days; incremental calls preserve the initial options required by Google and include deleted tombstones.

- [ ] **Step 5: Run Google calendar and brief regressions**

Run: `uv run --project backend pytest backend/tests/contract/test_calendar_adapter.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/briefs/test_daily_brief_graph.py -q`

Expected: PASS with events from all visible calendars available to the brief source query.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/integrations/google/calendar.py backend/src/ai_employee/application/ports/calendar.py backend/src/ai_employee/application/use_cases/sync_calendar.py backend/src/ai_employee/infrastructure/db/repositories/calendar.py backend/src/ai_employee/workers/sync_calendar.py backend/tests/contract/fixtures/google_calendar_list.json backend/tests/contract/test_calendar_adapter.py backend/tests/integration/google/test_calendar_sync.py
git commit -m "feat: sync Google calendar directory"
~~~

- [ ] **Step 7: Write failing per-calendar event identity, migration, and Fake tests**

Add a PostgreSQL sync regression where `primary` and `readonly@example.test` both return
`provider_event_id="shared-event-id"`. After directory synchronization, assert that two rows exist
under the same user and connection, each retaining its own `calendar_id`, title, ETag, and provider URL.
Run a second synchronization that changes only the primary event and assert the readonly row is byte-for-byte
unchanged in all projected non-sensitive fields.

Add a migration test that upgrades an isolated database to `20260808_0017`, seeds a historical calendar event,
then upgrades to `20260809_0018`. Assert the historical row remains, the only CalendarEvent identity constraint is
`(connection_id, calendar_id, provider_event_id)`, two calendars may insert the same provider event ID, and a
duplicate of the same three-part identity raises `IntegrityError`.

Add a Fake regression proving the shared calendar fixture belongs only to `primary`:

~~~python
@pytest.mark.asyncio
async def test_fake_calendar_fixture_is_not_cloned_across_directory_calendars() -> None:
    reader = FakeCalendarReader(calendar_fixture, directory_fixture=directory_fixture)

    primary = [
        event async for page in reader.initial_pages("primary") for event in page.events
    ]
    secondary = [
        event
        async for page in reader.initial_pages("readonly@example.test")
        for event in page.events
    ]

    assert primary
    assert secondary == []
    assert await reader.get_current_event("readonly@example.test", primary[0].event_id) is None
~~~

- [ ] **Step 8: Run the new tests and observe the expected failures**

Run from the repository root:

~~~bash
TEST_DATABASE_URL="${TEST_DATABASE_URL:?set an isolated PostgreSQL *_test URL}" \
uv run --project backend pytest \
  backend/tests/integration/google/test_calendar_sync.py::test_same_provider_event_id_is_scoped_per_calendar \
  backend/tests/integration/db/test_migrations.py::test_calendar_event_identity_migration_scopes_ids_per_calendar \
  backend/tests/integration/google/test_test_mode_adapters.py::test_fake_calendar_fixture_is_not_cloned_across_directory_calendars \
  -q
~~~

Expected: FAIL for three independent reasons: the old repository/constraint leaves one overwritten event row, the
`20260809_0018` revision and three-column constraint do not exist, and `FakeCalendarReader` currently rewrites the
same fixture event into the secondary calendar.

- [ ] **Step 9: Add the forward-only CalendarEvent identity migration**

Create `20260809_0018_calendar_event_calendar_identity.py` with direct predecessor
`20260808_0017`. The upgrade must:

1. Verify any existing legacy/new constraint or same-name index against exact catalog shape.
2. While the legacy binary constraint remains, create
   `uq_calendar_events_connection_calendar_provider_event` with
   `CREATE UNIQUE INDEX CONCURRENTLY` over
   `(connection_id, calendar_id, provider_event_id)`.
3. If an interrupted build left an invalid index, drop it concurrently only after proving the table, uniqueness,
   non-partial/non-expression shape, key count, and exact ordered columns; wrong-shape objects fail closed.
4. Attach the valid index using `UNIQUE USING INDEX`, then remove
   `uq_calendar_events_connection_provider_event` in the short contract transaction.
5. Never delete or merge CalendarEvent rows. Downgrade may recreate the legacy identity only after proving no
   cross-calendar duplicate provider IDs exist; otherwise raise a stable error and preserve all rows.

Deployment is not rolling-write compatible because old and new repositories reference different named constraints.
Disable Calendar scheduling, drain and stop every old `sync_calendar` Worker, apply `20260809_0018`, deploy the new
API/Worker/Scheduler release, then restore workers and scheduling. No old Calendar writer may run after migration.

- [ ] **Step 10: Change ORM, Repository identity, and exact event predicates**

In `CalendarEventModel`, replace the old unique constraint with:

~~~python
UniqueConstraint(
    "connection_id",
    "calendar_id",
    "provider_event_id",
    name="uq_calendar_events_connection_calendar_provider_event",
)
~~~

Change Calendar event upsert to target that constraint and remove `calendar_id` from the conflict update set because
it is now immutable identity. Audit every exact event read, update, version comparison, and single-event tombstone
predicate: each must include `user_id`, `connection_id`, `calendar_id`, and `provider_event_id`. Keep whole-calendar
cache removal predicates keyed by `calendar_id`; those are directory-level operations rather than exact identity.

- [ ] **Step 11: Stop FakeCalendarReader from cloning provider events**

`calendar_initial.json` represents the primary Calendar event collection. `initial_pages(non_primary)` and
`sync_pages(non_primary, ...)` must return empty pages with a stable synthetic cursor, while primary keeps the existing
fixture projection. `get_current_event()` must return `None` for non-primary calendars before inspecting the fixture.
Do not create a general fixture routing framework or copy the same event with a rewritten `calendar_id`.

- [ ] **Step 12: Run GREEN verification and commit the identity correction**

Run:

~~~bash
TEST_DATABASE_URL="${TEST_DATABASE_URL:?set an isolated PostgreSQL *_test URL}" \
uv run --project backend pytest \
  backend/tests/integration/google/test_calendar_sync.py \
  backend/tests/integration/google/test_test_mode_adapters.py \
  backend/tests/integration/db/test_migrations.py \
  backend/tests/integration/m2/test_connection_source_schema.py \
  backend/tests/integration/briefs/test_daily_brief_graph.py \
  backend/tests/integration/briefs/test_generate_brief_task.py \
  backend/tests/integration/workers/test_google_sync_schedule.py \
  backend/tests/unit/application/test_provider_neutral_sync.py \
  -q
uv run --project backend pytest backend/tests/contract/test_calendar_adapter.py -q
uv run --project backend ruff check backend/src backend/tests
uv run --project backend ruff format --check backend/src backend/tests
uv run --project backend mypy backend/src
git diff --check
~~~

Expected: PASS. Then inspect the migration deployment order, `git diff`, and staged file boundary before committing:

~~~bash
git add backend/migrations/versions/20260809_0018_calendar_event_calendar_identity.py \
  backend/src/ai_employee/infrastructure/db/models/sources.py \
  backend/src/ai_employee/infrastructure/db/repositories/calendar.py \
  backend/src/ai_employee/integrations/google/fake.py \
  backend/tests/integration/db/test_migrations.py \
  backend/tests/integration/m2/test_connection_source_schema.py \
  backend/tests/integration/google/test_calendar_sync.py \
  backend/tests/integration/google/test_test_mode_adapters.py
git commit -m "fix: scope calendar event identities per calendar"
~~~

### Task 13: Add Microsoft calendar directory and CalendarView Delta synchronization

**Files:**
- Modify: `docs/superpowers/specs/2026-08-06-executable-mail-calendar-assistant-m2-design.md`
- Modify: `docs/superpowers/plans/2026-08-06-executable-mail-calendar-assistant-m2.md`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `backend/src/ai_employee/application/ports/calendar.py`
- Modify: `backend/src/ai_employee/application/use_cases/sync_calendar.py`
- Create: `backend/src/ai_employee/integrations/microsoft/timezones.py`
- Create: `backend/src/ai_employee/integrations/microsoft/calendar.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/calendar.py`
- Modify: `backend/src/ai_employee/infrastructure/observability/sync.py`
- Modify: `backend/src/ai_employee/workers/sync_calendar.py`
- Modify: `backend/src/ai_employee/workers/schedules.py`
- Create: `backend/tests/contract/microsoft/fixtures/calendars.json`
- Create: `backend/tests/contract/microsoft/fixtures/calendar_view_delta_initial.json`
- Create: `backend/tests/contract/microsoft/fixtures/calendar_view_delta_incremental.json`
- Create: `backend/tests/unit/integrations/test_microsoft_timezones.py`
- Create: `backend/tests/contract/microsoft/test_calendar_adapter.py`
- Create: `backend/tests/integration/microsoft/test_calendar_sync.py`
- Modify: `backend/tests/unit/application/test_provider_neutral_sync.py`
- Modify: `backend/tests/integration/google/test_calendar_sync.py`
- Modify: `backend/tests/integration/google/test_test_mode_adapters.py`
- Modify: `backend/tests/integration/observability/test_task_health_metrics.py`
- Modify: `backend/tests/unit/workers/test_microsoft_mail_ownership.py`
- Modify: `backend/tests/integration/workers/test_google_sync_schedule.py`

- [ ] **Step 0: Correct the Microsoft Calendar source of truth**

Update the M2 specification and this plan before changing code. Record that Graph v1.0
`GET /me/calendars` is a paginated full collection without directory deltaLink/tombstones, while
CalendarView Delta remains independent per calendar. Define explicit full-snapshot mode, nullable
Microsoft directory provider cursor, separate `last_success_at` revision CAS, HTTP classification,
normalization/time/recurrence boundaries, Worker token/403 behavior, tzdata dependency purpose, and the
required regression matrix. Run `git diff --check`, review the complete documentation diff, then commit:

~~~bash
git add docs/superpowers/specs/2026-08-06-executable-mail-calendar-assistant-m2-design.md docs/superpowers/plans/2026-08-06-executable-mail-calendar-assistant-m2.md
git commit -m "docs: correct Microsoft calendar directory semantics"
~~~

- [ ] **Step 1: Write failing Graph calendar tests**

Contract tests must use the real Graph v1.0 shapes. `/me/calendars` is a bounded full collection:
follow only host/path-bound `@odata.nextLink`, accept a final page with neither nextLink nor deltaLink,
reject directory deltaLink/`@removed`, and classify directory 404/410 as permanent provider errors. Each
calendar's `calendarView/delta` independently covers the user's past-one-day/future-30-day window,
absolute next/delta links, tombstones, `@odata.etag`/`changeKey`, attendees, organizer, recurrence read
projection, 401 refresh, 403 scope loss, 429, 5xx, and calendar cursor expiry fallback. Exact event GET
404 returns `None`; only a persisted CalendarView deltaLink may map 404/410 or `syncStateNotFound` to
`CalendarCursorExpiredError`.

Provider-neutral tests must first prove that directory pages explicitly distinguish full snapshots from
incremental pages, a pagination chain cannot change mode, Microsoft full snapshots finish with
`next_cursor=None`, and Google keeps its initial/full versus token-based incremental behavior. PostgreSQL
tests must prove empty full snapshot deletion, missing-calendar cache deletion, reappearance forcing a
bounded event rebuild, and local directory revision CAS: two reads from the same revision cannot both
commit. The revision is separate from provider cursor and reuses the directory cursor row's
`last_success_at`; no migration or Microsoft-specific ORM is allowed.

Adapter boundary tests must cover calendar ID 512, event/series/ETag 255, status/transparency/access-role
32, timezone 64, C0/DEL/NUL, invalid mailbox addresses, unsafe URLs, seven-or-more fractional seconds with
`Z` and offsets, offset/timezone mismatch, `end <= start`, all-day midnight/zone rules, and Graph event
`type` recurrence contradictions. Malformed data must fail before persistence and leave every cursor
unchanged.

Worker integration tests must instantiate `CalendarSyncTaskStep` with PostgreSQL, synthetic AEAD token
rows, mocked Graph/OAuth, and fixed Microsoft metrics. Cover refresh-token rotation and no-rotation,
separate later 401 chains using the latest refresh token, refresh rejection, second resource 401,
403 stopping all remaining calendar reads while only `calendar.read` becomes action-required, and
APP_TEST_MODE remaining completely offline.

~~~python
@pytest.mark.asyncio
@respx.mock
async def test_graph_calendar_event_keeps_version_and_read_only_recurrence() -> None:
    adapter = MicrosoftCalendarAdapter(access_token="synthetic", user_timezone="UTC")

    page = await first(adapter.initial_pages("calendar-1"))
    event = page.events[0]

    assert event.etag == 'W/"synthetic-etag"'
    assert event.recurring_event_id == "series-1"
    assert event.can_edit is True
~~~

The timezone unit suite must prove `Asia/Shanghai → China Standard Time`,
`America/Los_Angeles → Pacific Standard Time`, reverse mapping for Graph responses, explicit `UTC`,
canonical IANA aliases, deterministic behavior when Babel mapping data is missing/malformed, and stable
rejection of unknown Windows/IANA zone names without changing the process-global TZPATH.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/integrations/test_microsoft_timezones.py backend/tests/contract/microsoft/test_calendar_adapter.py backend/tests/integration/microsoft/test_calendar_sync.py -q`

Expected: FAIL because the shared timezone boundary and Graph calendar adapter do not exist.

- [ ] **Step 3: Add and lock the shared Microsoft timezone boundary**

Add `Babel>=2.17,<3` and `tzdata>=2025.2,<2027`, then regenerate `backend/uv.lock` with
`uv lock --project backend`. Babel is BSD-licensed and used only for maintained CLDR Windows/IANA
mappings. Python tzdata is actively maintained, Apache-2.0 licensed packaging of IANA timezone data and
is used only so `zoneinfo` validation does not silently depend on the host image's optional tzdata. Create
`microsoft/timezones.py` with deterministic `to_iana_timezone()` and `to_windows_timezone()` functions
from `babel.core.get_global("windows_zone_mapping")`, explicit `UTC`, canonical IANA normalization,
and stable `calendar_timezone_mapping_unsupported` errors. Missing or malformed CLDR/tzdata must fail
stably at the mapping boundary; never skip entries, substitute the host timezone, or mutate global TZPATH.

- [ ] **Step 4: Implement the provider-neutral directory contract and Graph reads**

Extend `CalendarDirectoryPage` with an explicit strong `full_snapshot` fact and
`CalendarConnectionState` with an optional local directory revision separate from cursor. All pages in a
directory chain must use one mode. Google CalendarList initial/410 fallback pages are full snapshots;
token-based directory pages are incremental. Microsoft always starts from
`/me/calendars?$select=id,name,isDefaultCalendar,canEdit,canShare,owner,hexColor`; it rejects any non-null
stored directory provider cursor before HTTP, follows only validated `@odata.nextLink`, rejects
`@odata.deltaLink` and directory tombstones, and finishes with `next_cursor=None` plus
`full_snapshot=True`.

Use `/me/calendars/{id}/calendarView/delta?startDateTime={window_start}&endDateTime={window_end}` for
initial event windows. Follow only validated event next/delta links and store each calendar's final
deltaLink opaque. Delete the unused public `execute_request(url=...)` diagnostic entry point; every
outbound request must come from fixed collection/resource builders or validated Graph links, and tests
must prove an attacker URL receives no request or Bearer header.

Normalize Graph `dateTimeTimeZone` values through the Task 13 shared mapping into aware UTC instants plus
the internal IANA timezone. Preserve offsets after truncating excessive fractional seconds, verify an
explicit offset is valid for the declared zone at that local time, require `end > start`, and enforce
same-zone local-midnight boundaries for all-day events. Implement exact current-event GET for
proposal/restore preparation, including ETag/changeKey, deletion, recurrence, and permission facts. Read
and expose recurrence metadata, validate it against Graph event `type`, but do not add recurrence to any
write command schema.

- [ ] **Step 5: Persist full snapshots and independent revision CAS**

Use the same `provider_calendars`, `calendar_events`, and scoped cursor tables as Google. Provider
selection comes from the connection row; no Microsoft-specific ORM table or migration is allowed.
`CalendarSyncStore.mark_directory_success()` accepts `full_snapshot`, optional `next_cursor`, and
`expected_revision`. It verifies provider cursor and local revision CAS independently. Only
`full_snapshot=True` performs absence deletion; incremental Google pages delete only explicit tombstones.
Microsoft directory keeps cursor `NULL` and advances only `last_success_at` as the local revision. Event
scope final cursors remain mandatory and non-empty. All queries, deletes, upserts and CAS remain
user-filtered.

- [ ] **Step 6: Harden normalization, Worker lifecycle, metrics, and test mode**

Validate every normalized scalar against its shared PostgreSQL column length before returning a port
object. Reject disallowed control characters; body text keeps legitimate line breaks but rejects NUL and
unrepresentable controls. Normalize organizer/attendee addresses through
`normalize_mailbox_address()` and map its errors to content-free
`microsoft_calendar_invalid_response`. Safe provider URLs require absolute HTTPS, non-empty host, no
userinfo/fragment/control characters.

Microsoft refresh success updates the in-memory refresh-token closure; if the token response omits a new
refresh token it retains the previous value. A directory `UserActionRequiredError` stops the remaining
calendar loop immediately. Worker 403 only downgrades `calendar.read`; reauthorization marks the
connection expired. `observe_provider_sync()` records fixed `PermanentProviderError` codes as well as
transient/action-required errors. `_FakeMicrosoftCalendarReader` routes fixture events by calendar ID and
exact GET matches `(calendar_id, event_id)`; unmatched calendars return empty pages and never clone one
fixture across the directory.

- [ ] **Step 7: Run both providers' calendar suites and dependency checks**

Run the Microsoft timezone/contract/PostgreSQL/Worker suites, provider-neutral calendar use-case and
repository tests, Google Calendar contract/integration regressions, and schedule/observability/test-mode
tests with the isolated Task 13 PostgreSQL URL. Then run:

~~~bash
uv run --project backend ruff check backend/src backend/tests
uv run --project backend ruff format --check backend/src backend/tests
uv run --project backend mypy backend/src
uv pip check --project backend
git diff --check
~~~

Expected: PASS.

- [ ] **Step 8: Commit**

~~~bash
git add backend/pyproject.toml backend/uv.lock backend/src/ai_employee/application/ports/calendar.py backend/src/ai_employee/application/use_cases/sync_calendar.py backend/src/ai_employee/integrations/microsoft/timezones.py backend/src/ai_employee/integrations/microsoft/calendar.py backend/src/ai_employee/integrations/registry.py backend/src/ai_employee/infrastructure/db/repositories/calendar.py backend/src/ai_employee/infrastructure/observability/sync.py backend/src/ai_employee/workers/sync_calendar.py backend/src/ai_employee/workers/schedules.py backend/tests/unit/integrations/test_microsoft_timezones.py backend/tests/unit/application/test_provider_neutral_sync.py backend/tests/contract/microsoft/fixtures/calendars.json backend/tests/contract/microsoft/fixtures/calendar_view_delta_initial.json backend/tests/contract/microsoft/fixtures/calendar_view_delta_incremental.json backend/tests/contract/microsoft/test_calendar_adapter.py backend/tests/integration/microsoft/test_calendar_sync.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/google/test_test_mode_adapters.py backend/tests/integration/observability/test_task_health_metrics.py backend/tests/unit/workers/test_microsoft_mail_ownership.py backend/tests/integration/workers/test_google_sync_schedule.py
git commit -m "fix: correct Microsoft calendar synchronization"
~~~

### Task 14: Implement local mail drafts, immutable versions, and body-only model generation

**Files:**
- Create: `backend/src/ai_employee/application/use_cases/mail_drafts.py`
- Create: `backend/src/ai_employee/workers/generate_mail_draft.py`
- Create: `backend/src/ai_employee/prompts/mail_draft_v1.md`
- Modify: `backend/src/ai_employee/workers/execute_task.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/mail_drafts.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/email.py`
- Modify: `backend/src/ai_employee/domain/briefs.py`
- Modify: `backend/src/ai_employee/agents/daily_brief/nodes.py`
- Modify: `backend/src/ai_employee/agents/daily_brief/state.py`
- Modify: `backend/src/ai_employee/workers/conversation.py`
- Modify: `backend/src/ai_employee/application/use_cases/conversations.py`
- Create: `backend/tests/unit/application/test_mail_drafts.py`
- Create: `backend/tests/unit/application/test_mail_draft_generation.py`
- Create: `backend/tests/integration/m2/test_mail_draft_versions.py`
- Create: `backend/tests/evals/mail_draft_cases.json`
- Create: `backend/tests/evals/test_mail_draft_regression.py`
- Modify: `backend/tests/unit/agents/test_daily_brief.py`
- Modify: `backend/tests/integration/api/test_conversations.py`

- [ ] **Step 1: Write failing draft and model-minimization tests**

~~~python
async def test_reply_all_binds_source_connection_and_excludes_own_addresses() -> None:
    result = await use_case.create_reply_all(
        user_id=USER_ID,
        source_message_id=SOURCE_MESSAGE_ID,
    )

    assert result.connection_id == SOURCE_CONNECTION_ID
    assert result.mode == "reply_all"
    assert "owner@example.test" not in result.to + result.cc


def test_model_context_keeps_only_three_messages_and_twelve_thousand_characters() -> None:
    context = build_mail_draft_context(messages=four_long_messages(), instruction="Reply briefly")

    assert len(context.messages) == 3
    assert sum(len(item.body_text) for item in context.messages) <= 12_000
    assert all("tracking-pixel" not in item.body_text for item in context.messages)
~~~

Add tests for blank new drafts, deterministic `Re:` subjects, immutable reply thread/account, version conflict, approval lock, explicit withdrawal through the existing task-cancel API before editing, recipient validation, locally derived recipient suggestions capped at 20 unique non-self addresses, empty-draft fallback on model failure, spam exclusion, and LLM invocation metadata without prompt/body storage.

Add controlled-initiation tests proving a brief item with `needs_reply=True` exposes `suggested_action_kind="mail.reply"`, and an explicit conversation request creates an editable local draft plus a link but no ApprovalRequest, ToolExecution, or provider call. Ambiguous chat text must continue to explain capabilities rather than creating an action.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/application/test_mail_drafts.py backend/tests/unit/application/test_mail_draft_generation.py backend/tests/integration/m2/test_mail_draft_versions.py -q`

Expected: FAIL because the draft use cases and worker do not exist.

- [ ] **Step 3: Implement draft creation and optimistic editing**

Create typed snapshots and use cases for list/get/create/update/cancel. Every create intent requires a
user-scoped idempotency key and returns the existing draft for a replay of that same intent. New mail
selects the configured default send connection or requires an explicit eligible connection. Replies bind the source thread, source message, and connection; update requests may edit only To/CC/BCC/body, and may edit subject only for `mode="new"`. Build `recipient_suggestions` deterministically from the current user's synchronized sender/recipient history, exclude all of the user's connection addresses, normalize/de-duplicate with the same address rules, sort by most recent occurrence then address, and cap at 20; never call a Contacts API or model. Task 18 extends the existing task-cancel use case as the single explicit withdrawal transport; do not add an undocumented draft-specific withdrawal route.

Every successful PATCH calls `save_next_version(expected_version=current_version)`; `awaiting_approval` rejects edits until the related trusted task is cancelled and its approval is invalidated. `needs_attention` cannot be edited or cloned without an explicit confirmed-not-executed result.

- [ ] **Step 4: Implement body-only model generation**

Define a strict output model:

~~~python
class MailDraftModelOutput(BaseModel):
    """模型只能生成纯文本正文，不能控制地址、主题、线程或发送。"""

    model_config = ConfigDict(extra="forbid")
    body_text: str = Field(max_length=100_000)
~~~

`GenerateMailDraftTaskStep` loads the approved maximum of three sanitized related messages and 12,000 characters, sends only the user instruction plus M1 thread summary and body facts to `ModelGateway`, saves a new local version on success, records `mail_draft_v1`, and leaves an editable blank draft with a model error result when the gateway fails.

The prompt file must state that the output cannot change recipient, subject, thread, dates, amounts, promises, or decisions and must express uncertainty when facts are missing.

Extend the narrow conversation intent union with `prepare_mail_draft`. Deterministic parsing may select a source thread or explicit syntactically valid address, but the model cannot select recipients/account/thread. The conversation Worker derives the draft creation idempotency key from its persisted task/message intent, calls the draft use case, and returns the draft ID/link; it never submits approval. Daily brief composition sets `mail.reply` only on an existing source thread that the user can explicitly open and prepare; the click API supplies its own idempotency key.

- [ ] **Step 5: Run focused, eval, and M1 model tests**

Run: `uv run --project backend pytest backend/tests/unit/application/test_mail_drafts.py backend/tests/unit/application/test_mail_draft_generation.py backend/tests/integration/m2/test_mail_draft_versions.py backend/tests/evals/test_mail_draft_regression.py backend/tests/evals/test_daily_brief_regression.py backend/tests/unit/agents/test_daily_brief.py backend/tests/integration/api/test_conversations.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/application/use_cases/mail_drafts.py backend/src/ai_employee/workers/generate_mail_draft.py backend/src/ai_employee/prompts/mail_draft_v1.md backend/src/ai_employee/workers/execute_task.py backend/src/ai_employee/infrastructure/db/repositories/mail_drafts.py backend/src/ai_employee/infrastructure/db/repositories/email.py backend/src/ai_employee/domain/briefs.py backend/src/ai_employee/agents/daily_brief/nodes.py backend/src/ai_employee/agents/daily_brief/state.py backend/src/ai_employee/workers/conversation.py backend/src/ai_employee/application/use_cases/conversations.py backend/tests/unit/application/test_mail_drafts.py backend/tests/unit/application/test_mail_draft_generation.py backend/tests/integration/m2/test_mail_draft_versions.py backend/tests/evals/mail_draft_cases.json backend/tests/evals/test_mail_draft_regression.py backend/tests/unit/agents/test_daily_brief.py backend/tests/integration/api/test_conversations.py
git commit -m "feat: add encrypted local mail drafts"
~~~

### Task 15: Expose mail draft REST APIs with version and cache controls

**Files:**
- Create: `backend/src/ai_employee/api/routers/mail.py`
- Modify: `backend/src/ai_employee/api/deps.py`
- Modify: `backend/src/ai_employee/main.py`
- Create: `backend/tests/integration/api/test_mail_drafts.py`
- Create: `backend/tests/contract/test_mail_api_schema.py`

- [ ] **Step 1: Write failing API contracts**

~~~python
async def test_patch_mail_draft_requires_current_version(authenticated_api_clients) -> None:
    response = await authenticated_api_clients.owner.patch(
        f"/api/v1/mail/drafts/{DRAFT_ID}",
        json={"version": 1, "body_text": "Updated synthetic body"},
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.headers["cache-control"] == "no-store"


async def test_stale_draft_patch_returns_stable_problem(authenticated_api_clients) -> None:
    response = await authenticated_api_clients.owner.patch(
        f"/api/v1/mail/drafts/{DRAFT_ID}",
        json={"version": 1, "body_text": "Stale body"},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "draft_version_conflict"
~~~

Cover all seven specification routes, pagination, source-thread ownership, creation replay under the same `Idempotency-Key`, 201/202 semantics, CSRF on modifications, cross-user 404, invalid addresses/limits, cancellation states, model-task idempotency, submit-task idempotency, and `Cache-Control: no-store` on every response containing a body or full approval preview.

- [ ] **Step 2: Run API tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/integration/api/test_mail_drafts.py backend/tests/contract/test_mail_api_schema.py -q`

Expected: FAIL with 404 because the mail router is not registered.

- [ ] **Step 3: Implement strict request/response schemas and routes**

Create `MailDraftResponse`, `CreateMailDraftRequest`, and `UpdateMailDraftRequest` with `extra="forbid"`. Return only the authenticated user's decrypted current version. `POST /mail/drafts`, `generate`, and `submit` require `Idempotency-Key`; create returns the existing 201 resource representation on replay, while the long actions return:

~~~python
class AcceptedTaskResponse(BaseModel):
    """返回已持久创建的长任务标识，不暗示任务已完成。"""

    task_id: UUID
    status: Literal["queued"]
~~~

`MailDraftResponse` includes the server-derived `recipient_suggestions: list[str]`; it is not persisted
as a second source of truth and is recomputed on GET. Use a small response helper that sets
`Cache-Control: no-store` without duplicating business logic in route functions.

- [ ] **Step 4: Register dependencies and stable error mappings**

Compose the mail draft use cases in `main.py`/`deps.py`. Map `draft_version_conflict`, `idempotency_key_payload_mismatch`, `mail_recipient_limit_exceeded`, `mail_thread_binding_conflict`, `external_writes_disabled`, `connection_capability_disabled`, and `connection_scope_missing` to the exact 409/422 semantics from the specification; do not collapse them to generic `approval_conflict`.

- [ ] **Step 5: Run API, CSRF, and redaction checks**

Run: `uv run --project backend pytest backend/tests/integration/api/test_mail_drafts.py backend/tests/contract/test_mail_api_schema.py backend/tests/integration/api/test_auth.py backend/tests/unit/api/test_domain_error_mapping.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/api/routers/mail.py backend/src/ai_employee/api/deps.py backend/src/ai_employee/main.py backend/tests/integration/api/test_mail_drafts.py backend/tests/contract/test_mail_api_schema.py
git commit -m "feat: expose mail draft APIs"
~~~

### Task 16: Implement deterministic availability and versioned calendar proposals

**Files:**
- Create: `backend/src/ai_employee/domain/calendar_availability.py`
- Modify: `backend/src/ai_employee/domain/settings.py`
- Create: `backend/src/ai_employee/application/use_cases/calendar_proposals.py`
- Create: `backend/src/ai_employee/workers/prepare_calendar_restore.py`
- Modify: `backend/src/ai_employee/workers/execute_task.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/calendar_proposals.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/calendar.py`
- Modify: `backend/src/ai_employee/agents/daily_brief/nodes.py`
- Modify: `backend/src/ai_employee/workers/conversation.py`
- Create: `backend/tests/unit/domain/test_calendar_availability.py`
- Create: `backend/tests/unit/application/test_calendar_proposals.py`
- Create: `backend/tests/integration/m2/test_calendar_proposal_versions.py`
- Modify: `backend/tests/unit/agents/test_daily_brief.py`
- Modify: `backend/tests/integration/api/test_conversations.py`

- [ ] **Step 1: Write failing availability and proposal tests**

~~~python
def test_candidates_apply_working_hours_buffer_grid_and_limit() -> None:
    result = suggest_meeting_times(
        requested_duration=timedelta(minutes=30),
        search_start=datetime(2030, 3, 11, 0, tzinfo=UTC),
        timezone="America/Los_Angeles",
        working_hours=weekday_hours("09:00", "18:00"),
        meeting_buffer=timedelta(minutes=10),
        events=(busy("2030-03-11T17:00:00Z", "2030-03-11T18:00:00Z"),),
        horizon_days=14,
        grid_minutes=15,
        limit=3,
    )

    assert len(result.candidates) == 3
    assert all(item.starts_at.minute in {0, 15, 30, 45} for item in result.candidates)
    assert result.completeness == "complete"


async def test_update_proposal_captures_etag_and_encrypted_before_snapshot() -> None:
    proposal = await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes={"location": "Synthetic room"},
    )

    assert proposal.base_etag == 'W/"etag-1"'
    assert proposal.before_snapshot_id is not None
~~~

Add unit cases for spring-forward/fall-back DST, multiple working intervals, weekends, zero/120-minute buffer, all-day events, transparent/cancelled events, partial completeness with missing connections, no attendee Free/Busy claim, default notification policy, recurrence rejection with `calendar_recurring_event_unsupported`, read-only calendars, and restore rejection when the provider's current event is deleted or no longer editable. Integration coverage must prove the restore task performs the provider read outside its database transaction, then atomically persists the new proposal/current ETag/snapshot.

Add controlled-initiation cases proving a brief conflict exposes `suggested_action_kind="calendar.update"`, and an explicit conversation request creates only an editable proposal shell. The user must choose/confirm exact calendar, time, attendees, and notification policy before submission; neither conversation classification nor a model may freeze or execute it.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/domain/test_calendar_availability.py backend/tests/unit/application/test_calendar_proposals.py backend/tests/integration/m2/test_calendar_proposal_versions.py -q`

Expected: FAIL because availability and proposal use cases do not exist.

- [ ] **Step 3: Implement typed working hours and availability**

Add immutable `WorkingInterval`, `WeeklyWorkingHours`, `AvailabilityEvent`, `CandidateTime`, and
`AvailabilityResult`. The result always carries `attendee_availability_checked: Literal[False]` plus
`completeness` and `missing_connection_ids`, so neither a model nor frontend can imply attendee
Free/Busy was queried. `suggest_meeting_times()` must:

~~~python
for local_day in requested_local_days(search_start, timezone, horizon_days=14):
    for interval in working_hours.intervals_for(local_day.weekday()):
        for candidate_start in quarter_hour_grid(interval, minutes=15):
            candidate_end = candidate_start + requested_duration
            if candidate_end > interval.end:
                continue
            if not overlaps(buffered_busy_events, candidate_start, candidate_end):
                candidates.append(to_utc_candidate(candidate_start, candidate_end, timezone))
                if len(candidates) == 3:
                    return AvailabilityResult(tuple(candidates), completeness, missing_connections)
~~~

Resolve DST with an explicit wall-time policy: skip a grid point when either endpoint fails a
UTC→local round trip (spring-forward nonexistent time), use `fold=0` for an ambiguous endpoint, and
skip a candidate whose UTC offset changes inside the requested duration so the displayed wall-clock
duration cannot differ from the approved duration. De-duplicate candidates by UTC start/end. Reject
malformed or overlapping user working intervals at the settings boundary rather than guessing.

- [ ] **Step 4: Implement create, update, restore, edit, and suggest use cases**

Create each proposal through a user-scoped creation idempotency key and reuse the existing local object
on replay. Proposals are allowed only against enabled `calendar.write` connections and
`provider_calendars.can_write=True`. Update proposals load the latest normalized event, reject recurrence, decrypt description/location only in controlled memory, save an encrypted `before` snapshot, and set `base_etag`.

Implement `PrepareCalendarRestoreTaskStep`: load only user/connection/calendar/event/snapshot identifiers
in a short transaction, release it, call the injected provider-neutral `get_current_event()`, then open
a new transaction to recheck ownership/capability, load and decrypt the historical snapshot, compare
the provider event, and atomically create a new restore proposal with the current ETag, complete diff,
new version, and new operation identity. Deleted, recurring, or no-longer-editable provider facts fail
with the stable specification errors and create no proposal.

Every edit increments the proposal version and invalidates cached conflict results when a time field
changes. Creation defaults to `all` when attendees exist and `none` otherwise. Update/restore defaults
to `all` when `changed_fields` intersects time, location, or attendees, and to `none` for title,
description, or other personal-field-only changes; the user may change it before freezing. A
notification mapping unsupported by the chosen provider is rejected before submission.

Extend the conversation intent union with `prepare_calendar_proposal`; the Worker derives a stable
creation key from its persisted task/message intent, creates or reuses a proposal in `editing`, and
returns its link. Daily brief conflict suggestions bind the source event IDs but require an explicit
click with its own idempotency key before creating the proposal.

- [ ] **Step 5: Run domain and persistence tests**

Run: `uv run --project backend pytest backend/tests/unit/domain/test_calendar_availability.py backend/tests/unit/application/test_calendar_proposals.py backend/tests/integration/m2/test_calendar_proposal_versions.py backend/tests/unit/domain/test_calendar_conflicts.py backend/tests/unit/agents/test_daily_brief.py backend/tests/integration/api/test_conversations.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/domain/calendar_availability.py backend/src/ai_employee/domain/settings.py backend/src/ai_employee/application/use_cases/calendar_proposals.py backend/src/ai_employee/workers/prepare_calendar_restore.py backend/src/ai_employee/workers/execute_task.py backend/src/ai_employee/infrastructure/db/repositories/calendar_proposals.py backend/src/ai_employee/infrastructure/db/repositories/calendar.py backend/src/ai_employee/agents/daily_brief/nodes.py backend/src/ai_employee/workers/conversation.py backend/tests/unit/domain/test_calendar_availability.py backend/tests/unit/application/test_calendar_proposals.py backend/tests/integration/m2/test_calendar_proposal_versions.py backend/tests/unit/agents/test_daily_brief.py backend/tests/integration/api/test_conversations.py
git commit -m "feat: add calendar change proposals"
~~~

### Task 17: Expose calendar proposal, restore, and working-settings APIs

**Files:**
- Create: `backend/src/ai_employee/api/routers/calendar.py`
- Modify: `backend/src/ai_employee/api/routers/settings.py`
- Modify: `backend/src/ai_employee/application/use_cases/settings.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/settings.py`
- Modify: `backend/src/ai_employee/api/deps.py`
- Modify: `backend/src/ai_employee/main.py`
- Create: `backend/tests/integration/api/test_calendar_proposals.py`
- Modify: `backend/tests/integration/api/test_settings.py`
- Create: `backend/tests/contract/test_calendar_api_schema.py`

- [ ] **Step 1: Write failing calendar and settings API tests**

~~~python
async def test_suggest_times_returns_deterministic_partial_completeness(
    authenticated_api_clients,
) -> None:
    response = await authenticated_api_clients.owner.post(
        f"/api/v1/calendar/proposals/{PROPOSAL_ID}/suggest-times"
    )

    assert response.status_code == 200
    assert len(response.json()["candidates"]) <= 3
    assert response.json()["completeness"] == "partial"
    assert response.json()["missing_connections"] == [str(STALE_CONNECTION_ID)]


async def test_settings_accept_non_overlapping_weekly_hours(authenticated_api_clients) -> None:
    response = await authenticated_api_clients.owner.patch(
        "/api/v1/settings",
        json={
            "working_hours": {"monday": [["09:00", "12:00"], ["13:00", "18:00"]]},
            "meeting_buffer_minutes": 15,
        },
    )

    assert response.status_code == 200
    assert response.json()["meeting_buffer_minutes"] == 15
~~~

Cover all eight proposal routes, creation replay under the same `Idempotency-Key`, version conflicts, restore ownership, an exact historical `snapshot_id` in the restore request, restore idempotency, 201/202 semantics, no-store responses, recurrence/read-only/ETag errors, CSRF, cross-user 404, all-day date schemas, IANA timezone validation, and overlapping working-hours rejection.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/integration/api/test_calendar_proposals.py backend/tests/integration/api/test_settings.py backend/tests/contract/test_calendar_api_schema.py -q`

Expected: FAIL because the calendar router and settings fields are absent.

- [ ] **Step 3: Implement strict calendar API schemas**

Create discriminated `CreateProposalRequest` variants for `create` and `update`, an optimistic `UpdateProposalRequest`, typed field-diff responses, candidate-time responses, `RestoreProposalRequest(snapshot_id: UUID)`, and `AcceptedTaskResponse`. Reject provider-specific recurrence, conference, attachment, and raw extension fields with 422 at Pydantic validation.

The candidate response requires `attendee_availability_checked=false`; no route or provider adapter in
M2 accepts attendee Free/Busy inputs.

`suggest-times` performs database reads and deterministic CPU work without creating a TaskRun; it must not keep a database transaction open while calculating candidates. `POST /calendar/proposals`, `submit`, and `restore-proposal` require `Idempotency-Key`; proposal creation returns the same 201 representation on replay, while the long actions return HTTP 202 with persistent task IDs. The restore task input contains only event/snapshot identifiers and must prove the snapshot belongs to a completed modification of the same current-user event before any provider read.

- [ ] **Step 4: Extend settings responses and validation**

Add `default_mail_connection_id`, `default_calendar_connection_id`, `default_calendar_id`, fully normalized seven-day `working_hours`, and `meeting_buffer_minutes` to application/API types. The settings repository verifies defaults belong to the current user and have the required enabled capability; invalid defaults return `connection_capability_disabled` rather than silently falling back. Calendar routes map `idempotency_key_payload_mismatch` and `external_writes_disabled` to 409 with an actionable recovery response.

- [ ] **Step 5: Run API and domain regressions**

Run: `uv run --project backend pytest backend/tests/integration/api/test_calendar_proposals.py backend/tests/integration/api/test_settings.py backend/tests/contract/test_calendar_api_schema.py backend/tests/unit/domain/test_calendar_availability.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/api/routers/calendar.py backend/src/ai_employee/api/routers/settings.py backend/src/ai_employee/application/use_cases/settings.py backend/src/ai_employee/infrastructure/db/repositories/settings.py backend/src/ai_employee/api/deps.py backend/src/ai_employee/main.py backend/tests/integration/api/test_calendar_proposals.py backend/tests/integration/api/test_settings.py backend/tests/contract/test_calendar_api_schema.py
git commit -m "feat: expose calendar proposal APIs"
~~~

### Task 18: Freeze proposal versions into encrypted single-operation approvals

**Files:**
- Create: `backend/src/ai_employee/application/ports/trusted_actions.py`
- Create: `backend/src/ai_employee/application/use_cases/trusted_actions.py`
- Modify: `backend/src/ai_employee/application/use_cases/mail_drafts.py`
- Modify: `backend/src/ai_employee/application/use_cases/calendar_proposals.py`
- Modify: `backend/src/ai_employee/application/use_cases/approvals.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/approvals.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Modify: `backend/src/ai_employee/api/routers/approvals.py`
- Modify: `backend/src/ai_employee/application/use_cases/task_views.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/task_views.py`
- Modify: `backend/src/ai_employee/api/routers/tasks.py`
- Create: `backend/tests/unit/application/test_action_submission.py`
- Create: `backend/tests/integration/m2/test_encrypted_approval_submission.py`
- Modify: `backend/tests/integration/agents/test_fake_approval.py`
- Modify: `backend/tests/integration/api/test_tasks.py`

- [ ] **Step 1: Write failing freeze, invalidation, and deadline tests**

~~~python
async def test_submit_mail_draft_freezes_one_encrypted_command() -> None:
    result = await submit_mail.execute(
        user_id=USER_ID,
        draft_id=DRAFT_ID,
        expected_version=3,
        idempotency_key="synthetic-submit-1",
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )

    approval = await repository.get_approval(result.approval_id)
    assert approval.action == "mail.send"
    assert approval.expires_at == datetime(2030, 1, 1, 0, 10, tzinfo=UTC)
    assert approval.payload == {"storage": "encrypted", "schema_version": "mail_send.v1"}
    assert approval.payload_ciphertext is not None


async def test_edit_invalidates_old_approval_permanently() -> None:
    await withdraw_and_edit(DRAFT_ID, approval_id=APPROVAL_ID)

    with pytest.raises(StateConflictError) as raised:
        await decide(APPROVAL_ID, version=1, payload_hash=OLD_HASH)
    assert raised.value.error_code == "approval_invalidated_by_edit"


async def test_cancelling_waiting_trusted_task_withdraws_approval_for_editing() -> None:
    snapshot = await cancel_task.execute(
        task_id=TASK_ID,
        user_id=USER_ID,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert snapshot is not None and snapshot.status.value == "cancelled"
    assert await approval_status(APPROVAL_ID) == "invalidated"
    assert await draft_status(DRAFT_ID) == "editing"
    assert await provider_write_call_count() == 0
~~~

Add tests for one command per approval, recipient/attendee limit revalidation, exact connection/calendar, risk `high`/`medium`, 10-minute expiry, approved decision setting a five-minute execution deadline, duplicate decisions, payload-hash mismatch, global/provider write switch off before submit, capability loss before submit, calendar stale ETag, rejection/expiry/withdrawal requiring a newly saved version before resubmission, and legacy `fake.write` compatibility.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/application/test_action_submission.py backend/tests/integration/m2/test_encrypted_approval_submission.py backend/tests/integration/agents/test_fake_approval.py -q`

Expected: FAIL because submit still uses the fake-write approval shape.

- [ ] **Step 3: Implement atomic submission**

Create a provider-neutral, side-effect-free preflight boundary before the use case:

~~~python
class ApprovalWarningCode(StrEnum):
    GOOGLE_SEND_UPDATES_NONE_EXTERNAL_SYNC = (
        "google_send_updates_none_external_sync"
    )


@dataclass(frozen=True, slots=True)
class ApprovalPreflightResult:
    warnings: tuple[ApprovalWarningCode, ...] = ()


class TrustedActionPreflight(Protocol):
    provider: str

    def validate_for_approval(
        self, command: TrustedCommand
    ) -> ApprovalPreflightResult:
        """只验证供应商能否无损表达冻结命令；不得访问网络或产生外部副作用。"""
        ...
~~~

The fixed provider registry returns this preflight by exact provider/action and has no runtime
registration. Fake implementations cover all four actions; a provider path not implemented yet fails
closed with `provider_action_unavailable` rather than silently approving it.

For both draft and calendar proposal submission, first reuse an existing task for the same
user/idempotency key, then lock the current version. A different idempotency key must reject a version
already referenced by any terminal or pending ApprovalRequest (`draft_version_conflict` or
`proposal_version_conflict`). Require the global/provider write switches to be enabled, then recheck
capability and provider directory permissions; a disabled switch raises content-free
`external_writes_disabled` and creates no task or approval. Build the strict
command schema with a new stable `operation_id` (`message_date=now` for mail and
`client_event_id=calendar_client_event_id(operation_id)` for calendar create), invoke the pure
provider preflight, then canonicalize/hash/encrypt it and atomically create:

~~~python
task = TaskRunModel(
    id=task_id,
    user_id=user_id,
    kind="trusted_action",
    status="queued",
    idempotency_key=idempotency_key,
    input_payload={
        "approval_id": str(approval_id),
        "operation_id": str(operation_id),
    },
)
step = TaskStepModel(
    id=step_id,
    task_id=task_id,
    sequence=1,
    name="await_approval",
    kind="trusted_action",
    status="pending",
    input_summary={"action": command.action, "proposal_version": proposal_version},
)
approval = ApprovalRequestModel(
    id=approval_id,
    task_id=task_id,
    step_id=step_id,
    version=1,
    action=command.action,
    schema_version=command.schema_version,
    risk_level=risk_level.value,
    payload={"storage": "encrypted", "schema_version": command.schema_version},
    payload_hash=command_hash,
    payload_ciphertext=encrypted.ciphertext,
    payload_nonce=encrypted.nonce,
    payload_key_version=encrypted.key_version,
    proposal_kind=proposal_kind,
    proposal_id=proposal_id,
    proposal_version=proposal_version,
    preview_markdown="",
    expires_at=now + timedelta(minutes=10),
    status="pending",
)
outbox = OutboxEventModel(
    topic="task.execute",
    aggregate_id=task_id,
    deduplication_key=f"task.execute:{task_id}:initial",
    payload={"task_id": str(task_id)},
    available_at=now,
)
~~~

Set the draft/proposal to `awaiting_approval` in the same transaction. Persist only the preflight's
fixed warning codes in content-free preview metadata; never store a provider payload. The task input
and all audit/Outbox payloads contain IDs, action, version, status, risk, and approved warning codes
only.

- [ ] **Step 4: Make existing task cancellation the explicit withdrawal path**

Extend `CancelTaskUseCase` and its repository transaction for a `trusted_action` still in
`waiting_approval`: lock TaskRun, ApprovalRequest, and the bound draft/proposal; set the approval to
`invalidated`, the task to `cancelled`, and the local object to `editing`; clear lease/schedule fields
and append content-free `approval.invalidated` and `task.cancelled` audit/Outbox facts. Reject
cancellation after a ToolExecution claim exists. Keep the existing `/api/v1/tasks/{task_id}/cancel`
route and CSRF contract, so mail and calendar do not gain a second undocumented withdrawal API.

- [ ] **Step 5: Resolve encrypted approvals safely**

Extend `ApprovalDecisionUseCase` and repository resolution so the API compares request version and uses `hmac.compare_digest()` for the 64-character payload hash, verifies a durable LangGraph interrupt, checks current capability and proposal version, and rejects `invalidated`. On approval set `approved_execution_deadline_at = now + timedelta(minutes=5)`; on rejection return the local draft/proposal to `editing`. Extend the existing approval-expiry repository transaction to do the same for M2 proposal bindings while leaving legacy fake-write behavior unchanged. Rejection or expiry keeps the referenced local version consumed, so the next submit requires a saved incremented version. Do not decrypt the command merely to echo it in audit or SSE.

- [ ] **Step 6: Run submission, approval, withdrawal, and fake-write regressions**

Run: `uv run --project backend pytest backend/tests/unit/application/test_action_submission.py backend/tests/integration/m2/test_encrypted_approval_submission.py backend/tests/integration/agents/test_fake_approval.py backend/tests/integration/api/test_tasks.py backend/tests/unit/domain/test_approval_policy.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

~~~bash
git add backend/src/ai_employee/application/ports/trusted_actions.py backend/src/ai_employee/application/use_cases/trusted_actions.py backend/src/ai_employee/application/use_cases/mail_drafts.py backend/src/ai_employee/application/use_cases/calendar_proposals.py backend/src/ai_employee/application/use_cases/approvals.py backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py backend/src/ai_employee/infrastructure/db/repositories/approvals.py backend/src/ai_employee/integrations/registry.py backend/src/ai_employee/api/routers/approvals.py backend/src/ai_employee/application/use_cases/task_views.py backend/src/ai_employee/infrastructure/db/repositories/task_views.py backend/src/ai_employee/api/routers/tasks.py backend/tests/unit/application/test_action_submission.py backend/tests/integration/m2/test_encrypted_approval_submission.py backend/tests/integration/agents/test_fake_approval.py backend/tests/integration/api/test_tasks.py
git commit -m "feat: freeze encrypted trusted approvals"
~~~

### Task 19: Execute approved commands through a checkpointed claim-first graph

**Files:**
- Create: `backend/src/ai_employee/agents/trusted_actions/__init__.py`
- Create: `backend/src/ai_employee/agents/trusted_actions/state.py`
- Create: `backend/src/ai_employee/agents/trusted_actions/graph.py`
- Modify: `backend/src/ai_employee/application/ports/trusted_actions.py`
- Modify: `backend/src/ai_employee/application/use_cases/trusted_actions.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py`
- Create: `backend/src/ai_employee/workers/trusted_actions.py`
- Modify: `backend/src/ai_employee/workers/execute_task.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Create: `backend/tests/unit/agents/test_trusted_action_graph.py`
- Create: `backend/tests/integration/agents/test_trusted_action_resume.py`
- Create: `backend/tests/integration/m2/test_tool_execution_claim.py`

- [ ] **Step 1: Write failing graph, race, and deadline tests**

~~~python
async def test_two_workers_create_one_claim_and_one_provider_call() -> None:
    first, second = await asyncio.gather(
        worker.execute(TASK_ID, owner="worker-a"),
        worker.execute(TASK_ID, owner="worker-b"),
        return_exceptions=True,
    )

    assert fake_provider.write_calls == 1
    assert await count_tool_executions(TASK_ID) == 1
    assert sum(not isinstance(item, Exception) for item in (first, second)) == 1


async def test_claim_after_approved_deadline_invalidates_without_provider_call() -> None:
    await worker.execute(TASK_ID, now=datetime(2030, 1, 1, 0, 6, tzinfo=UTC))

    assert fake_provider.write_calls == 0
    assert await task_error(TASK_ID) == "approval_execution_deadline_expired"
    assert await approval_status(APPROVAL_ID) == "invalidated"
    assert await draft_status(DRAFT_ID) == "editing"


async def test_stale_claim_before_request_is_recovered_with_one_write() -> None:
    await persist_claim(
        task_id=TASK_ID,
        claimed_at=datetime(2030, 1, 1, 0, 1, tzinfo=UTC),
        request_started_at=None,
        lease_expired=True,
    )

    await worker.execute(TASK_ID, owner="recovery-worker")

    assert fake_provider.write_calls == 1
    assert await count_tool_executions(TASK_ID) == 1


async def test_non_production_claim_rejects_connection_outside_allowlist() -> None:
    worker.settings.write_test_account_allowlist = ["google::allowed-account"]

    await worker.execute(TASK_ID)

    assert fake_provider.write_calls == 0
    assert await task_error(TASK_ID) == "external_write_account_not_allowed"
~~~

Add crash/replay tests for pre-interrupt, post-interrupt, approved resume, rejected resume, after claim before request, after result commit before queue ack, duplicate queue delivery, capability disabled while queued, global/provider switch shutdown, normalized provider-identity allowlist mismatch, hash/AAD tampering, and checkpoint state containing no decrypted command fields.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/agents/test_trusted_action_graph.py backend/tests/integration/agents/test_trusted_action_resume.py backend/tests/integration/m2/test_tool_execution_claim.py -q`

Expected: FAIL because only the fake-write graph exists.

- [ ] **Step 3: Extend the preflight boundary with narrow write/reconciliation methods**

~~~python
@dataclass(frozen=True, slots=True)
class ProviderWriteOutcome:
    kind: ProviderWriteOutcomeKind
    retryable: bool
    retry_after_seconds: int | None
    provider_resource_id: str | None
    provider_request_id: str | None
    correlation_id: str
    provider_url: str | None
    error_code: str | None


class TrustedActionAdapter(TrustedActionPreflight, Protocol):
    provider: str

    async def execute(self, command: TrustedCommand) -> ProviderWriteOutcome:
        raise NotImplementedError

    async def reconcile(
        self, command: TrustedCommand, execution: ExecutionReference
    ) -> ProviderWriteOutcome:
        raise NotImplementedError
~~~

`retry_after_seconds` is accepted only for `confirmed_not_applied` retryable outcomes, is bounded by
the existing durable retry policy, and is persisted in the content-free result summary before a
`retry_scheduled` transition. Unknown outcomes and permanent failures require it to be `None`.
The registry exposes adapters only for the fixed Google/Microsoft provider and four command actions. It rejects any dynamic name or unsupported command class.

- [ ] **Step 4: Implement identifier-only graph and atomic claim**

`TrustedActionState` contains `task_id`, `approval_id`, `operation_id`, `payload_hash`, `decision`, and content-free messages only. The graph nodes are `load_approval → await_approval → claim → execute_or_reconcile → finalize`.

The first claim transaction must lock task/approval, require the owning user to remain active,
compare approved status/version/deadline and the payload hash with `hmac.compare_digest()`, and
recheck the global/provider switches and connection capability. It must derive the stable connection
identity by calling
`canonical_provider_identity_key(connection.provider, connection.provider_tenant_id, connection.provider_account_id)`;
claim code must not hand-compose raw fields. The returned physical key is the exact three-segment
`provider:encoded_tenant:encoded_account` form from Task 1 while retaining the logical
provider/tenant/stable-account semantics, including Microsoft tenant binding and fail-closed colon
handling. It must use the same unreserved-literal and uppercase `%HH` rules, 255-character raw and
3060-character encoded segment limits, and strict canonical parser from Task 1; malformed or
non-canonical values always fail closed. Require `settings.write_account_allowed()` with that
canonical key before inserting a ToolExecution. If the five-minute deadline already expired, atomically invalidate the approval,
fail the task with `approval_execution_deadline_expired`, return the local object to `editing`, and
create no ToolExecution; that consumed version still requires a new save before resubmission. This
allowlist check is mandatory in the separately configured non-production real-provider environment
and is an optional extra restriction in production. Neither the raw components nor complete
canonical key may be emitted to logs, metrics, audit, SSE, API responses, or committed evidence.
Insert this exact idempotency key before any provider call:

~~~python
idempotency_key = ":".join(
    (
        approval.action,
        str(task.id),
        str(approval.id),
        str(approval.version),
        str(operation_id),
    )
)
~~~

The same claim transaction sets the local object to `executing`, appends the content-free
`tool.claimed` audit event, and writes its Outbox/SSE notification. No Redis publication is allowed
before that PostgreSQL commit.

Handle an existing row from durable facts rather than status alone:

- `succeeded` or `confirmed_failed`: reuse the standardized terminal result.
- `claimed` with `request_started_at is None` and an expired TaskRun lease: recovery may acquire the
  lease and continue the same already-authorized claim into its first write; the five-minute deadline
  is not rechecked because the original claim met it.
- `claimed` with a live lease: the duplicate Worker exits without an adapter call.
- `executing`, `reconciling`, or `needs_attention`: never call `execute()`; route only to read-only
  reconciliation or wait for user handling.
- `retryable_failed`: call `execute()` again only because that persisted status can be created solely
  from `confirmed_not_applied` plus `ProviderWriteOutcome.retryable=True`; permanent confirmed
  rejections are stored as `confirmed_failed` and never re-enter execute.

This distinction proves that a crash after claim but before the committed request-start fact can
recover availability without weakening the no-blind-retry rule.

Decrypt and parse the command after claim, in the Worker process, and immediately before adapter
dispatch. Recompute its canonical hash and compare it to both ApprovalRequest and graph state before
changing `request_started_at`; AEAD or hash failure terminates with zero provider calls. In one
committed transaction set `request_started_at` if absent, increment `write_attempt_count`, and set
`executing` before the HTTP call. Reconciliation never changes `write_attempt_count`; safe
confirmed-not-applied retries increment it exactly once per actual adapter `execute()` call.

- [ ] **Step 5: Run graph, claim, and M1 recovery suites**

Run: `uv run --project backend pytest backend/tests/unit/agents/test_trusted_action_graph.py backend/tests/integration/agents/test_trusted_action_resume.py backend/tests/integration/m2/test_tool_execution_claim.py backend/tests/integration/agents/test_checkpoint_resume.py backend/tests/integration/faults/test_duplicate_delivery.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/agents/trusted_actions backend/src/ai_employee/application/ports/trusted_actions.py backend/src/ai_employee/application/use_cases/trusted_actions.py backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py backend/src/ai_employee/workers/trusted_actions.py backend/src/ai_employee/workers/execute_task.py backend/src/ai_employee/integrations/registry.py backend/tests/unit/agents/test_trusted_action_graph.py backend/tests/integration/agents/test_trusted_action_resume.py backend/tests/integration/m2/test_tool_execution_claim.py
git commit -m "feat: execute claimed trusted actions"
~~~

### Task 20: Reconcile unknown outcomes and record explicit manual resolution

**Files:**
- Modify: `backend/src/ai_employee/application/use_cases/trusted_actions.py`
- Create: `backend/src/ai_employee/application/use_cases/action_views.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py`
- Create: `backend/src/ai_employee/infrastructure/db/repositories/action_views.py`
- Create: `backend/src/ai_employee/workers/reconcile_actions.py`
- Modify: `backend/src/ai_employee/workers/schedules.py`
- Modify: `backend/src/ai_employee/workers/execute_task.py`
- Create: `backend/tests/unit/application/test_reconciliation_policy.py`
- Create: `backend/tests/integration/m2/test_action_reconciliation.py`
- Create: `backend/tests/integration/m2/test_manual_resolution_race.py`

- [ ] **Step 1: Write failing bounded-reconciliation and race tests**

~~~python
@pytest.mark.parametrize(
    ("attempt", "delay_seconds"),
    [(0, 1), (1, 5), (2, 30), (3, 120)],
)
def test_reconciliation_schedule_is_bounded(attempt: int, delay_seconds: int) -> None:
    assert reconciliation_delay(attempt) == timedelta(seconds=delay_seconds)


async def test_manual_not_executed_does_not_call_provider_or_requeue_write() -> None:
    await manual_resolution.execute(
        user_id=USER_ID,
        task_id=TASK_ID,
        task_version=TASK_VERSION,
        resolution="confirmed_not_executed",
        now=NOW,
    )

    assert fake_provider.write_calls == 0
    assert await task_status(TASK_ID) == "failed"
    assert await tool_manual_resolution(TASK_ID) == "confirmed_not_executed"
    assert await draft_status(DRAFT_ID) == "editing"
    assert await current_draft_version(DRAFT_ID) == FROZEN_DRAFT_VERSION


async def test_manual_resolution_rejects_a_stale_audit_cursor() -> None:
    with pytest.raises(StateConflictError) as raised:
        await manual_resolution.execute(
            user_id=USER_ID,
            task_id=TASK_ID,
            task_version=PREVIOUS_AUDIT_EVENT_ID,
            resolution="confirmed_executed",
            now=NOW,
        )

    assert raised.value.error_code == "manual_resolution_conflict"
~~~

Add tests for unknown write response entering `reconciling` with `provider_write_outcome_unknown`, four persisted read-only attempts, successful convergence, confirmed-not-applied convergence, one deduplicated read-only source refresh after applied/manual-executed results, no refresh after not-applied results, final `needs_attention` with `provider_reconciliation_failed`, user-triggered reconcile, manual/result race with one winner, provider link availability, no free-text evidence, stale audit-cursor rejection, draft/proposal status convergence, frozen-version resubmission rejection, and task states that never queue another write.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/application/test_reconciliation_policy.py backend/tests/integration/m2/test_action_reconciliation.py backend/tests/integration/m2/test_manual_resolution_race.py -q`

Expected: FAIL because unknown outcomes currently become a generic failure.

- [ ] **Step 3: Persist reconciliation state and scheduling**

When an adapter returns `UNKNOWN`, atomically persist its normalized `provider_resource_id`,
`provider_request_id`, `correlation_id`, and content-free result summary, set ToolExecution and TaskRun
to `reconciling`, set the task's stable error code to `provider_write_outcome_unknown`, set the bound
mail draft or calendar proposal to `needs_attention` so it cannot be edited or resubmitted, increment
no write-attempt counter, append `tool.reconciling`, and schedule the next task execution with
`TaskRun.scheduled_for = now + reconciliation_delay(attempt)`. Never persist the raw response. Each
reconciliation Worker decrypts the same command but invokes only `adapter.reconcile()`.

After the fourth unresolved attempt, set ToolExecution and TaskRun to `needs_attention`, replace the task error code with `provider_reconciliation_failed`, append `tool.needs_attention`, and clear leases/schedules. A user-requested reconcile transitions `needs_attention → reconciling` and creates an Outbox event without changing the original ToolExecution identity.

- [ ] **Step 4: Converge tool, task, and local proposal states together**

Every automatic or manual terminal result updates ToolExecution, TaskRun, the bound local object,
AuditEvent, Outbox, and content-free SSE fact in one transaction. Persist the normalized
`provider_resource_id`, `provider_request_id`, `correlation_id`, and `completed_at` facts supplied by
the adapter without retaining its raw response:

- confirmed applied or `confirmed_executed`: mail draft `sent`, calendar proposal `applied`, tool
  `succeeded`, and task `succeeded`;
- confirmed not applied or `confirmed_not_executed`: mail draft returns to `editing`; calendar
  proposal returns to `editing`, except `calendar_event_version_conflict` becomes `stale`; tool is
  `confirmed_failed` and task is `failed`;
- unresolved after the bounded schedule: local object, tool, and task all remain
  `needs_attention`;
- a persisted safe retry keeps the local object `executing` and never creates a new approval or
  ToolExecution.

The approved frozen draft/proposal version is terminal even after a confirmed-not-applied result.
Submission must query prior approval/execution facts and reject the same version; the user must save
or clone a new incremented version before another approval. Persist only a validated, address-free
provider URL in `ToolExecution.result_summary["provider_url"]` for later checking.

For confirmed applied or manually confirmed executed results, enqueue a deduplicated read-only source
refresh in the same transaction: Gmail's mailbox/Sent view, Microsoft `sentitems`, or the exact target
calendar scope. The refresh must normalize the provider's actual resource before updating
`EmailMessage`/`CalendarEvent`; never fabricate a synchronized provider row directly from the approved
command. Confirmed-not-applied results enqueue no source refresh. Tests must prove queue replay creates
one refresh intent and never a second write.

- [ ] **Step 5: Implement manual resolution CAS using the durable audit cursor**

Accept only `confirmed_executed` or `confirmed_not_executed` plus canonical decimal-string
`task_version`. M1 has no
`TaskRun.version` column: define this API field as the task snapshot's current PostgreSQL audit-event
cursor (`MAX(audit_events.id)` for the user/task, or zero before the first event). Expose the same
value from Task 26 as both `event_cursor` and `task_version`, serialized with the existing M1
canonical cursor validator so browser integers never lose BIGINT precision. Parse it only after range
validation, lock TaskRun and ToolExecution, recompute the cursor in that transaction, reject a
mismatch with `manual_resolution_conflict`, require both rows still represent an unresolved result,
and set:

~~~python
execution.manual_resolution = resolution
execution.manual_resolved_by_user_id = user_id
execution.manual_resolved_at = now
execution.status = (
    ToolExecutionStatus.SUCCEEDED.value
    if resolution == "confirmed_executed"
    else ToolExecutionStatus.CONFIRMED_FAILED.value
)
task.status = (
    TaskStatus.SUCCEEDED.value
    if resolution == "confirmed_executed"
    else TaskStatus.FAILED.value
)
~~~

Apply the local-object mapping from Step 4, append actor/time/source audit metadata without a
free-text note under `tool.manually_resolved`, write the matching Outbox/SSE fact, and return the newly appended audit ID as the next canonical string `task_version`. A concurrent
automatic reconciliation result that wins first returns `manual_resolution_conflict`.

- [ ] **Step 6: Run reconciliation, retry, and scheduler checks**

Run: `uv run --project backend pytest backend/tests/unit/application/test_reconciliation_policy.py backend/tests/integration/m2/test_action_reconciliation.py backend/tests/integration/m2/test_manual_resolution_race.py backend/tests/integration/workers/test_retry_recovery.py backend/tests/unit/workers/test_due_schedule.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

~~~bash
git add backend/src/ai_employee/application/use_cases/trusted_actions.py backend/src/ai_employee/application/use_cases/action_views.py backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py backend/src/ai_employee/infrastructure/db/repositories/action_views.py backend/src/ai_employee/workers/reconcile_actions.py backend/src/ai_employee/workers/schedules.py backend/src/ai_employee/workers/execute_task.py backend/tests/unit/application/test_reconciliation_policy.py backend/tests/integration/m2/test_action_reconciliation.py backend/tests/integration/m2/test_manual_resolution_race.py
git commit -m "feat: reconcile unknown action outcomes"
~~~

### Task 21: Implement Gmail send, reply, reply-all, and Sent reconciliation

**Files:**
- Create: `backend/src/ai_employee/integrations/mail_mime.py`
- Create: `backend/src/ai_employee/integrations/google/gmail_write.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Create: `backend/tests/contract/google/fixtures/gmail_send_success.json`
- Create: `backend/tests/contract/google/fixtures/gmail_sent_search.json`
- Create: `backend/tests/contract/google/test_gmail_write_adapter.py`
- Create: `backend/tests/integration/m2/test_gmail_trusted_action.py`

- [ ] **Step 1: Write failing MIME, send, and reconciliation contracts**

~~~python
def test_reply_mime_contains_frozen_thread_headers_and_plain_text_only() -> None:
    raw = build_mail_mime(mail_reply_command())
    message = BytesParser(policy=policy.default).parsebytes(raw)

    assert message.get_content_type() == "text/plain"
    assert message["In-Reply-To"] == "<source-message@example.test>"
    assert message["References"] == "<older@example.test> <source-message@example.test>"
    assert message["Message-ID"] == "<00000000-0000-0000-0000-000000000001@ai-employee.invalid>"
    assert message.get_body(preferencelist=("html",)) is None
~~~

HTTP tests must cover `POST /gmail/v1/users/me/messages/send`, Base64URL without padding requirements, `threadId` for replies, normalized returned message/thread IDs, Sent search by stable Message-ID, refresh-once 401, 400/403/429 confirmed rejection, connect failure before send, read timeout after send, ambiguous 5xx, and no Draft API call.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/contract/google/test_gmail_write_adapter.py backend/tests/integration/m2/test_gmail_trusted_action.py -q`

Expected: FAIL because Gmail has no write adapter.

- [ ] **Step 3: Build a shared deterministic plain-text MIME message**

`build_mail_mime()` must use `email.message.EmailMessage`, set From from the connection identity supplied by the adapter rather than the command, set the frozen To/CC/BCC/Subject, render `Date` from the command's frozen `message_date`, set the stable Message-ID from `operation_id`, use UTF-8 plain text, and set only application-generated `In-Reply-To`/`References`. It must reject CR/LF header injection and must not create HTML, attachment, or arbitrary user Header parts. Safe retries therefore produce byte-identical MIME except for transport framing.

Implement Gmail `validate_for_approval()` as a pure check for exact new/reply/reply-all source
bindings, generated-header safety, recipient limits, and MIME representability; it returns no warning
codes and performs no HTTP call.

- [ ] **Step 4: Implement Gmail write outcomes and Sent reconciliation**

~~~python
async def execute(self, command: MailSendCommand) -> ProviderWriteOutcome:
    raw = base64.urlsafe_b64encode(
        build_mail_mime(command, from_address=self._account_email)
    ).decode("ascii")
    body: dict[str, object] = {"raw": raw}
    if command.mode is not MailMode.NEW:
        body["threadId"] = command.source_thread_id
    return await self._send_with_classified_outcome(
        "/messages/send",
        json=body,
        correlation_id=message_id_for(command.operation_id),
    )
~~~

A 200 response with valid `id`/`threadId` is `confirmed_applied`. A failure proven before request transmission or a documented 4xx rejection is `confirmed_not_applied`; mark retry permission separately from permanent validation/scope failures. Read timeout, connection loss after request start, malformed success without IDs, and ambiguous 5xx are `unknown` and must not call send again.

`reconcile()` searches Sent with `in:sent rfc822msgid:<stable-id>`, validates the returned message/thread facts, and returns a provider URL that opens the Sent item without embedding addresses or subject.

- [ ] **Step 5: Run adapter, graph, and duplicate-delivery checks**

Run: `uv run --project backend pytest backend/tests/contract/google/test_gmail_write_adapter.py backend/tests/integration/m2/test_gmail_trusted_action.py backend/tests/integration/m2/test_tool_execution_claim.py backend/tests/integration/faults/test_duplicate_delivery.py -q`

Expected: PASS with at most one Gmail send call per approved operation.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/integrations/mail_mime.py backend/src/ai_employee/integrations/google/gmail_write.py backend/src/ai_employee/integrations/registry.py backend/tests/contract/google/fixtures/gmail_send_success.json backend/tests/contract/google/fixtures/gmail_sent_search.json backend/tests/contract/google/test_gmail_write_adapter.py backend/tests/integration/m2/test_gmail_trusted_action.py
git commit -m "feat: execute Gmail trusted sends"
~~~

### Task 22: Implement Google Calendar create, conditional update, restore, and reconciliation

**Files:**
- Create: `backend/src/ai_employee/integrations/google/calendar_write.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Create: `backend/tests/contract/google/fixtures/calendar_event_write.json`
- Create: `backend/tests/contract/google/test_calendar_write_adapter.py`
- Create: `backend/tests/integration/m2/test_google_calendar_trusted_action.py`

- [ ] **Step 1: Write failing Google Calendar write contracts**

~~~python
@pytest.mark.asyncio
@respx.mock
async def test_update_reads_and_compares_etag_before_put() -> None:
    get_route = respx.get(GOOGLE_EVENT_URL).mock(
        return_value=httpx.Response(200, json={"id": "event-1", "etag": '"etag-2"'})
    )
    put_route = respx.put(GOOGLE_EVENT_URL).mock(
        return_value=httpx.Response(200, json=event_write_fixture())
    )

    outcome = await adapter.execute(calendar_update_command(base_etag='"etag-1"'))

    assert get_route.called
    assert put_route.called is False
    assert outcome.error_code == "calendar_event_version_conflict"
~~~

Cover stable Google event IDs derived from `operation_id`, `extendedProperties.private.ai_employee_operation_id`, exact target calendar URL, `sendUpdates=all|none`, Google `none` warning metadata, full create/update payloads, `If-Match`, recurring-event rejection, 409 duplicate reconciliation, 412 conflict, 401/403/429, timeout/5xx unknown, and event GET reconciliation. The ID test must prove only Google-documented base32hex characters (`0-9a-v`) are emitted and the same UUID always produces the same ID.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/contract/google/test_calendar_write_adapter.py backend/tests/integration/m2/test_google_calendar_trusted_action.py -q`

Expected: FAIL because Google Calendar has no write adapter.

- [ ] **Step 3: Map the typed command without provider extensions leaking upward**

~~~python
def _event_body(command: CalendarCommand) -> dict[str, object]:
    return {
        "id": command.client_event_id if isinstance(command, CalendarCreateCommand) else None,
        "summary": command.title,
        "description": command.description,
        "location": command.location,
        "start": google_event_time(command.starts_at, command.timezone, command.all_day),
        "end": google_event_time(command.ends_at, command.timezone, command.all_day),
        "attendees": [{"email": item} for item in command.attendees],
        "extendedProperties": {
            "private": {"ai_employee_operation_id": str(command.operation_id)}
        },
    }
~~~

Use the already frozen `CalendarCreateCommand.client_event_id` generated by Task 2; assert it equals
`calendar_client_event_id(operation_id)` and never regenerate or send a raw hyphenated UUID as a
Google event ID.
Remove the `id` key for update/restore and omit empty optional fields explicitly; never copy
recurrence, conference, attachments, or unknown fields from a provider event.

- [ ] **Step 4: Implement create/update/restore outcome handling**

Implement Google Calendar `validate_for_approval()` as a pure mapping check. It returns
`google_send_updates_none_external_sync` when `notification_policy=none` and otherwise no warning;
unsupported command fields are already impossible in the strict union. Create uses
`POST /calendars/{calendarId}/events` with the stable ID and notification policy. Update and restore first GET the event, reject deleted/recurring/uneditable facts, compare current ETag with `base_etag`, then use `PUT` with `If-Match` and the complete desired state. A version conflict is confirmed not applied and terminal, not an automatic retry.

Reconciliation GETs the stable created event ID or the exact updated event ID, validates operation correlation/version and desired fields, and returns `confirmed_applied`, `confirmed_not_applied`, or `unknown` without another write.

- [ ] **Step 5: Run adapter and ETag race suites**

Run: `uv run --project backend pytest backend/tests/contract/google/test_calendar_write_adapter.py backend/tests/integration/m2/test_google_calendar_trusted_action.py backend/tests/integration/m2/test_calendar_proposal_versions.py backend/tests/integration/m2/test_action_reconciliation.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/integrations/google/calendar_write.py backend/src/ai_employee/integrations/registry.py backend/tests/contract/google/fixtures/calendar_event_write.json backend/tests/contract/google/test_calendar_write_adapter.py backend/tests/integration/m2/test_google_calendar_trusted_action.py
git commit -m "feat: execute Google calendar actions"
~~~

### Task 23: Implement Microsoft direct mail writes and Sent Items reconciliation

**Files:**
- Create: `backend/src/ai_employee/integrations/microsoft/mail_write.py`
- Modify: `backend/src/ai_employee/integrations/mail_mime.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Create: `backend/tests/contract/microsoft/fixtures/sent_message.json`
- Create: `backend/tests/contract/microsoft/test_mail_write_adapter.py`
- Create: `backend/tests/integration/m2/test_microsoft_mail_trusted_action.py`

- [ ] **Step 1: Write failing Graph mail-write contracts**

~~~python
@pytest.mark.asyncio
@respx.mock
async def test_graph_send_202_requires_read_only_reconciliation() -> None:
    respx.post("https://graph.microsoft.com/v1.0/me/sendMail").mock(
        return_value=httpx.Response(202)
    )

    outcome = await adapter.execute(new_mail_command())

    assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert outcome.correlation_id.endswith("@ai-employee.invalid>")
~~~

Cover Base64 MIME `text/plain` requests for `/me/sendMail`, `/me/messages/{id}/reply`, and `/replyAll`; the documented default save-to-Sent-Items behavior without an invalid JSON wrapper around MIME; `Prefer: IdType="ImmutableId"`; frozen recipient compatibility preflight; no Graph draft endpoint; 202 with empty body; Sent Items filter by stable `internetMessageId`; 401 refresh; 403 scope; 429 confirmed rejection; connect failure; post-send timeout; 5xx unknown; and personal/work account fixtures.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/contract/microsoft/test_mail_write_adapter.py backend/tests/integration/m2/test_microsoft_mail_trusted_action.py -q`

Expected: FAIL because Microsoft has no mail-write adapter.

- [ ] **Step 3: Implement exact direct-send routing and preflight**

New mail posts the Base64 MIME bytes directly to `/me/sendMail` with `Content-Type: text/plain`; do not
wrap that MIME body in the JSON `message/saveToSentItems` shape. M2 requires the documented default of
saving to Sent Items and does not expose a contrary option. Reply and reply-all use their corresponding
direct actions with MIME. Implement Microsoft Mail `validate_for_approval()` to compare the frozen To/CC/BCC set with the deterministic
provider reply/reply-all set supported by Graph; if Graph cannot represent the adjusted recipients
without a draft, return `mail_thread_binding_conflict` and do not create an approval.

Use the shared MIME builder to keep Content-Type Text and the stable Message-ID. Never request `Mail.ReadWrite` and never call createReply/createReplyAll draft APIs.

- [ ] **Step 4: Treat 202 as accepted-but-unconfirmed and reconcile Sent Items**

Every valid 202 returns `unknown` so the trusted-action workflow enters read-only reconciliation. Query `/me/mailFolders/sentitems/messages` with an escaped equality filter on `internetMessageId` and a minimal `$select=id,conversationId,internetMessageId,sentDateTime,webLink`. A unique matching message confirms applied; no match during the bounded window remains unknown; contradictory multiple matches enter `needs_attention`.

- [ ] **Step 5: Run Graph mail and duplicate-write checks**

Run: `uv run --project backend pytest backend/tests/contract/microsoft/test_mail_write_adapter.py backend/tests/integration/m2/test_microsoft_mail_trusted_action.py backend/tests/integration/m2/test_action_reconciliation.py backend/tests/integration/m2/test_tool_execution_claim.py -q`

Expected: PASS with no second Graph write during reconciliation.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/integrations/microsoft/mail_write.py backend/src/ai_employee/integrations/mail_mime.py backend/src/ai_employee/integrations/registry.py backend/tests/contract/microsoft/fixtures/sent_message.json backend/tests/contract/microsoft/test_mail_write_adapter.py backend/tests/integration/m2/test_microsoft_mail_trusted_action.py
git commit -m "feat: execute Microsoft mail sends"
~~~

### Task 24: Implement Microsoft Calendar create/update/restore with timezone and notification guards

**Files:**
- Modify: `backend/src/ai_employee/integrations/microsoft/timezones.py`
- Create: `backend/src/ai_employee/integrations/microsoft/calendar_write.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Modify: `backend/tests/unit/integrations/test_microsoft_timezones.py`
- Create: `backend/tests/contract/microsoft/fixtures/calendar_event_write.json`
- Create: `backend/tests/contract/microsoft/test_calendar_write_adapter.py`
- Create: `backend/tests/integration/m2/test_microsoft_calendar_trusted_action.py`

- [ ] **Step 1: Write failing timezone, notification, and ETag contracts**

~~~python
def test_iana_timezone_maps_to_graph_windows_zone() -> None:
    assert to_windows_timezone("Asia/Shanghai") == "China Standard Time"
    assert to_windows_timezone("America/Los_Angeles") == "Pacific Standard Time"


def test_graph_rejects_notification_none_when_attendees_exist() -> None:
    with pytest.raises(StateConflictError) as raised:
        validate_graph_notification_policy(
            attendees=("person@example.test",),
            policy=NotificationPolicy.NONE,
        )
    assert raised.value.error_code == "calendar_notification_mapping_unsupported"
~~~

Contract tests must cover `transactionId`, exact calendar endpoint, 201 create, conditional update/restore with current GET plus `If-Match`, ETag/changeKey normalization, IANA/Windows mapping, all-day dates, attendee notification limitations, recurrence rejection, 409/412, 401/403/429, timeout/5xx unknown, direct-ID reconciliation, and bounded-window client-side `transactionId` reconciliation without unsupported Delta filters.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/integrations/test_microsoft_timezones.py backend/tests/contract/microsoft/test_calendar_write_adapter.py backend/tests/integration/m2/test_microsoft_calendar_trusted_action.py -q`

Expected: FAIL because the write-time round-trip/notification guards and Graph calendar-write adapter do not exist.

- [ ] **Step 3: Extend the shared timezone boundary with write-time guards**

Reuse the Babel-backed mapping introduced in Task 13. Add write-specific validation that every frozen
IANA zone round-trips to the chosen Graph Windows zone and that an all-day date is not shifted through
UTC conversion. Unknown or non-round-tripping zones fail before approval with
`calendar_timezone_mapping_unsupported`; never silently substitute the server timezone.

- [ ] **Step 4: Implement Graph event writes and reconciliation**

Create uses `POST /me/calendars/{calendarId}/events` and sends `transactionId=str(operation_id)` plus a Graph `dateTimeTimeZone` body. Update/restore GET the exact event, reject recurrence/deletion/permission loss, compare `@odata.etag` or `changeKey`, and PATCH the complete frozen changed fields with `If-Match`.

Because Graph does not provide the Google `sendUpdates=none` control, implement Microsoft Calendar
`validate_for_approval()` to reject attendee-bearing `notification_policy=none` before approval.
Personal events with no attendees map safely to `none`; attendee-bearing events map to `all`.

Reconciliation first reads the returned resource ID when present. If the create response was lost,
read a tightly bounded target-calendar window covering the frozen event time, request the documented
`transactionId` property, and match it client-side; CalendarView Delta does not support `$filter`, and
the plan must not assume a documented direct lookup by `transactionId`. A unique exact match confirms
applied; no match or multiple matches remains `unknown`. Although Microsoft documents
`transactionId` as duplicate-create protection, M2 keeps the stricter approved rule and does not issue
another POST after an unknown result.

- [ ] **Step 5: Run Microsoft Calendar and dependency checks**

Run: `uv run --project backend pytest backend/tests/unit/integrations/test_microsoft_timezones.py backend/tests/contract/microsoft/test_calendar_write_adapter.py backend/tests/integration/m2/test_microsoft_calendar_trusted_action.py backend/tests/integration/m2/test_action_reconciliation.py -q`

Expected: PASS.

Run: `uv pip check --project backend`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/integrations/microsoft/timezones.py backend/src/ai_employee/integrations/microsoft/calendar_write.py backend/src/ai_employee/integrations/registry.py backend/tests/unit/integrations/test_microsoft_timezones.py backend/tests/contract/microsoft/fixtures/calendar_event_write.json backend/tests/contract/microsoft/test_calendar_write_adapter.py backend/tests/integration/m2/test_microsoft_calendar_trusted_action.py
git commit -m "feat: execute Microsoft calendar actions"
~~~

### Task 25: Invalidate unclaimed actions when capabilities or connections are disabled

**Files:**
- Modify: `backend/src/ai_employee/application/use_cases/connections.py`
- Modify: `backend/src/ai_employee/application/use_cases/trusted_actions.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/connections.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py`
- Modify: `backend/src/ai_employee/workers/trusted_actions.py`
- Modify: `backend/src/ai_employee/workers/schedules.py`
- Create: `backend/tests/unit/application/test_capability_revocation.py`
- Create: `backend/tests/integration/m2/test_capability_revocation_races.py`
- Modify: `backend/tests/integration/api/test_connections.py`

- [ ] **Step 1: Write failing disable/disconnect race tests**

~~~python
async def test_disable_write_capability_invalidates_pending_approval() -> None:
    await connections.disable_capability(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        capability=ConnectionCapability.MAIL_SEND,
        now=NOW,
    )

    assert await approval_status(APPROVAL_ID) == "invalidated"
    assert await task_status(TASK_ID) == "cancelled"
    assert fake_provider.write_calls == 0


async def test_disable_after_claim_keeps_action_in_reconciliation() -> None:
    await mark_execution_started(TOOL_EXECUTION_ID)
    await connections.disable_capability(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        capability=ConnectionCapability.MAIL_SEND,
        now=NOW,
    )

    assert await tool_status(TOOL_EXECUTION_ID) == "reconciling"
    assert await task_status(TASK_ID) == "reconciling"
~~~

Cover read-dependency disable rejection, waiting approval, queued before claim, claim/disable lock race, request-started disable, entire connection disconnect, local token deletion before remote revoke, unresolved revoke-maintenance backlog without retained token material, providers that expose no safe revoke endpoint, and no fallback to another account/calendar.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/unit/application/test_capability_revocation.py backend/tests/integration/m2/test_capability_revocation_races.py backend/tests/integration/api/test_connections.py -q`

Expected: FAIL because capability disable changes only its row and does not invalidate action facts.

- [ ] **Step 3: Implement transactional invalidation for unclaimed work**

Within the same connection/capability lock transaction, select related pending approvals and tasks. If no ToolExecution exists, set approval `invalidated`, draft/proposal back to `editing`, task `cancelled`, clear schedules/leases, and append content-free `approval.invalidated` plus `task.cancelled` audit events.

If ToolExecution is `claimed`, `executing`, or `reconciling`, keep the command immutable, set the bound local object to `needs_attention`, set/retain task `reconciling`, and enqueue only reconciliation. If a claimed row proves `request_started_at is None`, reconciliation may converge immediately to confirmed-not-applied without a provider write; otherwise it follows the bounded read-only policy. If ToolExecution is terminal, leave the historical result unchanged.

- [ ] **Step 4: Implement fail-closed disconnect and token-free revoke maintenance**

Decrypt the selected revocation token into controlled memory, then commit local credential deletion,
connection disconnection, capability `revoked` states, and unclaimed-action invalidation before making
the best-effort network call. If the provider call fails—or the provider exposes no narrow delegated
token-revocation endpoint—append a content-free `oauth.revoke_unresolved` maintenance fact containing
only provider, stable error code, connection ID, and time. Never persist or move the token into a
retry table: doing so would contradict the local-token deletion guarantee. The scheduler counts and
alerts on unresolved facts and can record an operator remediation event, but it must not retry a
revocation call after token material has been destroyed.

For a claimed/executing action, disconnect must not retain a refresh/access token merely to improve
later reconciliation. A Worker that already held a short-lived access token in memory may finish its
current read-only check; otherwise the action moves from `reconciling` to `needs_attention` with
`connection_scope_missing` and offers provider-link/manual resolution. It is never marked cancelled or
confirmed-not-applied solely because credentials were deleted.

The claim path already checks global/provider switches; add a scanner that invalidates still-unclaimed approvals when an operator turns a switch off while tasks are waiting. Tests must prove a failed revoke leaves zero credential ciphertext rows and no scheduled job capable of recreating or reusing the token.

- [ ] **Step 5: Run connection, claim, and reconciliation suites**

Run: `uv run --project backend pytest backend/tests/unit/application/test_capability_revocation.py backend/tests/integration/m2/test_capability_revocation_races.py backend/tests/integration/api/test_connections.py backend/tests/integration/m2/test_tool_execution_claim.py backend/tests/integration/m2/test_action_reconciliation.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/application/use_cases/connections.py backend/src/ai_employee/application/use_cases/trusted_actions.py backend/src/ai_employee/infrastructure/db/repositories/connections.py backend/src/ai_employee/infrastructure/db/repositories/trusted_actions.py backend/src/ai_employee/workers/trusted_actions.py backend/src/ai_employee/workers/schedules.py backend/tests/unit/application/test_capability_revocation.py backend/tests/integration/m2/test_capability_revocation_races.py backend/tests/integration/api/test_connections.py
git commit -m "feat: revoke unclaimed trusted actions"
~~~

### Task 26: Expose unified action snapshots, manual controls, SSE, and M2 metrics

**Files:**
- Create: `backend/src/ai_employee/api/routers/actions.py`
- Modify: `backend/src/ai_employee/application/use_cases/action_views.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/action_views.py`
- Modify: `backend/src/ai_employee/api/deps.py`
- Modify: `backend/src/ai_employee/main.py`
- Modify: `backend/src/ai_employee/api/sse.py`
- Modify: `backend/src/ai_employee/infrastructure/observability/metrics.py`
- Modify: `backend/src/ai_employee/infrastructure/observability/redaction.py`
- Modify: `ops/observability/prometheus.yml`
- Create: `backend/tests/integration/api/test_actions.py`
- Modify: `backend/tests/integration/api/test_task_sse.py`
- Create: `backend/tests/unit/observability/test_m2_metrics.py`
- Modify: `backend/tests/unit/observability/test_redaction.py`

- [ ] **Step 1: Write failing action-view, SSE, and metric tests**

~~~python
async def test_action_snapshot_decrypts_typed_preview_but_sets_no_store(
    authenticated_api_clients,
) -> None:
    response = await authenticated_api_clients.owner.get(f"/api/v1/actions/{TASK_ID}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["approval"]["preview"]["kind"] == "mail"
    assert response.json()["approval"]["preview"]["body_text"] == "Synthetic body"
    assert response.json()["task_version"] == response.json()["event_cursor"]


def test_m2_metrics_have_only_bounded_labels() -> None:
    metrics = Metrics(CollectorRegistry())
    metrics.record_provider_write(provider="google", action="mail.send", outcome="unknown")

    rendered = metrics.render().body.decode()
    assert "task_id" not in rendered
    assert "example.test" not in rendered
~~~

SSE tests must assert the six new durable event names, content-free payload keys only, replay/order/deduplication, unknown-event compatibility, Redis loss, and snapshot fallback. API tests cover list pagination/filters, get snapshot, reconcile 202, manual-resolution enums/version/CSRF, cross-user 404, provider link, and 409 convergence races.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/integration/api/test_actions.py backend/tests/integration/api/test_task_sse.py backend/tests/unit/observability/test_m2_metrics.py backend/tests/unit/observability/test_redaction.py -q`

Expected: FAIL because the action router and M2 event/metric contracts are absent.

- [ ] **Step 3: Implement unified snapshots and typed approval previews**

`ActionSnapshot` joins the current user's TaskRun, proposal/draft summary, ApprovalRequest, ToolExecution, and ordered timeline in one repeatable-read view. The repository decrypts a pending/full approval command only for this authenticated response and maps it to one of these response variants:

~~~python
class MailApprovalPreview(BaseModel):
    kind: Literal["mail"]
    provider: str
    account_email: str
    mode: Literal["new", "reply", "reply_all"]
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    body_text: str
    irreversible: Literal[True] = True


class CalendarPreviewFields(BaseModel):
    title: str
    description: str | None
    location: str | None
    starts_at: str
    ends_at: str
    timezone: str
    all_day: bool
    attendees: list[str]


class CalendarConflictPreview(BaseModel):
    kind: Literal["overlap", "outside_working_hours", "partial_sources"]
    starts_at: str | None = None
    ends_at: str | None = None
    missing_connection_ids: list[UUID] = Field(default_factory=list)


class CalendarApprovalPreview(BaseModel):
    kind: Literal["calendar"]
    provider: str
    account_email: str
    calendar_name: str
    operation: Literal["create", "update", "restore"]
    before: CalendarPreviewFields | None
    after: CalendarPreviewFields
    conflicts: list[CalendarConflictPreview]
    notification_policy: Literal["all", "none"]
    base_etag: str | None
    compensation_available: bool
    provider_warnings: list[
        Literal["google_send_updates_none_external_sync"]
    ] = Field(default_factory=list)
~~~

`ActionSnapshot` also exposes `event_cursor: str` and `task_version: str` with the same canonical
decimal value from the
repeatable-read audit snapshot; this is the CAS token required by Task 20 and does not introduce a
mutable `TaskRun.version` column. All preview and sensitive action responses set
`Cache-Control: no-store`. List responses contain only status, action, provider, timestamps, risk,
and content-free summaries. Define the list item as a discriminated union with
`item_kind="mail_draft" | "calendar_proposal" | "trusted_task"`; pre-submit local items have
`task_id=None` and their own exact object ID/editor link, while task items require `task_id`. Do not
invent a task ID or second persisted status for local editing objects.

Approval detail also carries `content_status="available" | "redacted"`; after retention clears an
AEAD triple, return `preview=None` with `redacted` instead of treating the historical action as corrupt
or falling back to legacy plaintext.

- [ ] **Step 4: Add routes, SSE mappings, and metrics**

Implement the four action routes from the specification. Map audit events to `action.submitted`, `approval.invalidated`, `tool.claimed`, `tool.reconciling`, `tool.needs_attention`, and `tool.manually_resolved`; whitelist only IDs, state, version, error code, provider, action, attempt count, and timestamps. Do not add a second user-level SSE stream: drafts, proposals, and capability changes that do not yet have a `task_id` remain REST-authoritative and are refreshed after focus/reconnect.

Add exactly the specified counters/gauges/histograms with bounded `{provider, action, outcome, capability, state}` labels. Extend redaction to keys matching address, subject, body, title, description, location, attendees, authorization, cookie, token, command, and prompt. Add alert rules for unresolved age, duplicate-call attempts, kill-switch mismatch, revoke backlog, and missing scheduler/outbox heartbeats.

- [ ] **Step 5: Run API, SSE, metrics, and Redis-loss tests**

Run: `uv run --project backend pytest backend/tests/integration/api/test_actions.py backend/tests/integration/api/test_task_sse.py backend/tests/unit/observability/test_m2_metrics.py backend/tests/unit/observability/test_redaction.py backend/tests/integration/faults/test_redis_loss.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/src/ai_employee/api/routers/actions.py backend/src/ai_employee/application/use_cases/action_views.py backend/src/ai_employee/infrastructure/db/repositories/action_views.py backend/src/ai_employee/api/deps.py backend/src/ai_employee/main.py backend/src/ai_employee/api/sse.py backend/src/ai_employee/infrastructure/observability/metrics.py backend/src/ai_employee/infrastructure/observability/redaction.py ops/observability/prometheus.yml backend/tests/integration/api/test_actions.py backend/tests/integration/api/test_task_sse.py backend/tests/unit/observability/test_m2_metrics.py backend/tests/unit/observability/test_redaction.py
git commit -m "feat: expose trusted action status"
~~~

### Task 27: Extend retention, privacy deletion, deployment, and incident runbooks for M2

**Files:**
- Modify: `backend/src/ai_employee/workers/retention.py`
- Modify: `backend/src/ai_employee/workers/privacy.py`
- Modify: `backend/src/ai_employee/application/use_cases/privacy.py`
- Modify: `backend/src/ai_employee/infrastructure/db/repositories/diagnostics.py`
- Modify: `backend/src/ai_employee/integrations/registry.py`
- Modify: `backend/tests/integration/privacy/test_source_cache_cleanup.py`
- Modify: `backend/tests/integration/privacy/test_all_data_deletion.py`
- Create: `backend/tests/integration/retention/test_m2_action_retention.py`
- Modify: `docs/operations.md`
- Modify: `docs/acceptance-checklist.md`
- Modify: `scripts/test-deployment.sh`

- [ ] **Step 1: Write failing retention and deletion-barrier tests**

~~~python
async def test_mail_body_retention_clears_all_aead_material_but_keeps_hash() -> None:
    await retention.run(now=RETAIN_AFTER)

    version = await load_mail_version(VERSION_ID)
    approval = await load_approval(APPROVAL_ID)
    assert (version.body_ciphertext, version.body_nonce, version.body_key_version) == (None, None, None)
    assert (approval.payload_ciphertext, approval.payload_nonce, approval.payload_key_version) == (None, None, None)
    assert approval.payload_hash == ORIGINAL_HASH


async def test_all_data_deletion_reconciles_claimed_write_then_removes_local_provider_ids() -> None:
    await deletion.execute(user_id=USER_ID, request_id=REQUEST_ID)

    assert fake_provider.reconcile_calls == 1
    deleted_user = await load_user(USER_ID)
    assert deleted_user.is_active is False
    assert deleted_user.email == f"deleted-{USER_ID}@invalid.local"
    assert await provider_resource_ids_for(USER_ID) == ()
~~~

Cover 30-day mail body cleanup, 180-day mail metadata, event-end-plus-180-day calendar snapshots, 365-day action/audit history, unresolved reconciliation becoming `needs_attention` before command redaction, expired content blocking resubmission, source-cache deletion distinctions, unclaimed cancellation, claimed bounded reconciliation, user write barrier, unknown-result warning, provider-neutral Google/Microsoft token cleanup, deletion audit minimization, and retention-role permissions for every new table. Preserve M1's inactive anonymized user row because the append-only deletion audit has a non-null user foreign key; do not assert physical user-row deletion without a separate approved schema redesign.

- [ ] **Step 2: Run tests and observe the expected failure**

Run: `uv run --project backend pytest backend/tests/integration/retention/test_m2_action_retention.py backend/tests/integration/privacy/test_source_cache_cleanup.py backend/tests/integration/privacy/test_all_data_deletion.py -q`

Expected: FAIL because M2 tables and encrypted command triples are not included in cleanup/deletion.

- [ ] **Step 3: Implement content-specific retention and source-cache cleanup**

Clear each AEAD triple atomically. Before clearing an encrypted command still attached to a
`reconciling` action, stop its automatic schedule and move it to `needs_attention` with
`action_content_expired`; manual resolution and provider links remain available, but no adapter can be
called without the authenticated command. A mail draft whose body expired becomes a content-free
historical record and cannot submit. Calendar snapshot retention is calculated from event end when
available, otherwise proposal retention. Source-mail deletion removes/cancels drafts bound to that
connection/thread only; source-calendar deletion does the same for proposals/snapshots. Independently
created drafts remain.

- [ ] **Step 4: Implement the all-data write barrier and bounded final reconciliation**

Before deleting rows, set the existing user active flag to false as the durable user-scoped write
barrier and make the claim transaction reject inactive users. Invalidate every unclaimed action. For
claimed/executing/reconciling actions, run one bounded read-only reconcile pass; regardless of result,
remove local tokens, command/body/snapshot ciphertext, provider IDs, drafts, proposals, and tasks,
then reuse M1's inactive anonymized user row and content-free deletion audit.

Refactor the privacy Worker from its Google-only revoker to the fixed provider registry. Hold at most
one selected token per connection in controlled memory, delete every provider credential locally,
then make one best-effort provider-specific revoke attempt. A provider without a safe delegated revoke
endpoint records the token-free maintenance fact defined in Task 25; local deletion must still finish.

- [ ] **Step 5: Update runbooks and deployment contracts**

Document Google/Microsoft progressive authorization, Microsoft administrator consent, global/provider kill switches, dedicated test-account allowlist, unknown-result handling, calendar restore, duplicate/mis-send incident response, and the rule forbidding rollback to M1 while any `reconciling`/`needs_attention` task exists.

Extend `scripts/test-deployment.sh` to assert write switches default off, Microsoft secret-file mounts, no host publication of internal metrics ports, and no secret values in rendered Compose.

- [ ] **Step 6: Run retention, privacy, deployment, and role checks**

Run: `uv run --project backend pytest backend/tests/integration/retention/test_m2_action_retention.py backend/tests/integration/privacy/test_source_cache_cleanup.py backend/tests/integration/privacy/test_all_data_deletion.py backend/tests/integration/retention/test_role_permissions.py -q`

Expected: PASS.

Run: `bash scripts/test-deployment.sh`

Expected: PASS.

- [ ] **Step 7: Commit**

~~~bash
git add backend/src/ai_employee/workers/retention.py backend/src/ai_employee/workers/privacy.py backend/src/ai_employee/application/use_cases/privacy.py backend/src/ai_employee/infrastructure/db/repositories/diagnostics.py backend/src/ai_employee/integrations/registry.py backend/tests/integration/privacy/test_source_cache_cleanup.py backend/tests/integration/privacy/test_all_data_deletion.py backend/tests/integration/retention/test_m2_action_retention.py docs/operations.md docs/acceptance-checklist.md scripts/test-deployment.sh
git commit -m "feat: protect M2 action lifecycle data"
~~~

### Task 28: Build typed frontend APIs, action state, and the unified action center

**Files:**
- Modify: `frontend/src/api/types.ts`
- Create: `frontend/src/api/actions.ts`
- Create: `frontend/src/api/mail.ts`
- Create: `frontend/src/api/calendar.ts`
- Modify: `frontend/src/api/connections.ts`
- Modify: `frontend/src/api/settings.ts`
- Create: `frontend/src/stores/actions.ts`
- Modify: `frontend/src/stores/tasks.ts`
- Create: `frontend/src/pages/ActionsPage.vue`
- Modify: `frontend/src/components/AppShell.vue`
- Modify: `frontend/src/router/index.ts`
- Create: `frontend/src/api/actions.spec.ts`
- Create: `frontend/src/stores/actions.spec.ts`
- Create: `frontend/src/pages/ActionsPage.spec.ts`
- Modify: `frontend/src/stores/tasks.spec.ts`

- [ ] **Step 1: Write failing browser-boundary and action-center tests**

~~~typescript
it('rejects an action response that contains an unknown manual resolution', () => {
  expect(() => parseActionSnapshot({
    id: 'task-1',
    status: 'needs_attention',
    manual_resolution: 'maybe',
  })).toThrow('Invalid action snapshot')
})

it('refreshes server actions after focus without persisting sensitive content', async () => {
  const wrapper = mount(ActionsPage, { global: { plugins: [createPinia()] } })
  window.dispatchEvent(new Event('focus'))
  await flushPromises()

  expect(api.listActions).toHaveBeenCalledTimes(2)
  expect(localStorage.length).toBe(0)
})
~~~

Cover all task statuses including `reconciling`/`needs_attention`, action list/snapshot validation, duplicate/unknown SSE behavior, snapshot recovery, loading/empty/error/partial states, filters, refresh after focus/reconnect, provider links, keyboard navigation, live status, and mobile layout.

- [ ] **Step 2: Run frontend tests and observe the expected failure**

Run: `pnpm --dir frontend test:unit --run src/api/actions.spec.ts src/stores/actions.spec.ts src/pages/ActionsPage.spec.ts src/stores/tasks.spec.ts`

Expected: FAIL because M2 action types, store, and page do not exist.

- [ ] **Step 3: Add strict browser types and API clients**

Extend `TaskStatus` with the two new states and define discriminated `MailApprovalPreview`, `CalendarApprovalPreview`, `ActionListItem`, `ActionSnapshot`, `MailDraft`, `CalendarProposal`, `ConnectionCapability`, `ProviderCalendar`, and expanded `UserSettings` types. `ActionSnapshot` requires canonical decimal-string `event_cursor` and `task_version` fields with equal values; manual-resolution requests send the latest `task_version` and replace local state only with the server response. Reuse the existing event-cursor parser/comparator rather than converting either value to a JavaScript number. Parse `content_status="redacted"` only with `preview=null` and render a content-expired history state. Every parser must reject missing enums/versions rather than cast unknown JSON.

All requests use `requestJson`; modification requests carry Cookie/CSRF automatically, and every
local-object/task creation call generates or reuses an intent-scoped `Idempotency-Key` until that
request resolves. Do not write action, draft, proposal, or preview responses into `localStorage`,
`sessionStorage`, query strings, console, or service-worker caches.

- [ ] **Step 4: Implement server-authoritative action state and page**

`useActionsStore` keeps only an in-memory projection and replaces items from REST snapshots. Task SSE events update content-free status/attempt fields, then trigger a snapshot refetch for terminal or needs-attention transitions. The page groups mail drafts, calendar proposals, pending approval, executing/reconciling, needs-attention, and completed history without deriving server state transitions locally.

Add `/actions` to the authenticated router and primary navigation. Narrow screens render the timeline/detail as a separate region rather than compressing the desktop three-column layout.

- [ ] **Step 5: Run unit, type, and accessibility-focused checks**

Run: `pnpm --dir frontend test:unit --run src/api/actions.spec.ts src/stores/actions.spec.ts src/pages/ActionsPage.spec.ts src/stores/tasks.spec.ts`

Expected: PASS.

Run: `pnpm --dir frontend type-check`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add frontend/src/api/types.ts frontend/src/api/actions.ts frontend/src/api/mail.ts frontend/src/api/calendar.ts frontend/src/api/connections.ts frontend/src/api/settings.ts frontend/src/stores/actions.ts frontend/src/stores/tasks.ts frontend/src/pages/ActionsPage.vue frontend/src/components/AppShell.vue frontend/src/router/index.ts frontend/src/api/actions.spec.ts frontend/src/stores/actions.spec.ts frontend/src/pages/ActionsPage.spec.ts frontend/src/stores/tasks.spec.ts
git commit -m "feat: add frontend action center"
~~~

### Task 29: Build mail/calendar editors, structured approvals, needs-attention, connections, and settings UI

**Files:**
- Create: `frontend/src/pages/MailDraftPage.vue`
- Create: `frontend/src/pages/CalendarProposalPage.vue`
- Create: `frontend/src/components/MailApprovalPreview.vue`
- Create: `frontend/src/components/CalendarApprovalPreview.vue`
- Create: `frontend/src/components/NeedsAttentionPanel.vue`
- Modify: `frontend/src/components/ApprovalCard.vue`
- Modify: `frontend/src/pages/ConnectionsPage.vue`
- Modify: `frontend/src/pages/SettingsPage.vue`
- Modify: `frontend/src/pages/ActionsPage.vue`
- Modify: `frontend/src/components/BriefView.vue`
- Modify: `frontend/src/pages/ChatPage.vue`
- Modify: `frontend/src/router/index.ts`
- Create: `frontend/src/pages/MailDraftPage.spec.ts`
- Create: `frontend/src/pages/CalendarProposalPage.spec.ts`
- Modify: `frontend/src/components/ApprovalCard.spec.ts`
- Create: `frontend/src/components/NeedsAttentionPanel.spec.ts`
- Create: `frontend/src/pages/ConnectionsPage.spec.ts`
- Modify: `frontend/src/pages/SettingsPage.spec.ts`
- Modify: `frontend/src/pages/ActionsPage.spec.ts`
- Modify: `frontend/src/components/BriefView.spec.ts`
- Create: `frontend/src/pages/ChatPage.spec.ts`

- [ ] **Step 1: Write failing editor and approval tests**

~~~typescript
it('renders a mail approval as fields instead of generic JSON', () => {
  const wrapper = mount(ApprovalCard, { props: { approval: mailApproval(), decide } })

  expect(wrapper.get('[data-testid="mail-approval-subject"]').text()).toBe('Synthetic subject')
  expect(wrapper.text()).toContain('不可撤回')
  expect(wrapper.find('pre').exists()).toBe(false)
})

it('warns that confirmed not executed will not resend', async () => {
  const wrapper = mount(NeedsAttentionPanel, { props: needsAttentionProps() })
  await wrapper.get('button[name="confirmed_not_executed"]').trigger('click')

  expect(wrapper.get('[role="alert"]').text()).toContain('不会自动重发')
})
~~~

Mail tests cover account/thread binding, server-provided local recipient suggestions without Contacts calls, To/CC/BCC validation, version conflict reload, model loading/failure, plain text, recipient count, withdrawal, and irreversible warning. Calendar tests cover exact calendar/timezone, all-day/timed fields, conflict invalidation after time edits, three candidates, partial sources, an explicit “未检查参会人可用性” notice, attendees, notification policy, before/after diff, and stale ETag. Connection/settings tests cover Google/Microsoft start, capability dependencies, administrator consent, defaults, weekly intervals, and buffer bounds.

Brief/Chat tests must prove suggestions require an explicit click or command before creating a local editing object, that the resulting link opens the focused editor, and that no pending approval appears until the user submits the reviewed version. The action center must also expose explicit “新邮件” and “新日程” buttons.

- [ ] **Step 2: Run frontend tests and observe the expected failure**

Run: `pnpm --dir frontend test:unit --run src/pages/MailDraftPage.spec.ts src/pages/CalendarProposalPage.spec.ts src/components/ApprovalCard.spec.ts src/components/NeedsAttentionPanel.spec.ts src/pages/ConnectionsPage.spec.ts src/pages/SettingsPage.spec.ts src/pages/ActionsPage.spec.ts src/components/BriefView.spec.ts src/pages/ChatPage.spec.ts`

Expected: FAIL because the focused M2 experiences do not exist.

- [ ] **Step 3: Implement focused editors and structured approval variants**

Mail editor inputs are ordinary text controls; body uses `<textarea>` and is never rendered as Markdown/HTML. Replies lock account/thread/subject. Calendar editor uses typed date/time controls and displays server-provided conflict/candidate facts without reimplementing the algorithm.

`ApprovalCard` switches on `approval.preview.kind` and renders `MailApprovalPreview` or `CalendarApprovalPreview`. It shows provider/account, exact fields, risk, version, expiry, conflict/notification warnings, and disables duplicate decisions. A 409 displays the stable recovery action: reload, reauthorize, or create a new version.

- [ ] **Step 4: Implement needs-attention and capability/settings controls**

`NeedsAttentionPanel` shows attempt count, last error code, provider link, reconcile button, and two enum-only manual-resolution buttons. Require a confirmation dialog with the exact statement that no provider write will be called and `confirmed_not_executed` will not resend.

Connections page shows four capability rows per account, dependency-aware enable/disable controls, actual-scope/action-required status, Microsoft administrator-consent guidance, and no silent fallback. Settings page edits default account/calendar, seven-day working intervals, IANA timezone, and 0–120 minute buffer. `ActionsPage` adds explicit “新邮件” and “新日程” controls that create only local `editing` objects and route to their editors; they never submit an approval in the same click.

Brief action buttons call the source-bound draft/proposal creation APIs only after the click; Chat follows the server-returned editor link for explicit `prepare_mail_draft` or `prepare_calendar_proposal` results and never renders a sent/applied state before the trusted task snapshot confirms it.

- [ ] **Step 5: Run unit, type, lint, and production build checks**

Run: `pnpm --dir frontend test:unit --run src/pages/MailDraftPage.spec.ts src/pages/CalendarProposalPage.spec.ts src/components/ApprovalCard.spec.ts src/components/NeedsAttentionPanel.spec.ts src/pages/ConnectionsPage.spec.ts src/pages/SettingsPage.spec.ts src/pages/ActionsPage.spec.ts src/components/BriefView.spec.ts src/pages/ChatPage.spec.ts`

Expected: PASS.

Run: `pnpm --dir frontend type-check`

Expected: PASS.

Run: `pnpm --dir frontend lint`

Expected: PASS.

Run: `pnpm --dir frontend build`

Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add frontend/src/pages/MailDraftPage.vue frontend/src/pages/CalendarProposalPage.vue frontend/src/components/MailApprovalPreview.vue frontend/src/components/CalendarApprovalPreview.vue frontend/src/components/NeedsAttentionPanel.vue frontend/src/components/ApprovalCard.vue frontend/src/pages/ConnectionsPage.vue frontend/src/pages/SettingsPage.vue frontend/src/pages/ActionsPage.vue frontend/src/components/BriefView.vue frontend/src/pages/ChatPage.vue frontend/src/router/index.ts frontend/src/pages/MailDraftPage.spec.ts frontend/src/pages/CalendarProposalPage.spec.ts frontend/src/components/ApprovalCard.spec.ts frontend/src/components/NeedsAttentionPanel.spec.ts frontend/src/pages/ConnectionsPage.spec.ts frontend/src/pages/SettingsPage.spec.ts frontend/src/pages/ActionsPage.spec.ts frontend/src/components/BriefView.spec.ts frontend/src/pages/ChatPage.spec.ts
git commit -m "feat: add trusted action editors"
~~~

### Task 30: Prove crash safety, complete E2E, and freeze M2 release evidence

**Files:**
- Create: `backend/tests/integration/faults/test_m2_write_recovery.py`
- Modify: `backend/tests/integration/faults/conftest.py`
- Create: `frontend/e2e/m2-actions.spec.ts`
- Modify: `backend/src/ai_employee/infrastructure/testing/scenarios.py`
- Modify: `backend/src/ai_employee/infrastructure/testing/test_support.py`
- Modify: `backend/src/ai_employee/api/routers/test_support.py`
- Create: `scripts/test-m2-release.sh`
- Create: `scripts/verify-m2-sensitive-output.py`
- Create: `docs/releases/2026-08-06-m2-release-evidence.md`
- Modify: `docs/acceptance-checklist.md`
- Modify: `README.md`

- [ ] **Step 1: Write the failing crash matrix and Playwright flows**

The backend fault test must parameterize these exact crash points for every one of the four actions: before claim, after claim before request-start commit, after request start before response, after provider response before result commit, after result commit before queue acknowledgement, and during each reconciliation attempt. It must assert one ToolExecution, zero or one provider write according to the crash point, and final convergence to success, confirmed failure, or needs-attention without a fabricated result.

Playwright must cover Google and Microsoft Fake connections, new/reply/reply-all mail, calendar create/update/restore, editing conflicts, approval/rejection/expiry/invalidation, refresh/SSE reconnect, partial availability, capability reauthorization/admin consent, reconciliation, both manual outcomes, keyboard/focus/live region, and mobile layout.

- [ ] **Step 2: Run focused fault/E2E tests and observe expected failures**

Run: `uv run --project backend pytest backend/tests/integration/faults/test_m2_write_recovery.py -q`

Expected: FAIL because the M2 fault scenarios are not registered.

Run: `pnpm --dir frontend test:e2e m2-actions.spec.ts`

Expected: FAIL because the M2 E2E support scenarios are absent.

- [ ] **Step 3: Add deterministic Fake-provider scenarios**

Extend test support with sanitized scenario enums for confirmed applied, confirmed not applied, timeout-after-accept, ambiguous 5xx, delayed reconciliation success, never-resolved, ETag conflict, capability revoked, and duplicate delivery. Scenario payloads contain only IDs/statuses and must be consumed atomically so tests can prove the expected call count.

- [ ] **Step 4: Add auditable release and sensitive-output contracts**

`scripts/verify-m2-sensitive-output.py` must use two distinct checks. First, statically scan committed
fixtures for real domains, credential formats, Authorization/Cookie values, raw provider payloads
outside the reviewed fixture schemas, and forbidden personal-data keys; ordinary `.example.test` and
`Synthetic ...` fixture values remain allowed. Second, inject unique runtime canaries for address,
subject, body, title, description, location, attendee, token, Cookie, and Prompt fields, then scan
captured logs, JUnit/Playwright output, metrics text, and structured traces. Runtime canaries may exist
only in the test-input declaration and explicit encrypted-byte assertions; any appearance in an
observable output fails the gate.

`scripts/test-m2-release.sh` must run, in order:

~~~bash
just ci
uv run --project backend pytest backend/tests/integration/faults/test_m2_write_recovery.py -q
bash scripts/test-deployment.sh
python3 scripts/verify-m2-sensitive-output.py
git diff --check
~~~

It must also verify that the working tree has no `.env`, secret, dump, Playwright report, coverage, or generated release artifact staged for commit.

- [ ] **Step 5: Run the complete automated release gate**

Run: `bash scripts/test-m2-release.sh`

Expected: PASS with fresh output from `just ci`, all crash cases, deployment contract, sensitive-output scan, and `git diff --check`.

- [ ] **Step 6: Pause for explicitly authorized provider-account E2E**

After the automated gate passes, stop before enabling real writes and request the user's explicit authorization plus out-of-band dedicated Google/Microsoft test-account configuration. Run the manual matrix only in a separate environment with the global/provider switches and exact account allowlist enabled. If authorization is not supplied, leave Task 30 incomplete and do not create a blank release-evidence document or claim M2 release completion.

- [ ] **Step 7: Write the completed release-evidence record**

Create `docs/releases/2026-08-06-m2-release-evidence.md` only after the manual matrix is complete. Record actual date, operator, commit, environment, locally hashed dedicated-account identifiers, exact enabled switches, Google new/reply/reply-all/create/update/restore results, Microsoft parity results, alternate Microsoft account-type contract evidence, approval/ToolExecution audit IDs, scope review, backup/restore, crash drill, sensitive-output scan, and the final release decision. Every row must contain evidence or an explicit failed result with disposition; the file must contain no blank field or future-action marker.

- [ ] **Step 8: Commit**

~~~bash
git add backend/tests/integration/faults/test_m2_write_recovery.py backend/tests/integration/faults/conftest.py frontend/e2e/m2-actions.spec.ts backend/src/ai_employee/infrastructure/testing/scenarios.py backend/src/ai_employee/infrastructure/testing/test_support.py backend/src/ai_employee/api/routers/test_support.py scripts/test-m2-release.sh scripts/verify-m2-sensitive-output.py docs/releases/2026-08-06-m2-release-evidence.md docs/acceptance-checklist.md README.md
git commit -m "test: freeze M2 release evidence"
~~~

## Spec Coverage Review

- Scope, single-operation control, exclusions, multi-account binding, and one-release Google/Microsoft parity: Tasks 1–3, 8–13, 21–24, and 30.
- Typed commands, canonical hashes, encrypted storage, exact approval, 10-minute decision window, five-minute claim window, invalidation, and legacy fake-write compatibility: Tasks 2–6 and 18–19.
- Draft generation/editing, reply/reply-all rules, recipient limits, model minimization, empty fallback, and no provider draft: Tasks 2, 11, 14–15, 21, and 23.
- Calendar directory, working hours, buffer, 15-minute grid, 14-day horizon, three candidates, completeness, create/update/restore, ETag, attendees, and notification policy: Tasks 4, 12–13, 16–17, 22, and 24.
- Progressive Google/Microsoft OAuth, personal/work accounts, scope dependencies, capability shutdown, disconnect, and revoke: Tasks 4, 8–10, and 25.
- Idempotent claim, safe retry, unknown-result reconciliation, manual resolution, compensation, Redis/checkpoint recovery, and crash points: Tasks 3, 18–25, and 30.
- API, SSE, action center, structured previews, needs-attention, accessibility, responsive layout, and server-authoritative recovery: Tasks 15, 17, 26, 28, and 29.
- Security, no-store, redaction, retention, source deletion, all-data barrier, metrics, alerts, deployment switches, rollback, incident response, and release evidence: Tasks 1, 5–6, 25–27, and 30.

## Execution Order and Checkpoints

1. Tasks 1–6 establish the approved scope, pure contracts, migrations, and encrypted persistence. Checkpoint: empty-database migration and AAD/user-isolation tests pass before any provider expansion.
2. Tasks 7–13 generalize reads and add Microsoft/Google parity. Checkpoint: both providers pass OAuth plus incremental mail/calendar contract tests with real writes still disabled.
3. Tasks 14–20 add editable proposals and the provider-independent trusted execution/reconciliation core. Within this stage, execute Task 14, then Task 16, Task 18, Task 15, Task 17, and finally Tasks 19–20. Task 18 must precede the two REST tasks because their `/submit` routes can only return an honest, durable `202` after the atomic encrypted approval-submission use cases exist; creating an unhandled placeholder task is forbidden. Task numbering remains grouped by domain and does not imply execution order inside this stage. Checkpoint: Fake adapters prove one claim, no blind retry, and durable needs-attention/manual resolution.
4. Tasks 21–24 add one high-risk provider write path per task and commit. Checkpoint: each adapter independently passes success, rejection, timeout, unknown-result, reconciliation, and duplicate-delivery contracts before starting the next adapter.
5. Tasks 25–27 close revocation, API/SSE, observability, privacy, retention, deployment, and operations gaps. Checkpoint: kill-switch, deletion-barrier, sensitive-event, and retention-role suites pass.
6. Tasks 28–29 deliver the frontend projection and focused editors. Checkpoint: unit tests, strict type checking, lint, production build, keyboard/focus, and mobile behavior pass.
7. Task 30 runs the full automated gate, pauses for explicit provider-account authorization, performs the dedicated Google/Microsoft matrix, and writes a fully evidenced final release record. Without that authorization, Task 30 remains incomplete.
